"""
Telegram Bot - Hệ thống quản lý cửa hàng hoa lan (Bản thực chiến Tết V2)
====================================================================
Tính năng:
  - Nhập sỉ tính theo Thùng (tự quy đổi x20 cành lẻ), xuất kho tính theo Cành.
  - Quản lý định danh theo cặp thuộc tính: Loại cành (don/doi) + Màu sắc.
  - Bảng đơn giá niêm yết cố định trong code: cành đơn 300k, cành đôi 350k.
  - Tạo đơn và sửa đơn cấu hình động bằng Database Transaction (Tự hoàn kho, khấu trừ, tính tiền).
  - Ghi nhận cột Giảm giá mặc cả vo tròn và SĐT khách hàng phòng ngừa sự cố.
  - Cộng sổ tự động thời gian thực theo ngày hiện tại và tổng doanh thu trọn đời.
"""

import os
import logging
import asyncpg
import functools  # ← thêm dòng này
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ── Tải biến môi trường từ file .env ──────────────────────────────────────────
load_dotenv()

BOT_TOKEN     = os.getenv("BOT_TOKEN")
DATABASE_URL  = os.getenv("DATABASE_URL")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))

# Đơn giá niêm yết cố định của cửa hàng (Bán lẻ cành đơn/đôi)
PRICES = {
    "don": 300000,  # Lan cành đơn (1 bầu 1 vòi): 300k/cành
    "doi": 350000   # Lan cành đôi (1 bầu 2 vòi): 350k/cành
}

# ── Cấu hình logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE ENGINE (Hỗ trợ Fallback trên môi trường Windows)
# ══════════════════════════════════════════════════════════════════════════════

async def get_db_connection():
    """Kết nối database an toàn, tự động sửa lỗi phân giải tên miền localhost trên Windows."""
    url = DATABASE_URL
    if url and "localhost" in url:
        url = url.replace("localhost", "127.0.0.1")
    return await asyncpg.connect(url)


async def init_db():
    """Khởi tạo cấu trúc bảng dữ liệu phẳng tối ưu thực chiến Tết."""
    conn = await get_db_connection()
    try:
        await conn.execute("""
            -- 1. Bảng quản lý kho hoa lan lẻ theo màu
            CREATE TABLE IF NOT EXISTS products (
                id              SERIAL PRIMARY KEY,
                loai_canh       VARCHAR(20) NOT NULL, -- 'don' hoặc 'doi'
                mau_sac         VARCHAR(50) NOT NULL, -- 'trang', 'vang', 'tim', 'hong'...
                so_luong_ton    INT NOT NULL DEFAULT 0, -- đơn vị tính: Cành lẻ
                created_at      TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_product_type UNIQUE (loai_canh, mau_sac)
            );

            -- 2. Bảng quản lý đơn hàng chậu tập trung (Bao gồm SĐT và Mặc cả)
            CREATE TABLE IF NOT EXISTS orders (
                ma_don              SERIAL PRIMARY KEY,
                sdt_khach           VARCHAR(20) NOT NULL,
                so_canh             INT NOT NULL,
                loai_canh           VARCHAR(20) NOT NULL,
                mau_sac             VARCHAR(50) NOT NULL,
                tien_chau           DECIMAL(15, 0) NOT NULL DEFAULT 0,
                tien_phu_kien       DECIMAL(15, 0) NOT NULL DEFAULT 0,
                tien_ship           DECIMAL(15, 0) NOT NULL DEFAULT 0,
                giam_gia            DECIMAL(15, 0) NOT NULL DEFAULT 0, -- Số tiền bớt mặc cả vo tròn
                tong_tien_ly_tuong  DECIMAL(15, 0) NOT NULL DEFAULT 0, -- Tính từ giá niêm yết gốc
                tong_tien_thuc_te   DECIMAL(15, 0) NOT NULL DEFAULT 0, -- Thực thu = Lý tưởng - Giảm giá
                trang_thai          VARCHAR(30) DEFAULT 'cho_thanh_toan', -- 'cho_thanh_toan', 'da_thanh_toan', 'da_huy'
                ngay_tao            TIMESTAMP DEFAULT NOW()
            );

            -- 3. Bảng nhật ký biến động kho để đối soát khi cần
            CREATE TABLE IF NOT EXISTS inventory_log (
                id              SERIAL PRIMARY KEY,
                loai_canh       VARCHAR(20) NOT NULL,
                mau_sac         VARCHAR(50) NOT NULL,
                loai_giao_dich  VARCHAR(10) NOT NULL, -- 'nhap' hoặc 'xuat'
                so_luong        INT NOT NULL,
                ghi_chu         TEXT,
                created_at      TIMESTAMP DEFAULT NOW()
            );
        """)
        logger.info("✅ Database V2 initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
    finally:
        await conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# PHÂN QUYỀN TRUY CẬP CHỦ SHOP
# ══════════════════════════════════════════════════════════════════════════════

def owner_only(func):
    """Decorator: Chặn người lạ, chỉ duy nhất chủ shop mới điều khiển được."""
    @functools.wraps(func)   # ← thêm dòng này
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != OWNER_CHAT_ID:
            await update.message.reply_text("⛔ Bạn không có quyền quản trị hệ thống kho này.")
            return
        return await func(update, context)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
# BỘ ĐIỀU KHIỂN LỆNH TELEGRAM (COMMAND HANDLERS)
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — Menu hướng dẫn gõ lệnh in sẵn cho mẹ."""
    await update.message.reply_text(
        "🌸 *HỆ THỐNG SỐ HÓA CỬA HÀNG HOA LAN HỒ ĐIỆP* 🌸\n\n"
        "📦 *1. QUẢN LÝ KHO (Quy đổi Thùng -> Cành):*\n"
        "• `/nhapkho [số_thùng] [loại_cành] [màu]` — Nhập sỉ Đà Lạt (1 thùng = 20 cành)\n"
        "• `/tonkho` — Kiểm tra lượng cành lẻ còn tồn trong vườn\n\n"
        "💰 *2. QUẢN LÝ ĐƠN HÀNG VÀ TÍNH TIỀN:*\n"
        "• `/taodon [số_cành] [loại_cành] [màu] [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]`\n"
        "• `/suadon [mã_đơn] [số_cành] [loại_cành] [màu] [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]`\n\n"
        "📑 *3. CỘNG SỔ & TRẠNG THÁI DOANH THU:*\n"
        "• `/capnhat [mã_đơn] [thanh_toan / huy / cho]` — Đổi trạng thái đơn hàng\n"
        "• `/donhang` — Xem các đơn đang nợ tiền (Chờ thanh toán)\n"
        "• `/lichsu` — Xem đơn đã thu tiền hôm nay | `/lichsu 25/01` — Xem theo ngày\n"
        "• `/congso` — Xem dòng tiền thực tế thu về hôm nay & trọn đời\n\n"
        "_*Chú ý:* Nhập chữ `don` cho cành đơn, `doi` cho cành đôi. Tất cả các số tiền nhập viết liền không dấu cách (Ví dụ: 200000).",
        parse_mode="Markdown",
    )


@owner_only
async def nhap_kho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/nhapkho [số_thùng] [loại_cành] [màu]"""
    args = context.args
    if len(args) != 3:
        await update.message.reply_text("❌ Sai cú pháp! Mẫu chuẩn:\n`/nhapkho 2 don trang`")
        return

    try:
        so_thung = int(args[0])
        loai_canh = args[1].lower().strip()
        mau_sac = args[2].lower().strip()
        
        if so_thung <= 0: raise ValueError
        if loai_canh not in ["don", "doi"]:
            await update.message.reply_text("❌ Loại cành chỉ được ghi chữ `don` (cành đơn) hoặc `doi` (cành đôi).")
            return
    except ValueError:
        await update.message.reply_text("❌ Số lượng thùng phải là một số nguyên dương lẻ (Ví dụ: 1, 2, 5).")
        return

    so_canh_quy_doi = so_thung * 20  # Quy đổi tự động từ thùng ra cành lẻ
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            # Thực hiện Upsert (Cập nhật nếu có, chèn mới nếu chưa tồn tại)
            await conn.execute("""
                INSERT INTO products (loai_canh, mau_sac, so_luong_ton) 
                VALUES ($1, $2, $3) 
                ON CONFLICT (loai_canh, mau_sac) 
                DO UPDATE SET so_luong_ton = products.so_luong_ton + EXCLUDED.so_luong_ton
            """, loai_canh, mau_sac, so_canh_quy_doi)
            
            # Lấy số lượng tồn mới sau khi cộng dồn
            ton_moi = await conn.fetchval(
                "SELECT so_luong_ton FROM products WHERE loai_canh = $1 AND mau_sac = $2", 
                loai_canh, mau_sac
            )
            
            # Ghi nhật ký hệ thống
            await conn.execute("""
                INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) 
                VALUES ($1, $2, 'nhap', $3, $4)
            """, loai_canh, mau_sac, so_canh_quy_doi, f"Nhập từ Đà Lạt {so_thung} thùng")

        loai_txt = "Cành đơn" if loai_canh == "don" else "Cành đôi"
        await update.message.reply_text(
            f"✅ *ĐÃ NHẬP KHO THÀNH CÔNG!*\n"
            f"🚛 Nguồn hàng: Lan Hồ Điệp Đà Lạt\n"
            f"📦 Quy mô: *{so_thung} Thùng* $\rightarrow$ Tự động đổi: *+{so_canh_quy_doi} cành lẻ*\n"
            f"🌸 Phân loại: *{loai_txt}* — Màu: *{mau_sac}*\n"
            f"📊 Tổng tồn kho hiện tại: *{ton_moi} cành*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi hệ thống khi nhập hàng: {str(e)}")
    finally:
        await conn.close()


@owner_only
async def xem_ton_kho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tonkho — Xem tình hình kho hoa lan lẻ thực tế."""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT loai_canh, mau_sac, so_luong_ton FROM products ORDER BY loai_canh, mau_sac")
        if not rows:
            await update.message.reply_text("📦 Sổ kho trống! Hiện tại chưa có hoa lan nào trong vườn.")
            return

        lines = ["📦 *SỐ LIỆU TỒN KHO HOA LAN THEO CÀNH CHUYÊN SÂU:*\n"]
        for row in rows:
            loai_txt = "Cành đơn (1 vòi)" if row['loai_canh'] == 'don' else "Cành đôi (2 vòi)"
            gia_niem_yet = PRICES[row['loai_canh']]
            lines.append(f"• 🌺 *{loai_txt}* | Màu: *{row['mau_sac']}*\n  ↳ Tồn vườn: `{row['so_luong_ton']} cành` | Giá bán gốc: {gia_niem_yet:,}đ")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    finally:
        await conn.close()


@owner_only
async def tao_don(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/taodon [số_cành] [loại_cành] [màu] [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]"""
    args = context.args
    if len(args) != 8:
        await update.message.reply_text(
            "❌ *Sai cú pháp tạo đơn!*\n"
            "Mẫu chuẩn in sẵn:\n`/taodon [số_cành] [loại_cành] [màu] [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]`\n\n"
            "Ví dụ: `/taodon 10 don vang 200000 100000 150000 50000 0912345678`",
            parse_mode="Markdown"
        )
        return

    try:
        so_canh = int(args[0])
        loai_canh = args[1].lower().strip()
        mau_sac = args[2].lower().strip()
        tien_chau = int(args[3])
        tien_phu_kien = int(args[4])
        tien_ship = int(args[5])
        giam_gia = int(args[6])
        sdt_khach = args[7].strip()

        if so_canh <= 0 or tien_chau < 0 or tien_phu_kien < 0 or tien_ship < 0 or giam_gia < 0:
            raise ValueError
        if loai_canh not in ["don", "doi"]:
            await update.message.reply_text("❌ Nhầm loại cành! Chỉ được gõ chữ `don` hoặc `doi`.")
            return
    except ValueError:
        await update.message.reply_text("❌ Lỗi định dạng! Số cành phải > 0, các ô chi phí phải ghi số liền nhau không có chữ hoặc dấu cách.")
        return

    conn = await get_db_connection()
    try:
        async with conn.transaction():
            # 1. Kiểm tra lượng tồn kho của màu lan này xem có đủ cắm chậu không
            stock = await conn.fetchval(
                "SELECT so_luong_ton FROM products WHERE loai_canh = $1 AND mau_sac = $2", 
                loai_canh, mau_sac
            )
            if stock is None or stock < so_canh:
                hiat_stock = stock if stock is not None else 0
                await update.message.reply_text(
                    f"❌ *Chặn giao dịch: Kho thiếu hoa!*\n"
                    f"Trong vườn loại `lan_{loai_canh}_{mau_sac}` hiện chỉ còn *{hiat_stock} cành*.\n"
                    f"Mẹ không đủ *{so_canh} cành* để cắm chậu này. Hãy nhập thêm hàng trước!",
                    parse_mode="Markdown"
                )
                return

            # 2. Khấu trừ trực tiếp số hoa ra khỏi kho vườn
            await conn.execute(
                "UPDATE products SET so_luong_ton = so_luong_ton - $1 WHERE loai_canh = $2 AND mau_sac = $3",
                so_canh, loai_canh, mau_sac
            )
            await conn.execute(
                "INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'xuat', $3, 'Xuất cắm chậu bán')",
                loai_canh, mau_sac, so_canh
            )

            # 3. Tính toán dòng tiền tự động hóa 100%
            gia_goc_canh = PRICES[loai_canh]
            tong_tien_ly_tuong = (so_canh * gia_goc_canh) + tien_chau + tien_phu_kien + tien_ship
            tong_tien_thuc_te = tong_tien_ly_tuong - giam_gia

            # 4. Lưu dữ liệu phẳng vào bảng Orders tập trung
            ma_don = await conn.fetchval("""
                INSERT INTO orders (sdt_khach, so_canh, loai_canh, mau_sac, tien_chau, tien_phu_kien, tien_ship, giam_gia, tong_tien_ly_tuong, tong_tien_thuc_te, trang_thai)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'cho_thanh_toan')
                RETURNING ma_don
            """, sdt_khach, so_canh, loai_canh, mau_sac, tien_chau, tien_phu_kien, tien_ship, giam_gia, tong_tien_ly_tuong, tong_tien_thuc_te)

        loai_txt = "Cành đơn" if loai_canh == "don" else "Cành đôi"
        thoi_gian_tao = __import__('datetime').datetime.now().strftime("%H:%M  %d/%m/%Y")
        await update.message.reply_text(
            f"🧾 *ĐÃ LẬP HÓA ĐƠN CHẬU LAN THÀNH CÔNG!*\n"
            f"🆔 *MÃ ĐƠN HÀNG: #{ma_don}*\n"
            f"🕐 Thời gian lập đơn: {thoi_gian_tao}\n"
            f"📞 SĐT khách liên hệ: `{sdt_khach}`\n"
            f"🌸 Quy cách chậu: *{so_canh} cành* {loai_txt} — Màu: *{mau_sac}*\n"
            f"───────────────────\n"
            f"💵 Tiền hoa gốc ({so_canh}c x {gia_goc_canh:,}đ): {so_canh * gia_goc_canh:,}đ\n"
            f"🏺 Phôi chậu sứ/gỗ: {tien_chau:,}đ\n"
            f"🎀 Phụ kiện trang trí + Công cắm: {tien_phu_kien:,}đ\n"
            f"🚗 Phí ship Tết điều động đường xa: {tien_ship:,}đ\n"
            f"📉 Bớt giá khách mặc cả vo tròn: -{giam_gia:,}đ\n"
            f"───────────────────\n"
            f"💰 *TỔNG DOANH THU LÝ TƯỞNG:* {tong_tien_ly_tuong:,}đ\n"
            f"⭐ *TIỀN THỰC TẾ PHẢI THU:* `{tong_tien_thuc_te:,}đ`\n"
            f"⏳ Trạng thái: *CHỜ THANH TOÁN*\n\n"
            f"👉 Khi khách trả tiền, gõ lệnh duyệt: `/capnhat {ma_don} thanh_toan`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi hệ thống khi tạo hóa đơn: {str(e)}")
    finally:
        await conn.close()


@owner_only
async def sua_don(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/suadon [mã_đơn] [số_cành] [loại_cành] [màu] [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]"""
    args = context.args
    if len(args) != 9:
        await update.message.reply_text(
            "❌ *Sai cú pháp sửa đơn!*\n"
            "Mẫu chuẩn in sẵn:\n`/suadon [mã_đơn] [số_cành] [loại_cành] [màu] [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]`",
            parse_mode="Markdown"
        )
        return

    try:
        ma_don = int(args[0])
        so_canh = int(args[1])
        loai_canh = args[2].lower().strip()
        mau_sac = args[3].lower().strip()
        tien_chau = int(args[4])
        tien_phu_kien = int(args[5])
        tien_ship = int(args[6])
        giam_gia = int(args[7])
        sdt_khach = args[8].strip()

        if ma_don <= 0 or so_canh <= 0 or tien_chau < 0 or tien_phu_kien < 0 or tien_ship < 0 or giam_gia < 0:
            raise ValueError
        if loai_canh not in ["don", "doi"]:
            await update.message.reply_text("❌ Nhầm loại cành! Chỉ được gõ chữ `don` hoặc `doi`.")
            return
    except ValueError:
        await update.message.reply_text("❌ Kiểm tra lại định dạng số của mã đơn hoặc tiền bạc.")
        return

    conn = await get_db_connection()
    try:
        async with conn.transaction():
            # Truy vấn thông tin của đơn hàng cũ trước khi sửa
            old_order = await conn.fetchrow(
                "SELECT so_canh, loai_canh, mau_sac, trang_thai FROM orders WHERE ma_don = $1", 
                ma_don
            )
            if not old_order:
                await update.message.reply_text(f"❌ Không tồn tại mã đơn hàng #{ma_don} trong sổ sách.")
                return

            old_so_canh = old_order["so_canh"]
            old_loai_canh = old_order["loai_canh"]
            old_mau_sac = old_order["mau_sac"]
            old_trang_thai = old_order["trang_thai"]

            # BƯỚC 1: Hoàn kho trả lại hoa cũ về vườn (nếu đơn cũ không phải là đơn đã hủy)
            if old_trang_thai != 'da_huy':
                await conn.execute("""
                    INSERT INTO products (loai_canh, mau_sac, so_luong_ton) 
                    VALUES ($1, $2, $3) 
                    ON CONFLICT (loai_canh, mau_sac) 
                    DO UPDATE SET so_luong_ton = products.so_luong_ton + EXCLUDED.so_luong_ton
                """, old_loai_canh, old_mau_sac, old_so_canh)
                await conn.execute(
                    "INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'nhap', $3, $4)",
                    old_loai_canh, old_mau_sac, old_so_canh, f"Hoàn kho tự động để sửa đơn #{ma_don}"
                )

            # BƯỚC 2: Kiểm tra kho xem có đủ loại hoa mới theo yêu cầu sửa đơn không
            stock = await conn.fetchval(
                "SELECT so_luong_ton FROM products WHERE loai_canh = $1 AND mau_sac = $2", 
                loai_canh, mau_sac
            )
            if stock is None or stock < so_canh:
                hiat_stock = stock if stock is not None else 0
                # Kích hoạt Rollback hủy toàn bộ giao dịch, giữ nguyên đơn cũ để bảo vệ an toàn hệ thống
                raise Exception(f"Không đủ hoa trong vườn cho cấu hình mới! Loại {loai_canh}-{mau_sac} chỉ còn {hiat_stock} cành.")

            # BƯỚC 3: Khấu trừ kho theo số lượng cấu hình hoa mới
            await conn.execute(
                "UPDATE products SET so_luong_ton = so_luong_ton - $1 WHERE loai_canh = $2 AND mau_sac = $3",
                so_canh, loai_canh, mau_sac
            )
            await conn.execute(
                "INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'xuat', $3, $4)",
                loai_canh, mau_sac, so_canh, f"Trừ kho cấu hình mới từ sửa đơn #{ma_don}"
            )

            # BƯỚC 4: Tính toán lại toàn bộ tiền và ghi đè cập nhật hóa đơn
            gia_goc_canh = PRICES[loai_canh]
            tong_tien_ly_tuong = (so_canh * gia_goc_canh) + tien_chau + tien_phu_kien + tien_ship
            tong_tien_thuc_te = tong_tien_ly_tuong - giam_gia

            await conn.execute("""
                UPDATE orders 
                SET sdt_khach = $1, so_canh = $2, loai_canh = $3, mau_sac = $4, 
                    tien_chau = $5, tien_phu_kien = $6, tien_ship = $7, giam_gia = $8, 
                    tong_tien_ly_tuong = $9, tong_tien_thuc_te = $10
                WHERE ma_don = $11
            """, sdt_khach, so_canh, loai_canh, mau_sac, tien_chau, tien_phu_kien, tien_ship, giam_gia, tong_tien_ly_tuong, tong_tien_thuc_te, ma_don)

        loai_txt = "Cành đơn" if loai_canh == "don" else "Cành đôi"
        thoi_gian_sua = __import__('datetime').datetime.now().strftime("%H:%M  %d/%m/%Y")
        await update.message.reply_text(
            f"🔄 *ĐỒNG BỘ & SỬA ĐƠN HÀNG THÀNH CÔNG ĐÃ CẬP NHẬT KHO!*\n"
            f"🆔 Đơn hàng: *#{ma_don}*\n"
            f"🕐 Thời gian sửa đơn: {thoi_gian_sua}\n"
            f"📞 SĐT khách điều chỉnh: `{sdt_khach}`\n"
            f"🌸 Chi tiết chậu mới: *{so_canh} cành* {loai_txt} ({mau_sac})\n"
            f"───────────────────\n"
            f"🏺 Chậu: {tien_chau:,}đ | 🎀 Phụ kiện: {tien_phu_kien:,}đ | 🚗 Ship: {tien_ship:,}đ\n"
            f"📉 Mặc cả bớt mới: -{giam_gia:,}đ\n"
            f"───────────────────\n"
            f"💰 *TỔNG LÝ TƯỞNG MỚI:* {tong_tien_ly_tuong:,}đ\n"
            f"⭐ *TIỀN THỰC TẾ PHẢI THU MỚI:* `{tong_tien_thuc_te:,}đ`\n"
            f"🔄 Trạng thái đơn giữ nguyên: *{old_trang_thai.upper()}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Huỷ lệnh sửa đơn do lỗi: {str(e)}")
    finally:
        await conn.close()


@owner_only
async def cap_nhat_don(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/capnhat [mã_đơn] [thanh_toan / huy / cho]"""
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("❌ Sai cú pháp! Hãy gõ: `/capnhat [mã_đơn] thanh_toan` hoặc `huy` hoặc `cho`")
        return

    try:
        ma_don = int(args[0])
        input_status = args[1].lower().strip()
    except ValueError:
        await update.message.reply_text("❌ Mã đơn hàng bắt buộc phải là một số nguyên.")
        return

    status_map = {
        "thanh_toan": "da_thanh_toan",
        "huy": "da_huy",
        "cho": "cho_thanh_toan"
    }

    if input_status not in status_map:
        await update.message.reply_text("❌ Lệnh sai! Trạng thái chỉ được ghi chữ: `thanh_toan`, `huy` hoặc `cho`.")
        return

    new_status = status_map[input_status]
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            order = await conn.fetchrow("SELECT so_canh, loai_canh, mau_sac, trang_thai FROM orders WHERE ma_don = $1", ma_don)
            if not order:
                await update.message.reply_text(f"❌ Không tìm thấy mã đơn #{ma_don} trong sổ ghi chép.")
                return

            old_status = order["trang_thai"]
            so_canh = order["so_canh"]
            loai_canh = order["loai_canh"]
            mau_sac = order["mau_sac"]

            if old_status == new_status:
                await update.message.reply_text(f"ℹ️ Đơn hàng #{ma_don} hiện tại vốn dĩ đã ở trạng thái *{new_status.upper()}* rồi.", parse_mode="Markdown")
                return

            # XỬ LÝ KHO KHI THAY ĐỔI TRẠNG THÁI HỦY ĐƠN
            if new_status == "da_huy" and old_status != "da_huy":
                # Trả lại hoa lan về vườn vì khách hủy đơn bùng hàng
                await conn.execute("""
                    INSERT INTO products (loai_canh, mau_sac, so_luong_ton) 
                    VALUES ($1, $2, $3) 
                    ON CONFLICT (loai_canh, mau_sac) 
                    DO UPDATE SET so_luong_ton = products.so_luong_ton + EXCLUDED.so_luong_ton
                """, loai_canh, mau_sac, so_canh)
                await conn.execute(
                    "INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'nhap', $3, $4)",
                    loai_canh, mau_sac, so_canh, f"Hoàn kho do hủy đơn hàng #{ma_don}"
                )
            elif old_status == "da_huy" and new_status != "da_huy":
                # Khôi phục lại đơn đã hủy -> Phải khấu trừ lại kho, nếu kho không đủ thì chặn lại
                stock = await conn.fetchval("SELECT so_luong_ton FROM products WHERE loai_canh = $1 AND mau_sac = $2", loai_canh, mau_sac)
                if stock is None or stock < so_canh:
                    hiat_stock = stock if stock is not None else 0
                    raise Exception(f"Kho vườn không đủ hoa để khôi phục lại đơn! Loại {loai_canh}-{mau_sac} chỉ còn {hiat_stock} cành.")
                await conn.execute(
                    "UPDATE products SET so_luong_ton = so_luong_ton - $1 WHERE loai_canh = $2 AND mau_sac = $3",
                    so_canh, loai_canh, mau_sac
                )
                await conn.execute(
                    "INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'xuat', $3, $4)",
                    loai_canh, mau_sac, so_canh, f"Trừ kho phục hồi đơn hủy #{ma_don}"
                )

            # Cập nhật trạng thái mới vào database
            await conn.execute("UPDATE orders SET trang_thai = $1 WHERE ma_don = $2", new_status, ma_don)

        emoji_map = {"cho_thanh_toan": "⏳", "da_thanh_toan": "✅", "da_huy": "❌"}
        await update.message.reply_text(f"{emoji_map[new_status]} Đơn hàng #{ma_don} đã đổi trạng thái sang: *{new_status.upper()}*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Không thể chuyển trạng thái đơn: {str(e)}")
    finally:
        await conn.close()


@owner_only
async def xem_don_hang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/donhang — Danh sách các đơn chờ khách thanh toán tiền mặt/chuyển khoản ngân hàng."""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT ma_don, sdt_khach, so_canh, loai_canh, mau_sac, tong_tien_thuc_te 
            FROM orders WHERE trang_thai = 'cho_thanh_toan' 
            ORDER BY ngay_tao DESC LIMIT 15
        """)
        if not rows:
            await update.message.reply_text("✅ Sổ đơn sạch sẽ! Không có đơn hàng nào bị nợ tiền.")
            return

        lines = [f"⏳ *DANH SÁCH {len(rows)} ĐƠN ĐANG CHỜ THANH TOÁN TIỀN:*\n"]
        for row in rows:
            loai_txt = "Đơn" if row['loai_canh'] == 'don' else "Đôi"
            lines.append(
                f"🔖 Đơn *#{row['ma_don']}* — ĐT Khách: `{row['sdt_khach']}`\n"
                f"   ↳ Cấu hình: {row['so_canh']} c cành {loai_txt} ({row['mau_sac']})\n"
                f"   💰 Tiền thực thu: *{int(row['tong_tien_thuc_te']):,}đ*\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    finally:
        await conn.close()


@owner_only
async def cong_so(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/congso — Tính toán doanh thu động thông minh theo thời gian thực."""
    conn = await get_db_connection()
    try:
        # 1. Tính tổng doanh thu thực thu thu về của ngày hôm nay (chỉ tính đơn da_thanh_toan)
        rev_today = await conn.fetchval("""
            SELECT COALESCE(SUM(tong_tien_thuc_te), 0) 
            FROM orders 
            WHERE (ngay_tao AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = CURRENT_DATE AND trang_thai = 'da_thanh_toan'
        """)

        # 2. Tính tổng số tiền đã bớt do khách mặc cả của ngày hôm nay
        discount_today = await conn.fetchval("""
            SELECT COALESCE(SUM(giam_gia), 0) 
            FROM orders 
            WHERE (ngay_tao AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = CURRENT_DATE AND trang_thai = 'da_thanh_toan'
        """)

        # 3. Tính doanh thu trọn đời (tích lũy tất cả các ngày từ trước tới nay)
        rev_lifetime = await conn.fetchval("""
            SELECT COALESCE(SUM(tong_tien_thuc_te), 0) 
            FROM orders 
            WHERE trang_thai = 'da_thanh_toan'
        """)

        await update.message.reply_text(
            f"📑 *SỔ CỘNG DOANH THU ĐỘNG THỜI GIAN THỰC*\n"
            f"📅 Hôm nay: _Hệ thống tự nhận diện ngày mới, tự hiển thị về 0đ nếu chưa phát sinh đơn_\n"
            f"───────────────────\n"
            f"💰 *Doanh thu thực thu hôm nay:* `{int(rev_today):,}đ`\n"
            f"📉 Tiền đã bớt (mặc cả) hôm nay: {int(discount_today):,}đ\n"
            f"───────────────────\n"
            f"📈 *TỔNG DOANH THU TÍCH LŨY TRỌN ĐỜI:* `{int(rev_lifetime):,}đ`\n\n"
            f"_💡 Sổ toán học tự động cập nhật lại toàn bộ số liệu ngay lập tức nếu mẹ có tiến hành sửa đơn hoặc cập nhật tiền cọc của khách bán lẻ._",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Thất bại khi cộng sổ: {str(e)}")
    finally:
        await conn.close()

@owner_only
async def lich_su_don(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lichsu [dd/mm] — Xem lịch sử đơn đã thanh toán. Không nhập ngày = hôm nay."""
    from datetime import datetime, date

    args = context.args
    if args:
        try:
            ngay_xem = datetime.strptime(args[0], "%d/%m").replace(
                year=date.today().year
            ).date()
        except ValueError:
            await update.message.reply_text(
                "❌ Sai định dạng ngày! Hãy gõ theo mẫu: `/lichsu 25/01`",
                parse_mode="Markdown"
            )
            return
    else:
        ngay_xem = date.today()

    nhan_ngay = ngay_xem.strftime("%d/%m/%Y")
    conn = None  # Khởi tạo biến để an toàn cho khối finally
    try:
        conn = await get_db_connection()
        rows = await conn.fetch("""
            SELECT ma_don, sdt_khach, so_canh, loai_canh, mau_sac,
                   tong_tien_ly_tuong, giam_gia, tong_tien_thuc_te, ngay_tao
            FROM orders
            WHERE (ngay_tao AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = $1 AND trang_thai = 'da_thanh_toan'
            ORDER BY ngay_tao ASC
        """, ngay_xem)


        if not rows:
            await update.message.reply_text(
                f"📭 Ngày *{nhan_ngay}* chưa có đơn nào được thanh toán.",
                parse_mode="Markdown"
            )
            return

        tong_tien = sum(int(r["tong_tien_thuc_te"]) for r in rows)
        tong_giam = sum(int(r["giam_gia"]) for r in rows)

        lines = [f"✅ *LỊCH SỬ {len(rows)} ĐƠN ĐÃ THU TIỀN — Ngày {nhan_ngay}*\n"]
        for i, row in enumerate(rows, 1):
            loai_txt = "Đơn" if row["loai_canh"] == "don" else "Đôi"
            gio = row["ngay_tao"].strftime("%H:%M")
            lines.append(
                f"{i}. 🔖 Đơn *#{row['ma_don']}* — {gio} — ĐT: `{row['sdt_khach']}`\n"
                f"   ↳ {row['so_canh']} cành {loai_txt} ({row['mau_sac']})\n"
                f"   💰 Thực thu: *{int(row['tong_tien_thuc_te']):,}đ*"
                + (f" _(bớt {int(row['giam_gia']):,}đ)_" if row["giam_gia"] > 0 else "")
                + "\n"
            )

        lines.append(
            f"───────────────────\n"
            f"🧾 Tổng *{len(rows)} đơn* | Thu về: *{tong_tien:,}đ*"
            + (f" | Đã bớt: {tong_giam:,}đ" if tong_giam > 0 else "")
        )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi khi tra lịch sử: {str(e)}")
    finally:
        await conn.close()

# ══════════════════════════════════════════════════════════════════════════════
# KHỞI CHẠY HỆ THỐNG
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(application: Application) -> None:
    """Tự động thiết lập cấu trúc database v2 trước khi Bot chính thức online."""
    await init_db()


def main():
    if not BOT_TOKEN:
        print("❌ LỖI: Không tìm thấy khóa BOT_TOKEN trong file cấu hình môi trường .env!")
        return

    # Khởi dựng ứng dụng điều phối
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Áp các lệnh điều phối từ giấy của mẹ vào Bot hệ thống
    application.add_handler(CommandHandler("start",     start))
    application.add_handler(CommandHandler("nhapkho",   nhap_kho))
    application.add_handler(CommandHandler("tonkho",    xem_ton_kho))
    application.add_handler(CommandHandler("taodon",    tao_don))
    application.add_handler(CommandHandler("suadon",    sua_don))
    application.add_handler(CommandHandler("donhang",   xem_don_hang))
    application.add_handler(CommandHandler("capnhat",   cap_nhat_don))
    application.add_handler(CommandHandler("congso",    cong_so))
    application.add_handler(CommandHandler("lichsu",    lich_su_don))

    logger.info("🤖 Bot quản lý hoa lan thực chiến đang lắng nghe tín hiệu Polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()