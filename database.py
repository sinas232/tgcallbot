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
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import (
    Column, Integer, String, Boolean, Float, DateTime, Text,
    BigInteger, func, select, update, delete, desc, text, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from config import Config

logger = logging.getLogger(__name__)
Base = declarative_base()

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
    async def set_setting(key: str, value: str, bot_id=1):
        async with AsyncSessionLocal() as db_session:
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
        async with AsyncSessionLocal() as db_session:
            user = await db_session.get(User, internal_user_id)
            if not user: return False, 0
            user.credit += amount
            trans = Transaction(bot_id=bot_id, user_id=internal_user_id, amount=amount, type=type, description=desc)
            db_session.add(trans)
            await db_session.commit()
            return True, user.credit

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
    async def start_order_duration(order_id: int) -> datetime:
        """Set billable service start at the end of the build phase."""
        async with AsyncSessionLocal() as db_session:
            now = datetime.utcnow()
            await db_session.execute(
                update(Order).where(Order.id == order_id).values(started_at=now)
            )
            await db_session.commit()
            return now
    
    @staticmethod
    async def mark_order_as_running(order_id: int):
        """Move the order to `running` WITHOUT starting the billable clock.

        `started_at` stays NULL during the whole join/build phase so the
        time accounts spend joining is never charged to the customer. The
        executor stamps `started_at` (via start_order_duration) only after
        the required accounts are present and the paid duration begins.
        """
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(
                update(Order)
                .where(Order.id == order_id)
                .values(status='running')
            )
            await db_session.commit()

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
            q = select(Order).filter(
                Order.target_link == link,
                Order.bot_id == bot_id,
                Order.status.in_(['running', 'scheduled'])
            ).limit(1)
            res = await db_session.execute(q)
            return res.scalar_one_or_none() is not None

    @staticmethod
    async def has_time_overlap_order(link: str, new_start_time: datetime, new_duration_minutes: int, bot_id: int = 1) -> bool:
        """بررسی تداخل زمانی سفارش جدید با سفارشات موجود برای یک لینک"""
        async with AsyncSessionLocal() as db_session:
            # محاسبه زمان پایان سفارش جدید
            new_end_time = new_start_time + timedelta(minutes=new_duration_minutes)
            
            # دریافت تمام سفارشات فعال/رزروی برای این لینک
            q = select(Order).filter(
                Order.target_link == link,
                Order.bot_id == bot_id,
                Order.status.in_(['running', 'scheduled'])
            )
            res = await db_session.execute(q)
            existing_orders = res.scalars().all()
            
            for order in existing_orders:
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
    async def get_capacity_reservations(bot_id: int = 1) -> List[Dict[str, Any]]:
        """
        رزروهای فعال ظرفیت برای Capacity Guard (گارد منابع قبل از ثبت سفارش).
        سبک: فقط ستون‌های لازم از سفارش‌های running/scheduled/pending خوانده
        می‌شود؛ هر رکورد یعنی «accounts_count اکانت از شروع تا پایان مدت اشغال
        است». محاسبهٔ تداخل/اوج در services/capacity_planner.py انجام می‌شود.
        """
        async with AsyncSessionLocal() as db_session:
            q = select(
                Order.id,
                Order.user_id,
                Order.accounts_count,
                Order.duration_minutes,
                Order.status,
                Order.started_at,
                Order.scheduled_for,
                Order.created_at,
            ).filter(
                Order.bot_id == bot_id,
                Order.status.in_(['running', 'scheduled', 'pending']),
            )
            res = await db_session.execute(q)
            return [
                {
                    "id": row[0],
                    "user_id": row[1],
                    "accounts_count": row[2],
                    "duration_minutes": row[3],
                    "status": row[4],
                    "started_at": row[5],
                    "scheduled_for": row[6],
                    "created_at": row[7],
                }
                for row in res.all()
            ]

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
            await db_session.execute(update(TelegramAccount).where(TelegramAccount.id == aid).values(spam_status=status, spam_check_result=result_text, last_health_check=datetime.utcnow()))
            await db_session.commit()

    @staticmethod
    async def create_voice_call_session(order_id, account_id, chat_id, bot_id=1):
        async with AsyncSessionLocal() as db_session:
            s = VoiceCallSession(bot_id=bot_id, order_id=order_id, account_id=account_id, chat_id=chat_id)
            db_session.add(s)
            await db_session.commit()

    @staticmethod
    async def update_voice_call_session(account_id, chat_id, status):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(VoiceCallSession).where(VoiceCallSession.account_id==account_id, VoiceCallSession.chat_id==chat_id).values(status=status))
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
        """Reset ONLY the voice-session rows left `joined` by a restart.

        ⚠️ This used to flip every `running` order to `stopped` as well.
        That silently destroyed paid, still-valid orders after an OOM
        kill / deploy (incident 1405-07-04: orders 859 & 860 were paid,
        the process restarted mid-build, and both ended up listed under
        "cancelled" with no re-join, no message and no refund).
        Order recovery now lives in `main._recover_interrupted_orders`.
        """
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(VoiceCallSession).where(VoiceCallSession.status == 'joined').values(status='reset'))
            await db_session.commit()

    @staticmethod
    async def get_running_orders():
        """Orders still marked `running` — i.e. interrupted by a restart."""
        async with AsyncSessionLocal() as db_session:
            q = select(Order).filter(Order.status == 'running').order_by(Order.id)
            res = await db_session.execute(q)
            return [to_dict(o) for o in res.scalars().all()]

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
    async def update_payment_status(trans_id: str, status: str):
        async with AsyncSessionLocal() as db_session:
            await db_session.execute(update(PaymentTransaction).where(PaymentTransaction.trans_id == trans_id).values(status=status))
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