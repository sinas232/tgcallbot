"""
services/backup_manager.py
مدیریت پشتیبان‌گیری و بازیابی دیتابیس (Backup & Restore) — فرمت JSON

این ماژول دقیقاً با فرمت پشتیبان اصلی ربات سازگار است:
    {
      "version": "1.0",
      "timestamp": "<ISO datetime>",
      "tables": { "users": [...], "orders": [...], ... }
    }

قابلیت‌ها:
1. ساخت فایل پشتیبان JSON از تمام جداول دیتابیس (users, telegram_accounts, orders, ...)
2. بازیابی کامل دیتابیس از روی فایل JSON (پاکسازی و درج مجدد + ریست سکوئنس‌ها)
3. سازگاری با ستون‌های اضافه/کم در فایل پشتیبان (فقط ستون‌های موجود در مدل درج می‌شوند)
4. پشتیبانی جانبی از بازیابی فایل‌های .sql (pg_dump) در صورت آپلود چنین فایلی
5. پاکسازی خودکار فایل‌های پشتیبان قدیمی

نکته: بازیابی JSON کاملاً درون‌برنامه‌ای و از طریق SQLAlchemy انجام می‌شود و به ابزار خارجی نیاز ندارد.
"""
import os
import json
import asyncio
import logging
import shutil
from datetime import datetime
from urllib.parse import urlparse, unquote

from config import Config

logger = logging.getLogger(__name__)

# مسیر نگهداری فایل‌های پشتیبان
BACKUP_DIR = os.path.join(os.getcwd(), "backups")

BACKUP_VERSION = "1.0"

# ترتیب جداول برای بازیابی (والدها ابتدا درج می‌شوند؛ حذف به‌صورت معکوس انجام می‌شود).
# هر آیتم: (کلید JSON، نام کلاس مدل در ماژول database)
TABLE_ORDER = [
    ("reseller_bots", "ResellerBot"),
    ("users", "User"),
    ("plans", "Plan"),
    ("bot_settings", "BotSetting"),
    ("telegram_accounts", "TelegramAccount"),
    ("bank_cards", "BankCard"),
    ("orders", "Order"),
    ("transactions", "Transaction"),
    ("payment_gateways", "PaymentGateway"),
    ("payment_transactions", "PaymentTransaction"),
    ("voice_call_sessions", "VoiceCallSession"),
    ("tickets", "Ticket"),
    ("ticket_messages", "TicketMessage"),
]


class BackupManager:
    """مدیریت عملیات پشتیبان‌گیری و بازیابی دیتابیس (فرمت JSON)."""

    def __init__(self):
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create backup dir: {e}")

    # ---------------- کمک‌متدها ----------------
    @staticmethod
    def _models():
        """بارگذاری تنبل ماژول database و نگاشت کلید JSON به کلاس مدل."""
        import database as db
        out = []
        for json_key, model_name in TABLE_ORDER:
            model = getattr(db, model_name, None)
            if model is not None:
                out.append((json_key, model))
        return db, out

    @staticmethod
    def _conn_params() -> dict:
        """استخراج پارامترهای اتصال دیتابیس (برای بازیابی فایل‌های .sql)."""
        host = os.getenv("POSTGRES_HOST")
        port = os.getenv("POSTGRES_PORT")
        user = os.getenv("POSTGRES_USER")
        password = os.getenv("POSTGRES_PASSWORD")
        dbname = os.getenv("POSTGRES_DB")

        if not (host and user and dbname):
            raw = os.getenv("DATABASE_URL", "") or Config.DATABASE_URL or ""
            raw = raw.strip().replace("postgresql+asyncpg://", "postgresql://").replace("postgres://", "postgresql://")
            if raw:
                try:
                    parsed = urlparse(raw)
                    host = host or parsed.hostname
                    port = port or (str(parsed.port) if parsed.port else None)
                    user = user or (unquote(parsed.username) if parsed.username else None)
                    password = password or (unquote(parsed.password) if parsed.password else None)
                    dbname = dbname or (parsed.path.lstrip("/") if parsed.path else None)
                except Exception as e:
                    logger.error(f"Failed to parse DATABASE_URL: {e}")

        return {
            "host": host or "localhost",
            "port": str(port or "5432"),
            "user": user or "postgres",
            "password": password or "",
            "dbname": dbname or "postgres",
        }

    @staticmethod
    def tools_available() -> bool:
        """در دسترس بودن psql (فقط برای بازیابی فایل‌های .sql لازم است)."""
        return bool(shutil.which("psql"))

    def temp_path(self, name: str) -> str:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        return os.path.join(BACKUP_DIR, name)

    @staticmethod
    def _serialize_value(v):
        """تبدیل مقادیر به فرمت قابل‌ذخیره در JSON."""
        if isinstance(v, datetime):
            return v.isoformat()
        return v

    @staticmethod
    def _row_to_dict(model, obj):
        return {c.name: BackupManager._serialize_value(getattr(obj, c.name)) for c in model.__table__.columns}

    @staticmethod
    def _coerce_row(model, row: dict) -> dict:
        """فقط ستون‌های موجود در مدل نگه داشته و رشته‌های تاریخ به datetime تبدیل می‌شوند."""
        from sqlalchemy import DateTime
        out = {}
        for col in model.__table__.columns:
            if col.name not in row:
                continue
            v = row[col.name]
            if v is not None and isinstance(col.type, DateTime) and isinstance(v, str):
                try:
                    v = datetime.fromisoformat(v)
                except Exception:
                    try:
                        v = datetime.strptime(v, "%Y-%m-%d %H:%M:%S.%f")
                    except Exception:
                        v = None
            out[col.name] = v
        return out

    # ---------------- ساخت پشتیبان (JSON) ----------------
    async def create_backup(self):
        """
        ساخت فایل پشتیبان JSON از تمام جداول.
        خروجی: (ok: bool, result: str) — در صورت موفقیت result مسیر فایل است.
        """
        try:
            db, models = self._models()
            from sqlalchemy import select

            os.makedirs(BACKUP_DIR, exist_ok=True)
            tables_data = {}
            async with db.AsyncSessionLocal() as session:
                for json_key, model in models:
                    res = await session.execute(select(model))
                    rows = res.scalars().all()
                    tables_data[json_key] = [self._row_to_dict(model, o) for o in rows]

            payload = {
                "version": BACKUP_VERSION,
                "timestamp": datetime.now().isoformat(),
                "tables": tables_data,
            }
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(BACKUP_DIR, f"backup_{ts}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, default=self._serialize_value)

            total_rows = sum(len(v) for v in tables_data.values())
            logger.info(f"JSON backup created: {out_path} ({total_rows} rows, {os.path.getsize(out_path)} bytes)")
            return True, out_path
        except Exception as e:
            logger.exception("create_backup error")
            return False, str(e)

    # ---------------- بازیابی ----------------
    async def restore_backup(self, file_path: str):
        """
        بازیابی دیتابیس از فایل پشتیبان.
        فرمت JSON (پیش‌فرض) یا .sql (از طریق psql) پشتیبانی می‌شود.
        خروجی: (ok: bool, message: str)
        """
        if not file_path or not os.path.exists(file_path):
            return False, "فایل پشتیبان یافت نشد."
        if os.path.getsize(file_path) == 0:
            return False, "فایل پشتیبان خالی است."

        # تشخیص فرمت: تلاش برای خواندن JSON
        is_json = file_path.lower().endswith(".json")
        if not is_json:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    head = f.read(64).lstrip()
                is_json = head.startswith("{")
            except Exception:
                is_json = False

        if is_json:
            return await self._restore_from_json(file_path)
        return await self._restore_from_sql(file_path)

    async def _restore_from_json(self, file_path: str):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return False, f"فایل JSON نامعتبر است: {e}"

        tables = data.get("tables")
        if not isinstance(tables, dict):
            return False, "ساختار فایل پشتیبان نامعتبر است (کلید tables یافت نشد)."

        try:
            db, models = self._models()
            from sqlalchemy import text as sqltext, select, func

            summary = {}
            async with db.AsyncSessionLocal() as session:
                # 1) پاکسازی جداول (به ترتیب معکوس)
                for json_key, model in reversed(models):
                    await session.execute(sqltext(f'DELETE FROM {model.__tablename__}'))
                await session.commit()

                # 2) درج مجدد (به ترتیب مستقیم)
                for json_key, model in models:
                    rows = tables.get(json_key, []) or []
                    n = 0
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        session.add(model(**self._coerce_row(model, row)))
                        n += 1
                        if n % 500 == 0:
                            await session.flush()
                    await session.commit()
                    summary[json_key] = n

                # 3) ریست سکوئنس‌ها تا id های جدید تداخل نداشته باشند
                for json_key, model in models:
                    t = model.__tablename__
                    try:
                        await session.execute(sqltext(
                            f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
                            f"COALESCE((SELECT MAX(id) FROM {t}), 1), true)"
                        ))
                    except Exception as e:
                        logger.warning(f"setval failed for {t}: {e}")
                await session.commit()

            total = sum(summary.values())
            detail = "، ".join(f"{k}: {v}" for k, v in summary.items() if v)
            return True, f"بازیابی کامل شد ({total} ردیف).\n{detail}"
        except Exception as e:
            logger.exception("restore_from_json error")
            return False, f"خطا در بازیابی JSON: {e}"

    async def _restore_from_sql(self, file_path: str):
        """بازیابی فایل‌های .sql با psql (سازگاری با فرمت pg_dump)."""
        if not shutil.which("psql"):
            return False, "این فایل SQL است اما ابزار psql روی سرور نصب نیست."
        params = self._conn_params()
        cmd = [
            "psql", "-h", params["host"], "-p", params["port"],
            "-U", params["user"], "-d", params["dbname"],
            "-v", "ON_ERROR_STOP=0", "-f", file_path,
        ]
        env = os.environ.copy()
        env["PGPASSWORD"] = params["password"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = (stderr or b"").decode(errors="ignore").strip()
                logger.error(f"psql restore failed: {err}")
                return False, f"خطای بازیابی SQL: {err[:300]}"
            return True, "بازیابی از فایل SQL با موفقیت انجام شد."
        except Exception as e:
            logger.exception("restore_from_sql error")
            return False, str(e)

    # ---------------- پاکسازی ----------------
    def cleanup_old_backups(self, keep: int = 10):
        """نگه‌داشتن آخرین N فایل پشتیبان و حذف بقیه."""
        try:
            files = sorted(
                [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                 if f.endswith(".json") or f.endswith(".sql")],
                key=os.path.getmtime, reverse=True,
            )
            for old in files[keep:]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception:
            pass


# نمونه سراسری
backup_manager = BackupManager()
