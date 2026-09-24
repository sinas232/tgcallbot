"""
database.py
نسخه نهایی و کامل:
1. اضافه شدن متد get_tickets_by_status اصلاح شده برای فیلتر تیکت‌ها
2. اضافه شدن متد get_user_tickets برای لیست تیکت‌های کاربر
3. شامل تمام جداول و متدهای قبلی (کارت بانکی، سفارش و ...) به صورت کامل
4. متد جدید has_active_order_for_link برای خروج هوشمند
5. ✅ متدهای دریافت لیست اکانت‌های محدود و سوخته (Dead/Limited)
"""
import logging
import hashlib
import hmac
import json
import math
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import (
    Column, Integer, String, Boolean, Float, DateTime, Text,
    BigInteger, func, select, update, delete, desc, text, case, or_, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from config import Config

logger = logging.getLogger(__name__)

# Only a typed USER_DEACTIVATED response from a guarded single-account probe
# may set this new marker. Historic 'dead', revoked and 406 labels are ineligible.
CONFIRMED_ACCOUNT_DELETED = 'Verified Telegram account deleted: USER_DEACTIVATED'
Base = declarative_base()


async def _lock_order_admission(session: AsyncSession) -> None:
    """Serialize capacity checks across bot/reseller processes in PostgreSQL.

    Not an auth-key lock. This lock is transaction-scoped and acquired BEFORE
    plan/wallet/order row locks so checkout and scheduled claims cannot race.
    SQLite test fixtures serialize writers themselves; production uses PG.
    """
    if session.get_bind().dialect.name == 'postgresql':
        await session.execute(text('SELECT pg_advisory_xact_lock(213669, 42)'))


async def _occupied_order_slots(session: AsyncSession, *, exclude_id: Optional[int] = None) -> int:
    stmt = select(func.count(Order.id)).where(Order.status.in_(('pending', 'running')))
    if exclude_id is not None:
        stmt = stmt.where(Order.id != int(exclude_id))
    return int((await session.execute(stmt)).scalar() or 0)


async def _global_maintenance_enabled(session: AsyncSession) -> bool:
    """Unlike get_setting(), a DB error must propagate (fail closed)."""
    res = await session.execute(select(BotSetting.value).where(
        BotSetting.bot_id == 1, BotSetting.key == 'maintenance_mode'))
    return res.scalar_one_or_none() == '1'


DB_URL_ASYNC = Config.get_normalized_database_url()
if not DB_URL_ASYNC:
    raise RuntimeError("DATABASE_URL not provided")

engine = create_async_engine(
    DB_URL_ASYNC, 
    echo=False, 
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    # DB awaits must never hang forever: one stuck await in the update
    # guard once silenced the whole bot. Caps are generous (normal
    # queries take <1s) but close the hang-forever hole.
    pool_timeout=15,
    pool_recycle=300,
    connect_args={"timeout": 10, "command_timeout": 30},
)
AsyncSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=AsyncSession, expire_on_commit=False)

def to_dict(obj):
    if not obj: return None
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

# ===================== MODELS =====================

class ResellerBot(Base):
    __tablename__ = "reseller_bots"
    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(100), unique=True, nullable=False)
    owner_id = Column(BigInteger, nullable=False) 
    api_id = Column(Integer, nullable=True) 
    api_hash = Column(String(100), nullable=True)
    expiry_date = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    name = Column(String(100), nullable=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True) 
    telegram_id = Column(BigInteger, nullable=False, index=True)
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    admin_role = Column(String(20), default="admin") 
    credit = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    phone_number = Column(String(20), nullable=True)
    is_verified = Column(Boolean, default=False) 
    is_banned = Column(Boolean, default=False) 
    kyc_status = Column(String(20), default="none") 
    kyc_reject_reason = Column(Text, nullable=True)
    exempt_phone_verify = Column(Boolean, default=False)
    kyc_card_number = Column(String(20), nullable=True)
    # 🛡 ممنوعیت ثبت سفارش جدید پس از لغو (ضد اسپم) — تا این لحظه (UTC) کاربر
    # نمی‌تواند سفارش تازه‌ای ثبت کند. NULL یعنی بدون ممنوعیت.
    cancel_cooldown_until = Column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint('telegram_id', 'bot_id', name='uq_user_bot'),)

class BankCard(Base):
    __tablename__ = "bank_cards"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    bot_id = Column(Integer, default=1)
    card_number = Column(String(20), nullable=False)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('card_number', 'user_id', name='uq_card_user'),)

class Plan(Base):
    __tablename__ = "plans"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    service_type = Column(String(50), nullable=False)
    accounts_count = Column(Integer, nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)

class BotSetting(Base):
    __tablename__ = "bot_settings"
    id = Column(Integer, primary_key=True, index=True) 
    bot_id = Column(Integer, default=1, index=True)
    key = Column(String(50), nullable=False)
    value = Column(Text, nullable=True)
    __table_args__ = (UniqueConstraint('bot_id', 'key', name='uq_setting_bot'),)

class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    user_id = Column(Integer, nullable=False) 
    phone_number = Column(String(20), nullable=False, index=True)
    session_string = Column(Text, nullable=False)
    api_id = Column(Integer, nullable=True)
    api_hash = Column(String(100), nullable=True)
    # 🔥 اطلاعات کش‌شدهٔ پروفایل اکانت (برای نمایش در لیست بدون نیاز به اتصال زنده)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    username = Column(String(255), nullable=True)
    account_status = Column(String(20), default="active")
    health_score = Column(Integer, default=100)
    last_health_check = Column(DateTime, nullable=True)
    spam_status = Column(String(50), default="unknown")
    spam_check_result = Column(Text, nullable=True)
    is_verified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('phone_number', 'bot_id', name='uq_account_bot'),)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    plan_id = Column(Integer, nullable=True)
    order_type = Column(String(50), nullable=False)
    target_link = Column(String(255), nullable=False)
    accounts_count = Column(Integer, nullable=False)
    duration_minutes = Column(Integer, nullable=True)
    price_paid = Column(Float, default=0.0)
    status = Column(String(20), default="pending", index=True) 
    created_at = Column(DateTime, default=datetime.utcnow)
    scheduled_for = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    amount = Column(Float, nullable=False)
    type = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class VoiceCallSession(Base):
    __tablename__ = "voice_call_sessions"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    order_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    status = Column(String(20), default="joining")
    join_time = Column(DateTime, default=datetime.utcnow)


class PendingGroupLeave(Base):
    """🛡 صف خروج به‌تأخیرافتادهٔ اکانت‌ها از گروه/کانال (ضد اسپم).

    بعد از پایان/لغو سفارش، اکانت از ویس‌کال بلافاصله خارج می‌شود اما خروج از
    خودِ گروه در این جدول زمان‌بندی می‌شود (پیش‌فرض: یک هفته بعد). جاب دوره‌ای
    services/group_leave_scheduler رکوردهای سررسید را «دونه‌به‌دونه و به‌ترتیب»
    (به‌ترتیب not_before سپس id، با فاصلهٔ زمانی بین هر خروج) اجرا می‌کند تا
    خروج انبوه و یک‌جا — الگوی کلاسیک ربات — رخ ندهد. اگر کاربر برای همان مقصد
    دوباره سفارش ثبت کند، رکوردهای pending همان مقصد لغو (cancelled) می‌شوند.
    """
    __tablename__ = "pending_group_leaves"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=True, index=True)     # وقتی شناخته‌شده (مسیر ویس‌کال)
    target_link = Column(String(255), nullable=True)            # متن لینک سفارش (مسیر عضویت گروه/کانال)
    canonical_target = Column(String(255), nullable=True, index=True)  # فرم نرمال‌شده برای تطبیق سفارش مجدد
    order_id = Column(Integer, nullable=True)
    # pending → processing → done / failed / cancelled
    status = Column(String(20), default="pending", index=True)
    attempts = Column(Integer, default=0)                       # دفعات تلاش برای خروج
    not_before = Column(DateTime, nullable=False, index=True)   # زودتر از این لحظه خارج نشود
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)


class VoiceJoinAttempt(Base):
    """Persistent record of a single join attempt (never overwritten).

    Supports post-mortem forensics ("why didn't all accounts join?") and
    crash recovery. Rows are append-only: 4 attempts → 4 rows.
    """
    __tablename__ = "voice_join_attempts"
    id = Column(Integer, primary_key=True, index=True)
    attempt_id = Column(String(64), unique=True, index=True)
    order_id = Column(Integer, nullable=False, index=True)
    account_id = Column(Integer, nullable=False, index=True)
    voice_chat_id = Column(BigInteger, nullable=True)
    attempt_number = Column(Integer, default=1)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    stage = Column(String(50), nullable=True)
    result = Column(String(50), default="PENDING")
    error_type = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)
    telegram_error = Column(Text, nullable=True)
    telegram_error_code = Column(Integer, nullable=True)
    flood_wait_seconds = Column(Float, nullable=True)
    retry_at = Column(Float, nullable=True)
    failure_class = Column(String(50), nullable=True)
    presence_result = Column(String(50), nullable=True)
    final_result = Column(String(50), nullable=True)
    reason = Column(Text, nullable=True)
    previous_state = Column(String(50), nullable=True)
    final_state = Column(String(50), nullable=True)
    trace_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PaymentGateway(Base):
    __tablename__ = "payment_gateways"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    name = Column(String(50), nullable=False)
    slug = Column(String(50), nullable=False) 
    is_active = Column(Boolean, default=False)
    config_json = Column(Text, default="{}")
    __table_args__ = (UniqueConstraint('bot_id', 'slug', name='uq_gateway_bot'),)

class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    user_id = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    trans_id = Column(String(100), unique=True, nullable=False)
    status = Column(String(20), default="pending")
    gateway_slug = Column(String(50))
    # لینک واقعی درگاه (StartPay). صفحهٔ میانیِ /pay/{trans_id} روی دامنهٔ خودمان
    # کاربر را به این آدرس هدایت می‌کند تا Referrer با دامنهٔ اصلی تطابق داشته باشد
    # (الزام شاپرک برای بات‌ها).
    pay_url = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, default=1, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    subject = Column(String(255), nullable=True)          # موضوع تیکت
    priority = Column(String(20), default="normal")        # low, normal, high
    status = Column(String(20), default="open")            # open, answered, closed
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)            # زمان بسته‌شدن

class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, nullable=False, index=True)
    sender_type = Column(String(20), nullable=False)       # user, admin, system
    sender_name = Column(String(255), nullable=True)       # نام نمایشیِ فرستنده
    message_type = Column(String(20), default="text")      # text, photo, voice, document, video
    content = Column(Text, nullable=True)
    file_id = Column(String(255), nullable=True)           # برای بازنمایی ضمیمه‌ها
    created_at = Column(DateTime, default=datetime.utcnow)

# ===================== MANAGER =====================

class DatabaseManager:
    @staticmethod
    async def init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await DatabaseManager._run_safe_migrations()
        await DatabaseManager.init_default_gateways(bot_id=1)
        await DatabaseManager.reserve_main_bot_id()

    @staticmethod
    async def _run_safe_migrations():
        commands = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bot_id INTEGER DEFAULT 1;",
            # لینک واقعی درگاه برای صفحهٔ میانیِ Referrer-safe (الزام شاپرک برای بات‌ها)
            "ALTER TABLE payment_transactions ADD COLUMN IF NOT EXISTS pay_url VARCHAR(500);",
            # جدول کارت بانکی
            """
            CREATE TABLE IF NOT EXISTS bank_cards (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                bot_id INTEGER DEFAULT 1,
                card_number VARCHAR(20) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP WITHOUT TIME ZONE
            );
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_card_user ON bank_cards (card_number, user_id);",
            # جداول تیکت
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                bot_id INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                status VARCHAR(20) DEFAULT 'open',
                created_at TIMESTAMP WITHOUT TIME ZONE,
                updated_at TIMESTAMP WITHOUT TIME ZONE
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER NOT NULL,
                sender_type VARCHAR(20) NOT NULL,
                message_type VARCHAR(20) DEFAULT 'text',
                content TEXT,
                created_at TIMESTAMP WITHOUT TIME ZONE
            );
            """,
            # ستون‌های جدیدِ سیستم تیکتینگ حرفه‌ای (موضوع، اولویت، زمان بستن).
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS subject VARCHAR(255);",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS priority VARCHAR(20) DEFAULT 'normal';",
            "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP WITHOUT TIME ZONE;",
            "ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS sender_name VARCHAR(255);",
            "ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS file_id VARCHAR(255);",
            # اطلاعات کش‌شدهٔ پروفایل اکانت‌های تلگرام (نام/نام‌خانوادگی/یوزرنیم)
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS first_name VARCHAR(255);",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS last_name VARCHAR(255);",
            "ALTER TABLE telegram_accounts ADD COLUMN IF NOT EXISTS username VARCHAR(255);",
            # 🛡 ضد اسپم: مهلت ممنوعیتِ ثبت سفارش پس از لغو برای کاربر
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS cancel_cooldown_until TIMESTAMP WITHOUT TIME ZONE;",
            # 🛡 ضد اسپم: صف خروج به‌تأخیرافتادهٔ دونه‌به‌دونه از گروه/کانال
            """
            CREATE TABLE IF NOT EXISTS pending_group_leaves (
                id SERIAL PRIMARY KEY,
                bot_id INTEGER DEFAULT 1,
                account_id INTEGER NOT NULL,
                chat_id BIGINT,
                target_link VARCHAR(255),
                canonical_target VARCHAR(255),
                order_id INTEGER,
                status VARCHAR(20) DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                not_before TIMESTAMP WITHOUT TIME ZONE,
                last_error TEXT,
                created_at TIMESTAMP WITHOUT TIME ZONE,
                processed_at TIMESTAMP WITHOUT TIME ZONE
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_pgl_status_notbefore ON pending_group_leaves (status, not_before);",
            "CREATE INDEX IF NOT EXISTS idx_pgl_bot_status ON pending_group_leaves (bot_id, status);",
        ]
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            for cmd in commands:
                try: await conn.execute(text(cmd))
                except: pass

    # ================= CARD MANAGEMENT =================
    @staticmethod
    async def add_bank_card(user_id: int, card_number: str, bot_id: int = 1):
        async with AsyncSessionLocal() as db_session:
            try:
                res = await db_session.execute(select(BankCard).filter(BankCard.card_number == card_number, BankCard.user_id == user_id))
                if res.scalar_one_or_none(): return False, "duplicate"
                card = BankCard(user_id=user_id, bot_id=bot_id, card_number=card_number, status='pending')
                db_session.add(card)
                await db_session.commit()
                return True, "added"
            except: return False, "error"

    @staticmethod
    async def get_user_cards(user_id: int, bot_id: int = 1):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(BankCard).filter(BankCard.user_id == user_id, BankCard.bot_id == bot_id))
            return [to_dict(c) for c in res.scalars().all()]

    @staticmethod
    async def update_card_status(card_id: int, status: str):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(BankCard).where(BankCard.id == card_id).values(status=status))
            if status == 'approved':
                card = await db_session.get(BankCard, card_id)
                if card:
                    await db_session.execute(update(User).where(User.id == card.user_id).values(kyc_status='verified'))
            await db_session.commit()

    @staticmethod
    async def get_card_by_id(card_id: int):
        async with AsyncSessionLocal() as db_session:
            c = await db_session.get(BankCard, card_id)
            return to_dict(c)

    # ================= TICKET METHODS (UPDATED) =================
    @staticmethod
    async def get_active_ticket(user_id: int, bot_id: int = 1):
        """گرفتن تیکت باز کاربر"""
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(Ticket).filter(Ticket.user_id == user_id, Ticket.bot_id == bot_id, Ticket.status.in_(['open', 'answered'])).order_by(desc(Ticket.created_at)))
            return to_dict(res.scalar_one_or_none())

    @staticmethod
    async def get_user_tickets(user_id: int, bot_id: int = 1):
        """لیست تمام تیکت‌های یک کاربر"""
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(Ticket).filter(Ticket.user_id == user_id, Ticket.bot_id == bot_id).order_by(desc(Ticket.updated_at)))
            return [to_dict(t) for t in res.scalars().all()]

    @staticmethod
    async def create_ticket(user_id: int, bot_id: int = 1, subject: str = None, priority: str = 'normal'):
        async with AsyncSessionLocal() as db_session:
            now = datetime.utcnow()
            ticket = Ticket(user_id=user_id, bot_id=bot_id, status='open', subject=subject, priority=priority, created_at=now, updated_at=now)
            db_session.add(ticket)
            await db_session.commit()
            await db_session.refresh(ticket)
            return to_dict(ticket)

    @staticmethod
    async def add_ticket_message(ticket_id: int, sender_type: str, message_type: str, content: str, sender_name: str = None, file_id: str = None):
        async with AsyncSessionLocal() as db_session:
            msg = TicketMessage(
                ticket_id=ticket_id, sender_type=sender_type, message_type=message_type,
                content=content, sender_name=sender_name, file_id=file_id, created_at=datetime.utcnow(),
            )
            db_session.add(msg)
            # پیام‌های سیستمی وضعیت را تغییر نمی‌دهند؛ پیام کاربر → open، پاسخ ادمین → answered.
            if sender_type == 'user':
                new_status = 'open'
            elif sender_type == 'admin':
                new_status = 'answered'
            else:
                new_status = None
            vals = {"updated_at": datetime.utcnow()}
            if new_status:
                vals["status"] = new_status
            await db_session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**vals))
            await db_session.commit()
            await db_session.refresh(msg)
            return to_dict(msg)

    @staticmethod
    async def get_ticket_messages(ticket_id: int):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(TicketMessage).filter(TicketMessage.ticket_id == ticket_id).order_by(TicketMessage.created_at))
            return [to_dict(m) for m in res.scalars().all()]

    @staticmethod
    async def get_ticket_by_id(ticket_id: int):
        async with AsyncSessionLocal() as db_session:
            t = await db_session.get(Ticket, ticket_id)
            return to_dict(t)

    @staticmethod
    async def close_ticket(ticket_id: int):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(Ticket).where(Ticket.id == ticket_id).values(status='closed', closed_at=datetime.utcnow()))
            await db_session.commit()

    @staticmethod
    async def set_ticket_priority(ticket_id: int, priority: str):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(Ticket).where(Ticket.id == ticket_id).values(priority=priority))
            await db_session.commit()

    @staticmethod
    async def reopen_ticket(ticket_id: int):
        """بازکردن مجدد تیکت بسته‌شده (مثلاً وقتی کاربر دوباره پیام می‌دهد)."""
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(Ticket).where(Ticket.id == ticket_id).values(status='open', closed_at=None, updated_at=datetime.utcnow()))
            await db_session.commit()

    @staticmethod
    async def get_tickets_to_autoclose(bot_id: int = None, idle_hours: int = 48):
        """تیکت‌هایی که ادمین به آن‌ها پاسخ داده (answered) و از آخرین فعالیت
        بیش از idle_hours ساعت گذشته → کاندید بستنِ خودکار.

        نکته: فقط تیکت‌های 'answered' بسته می‌شوند؛ یعنی حتماً پاسخ ادمین را
        گرفته‌اند و کاربر پیام جدیدی نداده (پیام جدید کاربر → status=open)."""
        cutoff = datetime.utcnow() - timedelta(hours=idle_hours)
        async with AsyncSessionLocal() as db_session:
            q = select(Ticket).filter(Ticket.status == 'answered', Ticket.updated_at <= cutoff)
            if bot_id is not None:
                q = q.filter(Ticket.bot_id == bot_id)
            res = await db_session.execute(q)
            return [to_dict(t) for t in res.scalars().all()]

    @staticmethod
    async def get_tickets_by_status(bot_id: int, status_filter: str = 'all', user_id: int = None):
        """فیلتر پیشرفته تیکت‌ها برای ادمین"""
        async with AsyncSessionLocal() as db_session:
            q = select(Ticket, User).join(User, Ticket.user_id == User.id).filter(Ticket.bot_id == bot_id)
            
            if user_id:
                q = q.filter(Ticket.user_id == user_id)
            
            if status_filter == 'open':
                q = q.filter(Ticket.status == 'open')
            elif status_filter == 'answered':
                q = q.filter(Ticket.status == 'answered')
            elif status_filter == 'closed':
                q = q.filter(Ticket.status == 'closed')
            elif status_filter == 'active':
                q = q.filter(Ticket.status.in_(['open', 'answered']))
                
            q = q.order_by(desc(Ticket.updated_at))
            res = await db_session.execute(q)
            rows = res.all()
            out = []
            for t, u in rows:
                # آخرین پیام و تعداد پیام‌ها برای پیش‌نمایش در لیست.
                mres = await db_session.execute(
                    select(TicketMessage).filter(TicketMessage.ticket_id == t.id).order_by(desc(TicketMessage.created_at)).limit(1)
                )
                last_msg = mres.scalar_one_or_none()
                cnt = (await db_session.execute(
                    select(func.count(TicketMessage.id)).filter(TicketMessage.ticket_id == t.id)
                )).scalar() or 0
                out.append({
                    'ticket': to_dict(t),
                    'user': to_dict(u),
                    'last_message': to_dict(last_msg),
                    'message_count': cnt,
                })
            return out

    @staticmethod
    async def get_tickets_count(bot_id: int, status_filter: str = 'all'):
        async with AsyncSessionLocal() as db_session:
            q = select(func.count(Ticket.id)).filter(Ticket.bot_id == bot_id)
            if status_filter == 'open': q = q.filter(Ticket.status == 'open')
            elif status_filter == 'answered': q = q.filter(Ticket.status == 'answered')
            elif status_filter == 'closed': q = q.filter(Ticket.status == 'closed')
            elif status_filter == 'active': q = q.filter(Ticket.status.in_(['open', 'answered']))
            return (await db_session.execute(q)).scalar() or 0

    # ================= STANDARD METHODS =================
    @staticmethod
    async def init_default_gateways(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            try:
                res = await db_session.execute(select(PaymentGateway).filter(PaymentGateway.slug == 'zarinpal', PaymentGateway.bot_id == bot_id))
                if not res.scalar_one_or_none():
                    db_session.add(PaymentGateway(bot_id=bot_id, name="زرین‌پال", slug="zarinpal", is_active=False, config_json=json.dumps({"merchant_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"})))
            except: pass
            try:
                res = await db_session.execute(select(PaymentGateway).filter(PaymentGateway.slug == 'aqayepardakht', PaymentGateway.bot_id == bot_id))
                if not res.scalar_one_or_none():
                    db_session.add(PaymentGateway(bot_id=bot_id, name="آقای پرداخت", slug="aqayepardakht", is_active=False, config_json=json.dumps({"pin": "sandbox"})))
            except: pass
            await db_session.commit()

    @staticmethod
    async def reserve_main_bot_id():
        async with AsyncSessionLocal() as db_session:
            try:
                bot1 = await db_session.get(ResellerBot, 1)
                if not bot1:
                    dummy = ResellerBot(id=1, token="RESERVED_FOR_MAIN_BOT", owner_id=0, expiry_date=datetime.utcnow() + timedelta(days=36500), is_active=False, name="Main Bot Placeholder")
                    db_session.add(dummy)
                    await db_session.commit()
                    await db_session.execute(text("SELECT setval('reseller_bots_id_seq', (SELECT MAX(id) FROM reseller_bots));"))
                    await db_session.commit()
            except: pass

    @staticmethod
    async def get_user(telegram_id: int, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(User).filter(User.telegram_id == telegram_id, User.bot_id == bot_id))
            return to_dict(result.scalar_one_or_none())
    
    @staticmethod
    async def get_user_by_id(internal_id: int):
        async with AsyncSessionLocal() as db_session:
            u = await db_session.get(User, internal_id)
            return to_dict(u)

    @staticmethod
    async def get_all_admins(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(User).filter(User.is_admin == True, User.bot_id == bot_id))
            return [to_dict(u) for u in result.scalars().all()]

    @staticmethod
    async def get_setting(key: str, default: str = "", bot_id=1) -> str:
        async with AsyncSessionLocal() as db_session:
            try:
                res = await db_session.execute(select(BotSetting).filter(BotSetting.bot_id == bot_id, BotSetting.key == key))
                setting = res.scalar_one_or_none()
                return setting.value if setting else default
            except: return default

    @staticmethod
    async def global_maintenance_enabled_strict() -> bool:
        async with AsyncSessionLocal() as db_session:
            return await _global_maintenance_enabled(db_session)

    @staticmethod
    async def set_setting(key: str, value: str, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            # Serialize the global maintenance transition with every paid
            # purchase / due reservation, even when the setting row is absent.
            if key == 'maintenance_mode' and int(bot_id) == 1:
                await _lock_order_admission(db_session)
            res = await db_session.execute(select(BotSetting).filter(BotSetting.bot_id == bot_id, BotSetting.key == key))
            setting = res.scalar_one_or_none()
            if not setting:
                setting = BotSetting(bot_id=bot_id, key=key, value=value)
                db_session.add(setting)
            else: setting.value = value
            await db_session.commit()

    @staticmethod
    async def create_or_update_user(user_data: dict, bot_id=1):
        telegram_id = user_data.get('id')
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(User).filter(User.telegram_id == telegram_id, User.bot_id == bot_id))
            user = result.scalar_one_or_none()
            if not user:
                user = User(bot_id=bot_id, telegram_id=telegram_id, username=user_data.get('username'), first_name=user_data.get('first_name'), last_name=user_data.get('last_name'))
                db_session.add(user)
            else:
                user.username = user_data.get('username')
                user.first_name = user_data.get('first_name')
                user.last_name = user_data.get('last_name')
            await db_session.commit()
            await db_session.refresh(user)
            return to_dict(user)

    @staticmethod
    async def verify_user(telegram_id: int, phone: Optional[str], bot_id=1):
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(User).filter(User.telegram_id == telegram_id, User.bot_id == bot_id))
            user = result.scalar_one_or_none()
            if user:
                if phone: user.phone_number = phone
                user.is_verified = True
                await db_session.commit()
                
    @staticmethod
    async def update_user_kyc(internal_id: int, status: str, card: str = None, reason: str = None):
        async with AsyncSessionLocal() as db_session:
            u = await db_session.get(User, internal_id)
            if u:
                u.kyc_status = status
                if card: u.kyc_card_number = card
                if reason: u.kyc_reject_reason = reason
                await db_session.commit()
    
    @staticmethod
    async def update_user_ban_status(internal_id: int, is_banned: bool):
        async with AsyncSessionLocal() as db_session:
            u = await db_session.get(User, internal_id)
            if u:
                u.is_banned = is_banned
                await db_session.commit()

    @staticmethod
    async def update_user_exempt_phone(internal_id: int, exempt: bool):
        async with AsyncSessionLocal() as db_session:
            u = await db_session.get(User, internal_id)
            if u:
                u.exempt_phone_verify = exempt
                await db_session.commit()

    @staticmethod
    async def set_admin_status(telegram_id: int, is_admin: bool, role: str = "admin", bot_id=1):
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(User).filter(User.telegram_id == telegram_id, User.bot_id == bot_id))
            user = result.scalar_one_or_none()
            if user:
                user.is_admin = is_admin
                user.admin_role = role
                await db_session.commit()

    @staticmethod
    async def update_user_credit(internal_user_id: int, amount: float, type: str, desc: str, bot_id=1):
        # Online top-ups / admin adjustments must serialize with locked order
        # purchases and refunds. An unlocked ORM read-modify-write could read
        # the old balance and later overwrite a concurrent debit (lost update).
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                user = await db_session.get(
                    User, internal_user_id, with_for_update=True)
                if not user:
                    return False, 0
                user.credit = float(user.credit or 0) + float(amount)
                db_session.add(Transaction(
                    bot_id=bot_id, user_id=internal_user_id, amount=amount,
                    type=type, description=desc,
                ))
                new_balance = user.credit
            return True, new_balance

    @staticmethod
    async def get_user_stats_full(user_id: int) -> Dict[str, Any]:
        async with AsyncSessionLocal() as db_session:
            total_paid = abs((await db_session.execute(select(func.sum(Transaction.amount)).filter(Transaction.user_id == user_id, Transaction.amount < 0))).scalar() or 0)
            orders_count = (await db_session.execute(select(func.count(Order.id)).filter(Order.user_id == user_id))).scalar() or 0
            total_dep = (await db_session.execute(select(func.sum(Transaction.amount)).filter(Transaction.user_id == user_id, Transaction.amount > 0, Transaction.type.in_(['online_charge', 'admin'])))).scalar() or 0
            return {"total_paid": total_paid, "total_deposited": total_dep, "orders_count": orders_count}

    @staticmethod
    async def get_user_transactions(uid, limit=10, offset=0):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(Transaction).filter(Transaction.user_id == uid).order_by(desc(Transaction.created_at)).limit(limit).offset(offset))
            return [to_dict(t) for t in res.scalars().all()]

    @staticmethod
    async def get_user_transactions_count(uid):
        async with AsyncSessionLocal() as db_session:
            return (await db_session.execute(select(func.count(Transaction.id)).filter(Transaction.user_id == uid))).scalar() or 0

    @staticmethod
    async def create_plan(name, desc, s_type, count, dur, price, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            plan = Plan(bot_id=bot_id, name=name, description=desc, service_type=s_type, accounts_count=count, duration_minutes=dur, price=price)
            db_session.add(plan)
            await db_session.commit()
    
    @staticmethod
    async def get_plans(service_type=None, active_only=True, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            q = select(Plan).filter(Plan.bot_id == bot_id)
            if service_type: q = q.filter(Plan.service_type == service_type)
            if active_only: q = q.filter(Plan.is_active == True)
            res = await db_session.execute(q)
            return [to_dict(p) for p in res.scalars().all()]

    @staticmethod
    async def get_plan_by_id(pid):
        async with AsyncSessionLocal() as db_session:
            p = await db_session.get(Plan, pid)
            return to_dict(p)

    @staticmethod
    async def update_plan(plan_id: int, **kwargs):
        async with AsyncSessionLocal() as db_session:
            plan = await db_session.get(Plan, plan_id)
            if plan:
                for key, value in kwargs.items():
                    if hasattr(plan, key): setattr(plan, key, value)
                await db_session.commit()
                return True
            return False

    @staticmethod
    async def delete_plan(pid):
        async with AsyncSessionLocal() as db_session:
            p = await db_session.get(Plan, pid)
            if p: 
                await db_session.delete(p) 
                await db_session.commit()
                return True
            return False

    @staticmethod
    async def create_paid_order(user_id: int, plan_id: int, target_link: str, *,
                                bot_id: int = 1, scheduled_for=None,
                                expected_plan: Optional[Dict[str, Any]] = None):
        """Buy exactly the shown plan or change NOTHING (wallet/order/ledger).

        The user's row is locked through the whole transaction. Two callback
        deliveries or concurrent purchases cannot both spend the same balance,
        and an insert failure rolls back the debit. A plan edited after the
        confirmation screen is rejected rather than silently re-priced.
        Returns (order dict or None, reason code). Capacity is preflighted by
        the handler and is NOT guaranteed by this transaction (no MTProto IO).
        """
        if expected_plan is None:
            raise ValueError('the confirmed plan snapshot is required')
        from services.link_validator import invite_hash, normalize_invite_link, target_key
        target_link = (normalize_invite_link(target_link) if invite_hash(target_link)
                       else str(target_link or '').strip())
        if not target_link:
            return None, 'invalid_link'
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Share ONE transaction lock with the global maintenance
                # toggle, including scheduled/unlimited-capacity purchases.
                # A purchase that read OFF must finish before the toggle can
                # commit ON; the deploy preflight will then see its order.
                await _lock_order_admission(db_session)
                if await _global_maintenance_enabled(db_session):
                    return None, 'maintenance'
                limit = max(0, int(getattr(Config, 'MAX_CONCURRENT_ORDERS', 10)))
                if limit and scheduled_for is None:
                    if await _occupied_order_slots(db_session) >= limit:
                        return None, 'order_capacity'
                # Shared row lock keeps admin price/activation edits from
                # racing the wallet debit. FOR SHARE permits parallel buyers;
                # the per-user FOR UPDATE below serializes their purchases.
                plan = await db_session.get(
                    Plan, int(plan_id), with_for_update={'read': True})
                if not plan or plan.bot_id != int(bot_id) or not plan.is_active:
                    return None, 'plan_unavailable'
                fields = ('service_type', 'accounts_count', 'duration_minutes', 'price')
                if any(getattr(plan, field) != expected_plan.get(field) for field in fields):
                    return None, 'plan_changed'
                price = float(plan.price)
                if not math.isfinite(price) or price < 0 or int(plan.accounts_count) < 1:
                    return None, 'plan_unavailable'
                row = await db_session.execute(
                    select(User).where(User.id == int(user_id),
                                       User.bot_id == int(bot_id)).with_for_update()
                )
                user = row.scalar_one_or_none()
                if not user:
                    return None, 'user_missing'
                if float(user.credit or 0) < price:
                    return None, 'insufficient_credit'
                # Same-user wallet lock serializes simultaneous callbacks.
                # A duplicate click must not buy/charge the same open plan
                # twice even if the wallet can afford both. Different links,
                # plans and completed/failed orders remain independent.
                duplicate = await db_session.execute(
                    select(Order.target_link).where(
                        Order.bot_id == int(bot_id), Order.user_id == user.id,
                        Order.plan_id == plan.id,
                        Order.scheduled_for == scheduled_for,
                        Order.status.in_(('pending', 'running', 'scheduled')),
                        Order.created_at >= datetime.utcnow() - timedelta(minutes=2),
                    )
                )
                if any(target_key(old) == target_key(target_link) for old in duplicate.scalars()):
                    return None, 'duplicate_purchase'
                user.credit = float(user.credit or 0) - price
                order = Order(
                    bot_id=int(bot_id), user_id=user.id, plan_id=plan.id,
                    order_type=plan.service_type, target_link=target_link,
                    accounts_count=plan.accounts_count,
                    duration_minutes=plan.duration_minutes, price_paid=price,
                    status='scheduled' if scheduled_for else 'pending',
                    scheduled_for=scheduled_for,
                )
                db_session.add(order)
                await db_session.flush()  # order id for the immutable ledger
                db_session.add(Transaction(
                    bot_id=int(bot_id), user_id=user.id, amount=-price,
                    type='order', description=f'خرید {plan.name} | سفارش {order.id}',
                ))
            return to_dict(order), 'created'

    @staticmethod
    async def create_order(user_id, order_type, target_link, accounts_count, duration_minutes, price_paid=0, plan_id=None, scheduled_for=None, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            status = "scheduled" if scheduled_for else "pending"
            order = Order(
                bot_id=bot_id, user_id=user_id, order_type=order_type, 
                target_link=target_link, accounts_count=accounts_count, 
                duration_minutes=duration_minutes, price_paid=price_paid, 
                plan_id=plan_id, status=status, scheduled_for=scheduled_for
            )
            db_session.add(order)
            await db_session.commit()
            await db_session.refresh(order)
            return to_dict(order)

    @staticmethod
    async def get_order(order_id: int):
        async with AsyncSessionLocal() as db_session:
            order = await db_session.get(Order, order_id)
            return to_dict(order)

    @staticmethod
    async def get_user_running_voice_orders(user_id: int, bot_id: int = 1):
        """سفارش‌های ویس‌کالِ در حال اجرای یک کاربر (برای مرکز پیام درون‌تماس)."""
        async with AsyncSessionLocal() as db_session:
            q = (
                select(Order)
                .filter(
                    Order.user_id == user_id,
                    Order.bot_id == bot_id,
                    Order.status == 'running',
                    Order.order_type.ilike('%voice%'),
                )
                .order_by(desc(Order.created_at))
            )
            res = await db_session.execute(q)
            return [to_dict(o) for o in res.scalars().all()]

    @staticmethod
    async def get_orders_history(user_id=None, limit=20, offset=0):
        async with AsyncSessionLocal() as db_session:
            q = select(Order).order_by(desc(Order.created_at)).limit(limit).offset(offset)
            if user_id: q = q.filter(Order.user_id == user_id)
            res = await db_session.execute(q)
            return [to_dict(o) for o in res.scalars().all()]

    @staticmethod
    async def get_all_orders_extended(limit=20, offset=0, status_filter=None, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            q = select(Order, User).join(User, Order.user_id == User.id).filter(Order.bot_id == bot_id)
            if status_filter and status_filter != 'all':
                if status_filter == 'active': q = q.filter(Order.status.in_(['running', 'pending']))
                elif status_filter == 'completed': q = q.filter(Order.status == 'completed')
                elif status_filter == 'scheduled': q = q.filter(Order.status == 'scheduled')
                elif status_filter == 'cancelled': q = q.filter(Order.status.in_(['stopped', 'failed']))
            q = q.order_by(desc(Order.created_at)).limit(limit).offset(offset)
            res = await db_session.execute(q)
            return [{'order': to_dict(o), 'user': to_dict(u)} for o, u in res.all()]

    @staticmethod
    async def get_all_orders_count(status_filter=None, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            q = select(func.count(Order.id)).filter(Order.bot_id == bot_id)
            if status_filter and status_filter != 'all':
                if status_filter == 'active': q = q.filter(Order.status.in_(['running', 'pending']))
                elif status_filter == 'completed': q = q.filter(Order.status == 'completed')
                elif status_filter == 'scheduled': q = q.filter(Order.status == 'scheduled')
                elif status_filter == 'cancelled': q = q.filter(Order.status.in_(['stopped', 'failed']))
            res = await db_session.execute(q)
            return res.scalar() or 0

    @staticmethod
    async def get_orders_count(user_id=None):
        async with AsyncSessionLocal() as db_session:
            q = select(func.count(Order.id))
            if user_id: q = q.filter(Order.user_id == user_id)
            res = await db_session.execute(q)
            return res.scalar() or 0

    @staticmethod
    async def update_order_status(order_id, status):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(Order).where(Order.id == order_id).values(status=status))
            await db_session.commit()

    @staticmethod
    async def settle_cancel_order(order_id: int, *, bot_id: int, do_refund: bool,
                                  settlement_calculator, expected_user_id=None,
                                  final_status: str = 'stopped'):
        """Claim a paid order and settle its wallet exactly once in one txn.

        Lock ORDER before USER (same ordering for concurrent callbacks). Never
        credit before the status claim: the old handler credited the wallet
        first, so repeated callbacks could each refund the full purchase.
        Return None for already claimed/finished orders; do not mutate them.
        """
        if final_status not in ('stopped', 'failed'):
            raise ValueError('unsupported settlement status')
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                order = await db_session.get(Order, int(order_id), with_for_update=True)
                if (not order or order.bot_id != int(bot_id)
                        or (expected_user_id is not None
                            and order.user_id != int(expected_user_id))
                        or order.status not in ('running', 'scheduled', 'pending')):
                    return None
                user = await db_session.get(User, order.user_id, with_for_update=True)
                if not user or user.bot_id != int(bot_id):
                    raise ValueError('order owner is missing or belongs to another bot')
                original = to_dict(order)
                total_price = float(order.price_paid or 0)
                if not math.isfinite(total_price) or total_price < 0:
                    raise ValueError('invalid order price')
                used_cost, refund_amount, _elapsed = settlement_calculator(original)
                if not do_refund:
                    used_cost, refund_amount = total_price, 0.0
                if (not math.isfinite(float(refund_amount)) or refund_amount < 0
                        or refund_amount > total_price):
                    raise ValueError('invalid settlement amount')
                refund_tx_id = f'TX-{order.id}' if refund_amount else None
                order.status = final_status
                if refund_amount:
                    user.credit = float(user.credit or 0) + float(refund_amount)
                    db_session.add(Transaction(
                        bot_id=int(bot_id), user_id=user.id, amount=float(refund_amount),
                        type='order_refund',
                        description=f'عودت لغو سفارش {order.id} | {refund_tx_id}',
                    ))
                result = {
                    'order': original, 'total_cost': total_price,
                    'used_cost': used_cost, 'refund_amount': float(refund_amount),
                    'refund_tx_id': refund_tx_id,
                    'user_wallet_balance': float(user.credit or 0),
                }
            return result

    @staticmethod
    async def cancel_order_once(order_id: int) -> bool:
        """Atomically claim a running/scheduled order for cancellation."""
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                update(Order)
                .where(Order.id == order_id, Order.status.in_(['running', 'scheduled']))
                .values(status='stopped')
            )
            await db_session.commit()
            return bool(result.rowcount)

    @staticmethod
    async def start_order_duration(order_id: int) -> Optional[datetime]:
        """Persist the billable start ONCE, only while the order is running.

        A duplicate worker must reuse the old start (never grant an extra hour
        or erase elapsed time). A cancelled order must not become billable in
        the gap between build completion and the DB update. None means it is
        not running; DB failures raise, so callers never use a local clock as
        a fake persisted start.
        """
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                now = datetime.utcnow()
                result = await db_session.execute(
                    update(Order)
                    .where(Order.id == order_id, Order.status == 'running',
                           Order.started_at.is_(None))
                    .values(started_at=now)
                    .returning(Order.started_at)
                )
                started_at = result.scalar_one_or_none()
                if started_at is None:
                    order = await db_session.get(Order, order_id)
                    if order and order.status == 'running':
                        started_at = order.started_at
            return started_at
    
    @staticmethod
    async def mark_order_as_running(order_id: int, *, expected_status: str) -> bool:
        """Claim an open order without resurrecting a cancelled/failed one.

        A scheduled-order poll can race a customer's refund. Only the expected
        pending/scheduled state can transition to running; otherwise do not
        spawn a worker or start a paid timer. The timer remains NULL throughout
        build and is stamped separately by start_order_duration.
        """
        if expected_status not in ('pending', 'scheduled'):
            raise ValueError('only a paid open order can start')
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                if expected_status == 'scheduled':
                    await _lock_order_admission(db_session)
                    if await _global_maintenance_enabled(db_session):
                        return False
                # A due reservation is paid but does not occupy a worker slot
                # until this transition. Leave it scheduled when busy; the
                # scheduler will retry without changing its paid duration.
                limit = max(0, int(getattr(Config, 'MAX_CONCURRENT_ORDERS', 10)))
                if expected_status == 'scheduled' and limit:
                    if await _occupied_order_slots(db_session) >= limit:
                        return False
                # Pending orders already reserved a slot at checkout and
                # MUST NOT be stranded by a later config change.
                result = await db_session.execute(
                    update(Order)
                    .where(Order.id == order_id, Order.status == expected_status)
                    .values(status='running')
                )
                return bool(result.rowcount)

    @staticmethod
    async def complete_order(order_id: int):
        async with AsyncSessionLocal() as db_session:
            now = datetime.utcnow()
            await db_session.execute(update(Order).where(Order.id == order_id).values(status='completed', completed_at=now))
            await db_session.commit()

    @staticmethod
    async def get_due_scheduled_orders():
        async with AsyncSessionLocal() as db_session:
            now = datetime.utcnow()
            q = select(Order).filter(Order.status == 'scheduled', Order.scheduled_for <= now)
            res = await db_session.execute(q)
            return [to_dict(o) for o in res.scalars().all()]

    @staticmethod
    async def has_active_order_for_link(link: str, bot_id: int = 1) -> bool:
        """بررسی وجود سفارش فعال برای یک لینک خاص (جهت خروج هوشمند)"""
        async with AsyncSessionLocal() as db_session:
            # بررسی سفارشات با وضعیت running یا scheduled برای این لینک
            from services.link_validator import target_key
            q = select(Order.target_link).filter(
                Order.bot_id == bot_id,
                Order.status.in_(['running', 'scheduled'])
            )
            res = await db_session.execute(q)
            key = target_key(link)
            return any(target_key(other) == key for other in res.scalars())

    @staticmethod
    async def has_time_overlap_order(link: str, new_start_time: datetime, new_duration_minutes: int, bot_id: int = 1) -> bool:
        """بررسی تداخل زمانی سفارش جدید با سفارشات موجود برای یک لینک"""
        async with AsyncSessionLocal() as db_session:
            # محاسبه زمان پایان سفارش جدید
            new_end_time = new_start_time + timedelta(minutes=new_duration_minutes)
            
            # Include legacy spellings of the SAME invitation in overlap checks.
            from services.link_validator import target_key
            key = target_key(link)
            q = select(Order).filter(
                Order.bot_id == bot_id,
                Order.status.in_(['running', 'scheduled'])
            )
            res = await db_session.execute(q)
            existing_orders = res.scalars().all()
            
            for order in existing_orders:
                if target_key(order.target_link) != key:
                    continue
                # تعیین زمان شروع سفارش موجود
                existing_start = order.started_at if order.started_at else (order.scheduled_for if order.scheduled_for else order.created_at)
                if not existing_start:
                    continue
                
                # محاسبه زمان پایان سفارش موجود
                existing_duration = order.duration_minutes or 0
                existing_end = existing_start + timedelta(minutes=existing_duration)
                
                # بررسی تداخل: اگر زمان شروع یا پایان جدید در بازه موجود باشد، تداخل وجود دارد
                if (new_start_time < existing_end and new_end_time > existing_start):
                    return True
            
            return False

    @staticmethod
    async def add_telegram_account(user_id, phone, session_str, bot_id=1, api_id=None, api_hash=None,
                                   first_name=None, last_name=None, username=None):
        async with AsyncSessionLocal() as db_session:
            try:
                existing = await db_session.execute(select(TelegramAccount).filter(TelegramAccount.phone_number == phone, TelegramAccount.bot_id == bot_id))
                acc = existing.scalar_one_or_none()
                status = "created"
                if acc:
                    acc.session_string = session_str
                    acc.account_status = 'active'
                    # A verified phone login/import replaces the prior key.
                    # Never leave an old 'dead' flag on the new active session:
                    # the dead-account menu also looks at spam_status.
                    acc.spam_status = 'unknown'
                    acc.spam_check_result = None
                    acc.last_health_check = None
                    if api_id: acc.api_id = api_id
                    if api_hash: acc.api_hash = api_hash
                    if first_name is not None: acc.first_name = first_name
                    if last_name is not None: acc.last_name = last_name
                    if username is not None: acc.username = username
                    status = "updated"
                else:
                    acc = TelegramAccount(
                        bot_id=bot_id, user_id=user_id, phone_number=phone, 
                        session_string=session_str, is_verified=True, 
                        account_status='active', api_id=api_id, api_hash=api_hash,
                        first_name=first_name, last_name=last_name, username=username
                    )
                    db_session.add(acc)
                await db_session.commit()
                return True, status
            except Exception as e:
                logger.error(f"❌ Add account db error: {e}")
                return False, "error"

    @staticmethod
    async def update_account_profile_cache(aid, first_name=None, last_name=None, username=None):
        """بروزرسانی اطلاعات کش‌شدهٔ پروفایل یک اکانت (فقط فیلدهای ارسال‌شده)."""
        async with AsyncSessionLocal() as db_session:
            try:
                acc = await db_session.get(TelegramAccount, aid)
                if not acc:
                    return False
                if first_name is not None: acc.first_name = first_name
                if last_name is not None: acc.last_name = last_name
                if username is not None: acc.username = username
                await db_session.commit()
                return True
            except Exception as e:
                logger.error(f"❌ update_account_profile_cache error: {e}")
                return False

    @staticmethod
    async def get_accounts_paginated(limit=10, offset=0, active_only=False, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            q = select(TelegramAccount).filter(TelegramAccount.bot_id == bot_id)
            if active_only: q = q.filter(TelegramAccount.account_status == 'active')
            q = q.order_by(desc(TelegramAccount.id)).limit(limit).offset(offset)
            res = await db_session.execute(q)
            accounts = [to_dict(a) for a in res.scalars().all()]
            q_count = select(func.count(TelegramAccount.id)).filter(TelegramAccount.bot_id == bot_id)
            if active_only: q_count = q_count.filter(TelegramAccount.account_status == 'active')
            total = (await db_session.execute(q_count)).scalar() or 0
            return accounts, total

    @staticmethod
    async def get_total_accounts_count(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            return (await db_session.execute(select(func.count(TelegramAccount.id)).filter(TelegramAccount.bot_id == bot_id))).scalar() or 0

    @staticmethod
    async def get_all_active_accounts(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                select(TelegramAccount).filter(
                    func.lower(func.trim(TelegramAccount.account_status)) == 'active',
                    TelegramAccount.bot_id == bot_id,
                )
            )
            accounts = [to_dict(a) for a in res.scalars().all()]
            if accounts:
                return accounts

            # Fallback for non-standard storage/encoding of active status values
            res2 = await db_session.execute(
                select(TelegramAccount).filter(
                    TelegramAccount.account_status.ilike('active'),
                    TelegramAccount.bot_id == bot_id,
                )
            )
            return [to_dict(a) for a in res2.scalars().all()]

    @staticmethod
    async def count_active_accounts(bot_id=1) -> int:
        """
        Efficient COUNT of eligible (active) accounts for an order.
        Used to compute target_count = min(requested, eligible) WITHOUT
        loading the whole account table into RAM.
        """
        async with AsyncSessionLocal() as db_session:
            try:
                q = select(func.count(TelegramAccount.id)).filter(
                    func.lower(func.trim(TelegramAccount.account_status)) == 'active',
                    TelegramAccount.bot_id == bot_id,
                    or_(TelegramAccount.spam_check_result.is_(None),
                        ~TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%')),
                )
                cnt = (await db_session.execute(q)).scalar() or 0
                if cnt > 0:
                    return int(cnt)
            except Exception:
                pass
            # Fallback for non-standard storage/encoding of active status values
            try:
                q2 = select(func.count(TelegramAccount.id)).filter(
                    TelegramAccount.account_status.ilike('active'),
                    TelegramAccount.bot_id == bot_id,
                    or_(TelegramAccount.spam_check_result.is_(None),
                        ~TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%')),
                )
                return int((await db_session.execute(q2)).scalar() or 0)
            except Exception:
                return 0

    @staticmethod
    async def get_active_accounts_batch(bot_id=1, offset=0, limit=20):
        """
        Fetch a small page of eligible (active) accounts, ordered by id.
        Used for progressive / batched account allocation so a large order
        never loads thousands of full account records into memory at once.
        """
        async with AsyncSessionLocal() as db_session:
            try:
                query = select(TelegramAccount).where(
                    func.lower(func.trim(TelegramAccount.account_status)) == 'active',
                    TelegramAccount.bot_id == bot_id,
                    or_(TelegramAccount.spam_check_result.is_(None),
                        ~TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%')),
                ).order_by(TelegramAccount.id).offset(max(0, offset)).limit(max(1, limit))
                res = await db_session.execute(query)
                accs = [to_dict(a) for a in res.scalars().all()]
                if accs:
                    return accs
            except Exception:
                pass
            # Fallback query
            try:
                query2 = select(TelegramAccount).where(
                    TelegramAccount.account_status.ilike('active'),
                    TelegramAccount.bot_id == bot_id,
                    or_(TelegramAccount.spam_check_result.is_(None),
                        ~TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%')),
                ).order_by(TelegramAccount.id).offset(max(0, offset)).limit(max(1, limit))
                res2 = await db_session.execute(query2)
                return [to_dict(a) for a in res2.scalars().all()]
            except Exception:
                return []

    @staticmethod
    async def get_active_accounts_for_order(limit, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            query = select(TelegramAccount).where(
                func.lower(func.trim(TelegramAccount.account_status)) == 'active',
                TelegramAccount.bot_id == bot_id,
                or_(TelegramAccount.spam_check_result.is_(None),
                    ~TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%')),
            ).limit(limit)
            res = await db_session.execute(query)
            return [to_dict(a) for a in res.scalars().all()]

    @staticmethod
    async def get_account_by_id(aid):
        async with AsyncSessionLocal() as db_session:
            return to_dict(await db_session.get(TelegramAccount, aid))

    @staticmethod
    async def delete_account(aid, uid):
        async with AsyncSessionLocal() as db_session:
            a = await db_session.get(TelegramAccount, aid)
            if a:
                await db_session.delete(a)
                await db_session.commit()
                return True
            return False

    @staticmethod
    async def update_account_status(aid, status):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(TelegramAccount).where(TelegramAccount.id == aid).values(account_status=status))
            await db_session.commit()

    @staticmethod
    async def update_account_spam_status(aid, status, result_text):
        async with AsyncSessionLocal() as db_session:
            # A SpamBot check cannot silently clear an unresolved 406 hold.
            await db_session.execute(update(TelegramAccount).where(
                TelegramAccount.id == aid,
                or_(TelegramAccount.spam_check_result.is_(None),
                    ~TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%')),
            ).values(spam_status=status, spam_check_result=result_text,
                     last_health_check=datetime.utcnow()))
            await db_session.commit()

    @staticmethod
    async def note_session_conflict_if_current(aid: int, encrypted_session: str) -> bool:
        """Store 406 only on the matching active key; never disable it."""
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                update(TelegramAccount).where(
                    TelegramAccount.id == int(aid),
                    TelegramAccount.account_status == 'active',
                    TelegramAccount.session_string == encrypted_session,
                ).values(
                    spam_status='cooldown',
                    spam_check_result='AUTH_KEY_DUPLICATED: check shared key; validity unknown',
                    last_health_check=datetime.utcnow(),
                )
            )
            await db_session.commit()
            return result.rowcount == 1

    @staticmethod
    async def resolve_session_conflict_after_verified_probe(
        aid: int, bot_id: int, encrypted_session: str, conflict_at: datetime,
        *, account_deleted: bool = False,
    ) -> bool:
        """Resolve only a matching 406 marker after typed Telegram evidence.

        The in-bot, ownership-guarded probe must have finished get_me() and
        confirmed disconnect. The timestamp also prevents a newer 406 on the
        SAME key from being silently cleared by an older in-flight probe.
        An explicit USER_DEACTIVATED may mark the exact account as deleted.
        """
        if conflict_at is None:
            return False
        values = {
            'last_health_check': datetime.utcnow(),
            'spam_status': 'dead' if account_deleted else 'unknown',
            'spam_check_result': (CONFIRMED_ACCOUNT_DELETED if account_deleted else
                                  'Session verified and disconnected; 406 hold cleared'),
        }
        if account_deleted:
            values['account_status'] = 'inactive'
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(update(TelegramAccount).where(
                TelegramAccount.id == int(aid),
                TelegramAccount.bot_id == int(bot_id),
                TelegramAccount.account_status == 'active',
                TelegramAccount.spam_status == 'cooldown',
                TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%'),
                TelegramAccount.session_string == encrypted_session,
                TelegramAccount.last_health_check == conflict_at,
            ).values(**values))
            await db_session.commit()
            return result.rowcount == 1

    @staticmethod
    async def mark_account_auth_invalid(aid: int, encrypted_session: str, category: str) -> bool:
        """Atomically disable ONLY the auth key that produced a fatal RPC.

        A stale voice task must not disable a fresh login saved to the same
        account row meanwhile. Never store raw error text/session material.
        """
        allowed = {'SESSION_REVOKED', 'AUTH_KEY_UNREGISTERED', 'AUTH_KEY_INVALID',
                   'USER_DEACTIVATED', 'USER_DEACTIVATED_BAN', 'RPC_401'}
        if category not in allowed:
            raise ValueError('Unverified auth failure category')
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                update(TelegramAccount).where(
                    TelegramAccount.id == int(aid),
                    TelegramAccount.account_status == 'active',
                    TelegramAccount.session_string == encrypted_session,
                ).values(
                    account_status='inactive', spam_status='dead',
                    spam_check_result='Explicit auth failure: ' + category,
                    last_health_check=datetime.utcnow(),
                )
            )
            await db_session.commit()
            if result.rowcount == 1:
                logger.warning('Account %s disabled: explicit auth failure %s', aid, category)
            return result.rowcount == 1

    @staticmethod
    async def mark_account_deleted_after_verified_probe(aid: int, bot_id: int,
                                                       encrypted_session: str) -> bool:
        """Mark a deleted Telegram *account*, not an expired authorization key.

        Caller must have received a typed USER_DEACTIVATED RPC from its own
        guarded single-account probe AND confirmed disconnect. A replaced or
        reactivated session must never inherit the deletion proof.
        """
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                update(TelegramAccount).where(
                    TelegramAccount.id == int(aid),
                    TelegramAccount.bot_id == int(bot_id),
                    TelegramAccount.account_status == 'inactive',
                    TelegramAccount.session_string == encrypted_session,
                ).values(
                    spam_status='dead',
                    spam_check_result=CONFIRMED_ACCOUNT_DELETED,
                    last_health_check=datetime.utcnow(),
                )
            )
            await db_session.commit()
            return result.rowcount == 1

    @staticmethod
    async def get_confirmed_deleted_accounts(bot_id: int, *,
                                              account_id: int | None = None) -> list[dict]:
        """Return only fresh, explicit account-deletion evidence for this bot.

        Optionally select the exact account that was just reviewed. Session
        ciphertext stays server-side; callers must not send it to Telegram or
        store it in callback data/user_data.
        """
        async with AsyncSessionLocal() as db_session:
            query = select(TelegramAccount).where(
                TelegramAccount.bot_id == int(bot_id),
                TelegramAccount.account_status == 'inactive',
                TelegramAccount.spam_status == 'dead',
                TelegramAccount.spam_check_result == CONFIRMED_ACCOUNT_DELETED,
            )
            if account_id is not None:
                query = query.where(TelegramAccount.id == int(account_id))
            result = await db_session.execute(query.order_by(TelegramAccount.id))
            return [{'id': account.id, 'session_string': account.session_string}
                    for account in result.scalars().all()]

    @staticmethod
    async def get_deletion_review_page(bot_id: int, *, page: int = 1,
                                       page_size: int = 8) -> tuple[list[dict], int]:
        """Paginate uncertain accounts without loading/decrypting session strings.

        A legacy inactive marker or an active 406 hold is a *candidate for
        optional single-account review*, never proof of account deletion.
        Confirmed-deleted rows are shown only in the separate deletion preview.
        """
        size = min(8, max(1, int(page_size)))
        offset = (max(1, int(page)) - 1) * size
        uncertain = or_(
            (TelegramAccount.account_status == 'inactive') & or_(
                TelegramAccount.spam_check_result.is_(None),
                TelegramAccount.spam_check_result != CONFIRMED_ACCOUNT_DELETED,
            ),
            (TelegramAccount.account_status == 'active') &
            (TelegramAccount.spam_status == 'cooldown') &
            TelegramAccount.spam_check_result.like('AUTH_KEY_DUPLICATED:%'),
        )
        condition = (TelegramAccount.bot_id == int(bot_id)) & uncertain
        async with AsyncSessionLocal() as db_session:
            total = int((await db_session.execute(
                select(func.count(TelegramAccount.id)).where(condition)
            )).scalar() or 0)
            result = await db_session.execute(select(
                TelegramAccount.id, TelegramAccount.phone_number,
                TelegramAccount.account_status, TelegramAccount.spam_status,
            ).where(condition).order_by(TelegramAccount.id)
             .offset(offset).limit(size))
            return [dict(row) for row in result.mappings().all()], total

    @staticmethod
    async def deletion_review_probe_allowed(bot_id: int) -> tuple[bool, str]:
        """No live probe until global maintenance and a quiet order window.

        This is a preflight only, not permission to bulk probe or to delete.
        SessionOwnership in the running bot separately rejects shared/busy keys.
        """
        async with AsyncSessionLocal() as db_session:
            if not await _global_maintenance_enabled(db_session):
                return False, 'maintenance'
            deadline = datetime.utcnow() + timedelta(minutes=30)
            busy = (await db_session.execute(select(Order.id).where(
                Order.bot_id == int(bot_id),
                or_(Order.status.in_(('running', 'pending')),
                    (Order.status == 'scheduled') & or_(
                        Order.scheduled_for.is_(None),
                        Order.scheduled_for <= deadline)),
            ).limit(1))).first()
            return (False, 'busy') if busy else (True, 'ready')

    @staticmethod
    async def delete_confirmed_deleted_accounts(bot_id: int,
                                                expected_fingerprints: dict[int, str], *,
                                                single_account_id: int | None = None) -> tuple[int, str]:
        """All-or-nothing cleanup of the exact accounts shown to a superadmin.

        This DB operation is deliberately narrower than delete_account(): it
        locks/rechecks the bot, marker, status AND ciphertext fingerprint. No
        historical dead/revoked/406 row qualifies, even if it was in a stale
        preview. The handler separately enforces role, nonce and expiry.
        """
        if not expected_fingerprints:
            return 0, 'empty'
        if single_account_id is not None and set(expected_fingerprints) != {int(single_account_id)}:
            return 0, 'changed'
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Main-bot maintenance is the global switch for every reseller
                # bot too. Require it ON and no running/pending or imminent work.
                setting = (await db_session.execute(select(BotSetting).where(
                    BotSetting.bot_id == 1,
                    BotSetting.key == 'maintenance_mode',
                ).with_for_update())).scalar_one_or_none()
                if setting is None or setting.value != '1':
                    return 0, 'maintenance'
                deadline = datetime.utcnow() + timedelta(minutes=30)
                busy = (await db_session.execute(select(Order.id).where(
                    Order.bot_id == int(bot_id),
                    or_(Order.status.in_(('running', 'pending')),
                        (Order.status == 'scheduled') & or_(
                            Order.scheduled_for.is_(None),
                            Order.scheduled_for <= deadline)),
                ).limit(1))).first()
                if busy:
                    return 0, 'busy'

                # Bulk preview must cover ALL eligible rows; after a single
                # account review, lock/delete ONLY that exact confirmed row.
                query = select(TelegramAccount).where(
                    TelegramAccount.bot_id == int(bot_id),
                    TelegramAccount.account_status == 'inactive',
                    TelegramAccount.spam_status == 'dead',
                    TelegramAccount.spam_check_result == CONFIRMED_ACCOUNT_DELETED,
                )
                if single_account_id is not None:
                    query = query.where(TelegramAccount.id == int(single_account_id))
                result = await db_session.execute(query.order_by(TelegramAccount.id).with_for_update())
                rows = result.scalars().all()
                if len(rows) != len(expected_fingerprints):
                    return 0, 'changed'
                for account in rows:
                    fingerprint = hashlib.sha256(account.session_string.encode('utf-8')).hexdigest()
                    expected = expected_fingerprints.get(account.id, '')
                    if not hmac.compare_digest(fingerprint, expected):
                        return 0, 'changed'
                for account in rows:
                    await db_session.delete(account)
            return len(rows), 'deleted'

    @staticmethod
    async def recover_account_after_verified_probe(aid: int, bot_id: int, encrypted_session: str) -> bool:
        """Reactivate ONLY the exact inactive row whose key was just verified.

        A parallel login/import may replace the session while a network probe
        is in flight. The conditional UPDATE prevents that old probe from
        blessing a different, untested key. Caller must confirm both get_me()
        and MTProto disconnect before invoking this method.
        """
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                update(TelegramAccount).where(
                    TelegramAccount.id == int(aid),
                    TelegramAccount.bot_id == int(bot_id),
                    TelegramAccount.account_status == 'inactive',
                    TelegramAccount.session_string == encrypted_session,
                ).values(
                    account_status='active',
                    # A dead marker from an old auth error is no longer valid,
                    # but get_me() does NOT prove the account is spam-free.
                    spam_status=case(
                        (TelegramAccount.spam_status == 'dead', 'unknown'),
                        else_=TelegramAccount.spam_status,
                    ),
                    spam_check_result=case(
                        (TelegramAccount.spam_status == 'dead', func.concat(
                            'Session verified and disconnected; SpamBot not checked. Previous flag: ',
                            func.coalesce(TelegramAccount.spam_check_result, 'unknown'),
                        )),
                        else_=TelegramAccount.spam_check_result,
                    ),
                    last_health_check=datetime.utcnow(),
                )
            )
            await db_session.commit()
            return result.rowcount == 1

    @staticmethod
    async def create_voice_call_session(order_id, account_id, chat_id, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            s = VoiceCallSession(bot_id=bot_id, order_id=order_id, account_id=account_id, chat_id=chat_id)
            db_session.add(s)
            await db_session.commit()

    @staticmethod
    async def update_voice_call_session(account_id, chat_id, status, *, order_id=None):
        """Change only this order's ledger row, not another shared binding."""
        if order_id is None:
            raise ValueError('order_id is required to avoid touching other orders')
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(
                update(VoiceCallSession)
                .where(VoiceCallSession.order_id == order_id,
                       VoiceCallSession.account_id == account_id,
                       VoiceCallSession.chat_id == chat_id)
                .values(status=status)
            )
            await db_session.commit()

    @staticmethod
    async def insert_join_attempt(record: dict):
        """Persist a single join attempt (append-only, never overwritten)."""
        async with AsyncSessionLocal() as db_session:
            # Idempotency guard via unique attempt_id.
            existing = await db_session.execute(
                select(VoiceJoinAttempt).filter(VoiceJoinAttempt.attempt_id == record.get("attempt_id"))
            )
            if existing.scalar_one_or_none():
                return
            started = record.get("started_at")
            finished = record.get("finished_at")
            rec = VoiceJoinAttempt(
                attempt_id=record.get("attempt_id"),
                order_id=record.get("order_id"),
                account_id=record.get("account_id"),
                voice_chat_id=record.get("voice_chat_id"),
                attempt_number=record.get("attempt_number", 1),
                started_at=datetime.utcfromtimestamp(started) if started else None,
finished_at=datetime.utcfromtimestamp(finished) if finished else None,
                duration_ms=record.get("duration_ms"),
                stage=record.get("stage"),
                result=record.get("result", "PENDING"),
                error_type=record.get("error_type"),
                error_message=record.get("error_message"),
                telegram_error=record.get("telegram_error"),
                telegram_error_code=record.get("telegram_error_code"),
                flood_wait_seconds=record.get("flood_wait_seconds"),
                retry_at=record.get("retry_at"),
                failure_class=record.get("failure_class"),
                presence_result=record.get("presence_result"),
                final_result=record.get("final_result"),
                reason=record.get("reason"),
                previous_state=record.get("previous_state"),
                final_state=record.get("final_state"),
                trace_id=record.get("trace_id"),
            )
            db_session.add(rec)
            await db_session.commit()

    @staticmethod
    async def get_join_attempts(order_id: int, account_id: int = None, limit: int = 200):
        """Retrieve join-attempt records for forensics."""
        async with AsyncSessionLocal() as db_session:
            q = select(VoiceJoinAttempt).filter(VoiceJoinAttempt.order_id == order_id)
            if account_id is not None:
                q = q.filter(VoiceJoinAttempt.account_id == account_id)
            q = q.order_by(desc(VoiceJoinAttempt.id)).limit(limit)
            res = await db_session.execute(q)
            return [to_dict(r) for r in res.scalars().all()]

    @staticmethod
    async def get_active_join_attempt_accounts(order_id: int):
        """Return distinct account_ids with an open (PENDING) attempt — for crash recovery."""
        async with AsyncSessionLocal() as db_session:
            q = select(VoiceJoinAttempt.account_id).filter(
                VoiceJoinAttempt.order_id == order_id,
                VoiceJoinAttempt.result == "PENDING",
            ).distinct()
            res = await db_session.execute(q)
            return res.scalars().all()

    @staticmethod
    async def get_all_account_stats(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            total = (await db_session.execute(select(func.count(TelegramAccount.id)).filter(TelegramAccount.bot_id == bot_id))).scalar() or 0
            active = (await db_session.execute(select(func.count(TelegramAccount.id)).filter(TelegramAccount.account_status == 'active', TelegramAccount.bot_id == bot_id))).scalar() or 0
            limited = (await db_session.execute(select(func.count(TelegramAccount.id)).filter(TelegramAccount.spam_status == 'limited', TelegramAccount.bot_id == bot_id))).scalar() or 0
            return {'total': total, 'active': active, 'limited': limited}

    @staticmethod
    async def get_dead_accounts(bot_id=1):
        """لیست اکانت‌های غیرفعال (سوخته)"""
        async with AsyncSessionLocal() as db_session:
            # account_status == 'inactive'
            res = await db_session.execute(select(TelegramAccount).filter(TelegramAccount.account_status == 'inactive', TelegramAccount.bot_id == bot_id))
            return [to_dict(a) for a in res.scalars().all()]

    @staticmethod
    async def get_limited_accounts(bot_id=1):
        """لیست اکانت‌های محدود شده"""
        async with AsyncSessionLocal() as db_session:
            # spam_status == 'limited'
            res = await db_session.execute(select(TelegramAccount).filter(TelegramAccount.spam_status == 'limited', TelegramAccount.bot_id == bot_id))
            return [to_dict(a) for a in res.scalars().all()]

    @staticmethod
    async def get_all_order_stats(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            total = (await db_session.execute(select(func.count(Order.id)).filter(Order.bot_id == bot_id))).scalar() or 0
            running = (await db_session.execute(select(func.count(Order.id)).filter(Order.status == 'running', Order.bot_id == bot_id))).scalar() or 0
            scheduled = (await db_session.execute(select(func.count(Order.id)).filter(Order.status == 'scheduled', Order.bot_id == bot_id))).scalar() or 0
            # 🐞 فیکس «سفارش ثبت‌شده در آمار دیده نمی‌شود»: سفارش تازه ثبت‌شده
            # تا لحظهٔ تحویل به executor در وضعیت pending (صف) است؛ بدون این
            # شمارنده، سفارش جدید در آمار کلاً غایب بود.
            pending = (await db_session.execute(select(func.count(Order.id)).filter(Order.status == 'pending', Order.bot_id == bot_id))).scalar() or 0
            completed = (await db_session.execute(select(func.count(Order.id)).filter(Order.status == 'completed', Order.bot_id == bot_id))).scalar() or 0
            # سفارش‌های ثبت‌شدهٔ امروز (به وقت UTC — مبنای created_at دیتابیس)
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            today = (await db_session.execute(select(func.count(Order.id)).filter(Order.bot_id == bot_id, Order.created_at >= today_start))).scalar() or 0
            return {'total': total, 'running': running, 'scheduled': scheduled, 'pending': pending, 'completed': completed, 'today': today}

    @staticmethod
    async def reset_stuck_orders():
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(Order).where(Order.status == 'running').values(status='stopped'))
            await db_session.execute(update(VoiceCallSession).where(VoiceCallSession.status == 'joined').values(status='reset'))
            await db_session.commit()

    @staticmethod
    async def get_gateway(slug: str, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(PaymentGateway).filter(PaymentGateway.slug == slug, PaymentGateway.bot_id == bot_id))
            return to_dict(res.scalar_one_or_none())
            
    @staticmethod
    async def get_all_gateways(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(PaymentGateway).filter(PaymentGateway.bot_id == bot_id))
            return [to_dict(g) for g in res.scalars().all()]

    @staticmethod
    async def update_gateway_config(slug: str, is_active: bool, config_dict: dict, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(PaymentGateway).where(PaymentGateway.slug == slug, PaymentGateway.bot_id == bot_id).values(is_active=is_active, config_json=json.dumps(config_dict)))
            await db_session.commit()

    @staticmethod
    async def create_payment_transaction(user_id: int, amount: float, trans_id: str, gateway: str, bot_id=1, pay_url: str = None):
        async with AsyncSessionLocal() as db_session:
            pt = PaymentTransaction(bot_id=bot_id, user_id=user_id, amount=amount, trans_id=trans_id, gateway_slug=gateway, pay_url=pay_url)
            db_session.add(pt)
            await db_session.commit()

    @staticmethod
    async def get_payment_transaction(trans_id: str):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(PaymentTransaction).filter(PaymentTransaction.trans_id == trans_id))
            return to_dict(res.scalar_one_or_none())

    @staticmethod
    async def credit_verified_payment_once(trans_id: str, *, bot_id: int,
                                           gateway_slug: str, description: str) -> bool:
        """Credit a verified gateway payment at most once, with its status.

        A preflight `status != paid` outside the transaction is racy under
        concurrent callbacks; both formerly credited the full amount. The
        payment row locks first, then the wallet (same wallet lock as orders).
        The gateway API verification MUST have succeeded before calling this.
        """
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                row = await db_session.execute(
                    select(PaymentTransaction)
                    .where(PaymentTransaction.trans_id == trans_id)
                    .with_for_update()
                )
                payment = row.scalar_one_or_none()
                if (not payment or payment.status == 'paid'
                        or payment.status not in ('pending', 'failed')
                        or payment.bot_id != int(bot_id)
                        or payment.gateway_slug != gateway_slug):
                    return False
                value = float(payment.amount)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError('invalid verified payment amount')
                user = await db_session.get(User, payment.user_id, with_for_update=True)
                if not user or user.bot_id != payment.bot_id:
                    raise ValueError('payment owner missing or belongs to another bot')
                amount = int(value)  # same toman conversion as the old callback
                if amount <= 0:
                    raise ValueError('payment amount below one toman')
                user.credit = float(user.credit or 0) + amount
                payment.status = 'paid'
                db_session.add(Transaction(
                    bot_id=payment.bot_id, user_id=user.id, amount=amount,
                    type='online_charge', description=description,
                ))
            return True

    @staticmethod
    async def update_payment_status(trans_id: str, status: str):
        async with AsyncSessionLocal() as db_session:
            statement = update(PaymentTransaction).where(
                PaymentTransaction.trans_id == trans_id)
            if status == 'failed':
                # A late failed callback must not erase an earlier paid credit.
                statement = statement.where(PaymentTransaction.status != 'paid')
            await db_session.execute(statement.values(status=status))
            await db_session.commit()

    @staticmethod
    async def get_order_voice_sessions(order_id: int):
        async with AsyncSessionLocal() as db_session:
            q = select(VoiceCallSession).filter(VoiceCallSession.order_id == order_id)
            res = await db_session.execute(q)
            return [to_dict(s) for s in res.scalars().all()]

    @staticmethod
    async def is_account_active_in_chat(account_id: int, chat_id: int, exclude_order_id: int = 0) -> bool:
        async with AsyncSessionLocal() as db_session:
            q = select(VoiceCallSession).join(Order, VoiceCallSession.order_id == Order.id).where(
                VoiceCallSession.account_id == account_id,
                VoiceCallSession.chat_id == chat_id,
                VoiceCallSession.status == 'joined',
                VoiceCallSession.order_id != exclude_order_id,
                Order.status == 'running'
            )
            res = await db_session.execute(q)
            return res.scalar_one_or_none() is not None

    @staticmethod
    async def get_security_setting(key: str, default: bool = False, bot_id: int = 1) -> bool:
        val = await DatabaseManager.get_setting(key, str(default).lower(), bot_id)
        return val == "true"

    @staticmethod
    async def set_security_setting(key: str, value: bool, bot_id: int = 1):
        await DatabaseManager.set_setting(key, str(value).lower(), bot_id)

    # ================= ANTI-SPAM: CANCEL COOLDOWN =================
    @staticmethod
    async def set_user_cancel_cooldown(internal_user_id: int, until: Optional[datetime]):
        """ست/پاک کردن مهلت ممنوعیتِ ثبت سفارش جدید کاربر (UTC naive یا None)."""
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(
                update(User).where(User.id == internal_user_id).values(cancel_cooldown_until=until)
            )
            await db_session.commit()

    # ================= ANTI-SPAM: PENDING GROUP LEAVES =================
    @staticmethod
    async def schedule_group_leave(bot_id: int, account_id: int, chat_id: Optional[int],
                                   target_link: Optional[str], canonical_target: Optional[str],
                                   order_id: Optional[int], not_before: datetime) -> int:
        """زمان‌بندی خروج تأخیری یک اکانت از یک گروه/کانال.

        رکوردهای pending قبلیِ همین اکانت/چت ابطال می‌شوند تا همیشه فقط یک
        خروج زمان‌بندی‌شدهٔ معتبر برای هر (اکانت، گروه) وجود داشته باشد؛ وگرنه
        با سفارش‌های پیاپی چند خروج تکراری صف می‌شد.
        خروجی: id رکورد جدید.
        """
        async with AsyncSessionLocal() as db_session:
            q = update(PendingGroupLeave).where(
                PendingGroupLeave.bot_id == bot_id,
                PendingGroupLeave.account_id == account_id,
                PendingGroupLeave.status == "pending",
            )
            if chat_id:
                q = q.where(PendingGroupLeave.chat_id == int(chat_id))
            elif canonical_target:
                q = q.where(PendingGroupLeave.canonical_target == canonical_target)
            await db_session.execute(q.values(
                status="cancelled", processed_at=datetime.utcnow(),
                last_error="superseded by newer schedule",
            ))
            row = PendingGroupLeave(
                bot_id=bot_id, account_id=account_id,
                chat_id=int(chat_id) if chat_id else None,
                target_link=(target_link or None),
                canonical_target=(canonical_target or None),
                order_id=order_id, status="pending", attempts=0,
                not_before=not_before, created_at=datetime.utcnow(),
            )
            db_session.add(row)
            await db_session.commit()
            await db_session.refresh(row)
            return row.id

    @staticmethod
    async def count_pending_group_leaves(bot_id: int, chat_id: Optional[int] = None,
                                         canonical_target: Optional[str] = None) -> int:
        """شمارش خروج‌های در صف؛ اگر چت/کانونیکال داده شود محدود به همان مقصد
        (برای محاسبهٔ شمارهٔ نفر در صف همان گروه جهت فاصله‌گذاری خروج‌ها)."""
        async with AsyncSessionLocal() as db_session:
            q = select(func.count(PendingGroupLeave.id)).filter(
                PendingGroupLeave.bot_id == bot_id,
                PendingGroupLeave.status == "pending",
            )
            if chat_id:
                q = q.filter(PendingGroupLeave.chat_id == int(chat_id))
            elif canonical_target:
                q = q.filter(PendingGroupLeave.canonical_target == canonical_target)
            return (await db_session.execute(q)).scalar() or 0

    @staticmethod
    async def cancel_group_leaves_for_target(canonical_target: str, bot_id: int = 1) -> int:
        """لغو همهٔ خروج‌های زمان‌بندی‌شدهٔ یک مقصد (سفارش مجدد برای همان گروه
        → اکانت‌ها عضو می‌مانند؛ هیچ چرخهٔ مضر leave/rejoin رخ نمی‌دهد)."""
        if not canonical_target:
            return 0
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                update(PendingGroupLeave).where(
                    PendingGroupLeave.bot_id == bot_id,
                    PendingGroupLeave.status == "pending",
                    PendingGroupLeave.canonical_target == canonical_target,
                ).values(status="cancelled", processed_at=datetime.utcnow(),
                         last_error="new order for this target")
            )
            await db_session.commit()
            return int(res.rowcount or 0)

    @staticmethod
    async def cancel_group_leaves_for_account_chat(account_id: int, chat_id: Optional[int]) -> int:
        """لغو خروج زمان‌بندی‌شدهٔ یک اکانت از یک چت (وقتی همان اکانت دوباره
        وارد همان چت می‌شود، خروجش دیگر معنا ندارد)."""
        if not chat_id:
            return 0
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                update(PendingGroupLeave).where(
                    PendingGroupLeave.account_id == account_id,
                    PendingGroupLeave.chat_id == int(chat_id),
                    PendingGroupLeave.status == "pending",
                ).values(status="cancelled", processed_at=datetime.utcnow(),
                         last_error="account rejoined chat")
            )
            await db_session.commit()
            return int(res.rowcount or 0)

    @staticmethod
    async def cancel_all_pending_group_leaves(bot_id: int = 1) -> int:
        """لغو دستیِ کل صف خروج (از پنل ادمین). خروجی: تعداد رکوردهای لغوشده."""
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                update(PendingGroupLeave).where(
                    PendingGroupLeave.bot_id == bot_id,
                    PendingGroupLeave.status == "pending",
                ).values(status="cancelled", processed_at=datetime.utcnow(),
                         last_error="cleared by admin")
            )
            await db_session.commit()
            return int(res.rowcount or 0)

    @staticmethod
    async def get_due_group_leaves(now: datetime, limit: int = 25, bot_id: Optional[int] = None):
        """رکوردهای سررسید خروج — به‌ترتیب زمانی (قدیمی‌ترین اول) تا خروج‌ها
        دقیقاً «به‌ترتیب و دونه‌به‌دونه» انجام شوند."""
        async with AsyncSessionLocal() as db_session:
            q = select(PendingGroupLeave).filter(
                PendingGroupLeave.status == "pending",
                PendingGroupLeave.not_before <= now,
            ).order_by(PendingGroupLeave.not_before.asc(), PendingGroupLeave.id.asc()).limit(max(1, int(limit)))
            if bot_id is not None:
                q = q.filter(PendingGroupLeave.bot_id == bot_id)
            res = await db_session.execute(q)
            return [to_dict(r) for r in res.scalars().all()]

    @staticmethod
    async def claim_group_leave(row_id: int) -> bool:
        """claim اتمیک یک رکورد pending (جلوگیری از پردازش دوباره/موازی)."""
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                update(PendingGroupLeave).where(
                    PendingGroupLeave.id == row_id,
                    PendingGroupLeave.status == "pending",
                ).values(status="processing")
            )
            await db_session.commit()
            return bool(res.rowcount)

    @staticmethod
    async def finish_group_leave(row_id: int, status: str = "done", error: Optional[str] = None):
        """نهایی کردن یک خروج (done / failed / cancelled) با زمان پردازش."""
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(
                update(PendingGroupLeave).where(PendingGroupLeave.id == row_id).values(
                    status=status, processed_at=datetime.utcnow(),
                    last_error=(str(error)[:400] if error else None),
                )
            )
            await db_session.commit()

    @staticmethod
    async def reschedule_group_leave(row_id: int, not_before: datetime, error: Optional[str] = None):
        """برگرداندن رکورد به صف (تلاش مجدد با تأخیر) پس از خطای موقت."""
        async with AsyncSessionLocal() as db_session:
            row = await db_session.get(PendingGroupLeave, row_id)
            if not row:
                return
            await db_session.execute(
                update(PendingGroupLeave).where(PendingGroupLeave.id == row_id).values(
                    status="pending", attempts=int(row.attempts or 0) + 1,
                    not_before=not_before,
                    last_error=(str(error)[:400] if error else None),
                )
            )
            await db_session.commit()

    @staticmethod
    async def purge_old_group_leaves(days: int = 30) -> int:
        """پاک‌سازی سابقهٔ قدیمی خروج‌ها تا جدول سبک بماند."""
        cutoff = datetime.utcnow() - timedelta(days=max(1, int(days)))
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                delete(PendingGroupLeave).where(
                    PendingGroupLeave.status.in_(["done", "cancelled", "failed"]),
                    PendingGroupLeave.created_at < cutoff,
                )
            )
            await db_session.commit()
            return int(res.rowcount or 0)

    # ================= RESELLER =================
    @staticmethod
    async def create_reseller(token, owner_id, api_id, api_hash, days_charge, name=None):
        async with AsyncSessionLocal() as db_session:
            expiry = datetime.utcnow() + timedelta(days=days_charge)
            bot = ResellerBot(token=token, owner_id=owner_id, api_id=api_id, api_hash=api_hash, expiry_date=expiry, name=name)
            db_session.add(bot)
            await db_session.commit()
            await db_session.refresh(bot)
            await DatabaseManager.init_default_gateways(bot_id=bot.id)
            return to_dict(bot)

    @staticmethod
    async def get_reseller(bot_id):
        async with AsyncSessionLocal() as db_session:
            bot = await db_session.get(ResellerBot, bot_id)
            return to_dict(bot)

    @staticmethod
    async def get_reseller_by_token(token):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(ResellerBot).filter(ResellerBot.token == token))
            return to_dict(res.scalar_one_or_none())

    @staticmethod
    async def get_all_resellers():
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(ResellerBot).order_by(ResellerBot.id))
            return [to_dict(b) for b in res.scalars().all()]

    @staticmethod
    async def renew_reseller(bot_id, days):
        async with AsyncSessionLocal() as db_session:
            bot = await db_session.get(ResellerBot, bot_id)
            if bot:
                now = datetime.utcnow()
                if bot.expiry_date < now: bot.expiry_date = now + timedelta(days=days)
                else: bot.expiry_date += timedelta(days=days)
                bot.is_active = True
                await db_session.commit()
                return True, bot.expiry_date
            return False, None

    @staticmethod
    async def update_reseller_info(bot_id, token=None, owner_id=None, api_id=None, api_hash=None, is_active=None):
        async with AsyncSessionLocal() as db_session:
            bot = await db_session.get(ResellerBot, bot_id)
            if not bot: return False
            if token is not None: bot.token = token
            if owner_id is not None: bot.owner_id = owner_id
            if api_id is not None: bot.api_id = api_id
            if api_hash is not None: bot.api_hash = api_hash
            if is_active is not None: bot.is_active = is_active
            await db_session.commit()
            return True

    @staticmethod
    async def delete_reseller(bot_id):
        async with AsyncSessionLocal() as db_session:
            tables = [User, Plan, TelegramAccount, Order, Transaction, VoiceCallSession, PaymentGateway, PaymentTransaction, BotSetting]
            for table in tables:
                if hasattr(table, 'bot_id'):
                    await db_session.execute(delete(table).where(table.bot_id == bot_id))
            await db_session.execute(delete(ResellerBot).where(ResellerBot.id == bot_id))
            await db_session.commit()
            return True

    @staticmethod
    async def is_service_active(service_type: str, bot_id=1) -> bool:
        key = f"service_{service_type}"
        val = await DatabaseManager.get_setting(key, "true", bot_id=bot_id)
        return val == "true"

    @staticmethod
    async def get_active_gateway(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(PaymentGateway).filter(PaymentGateway.bot_id == bot_id, PaymentGateway.is_active == True))
            gws = res.scalars().all()
            if not gws: return None
            return to_dict(gws[0])

    @staticmethod
    async def get_active_gateways(bot_id=1):
        """همهٔ درگاه‌های فعال (نه فقط یکی) را برمی‌گرداند تا بتوان چند درگاه را
        هم‌زمان فعال داشت و به کاربر امکان انتخاب داد."""
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(PaymentGateway).filter(PaymentGateway.bot_id == bot_id, PaymentGateway.is_active == True))
            return [to_dict(g) for g in res.scalars().all()]

    @staticmethod
    async def get_all_users_list(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(select(User.telegram_id).filter(User.bot_id == bot_id))
            return result.scalars().all()

    @staticmethod
    async def get_total_users_count(bot_id=1):
        async with AsyncSessionLocal() as db_session:
            count = await db_session.execute(select(func.count(User.id)).filter(User.bot_id == bot_id))
            return count.scalar() or 0