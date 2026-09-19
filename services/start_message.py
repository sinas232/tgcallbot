"""Tenant-scoped welcome templates. Values are text, never executable formatting."""
import html
import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import jdatetime
from utils.helpers import format_price
from utils.premium_emoji import markdown_to_html

logger = logging.getLogger(__name__)
DEFAULT_START_TEXT = """👋 <b>{name} عزیز، خوش آمدید!</b>

💎 <b>{brand}</b>
<i>{tagline}</i>
━━━━━━━━━━━━━━
🎯 <b>خدمات در دسترس شما</b>
{services}

💳 <b>کیف پول شما</b>
موجودی: <code>{credit}</code> تومان
شناسه کاربری: <code>{id}</code>
━━━━━━━━━━━━━━
✨ <b>از انتخاب تا پیگیری، همین‌جا</b>
۱. از «🛍 خرید سرویس» پلن دلخواهتان را انتخاب کنید.
۲. جزئیات و مبلغ را بررسی و سفارش را ثبت کنید.
۳. وضعیت را از «📦 سفارشات من» دنبال کنید.

📞 <b>کنار شما هستیم</b>
{support}
🕒 {support_hours}

👇 <b>برای شروع، از منوی پایین انتخاب کنید.</b>"""

START_DEFAULTS = {
    'start_text': DEFAULT_START_TEXT,
    'start_brand': '',  # Empty means the current bot's Telegram display name.
    'start_tagline': 'انتخاب آسان، هزینه روشن، پیگیری سفارش در یک‌جا',
    'start_support': 'از بخش «🆘 پشتیبانی» با ما در ارتباط باشید.',
    'start_support_hours': 'زمان پاسخ‌گویی را از پشتیبانی بپرسید.',
    'service_voice_chat': 'true',
    'service_group_join': 'true',
    'service_channel_join': 'true',
    'service_incall_chat': 'false',
}
START_FIELDS = {
    '🏷 نام برند': ('start_brand', 'نام برند (حداکثر ۱۲۰ نویسه؛ متن ساده):'),
    '💬 شعار برند': ('start_tagline', 'شعار کوتاه برند (حداکثر ۱۲۰ نویسه؛ متن ساده):'),
    '📞 راه ارتباطی پشتیبانی': ('start_support', 'آیدی یا توضیح راه ارتباطی پشتیبانی (حداکثر ۱۲۰ نویسه):'),
    '🕒 ساعت پاسخ‌گویی': ('start_support_hours', 'ساعت واقعی پاسخ‌گویی، مثلاً هر روز ۹ تا ۲۳ (حداکثر ۱۲۰ نویسه):'),
}
PREVIEW_START = '👁 پیش‌نمایش استارت'
USE_DEFAULT_START = '✨ قالب پیشنهادی'
START_VARIABLES = {
    'name', 'first_name', 'full_name', 'id', 'username', 'credit',
    'bot_name', 'bot_username', 'brand', 'tagline', 'support', 'support_hours',
    'services', 'date', 'time',
}
_TOKEN = re.compile(r'{{|}}|{([^{}]+)}')
_HTML = re.compile(r'</?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|span|tg-spoiler|tg-emoji|blockquote)\b[^>]*>', re.I)


class _TemplateHTML(HTMLParser):
    """Reject broken templates at save time; Telegram remains the final parser."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if not _HTML.fullmatch(self.get_starttag_text()):
            raise ValueError(f'تگ HTML پشتیبانی نمی‌شود: {tag}')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError('تگ‌های HTML درست بسته نشده‌اند.')

    def handle_startendtag(self, tag, attrs):
        raise ValueError('برای خط جدید از Enter استفاده کنید، نه تگ HTML خودبسته.')


def validate_start_template(template):
    if not template.strip() or len(template) > 2500:
        raise ValueError('متن باید بین ۱ تا ۲۵۰۰ نویسه باشد.')
    for match in _TOKEN.finditer(template):
        if match.group(1) and match.group(1) not in START_VARIABLES:
            raise ValueError(f'متغیر ناشناخته: {match.group(0)}')
    # Unmatched braces are usually typos. Double braces explicitly mean literal braces.
    if re.search(r'[{}]', _TOKEN.sub('', template)):
        raise ValueError('آکولاد متغیرها را کامل کنید؛ برای آکولاد معمولی از {{ و }} استفاده کنید.')
    if _HTML.search(template):
        parser = _TemplateHTML()
        parser.feed(template)
        parser.close()
        if parser.stack:
            raise ValueError('تگ‌های HTML درست بسته نشده‌اند.')


def _bot_attr(bot, name, fallback):
    try:
        return getattr(bot, name, None) or fallback
    except RuntimeError:  # No getMe RPC just to render an admin preview.
        return fallback


def start_values(user, db_user, bot, settings, now=None):
    now = now or datetime.now(ZoneInfo('Asia/Tehran'))
    local = now.astimezone(ZoneInfo('Asia/Tehran')) if now.tzinfo else now
    name = getattr(user, 'first_name', None) or 'کاربر'
    full = ' '.join(x for x in [getattr(user, 'first_name', None), getattr(user, 'last_name', None)] if x) or name
    services = []
    for key, label in (
        ('service_voice_chat', '🎙 حضور اکانت‌ها در ویس‌کال'),
        ('service_group_join', '👥 عضویت در گروه'),
        ('service_channel_join', '📢 عضویت در کانال'),
        ('service_incall_chat', '💬 چت در ویس‌کال'),
    ):
        if settings.get(key, START_DEFAULTS[key]) == 'true':
            services.append(label)
    bot_name = _bot_attr(bot, 'first_name', 'خدمات تلگرام')
    return {
        'name': name, 'first_name': name, 'full_name': full,
        'id': str(user.id), 'username': getattr(user, 'username', None) or 'بدون نام کاربری',
        'credit': format_price(db_user.get('credit', 0)),
        'bot_name': bot_name, 'bot_username': _bot_attr(bot, 'username', ''),
        'brand': (settings.get('start_brand') or bot_name)[:120],
        'tagline': settings.get('start_tagline', START_DEFAULTS['start_tagline'])[:120],
        'support': settings.get('start_support', START_DEFAULTS['start_support'])[:120],
        'support_hours': settings.get('start_support_hours', START_DEFAULTS['start_support_hours'])[:120],
        'services': '\n'.join(services) or 'فعلاً سرویسی فعال نیست؛ برای راهنمایی با پشتیبانی تماس بگیرید.',
        'date': jdatetime.datetime.fromgregorian(datetime=local).strftime('%Y/%m/%d'),
        'time': local.strftime('%H:%M'),
    }


def _render(template, values):
    # Convert trusted template markup BEFORE substituting escaped values. A name
    # containing Markdown, tags or {credit} must never become markup/a new token.
    fragments = []
    prefix = '\ue000WELCOME'
    while prefix in template:
        prefix += 'X'

    def stash(match):
        raw, key = match.group(0), match.group(1)
        value = '{' if raw == '{{' else '}' if raw == '}}' else values.get(key, raw)
        fragments.append(html.escape(str(value), quote=True))
        return f'{prefix}{len(fragments) - 1}\ue001'

    masked = _TOKEN.sub(stash, template)
    body = masked if _HTML.search(masked) else markdown_to_html(masked)
    # A single substitution pass: even a value containing our token cannot recurse.
    return re.sub(re.escape(prefix) + r'(\d+)\ue001', lambda m: fragments[int(m.group(1))], body)


def render_start_message(user, db_user, bot, settings, now=None):
    values = start_values(user, db_user, bot, settings, now)
    template = settings.get('start_text') or DEFAULT_START_TEXT
    rendered = _render(template, values)
    # Bound even legacy/expanded templates. Don't split HTML or a surrogate pair.
    if len(rendered.encode('utf-16-le')) // 2 > 3900:
        logger.warning('Start template exceeded message budget; using standard layout')
        rendered = _render(DEFAULT_START_TEXT, values)
    return rendered
