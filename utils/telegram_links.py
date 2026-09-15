"""Validation and canonicalization for Telegram order targets.

Order targets are ultimately handed to MTProto methods such as
``Client.join_chat``.  Those methods accept a fairly broad set of strings, so
accepting arbitrary chat text at the bot boundary can turn an accidental paste
into a paid order that will never be fulfilled.  Keep the accepted syntax small
and explicit here, then use this same helper at every order boundary.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple
from urllib.parse import urlsplit

# Telegram public usernames are 5–32 characters.  Requiring a leading letter
# intentionally prevents everyday numeric text (an order number, for example)
# from being mistaken for a valid destination.
_PUBLIC_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
# Invite hashes currently use this alphabet.  The conservative minimum catches
# truncated/corrupted links while retaining old joinchat links.
_INVITE_HASH = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}


_TARGET_ERROR = (
    "لینک مقصد معتبر نیست. لطفاً لینک عمومی مانند https://t.me/channelname "
    "یا لینک دعوت خصوصی https://t.me/+... را ارسال کنید."
)


def normalize_telegram_target(value: object) -> Tuple[bool, Optional[str], Optional[str]]:
    """Validate an order target and return its canonical Telegram URL.

    Accepted forms are public usernames (``@name``, ``name``, or a Telegram
    URL), public message URLs (normalized to their group/channel username),
    and private ``t.me/+hash`` / legacy ``t.me/joinchat/hash`` invites.

    Returns ``(ok, canonical_url, error_message)``.  This module deliberately
    has no Telegram, database, or bot dependency so it can run before charging
    the wallet and can be tested without a live Telegram stack.
    """
    if not isinstance(value, str):
        return False, None, _TARGET_ERROR

    raw = value.strip()
    if not raw or len(raw) > 255 or any(char.isspace() for char in raw):
        return False, None, _TARGET_ERROR

    # @username and a bare username are convenient in Telegram chats.  Bare
    # text is accepted only when it is exactly a plausible Telegram username;
    # this is what rejects pasted bot messages such as “سفارش ثبت شد”.
    username = raw[1:] if raw.startswith("@") else raw
    if _PUBLIC_USERNAME.fullmatch(username):
        return True, f"https://t.me/{username}", None
    if raw.startswith("@"):
        return False, None, _TARGET_ERROR

    # urlsplit needs a scheme to recognize a bare t.me/foo address as a host.
    parseable = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlsplit(parseable)
    except ValueError:
        return False, None, _TARGET_ERROR

    if parsed.scheme.lower() not in {"http", "https"}:
        return False, None, _TARGET_ERROR
    if (parsed.hostname or "").lower() not in _TELEGRAM_HOSTS:
        return False, None, _TARGET_ERROR
    try:
        has_port = parsed.port is not None
    except ValueError:
        return False, None, _TARGET_ERROR
    if parsed.username or parsed.password or has_port:
        return False, None, _TARGET_ERROR

    pieces = [piece for piece in parsed.path.split("/") if piece]
    if not pieces:
        return False, None, _TARGET_ERROR

    first = pieces[0]
    # Modern private invite: https://t.me/+AbCd...
    if first.startswith("+") and len(pieces) == 1 and _INVITE_HASH.fullmatch(first[1:]):
        return True, f"https://t.me/{first}", None
    # Legacy private invite: https://t.me/joinchat/AbCd...
    if (
        first.lower() == "joinchat"
        and len(pieces) == 2
        and _INVITE_HASH.fullmatch(pieces[1])
    ):
        return True, f"https://t.me/joinchat/{pieces[1]}", None

    # Public channel/group, optionally with a numeric message id.  A /c/ link
    # deliberately fails: it does not expose the public username needed for a
    # fresh account to join the chat.
    if _PUBLIC_USERNAME.fullmatch(first):
        if len(pieces) == 1 or (len(pieces) == 2 and pieces[1].isdigit()):
            return True, f"https://t.me/{first}", None

    return False, None, _TARGET_ERROR
