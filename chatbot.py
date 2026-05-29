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
import json # ← Thêm dòng này để hỗ trợ lưu cấu hình chậu ghép
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
            CREATE TABLE IF NOT EXISTS products (
                id              SERIAL PRIMARY KEY,
                loai_canh       VARCHAR(20) NOT NULL,
                mau_sac         VARCHAR(50) NOT NULL,
                so_luong_ton    INT NOT NULL DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_product_type UNIQUE (loai_canh, mau_sac)
            );

            -- [ĐÃ CẬP NHẬT] Bảng quản lý đơn hàng hỗ trợ chậu ghép n màu
            CREATE TABLE IF NOT EXISTS orders (
                ma_don              SERIAL PRIMARY KEY,
                sdt_khach           VARCHAR(20) NOT NULL,
                tong_so_canh        INT NOT NULL,
                cau_hinh_hoa        TEXT NOT NULL, -- Lưu JSON để tiện hoàn kho tự động
                chi_tiet_text       TEXT NOT NULL, -- Lưu text hiển thị cho hóa đơn
                tien_chau           DECIMAL(15, 0) NOT NULL DEFAULT 0,
                tien_phu_kien       DECIMAL(15, 0) NOT NULL DEFAULT 0,
                tien_ship           DECIMAL(15, 0) NOT NULL DEFAULT 0,
                giam_gia            DECIMAL(15, 0) NOT NULL DEFAULT 0,
                tong_tien_ly_tuong  DECIMAL(15, 0) NOT NULL DEFAULT 0,
                tong_tien_thuc_te   DECIMAL(15, 0) NOT NULL DEFAULT 0,
                trang_thai          VARCHAR(30) DEFAULT 'cho_thanh_toan',
                ngay_tao            TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS inventory_log (
                id              SERIAL PRIMARY KEY,
                loai_canh       VARCHAR(20) NOT NULL,
                mau_sac         VARCHAR(50) NOT NULL,
                loai_giao_dich  VARCHAR(10) NOT NULL,
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
    """/taodon [sốcành] [loại] [màu] ... [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]"""
    args = context.args
    # Kiểm tra xem độ dài tham số có hợp lệ không (Phải có 5 tham số cuối cố định, phần còn lại chia hết cho 3)
    if len(args) < 8 or (len(args) - 5) % 3 != 0:
        await update.message.reply_text(
            "❌ *Sai cú pháp tạo đơn ghép!*\n"
            "Mẫu: `/taodon [số] [loại] [màu] ... [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]`\n\n"
            "Ví dụ chậu mix 2 loại: `/taodon 3 don vang 2 doi trang 200000 100000 150000 50000 0912345678`",
            parse_mode="Markdown"
        )
        return

    try:
        # Lấy 5 tham số chi phí ở cuối cùng
        tien_chau, tien_phu_kien, tien_ship, giam_gia = map(int, args[-5:-1])
        sdt_khach = args[-1].strip()
        if any(v < 0 for v in [tien_chau, tien_phu_kien, tien_ship, giam_gia]): raise ValueError

        # Lọc danh sách cấu hình hoa ở phần đầu
        flower_args = args[:-5]
        danh_sach_hoa = []
        tong_so_canh = 0
        tien_hoa_goc = 0
        chi_tiet_text_list = []

        for i in range(0, len(flower_args), 3):
            so_c = int(flower_args[i])
            loai = flower_args[i+1].lower().strip()
            mau = flower_args[i+2].lower().strip()
            
            if so_c <= 0 or loai not in ["don", "doi"]: raise ValueError
            
            danh_sach_hoa.append({"so_canh": so_c, "loai": loai, "mau": mau})
            tong_so_canh += so_c
            tien_hoa_goc += so_c * PRICES[loai]
            loai_txt = "Đơn" if loai == "don" else "Đôi"
            chi_tiet_text_list.append(f"{so_c} {loai_txt} {mau}")

        chi_tiet_hoa_str = " + ".join(chi_tiet_text_list)
        cau_hinh_json = json.dumps(danh_sach_hoa)

    except ValueError:
        await update.message.reply_text("❌ Lỗi định dạng! Hãy kiểm tra lại các con số và loại cành (chỉ điền don/doi).")
        return

    conn = await get_db_connection()
    try:
        async with conn.transaction():
            # 1. Kiểm tra tồn kho hàng loạt xem có đủ cắm nguyên chậu không
            for hoa in danh_sach_hoa:
                stock = await conn.fetchval(
                    "SELECT so_luong_ton FROM products WHERE loai_canh = $1 AND mau_sac = $2", 
                    hoa['loai'], hoa['mau']
                )
                if stock is None or stock < hoa['so_canh']:
                    hien_tai = stock if stock is not None else 0
                    await update.message.reply_text(f"❌ *Thiếu hàng!* Loại `{hoa['loai']}-{hoa['mau']}` trong vườn chỉ còn *{hien_tai} cành*.", parse_mode="Markdown")
                    return

            # 2. Khấu trừ kho hàng loạt
            for hoa in danh_sach_hoa:
                await conn.execute("UPDATE products SET so_luong_ton = so_luong_ton - $1 WHERE loai_canh = $2 AND mau_sac = $3", hoa['so_canh'], hoa['loai'], hoa['mau'])
                await conn.execute("INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'xuat', $3, 'Xuất cắm chậu ghép')", hoa['loai'], hoa['mau'], hoa['so_canh'])

            # 3. Tính tiền
            tong_tien_ly_tuong = tien_hoa_goc + tien_chau + tien_phu_kien + tien_ship
            tong_tien_thuc_te = tong_tien_ly_tuong - giam_gia

            # 4. Lưu đơn
            ma_don = await conn.fetchval("""
                INSERT INTO orders (sdt_khach, tong_so_canh, cau_hinh_hoa, chi_tiet_text, tien_chau, tien_phu_kien, tien_ship, giam_gia, tong_tien_ly_tuong, tong_tien_thuc_te, trang_thai)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'cho_thanh_toan') RETURNING ma_don
            """, sdt_khach, tong_so_canh, cau_hinh_json, chi_tiet_hoa_str, tien_chau, tien_phu_kien, tien_ship, giam_gia, tong_tien_ly_tuong, tong_tien_thuc_te)

        thoi_gian_tao = __import__('datetime').datetime.now().strftime("%H:%M  %d/%m/%Y")
        await update.message.reply_text(
            f"🧾 *ĐÃ LẬP HÓA ĐƠN CHẬU LAN THÀNH CÔNG!*\n"
            f"🆔 *Mã Đơn: #{ma_don}*\n🕐 Thời gian: {thoi_gian_tao}\n📞 SĐT khách: `{sdt_khach}`\n"
            f"🌸 Cấu hình: *{chi_tiet_hoa_str}* (Tổng: {tong_so_canh} cành)\n"
            f"───────────────────\n"
            f"💵 Tiền hoa gốc: {tien_hoa_goc:,}đ\n"
            f"🏺 Chậu: {tien_chau:,}đ | 🎀 Phụ kiện: {tien_phu_kien:,}đ | 🚗 Ship: {tien_ship:,}đ\n"
            f"📉 Giảm giá: -{giam_gia:,}đ\n"
            f"───────────────────\n"
            f"💰 *TỔNG THỰC THU:* `{tong_tien_thuc_te:,}đ`\n"
            f"⏳ Trạng thái: *CHỜ THANH TOÁN*", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi hệ thống: {str(e)}")
    finally:
        await conn.close()


@owner_only
async def sua_don(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/suadon [mã_đơn] [sốcành] [loại] [màu] ... [tiền_chậu] [phụ_kiện] [ship] [giảm_giá] [sđt_khách]"""
    args = context.args
    if len(args) < 9 or (len(args) - 6) % 3 != 0:
        await update.message.reply_text("❌ *Sai cú pháp sửa đơn ghép!*", parse_mode="Markdown")
        return

    try:
        ma_don = int(args[0])
        tien_chau, tien_phu_kien, tien_ship, giam_gia = map(int, args[-5:-1])
        sdt_khach = args[-1].strip()

        flower_args = args[1:-5]
        danh_sach_hoa_moi = []
        tong_so_canh = 0
        tien_hoa_goc = 0
        chi_tiet_text_list = []

        for i in range(0, len(flower_args), 3):
            so_c = int(flower_args[i])
            loai = flower_args[i+1].lower().strip()
            mau = flower_args[i+2].lower().strip()
            danh_sach_hoa_moi.append({"so_canh": so_c, "loai": loai, "mau": mau})
            tong_so_canh += so_c
            tien_hoa_goc += so_c * PRICES[loai]
            loai_txt = "Đơn" if loai == "don" else "Đôi"
            chi_tiet_text_list.append(f"{so_c} {loai_txt} {mau}")

        chi_tiet_hoa_str = " + ".join(chi_tiet_text_list)
        cau_hinh_json = json.dumps(danh_sach_hoa_moi)

    except ValueError:
        await update.message.reply_text("❌ Lỗi định dạng số liệu.")
        return

    conn = await get_db_connection()
    try:
        async with conn.transaction():
            old_order = await conn.fetchrow("SELECT cau_hinh_hoa, trang_thai FROM orders WHERE ma_don = $1", ma_don)
            if not old_order:
                await update.message.reply_text(f"❌ Không tồn tại mã đơn #{ma_don}.")
                return

            # BƯỚC 1: Hoàn lại hoa cũ về kho
            if old_order["trang_thai"] != 'da_huy':
                old_cau_hinh = json.loads(old_order["cau_hinh_hoa"])
                for hoa in old_cau_hinh:
                    await conn.execute("""
                        INSERT INTO products (loai_canh, mau_sac, so_luong_ton) VALUES ($1, $2, $3)
                        ON CONFLICT (loai_canh, mau_sac) DO UPDATE SET so_luong_ton = products.so_luong_ton + EXCLUDED.so_luong_ton
                    """, hoa['loai'], hoa['mau'], hoa['so_canh'])
                    await conn.execute("INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'nhap', $3, 'Hoàn kho sửa đơn')", hoa['loai'], hoa['mau'], hoa['so_canh'])

            # BƯỚC 2: Kiểm tra tồn kho cấu hình mới
            for hoa in danh_sach_hoa_moi:
                stock = await conn.fetchval("SELECT so_luong_ton FROM products WHERE loai_canh = $1 AND mau_sac = $2", hoa['loai'], hoa['mau'])
                if stock is None or stock < hoa['so_canh']:
                    hien_tai = stock if stock is not None else 0
                    raise Exception(f"Thiếu hàng! Loại {hoa['loai']}-{hoa['mau']} chỉ còn {hien_tai} cành.")

            # BƯỚC 3: Khấu trừ cấu hình mới
            for hoa in danh_sach_hoa_moi:
                await conn.execute("UPDATE products SET so_luong_ton = so_luong_ton - $1 WHERE loai_canh = $2 AND mau_sac = $3", hoa['so_canh'], hoa['loai'], hoa['mau'])
                await conn.execute("INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'xuat', $3, 'Trừ kho sửa đơn')", hoa['loai'], hoa['mau'], hoa['so_canh'])

            # BƯỚC 4: Cập nhật hóa đơn
            tong_tien_ly_tuong = tien_hoa_goc + tien_chau + tien_phu_kien + tien_ship
            tong_tien_thuc_te = tong_tien_ly_tuong - giam_gia

            await conn.execute("""
                UPDATE orders SET sdt_khach = $1, tong_so_canh = $2, cau_hinh_hoa = $3, chi_tiet_text = $4,
                tien_chau = $5, tien_phu_kien = $6, tien_ship = $7, giam_gia = $8, tong_tien_ly_tuong = $9, tong_tien_thuc_te = $10 WHERE ma_don = $11
            """, sdt_khach, tong_so_canh, cau_hinh_json, chi_tiet_hoa_str, tien_chau, tien_phu_kien, tien_ship, giam_gia, tong_tien_ly_tuong, tong_tien_thuc_te, ma_don)

        await update.message.reply_text(f"🔄 *ĐÃ SỬA ĐƠN #{ma_don} THÀNH CÔNG!*\n🌸 Cấu hình mới: {chi_tiet_hoa_str}\n💰 Thực thu mới: `{tong_tien_thuc_te:,}đ`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Hủy sửa đơn do lỗi: {str(e)}")
    finally:
        await conn.close()


@owner_only
async def cap_nhat_don(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/capnhat [mã_đơn] [thanh_toan / huy / cho]"""
    args = context.args
    if len(args) != 2: return await update.message.reply_text("❌ Dùng: `/capnhat [mã_đơn] thanh_toan` / `huy` / `cho`")
    
    ma_don = int(args[0])
    status_map = {"thanh_toan": "da_thanh_toan", "huy": "da_huy", "cho": "cho_thanh_toan"}
    if args[1].lower() not in status_map: return await update.message.reply_text("❌ Sai trạng thái.")
    
    new_status = status_map[args[1].lower()]
    conn = await get_db_connection()
    try:
        async with conn.transaction():
            order = await conn.fetchrow("SELECT cau_hinh_hoa, trang_thai FROM orders WHERE ma_don = $1", ma_don)
            if not order: return await update.message.reply_text("❌ Không tìm thấy đơn hàng.")
            if order["trang_thai"] == new_status: return await update.message.reply_text("ℹ️ Trạng thái đã trùng.")

            cau_hinh = json.loads(order["cau_hinh_hoa"])
            
            # XỬ LÝ HỦY ĐƠN: Hoàn kho hàng loạt
            if new_status == "da_huy":
                for hoa in cau_hinh:
                    await conn.execute("INSERT INTO products (loai_canh, mau_sac, so_luong_ton) VALUES ($1, $2, $3) ON CONFLICT (loai_canh, mau_sac) DO UPDATE SET so_luong_ton = products.so_luong_ton + EXCLUDED.so_luong_ton", hoa['loai'], hoa['mau'], hoa['so_canh'])
                    await conn.execute("INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'nhap', $3, 'Hoàn kho hủy đơn')", hoa['loai'], hoa['mau'], hoa['so_canh'])
            
            # XỬ LÝ KHÔI PHỤC ĐƠN HỦY: Trừ kho hàng loạt
            elif order["trang_thai"] == "da_huy":
                for hoa in cau_hinh:
                    stock = await conn.fetchval("SELECT so_luong_ton FROM products WHERE loai_canh = $1 AND mau_sac = $2", hoa['loai'], hoa['mau'])
                    if stock is None or stock < hoa['so_canh']: raise Exception(f"Không đủ hoa {hoa['loai']}-{hoa['mau']} để khôi phục đơn!")
                    await conn.execute("UPDATE products SET so_luong_ton = so_luong_ton - $1 WHERE loai_canh = $2 AND mau_sac = $3", hoa['so_canh'], hoa['loai'], hoa['mau'])
                    await conn.execute("INSERT INTO inventory_log (loai_canh, mau_sac, loai_giao_dich, so_luong, ghi_chu) VALUES ($1, $2, 'xuat', $3, 'Trừ kho phục hồi đơn')", hoa['loai'], hoa['mau'], hoa['so_canh'])

            await conn.execute("UPDATE orders SET trang_thai = $1 WHERE ma_don = $2", new_status, ma_don)
        await update.message.reply_text(f"✅ Đơn #{ma_don} đổi sang: *{new_status.upper()}*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {str(e)}")
    finally:
        await conn.close()


@owner_only
async def xem_don_hang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/donhang — Xem các đơn đang nợ tiền."""
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT ma_don, sdt_khach, chi_tiet_text, tong_tien_thuc_te FROM orders WHERE trang_thai = 'cho_thanh_toan' ORDER BY ngay_tao DESC LIMIT 15")
        if not rows: return await update.message.reply_text("✅ Sổ đơn sạch sẽ! Không có đơn nợ.")

        lines = [f"⏳ *DANH SÁCH {len(rows)} ĐƠN ĐANG CHỜ THANH TOÁN:*\n"]
        for row in rows:
            lines.append(f"🔖 Đơn *#{row['ma_don']}* — ĐT: `{row['sdt_khach']}`\n   ↳ Gồm: {row['chi_tiet_text']}\n   💰 Thực thu: *{int(row['tong_tien_thuc_te']):,}đ*\n")
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
    """/lichsu [dd/mm]"""
    from datetime import datetime, date
    args = context.args
    ngay_xem = datetime.strptime(args[0], "%d/%m").replace(year=date.today().year).date() if args else date.today()
    nhan_ngay = ngay_xem.strftime("%d/%m/%Y")
    
    conn = await get_db_connection()
    try:
        rows = await conn.fetch("SELECT ma_don, sdt_khach, chi_tiet_text, giam_gia, tong_tien_thuc_te, ngay_tao FROM orders WHERE (ngay_tao AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = $1 AND trang_thai = 'da_thanh_toan' ORDER BY ngay_tao ASC", ngay_xem)
        if not rows: return await update.message.reply_text(f"📭 Ngày *{nhan_ngay}* chưa có đơn nào.", parse_mode="Markdown")

        tong_tien, tong_giam = sum(int(r["tong_tien_thuc_te"]) for r in rows), sum(int(r["giam_gia"]) for r in rows)
        lines = [f"✅ *LỊCH SỬ {len(rows)} ĐƠN NGÀY {nhan_ngay}*\n"]
        for i, row in enumerate(rows, 1):
            gio = row["ngay_tao"].strftime("%H:%M")
            lines.append(f"{i}. 🔖 *#{row['ma_don']}* — {gio} — ĐT: `{row['sdt_khach']}`\n   ↳ {row['chi_tiet_text']}\n   💰 Thực thu: *{int(row['tong_tien_thuc_te']):,}đ*\n")
        lines.append(f"───────────────────\n🧾 Tổng thu: *{tong_tien:,}đ*")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
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