#!/usr/bin/env python3
"""
tools/premium_emoji_sync.py
══════════════════════════════════════════════════════════════════════════
ابزار خطِ فرمانِ ایموجی پریمیوم (Custom Emoji)
══════════════════════════════════════════════════════════════════════════

سه کار:

1) ``--list``
   فهرست بستهٔ ایموجی پریمیومِ داخل کد (کلید → شناسه → ایموجی جایگزین).

2) ``--validate``
   با توکن ربات، همهٔ شناسه‌ها را از طریق متد رسمی ``getCustomEmojiStickers``
   می‌سنجد و گزارش می‌دهد کدام‌ها معتبرند. خروجی را می‌توان به JSON یا به
   یک خط ``PREMIUM_EMOJI_OVERRIDES=`` برای .env تبدیل کرد.

3) ``--discover PACK``
   با یک اکانت MTProto (api_id/api_hash/session_string) یک پکِ ایموجی
   پریمیوم را می‌خواند و نگاشتِ «ایموجی یونیکد → شناسه» را استخراج می‌کند.
   این همان کاری است که دکمهٔ «کشف از اکانت» در پنل ادمین انجام می‌دهد،
   ولی بدون نیاز به دیتابیس (برای تست روی لپ‌تاپ).

نمونه:
    python tools/premium_emoji_sync.py --list
    python tools/premium_emoji_sync.py --validate --token "$BOT_TOKEN"
    python tools/premium_emoji_sync.py --validate --token "$BOT_TOKEN" --env
    python tools/premium_emoji_sync.py --discover NewsEmoji \
        --api-id 1234 --api-hash xxxx --session-string "..." --json pack.json

نکته: هیچ‌کدام از این‌ها برای کارکرد ربات لازم نیست — ربات در هر استارت
خودش اعتبارسنجی می‌کند (``PREMIUM_EMOJI_VALIDATE=true``). این ابزار برای
زمانی است که می‌خواهید ایموجی اختصاصیِ خودتان را اضافه کنید.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.premium_emoji import (  # noqa: E402
    KEY_TO_FALLBACK,
    KEY_TO_ID,
    PREMIUM_EMOJI_PACK,
)

VALIDATION_CHUNK = 200  # محدودیت رسمی تلگرام در هر فراخوانی


# ══════════════════════════════════════════════════════════════════════
def cmd_list() -> int:
    print(f"بستهٔ ایموجی پریمیوم — {len(PREMIUM_EMOJI_PACK)} کلید")
    print(f"{'KEY':<16} {'EMOJI':<6} {'CUSTOM_EMOJI_ID'}")
    print("-" * 48)
    for key, (emoji_id, unicode_char) in PREMIUM_EMOJI_PACK.items():
        print(f"{key:<16} {unicode_char:<6} {emoji_id}")
    return 0


async def _validate_with_token(token: str, ids: Sequence[str]) -> Dict[str, Any]:
    from telegram import Bot

    valid: set = set()
    errors: List[str] = []
    bot = Bot(token=token)
    try:
        for start in range(0, len(ids), VALIDATION_CHUNK):
            chunk = list(ids[start: start + VALIDATION_CHUNK])
            try:
                stickers = await bot.get_custom_emoji_stickers(chunk)
                for sticker in stickers or []:
                    cid = getattr(sticker, "custom_emoji_id", None)
                    if cid:
                        valid.add(str(cid))
            except Exception as exc:
                errors.append(f"chunk {start // VALIDATION_CHUNK}: {exc}")
    finally:
        try:
            await bot.shutdown()
        except Exception:
            pass
    return {"valid": sorted(valid), "invalid": [i for i in ids if i not in valid], "errors": errors}


def cmd_validate(args: argparse.Namespace) -> int:
    token = args.token or os.getenv("BOT_TOKEN") or ""
    if not token:
        print("خطا: توکن ربات لازم است (--token یا متغیر BOT_TOKEN).", file=sys.stderr)
        return 2

    ids = list(KEY_TO_ID.values())
    ids += [str(v) for v in (args.extra_ids or "").split(",") if v.strip().isdigit()]
    ids = list(dict.fromkeys(ids))  # حفظ ترتیب + یکتا

    print(f"اعتبارسنجی {len(ids)} شناسه با getCustomEmojiStickers …")
    report = asyncio.run(_validate_with_token(token, ids))

    valid = set(report["valid"])
    print(f"\n✅ معتبر: {len(valid)}   ❌ نامعتبر: {len(report['invalid'])}")
    for bad in report["invalid"]:
        key = next((k for k, v in KEY_TO_ID.items() if v == bad), "?")
        print(f"   - {bad}  (کلید: {key}, ایموجی: {KEY_TO_FALLBACK.get(key, '?')})")
    for err in report["errors"]:
        print(f"   ! {err}", file=sys.stderr)

    _write_outputs(args, valid_ids=valid, mapping=None)
    return 0 if valid else 1


async def _discover_pack(api_id: int, api_hash: str, session_string: str, packs: Sequence[str]) -> Dict[str, Dict[str, str]]:
    from pyrogram import Client
    from pyrogram.raw import functions as raw_functions
    from pyrogram.raw import types as raw_types

    found: Dict[str, Dict[str, str]] = {}
    client = Client(
        name="premium_emoji_sync",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        in_memory=True,
        no_updates=True,
    )
    await client.start()
    try:
        for short_name in packs:
            try:
                # kurigram: پارامتر رسمی ``stickerset`` است (نه sticker_set)
                stickerset = raw_types.InputStickerSetShortName(short_name=short_name)
                res = None
                last_err = None
                for kwargs in (
                    {"stickerset": stickerset, "hash": 0},
                    {"sticker_set": stickerset, "hash": 0},
                    {"stickerset": stickerset},
                    {"sticker_set": stickerset},
                ):
                    try:
                        res = await client.invoke(
                            raw_functions.messages.GetStickerSet(**kwargs)
                        )
                        break
                    except TypeError as te:
                        last_err = te
                        continue
                if res is None:
                    raise last_err or TypeError("GetStickerSet signature mismatch")
                for pack in getattr(res, "packs", []) or []:
                    emoticon = getattr(pack, "emoticon", None)
                    doc_ids = getattr(pack, "documents", None) or []
                    if emoticon and doc_ids:
                        found.setdefault(emoticon, {"id": str(doc_ids[0]), "pack": short_name})
                print(f"  • {short_name}: مجموعاً {len(found)} ایموجی تا اینجا")
            except Exception as exc:
                print(f"  ! پک {short_name} خوانده نشد: {exc}", file=sys.stderr)
    finally:
        await client.stop()
    return found


def cmd_discover(args: argparse.Namespace) -> int:
    if not (args.api_id and args.api_hash and args.session_string):
        print("خطا: --api-id و --api-hash و --session-string لازم است.", file=sys.stderr)
        return 2

    packs = [p.strip() for p in (args.discover or "").split(",") if p.strip()]
    if not packs:
        print("خطا: نام پک را بدهید، مثال: --discover NewsEmoji,TgAndroidIcons", file=sys.stderr)
        return 2

    print(f"کشف شناسه‌ها از پک‌های: {', '.join(packs)}")
    found = asyncio.run(_discover_pack(args.api_id, args.api_hash, args.session_string, packs))
    if not found:
        print("هیچ شناسه‌ای کشف نشد.", file=sys.stderr)
        return 1

    print(f"\n✅ {len(found)} ایموجی پیدا شد:")
    for unicode_char, info in sorted(found.items()):
        known = ""
        for key, uni in KEY_TO_FALLBACK.items():
            if uni.rstrip("\ufe0f") == unicode_char.rstrip("\ufe0f"):
                known = f"  (کلید ربات: {key})"
                break
        print(f"   {unicode_char}  {info['id']}  [{info['pack']}]{known}")

    _write_outputs(args, valid_ids=None, mapping={c: i["id"] for c, i in found.items()})
    return 0


def _write_outputs(args: argparse.Namespace, valid_ids: Optional[set], mapping: Optional[Dict[str, str]]) -> None:
    """ذخیرهٔ JSON و/یا چاپِ خطِ PREMIUM_EMOJI_OVERRIDES برای .env"""
    payload: Dict[str, Any] = {}
    if mapping is not None:
        payload["mapping"] = mapping
    if valid_ids is not None:
        payload["valid_ids"] = sorted(valid_ids)

    if args.json and payload:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\n📄 ذخیره شد: {args.json}")

    if args.env and mapping:
        # فقط ایموجی‌هایی که ربات می‌شناسد (یا کلیدهای صریح) در override می‌آیند
        overrides: Dict[str, str] = {}
        for unicode_char, emoji_id in mapping.items():
            key = None
            for candidate, uni in KEY_TO_FALLBACK.items():
                if uni.rstrip("\ufe0f") == unicode_char.rstrip("\ufe0f"):
                    key = candidate
                    break
            if key:
                overrides[key] = emoji_id
            elif not args.only_known:
                overrides[unicode_char] = emoji_id
        if overrides:
            print("\n# این خط را در .env بگذارید:")
            print("PREMIUM_EMOJI_OVERRIDES=" + json.dumps(overrides, ensure_ascii=False))
        else:
            print("\n# هیچ موردِ قابل جایگزینی پیدا نشد (--only-known را بردارید).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ابزار ایموجی پریمیوم (Custom Emoji) ربات تماس صوتی تلگرام",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="فهرست بستهٔ ایموجی داخل کد")
    mode.add_argument("--validate", action="store_true", help="اعتبارسنجی شناسه‌ها با توکن ربات")
    mode.add_argument("--discover", metavar="PACKS", help="کشف شناسه از پک(ها) با اکانت MTProto")

    parser.add_argument("--token", help="توکن ربات (پیش‌فرض: متغیر BOT_TOKEN)")
    parser.add_argument("--api-id", type=int, help="API ID برای کشف با اکانت")
    parser.add_argument("--api-hash", help="API HASH برای کشف با اکانت")
    parser.add_argument("--session-string", help="Session String (رمزنگاری‌نشده) برای کشف")
    parser.add_argument("--extra-ids", help="شناسه‌های اضافه برای اعتبارسنجی (جداشده با کاما)")
    parser.add_argument("--json", help="مسیر فایل JSON خروجی")
    parser.add_argument("--env", action="store_true", help="چاپ خط PREMIUM_EMOJI_OVERRIDES برای .env")
    parser.add_argument(
        "--only-known",
        action="store_true",
        help="در خروجی .env فقط ایموجی‌های شناخته‌شدهٔ ربات بیایند (پیش‌فرض: بله)",
    )
    parser.set_defaults(only_known=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        return cmd_list()
    if args.validate:
        return cmd_validate(args)
    if args.discover:
        return cmd_discover(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
