#!/usr/bin/env python3
"""
tools/check_order_links.py
════════════════════════════════════════════════════════════════════════
بررسیِ لینک مقصدِ سفارش‌ها (Order Target Link Audit)
════════════════════════════════════════════════════════════════════════

چرا لازم است؟
پیش از این، لینک مقصد بدون هیچ اعتبارسنجی از پیام کاربر ذخیره می‌شد. اگر
کاربر متنِ پیامِ «سفارش با موفقیت ثبت شد» (یا هر متن دیگری) را کپی/فوروارد
می‌کرد، همان متن به‌عنوان لینک ثبت می‌شد و سفارش هیچ‌وقت اجرا نمی‌شد؛ در
حالی که موجودی کاربر کسر شده بود و موجی از خطای «Invalid Link» کل استخر
اکانت‌ها را مصرف می‌کرد.

نسخهٔ فعلی ربات جلوی ثبتِ چنین سفارشی را می‌گیرد، اما سفارش‌هایی که قبلاً
با لینکِ خراب ثبت شده‌اند باید پیدا و پاک‌سازی شوند — این ابزار همان کار را
می‌کند.

نمونه‌ها:
    # فقط گزارش (سفارش‌های در حال اجرا و رزرو‌شده)
    python3 tools/check_order_links.py

    # همهٔ سفارش‌ها (حتی completed / failed)
    python3 tools/check_order_links.py --all

    # فقط یک سفارش (مثلاً کد پیگیری 692)
    python3 tools/check_order_links.py --order 692

    # لغو + عودت کاملِ سفارش‌های در حال اجرا/رزرو‌شده‌ی دارای لینک نامعتبر
    python3 tools/check_order_links.py --cancel-invalid

    # عودتِ سفارش‌هایی که قبلاً با لینک خراب fail شده بودند (بدون عودتِ تکراری)
    python3 tools/check_order_links.py --refund-failed

ترکیبِ متداول برای پاک‌سازی کامل:
    python3 tools/check_order_links.py --all --cancel-invalid --refund-failed

نکته: این اسکریپت به دیتابیس وصل می‌شود، بنابراین باید جایی اجرا شود که
متغیرهای محیطیِ دیتابیس (DATABASE_URL یا POSTGRES_*) در دسترس باشند —
مثلاً داخل کانتینر ربات:

    docker compose exec telegram_bot python3 tools/check_order_links.py --all
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.link_utils import validate_target_link  # noqa: E402

ACTIVE_STATUSES = ("running", "scheduled", "pending")


async def fetch_orders(statuses, order_id=None, limit=500):
    """خواندن سفارش‌ها از دیتابیس (بدون وابستگی به لایهٔ ربات)."""
    from sqlalchemy import select

    from database import AsyncSessionLocal, Order

    async with AsyncSessionLocal() as session:
        query = select(Order)
        if order_id:
            query = query.where(Order.id == order_id)
        elif statuses:
            query = query.where(Order.status.in_(list(statuses)))
        query = query.order_by(Order.id.desc()).limit(limit)
        rows = (await session.execute(query)).scalars().all()
        return [
            {
                "id": r.id,
                "bot_id": r.bot_id,
                "user_id": r.user_id,
                "order_type": r.order_type,
                "target_link": r.target_link,
                "accounts_count": r.accounts_count,
                "price_paid": r.price_paid,
                "status": r.status,
                "created_at": r.created_at,
                "started_at": r.started_at,
            }
            for r in rows
        ]


async def already_refunded(order_id: int) -> bool:
    """آیا برای این سفارش قبلاً عودت ثبت شده است؟ (جلوگیری از عودتِ تکراری)"""
    from sqlalchemy import select

    from database import AsyncSessionLocal, Transaction

    async with AsyncSessionLocal() as session:
        query = (
            select(Transaction.id)
            .where(Transaction.type == "order_refund")
            .where(Transaction.description.like(f"%{order_id}%"))
            .limit(1)
        )
        return (await session.execute(query)).first() is not None


async def refund_order(order: dict) -> bool:
    """عودت کاملِ مبلغ سفارش به کیف پول کاربر."""
    from database import DatabaseManager

    price = float(order.get("price_paid") or 0)
    if price <= 0:
        return False
    ok, _balance = await DatabaseManager.update_user_credit(
        order["user_id"],
        price,
        "order_refund",
        f"عودت کامل سفارش {order['id']} | لینک مقصد نامعتبر",
        bot_id=order.get("bot_id") or 1,
    )
    return bool(ok)


async def cancel_order(order: dict) -> str:
    """لغو + تسویه + عودت از مسیرِ رسمی executor (اگر در دسترس باشد)."""
    try:
        from services.order_executor import order_executor

        result = await order_executor.settle_and_refund_order(
            order["id"],
            do_refund=True,
            canceled_by_role="سیستم",
            canceled_by_name="بررسی لینک مقصد",
            cancellation_reason="لینک مقصد نامعتبر",
            bot_id=order.get("bot_id") or 1,
        )
        return f"لغو شد | عودت: {result.get('refund_amount')}"
    except Exception as exc:  # executor خاموش است (مثلاً اجرا روی لپ‌تاپ)
        if await refund_order(order):
            return f"عودتِ دستی انجام شد (executor در دسترس نبود: {exc})"
        return f"ناموفق: {exc}"


def _short(value, size=60):
    text = str(value or "")
    text = text.replace("\n", " ⏎ ")
    return text if len(text) <= size else text[: size - 1] + "…"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Order target-link audit")
    parser.add_argument("--order", type=int, help="بررسی یک سفارش خاص")
    parser.add_argument(
        "--all", action="store_true",
        help="بررسی همهٔ وضعیت‌ها (پیش‌فرض: running/scheduled/pending)",
    )
    parser.add_argument(
        "--cancel-invalid", action="store_true",
        help="لغو + عودت کاملِ سفارش‌های فعالِ دارای لینک نامعتبر",
    )
    parser.add_argument(
        "--refund-failed", action="store_true",
        help="عودتِ سفارش‌های fail‌شده با لینک نامعتبر (بدون عودتِ تکراری)",
    )
    parser.add_argument("--limit", type=int, default=500, help="حداکثر تعداد سفارش")
    args = parser.parse_args()

    statuses = None if (args.all or args.order) else ACTIVE_STATUSES
    try:
        orders = await fetch_orders(statuses, order_id=args.order, limit=args.limit)
    except Exception as exc:
        print(f"⛔️ اتصال به دیتابیس ناموفق بود: {exc}")
        return 1

    if not orders:
        print("هیچ سفارشی با این فیلتر پیدا نشد.")
        return 0

    invalid = []
    for order in orders:
        ok, clean, reason = validate_target_link(order.get("target_link"))
        if ok:
            continue
        order["_reason"] = reason
        order["_clean"] = clean
        invalid.append(order)

    print(f"🔎 {len(orders)} سفارش بررسی شد | {len(invalid)} لینک نامعتبر")
    if not invalid:
        return 0

    print()
    header = f"{'ID':>6}  {'وضعیت':<12} {'مبلغ':>10}  {'علت':<20} لینک"
    print(header)
    print("-" * max(len(header), 70))
    for order in invalid:
        print(
            f"{order['id']:>6}  {str(order['status']):<12} "
            f"{float(order.get('price_paid') or 0):>10,.0f}  "
            f"{order['_reason']:<20} {_short(order.get('target_link'))}"
        )

    if not (args.cancel_invalid or args.refund_failed):
        print()
        print(
            "ℹ️ فقط حالتِ گزارش اجرا شد. برای اصلاح:\n"
            "   --cancel-invalid   لغو + عودتِ سفارش‌های فعالِ دارای لینک خراب\n"
            "   --refund-failed    عودتِ سفارش‌هایی که قبلاً با لینک خراب بسته شده‌اند"
        )
        return 0

    print()
    acted = 0
    for order in invalid:
        status = str(order.get("status"))
        note = "بدون اقدام"
        if args.cancel_invalid and status in ACTIVE_STATUSES:
            note = await cancel_order(order)
            acted += 1
        elif args.refund_failed and status == "failed":
            if await already_refunded(order["id"]):
                note = "قبلاً عودت خورده — رد شد"
            else:
                note = "عودت انجام شد" if await refund_order(order) else "عودت ناموفق"
                acted += 1
        print(f"• سفارش {order['id']} ({status}): {note}")

    print(f"\n✅ پایان — {acted} سفارش اصلاح شد.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
