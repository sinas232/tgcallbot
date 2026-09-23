"""
constants.py
شامل تمامی وضعیت‌های ConversationHandler و متن دکمه‌ها
"""

# نسخهٔ جاری ربات (برای لاگ استارت، پنل ادمین و Release گیت‌هاب)
BOT_VERSION = "2.3.13"

GATEWAY_SLUG_AGHAYE_PARDAKHT = "aqayepardakht"
GATEWAY_SLUG_ZARINPAL = "zarinpal"

# ===================== STATES =====================

AWAITING_PHONE_NUMBER = 0
AWAITING_CODE = 1
AWAITING_PASSWORD = 2
AWAITING_SESSION_STRING = 3
AWAITING_SESSION_API_ID = 4
AWAITING_SESSION_API_HASH = 5

AWAITING_SELECT_PLAN = 10
AWAITING_ORDER_LINK = 11
AWAITING_ORDER_CONFIRMATION = 12
AWAITING_ORDER_TIMING_TYPE = 13
AWAITING_SCHEDULE_DATE = 14
AWAITING_SCHEDULE_TIME = 15

AWAITING_PLAN_NAME = 20
AWAITING_PLAN_DESC = 21
AWAITING_PLAN_TYPE = 22
AWAITING_PLAN_COUNT = 23
AWAITING_PLAN_DURATION = 24
AWAITING_PLAN_PRICE = 25
AWAITING_PLAN_DELETE = 26
AWAITING_PLAN_DELETE_INDEX = 27
AWAITING_PLAN_EDIT_INDEX = 28
AWAITING_PLAN_EDIT_SELECT = 29
AWAITING_PLAN_EDIT_VALUE = 30

AWAITING_USER_SEARCH = 35
AWAITING_USER_AMOUNT = 36
AWAITING_ADD_ADMIN = 37
AWAITING_REMOVE_ADMIN = 38
AWAITING_ORDER_USER_ID = 39

AWAITING_SUPPORT_TEXT = 40
AWAITING_ADMIN_REPLY = 41
AWAITING_SUPPORT_MESSAGE = 42

AWAITING_SELECT_ACCOUNT_FOR_PROFILE = 50
AWAITING_PROFILE_ACTION = 51
AWAITING_NEW_NAME = 52
AWAITING_NEW_BIO = 53
AWAITING_PROFILE_PHOTO = 54
AWAITING_STORY_MEDIA = 55
AWAITING_NEW_USERNAME = 56
AWAITING_PHOTO_NAVIGATION = 57
AWAITING_NEW_LAST_NAME = 58
AWAITING_STORY_CAPTION = 59

AWAITING_CHARGE_AMOUNT = 60
AWAITING_WALLET_ACTION = 61

AWAITING_SETTINGS_ACTION = 70
AWAITING_ACCOUNT_ID_DELETE = 71
AWAITING_GET_CODE_ACCOUNT = 72
AWAITING_LEAVE_ALL_CONFIRM = 73  # وضعیت جدید برای تایید خروج همگانی
AWAITING_ACCOUNT_MENU = 74  # منوی اصلی مدیریت اکانت‌ها
AWAITING_INCALL_TEXT = 79   # دریافت متن پیام درون ویس‌کال

AWAITING_PRIVACY_CHOICE = 80
AWAITING_PRIVACY_VALUE = 81

AWAITING_PM_ID = 90
AWAITING_PM_MSG = 91
AWAITING_BROADCAST_MSG = 92
AWAITING_BROADCAST_CONFIRM = 93

AWAITING_FORCE_JOIN_LINK = 75
AWAITING_VERIFY_USER_ID = 76
AWAITING_SET_LOG_CHANNEL = 77
AWAITING_SPAM_INTERVAL = 78

AWAITING_GATEWAY_SELECT = 94
AWAITING_GATEWAY_ACTION = 95
AWAITING_GATEWAY_CONFIG_INPUT = 96

AWAITING_KYC_CARD = 100
AWAITING_KYC_VIDEO = 101
AWAITING_KYC_TEXT = 102

AWAITING_STOP_ORDER_INDEX = 150

AWAITING_RESELLER_TOKEN = 160
AWAITING_RESELLER_ADMIN = 161
AWAITING_RESELLER_CHARGE = 162
AWAITING_RESELLER_RENEW_DAYS = 163
AWAITING_RESELLER_API_ID = 164
AWAITING_RESELLER_API_HASH = 165
AWAITING_RESELLER_EDIT_VALUE = 166

# 🔥 وضعیت‌های تیکت
AWAITING_TICKET_MESSAGE = 200
AWAITING_ADMIN_TICKET_REPLY = 201
AWAITING_TICKET_SUBJECT = 202   # انتخاب/نوشتن موضوع تیکت جدید
AWAITING_TICKET_BODY = 203      # نوشتن متن تیکت پس از موضوع

# مدت زمان بی‌فعالیتی برای بستن خودکار تیکت (ساعت)
TICKET_AUTOCLOSE_HOURS = 48

# 💾 وضعیت‌های پشتیبان‌گیری و بازیابی
AWAITING_RESTORE_FILE = 210
AWAITING_BACKUP_CHANNEL = 211
AWAITING_BACKUP_INTERVAL = 212

# 💎 وضعیت‌های ایموجی پریمیوم (جایگزینی شناسهٔ ایموجی توسط ادمین)
AWAITING_PREMIUM_EMOJI_OVERRIDE = 220

# 🛡 وضعیت دریافت مقدار عددی در پنل «ضد اسپم و محافظت» (سوپرادمین)
AWAITING_ANTISPAM_VALUE = 230

# ===================== BUTTONS =====================

BTN_BACK = "🔙 بازگشت"
BTN_CANCEL = "🔙 انصراف"
BTN_BACK_MAIN = "بازگشت به منوی اصلی"
BTN_EXIT_ADMIN = "🔙 خروج از پنل ادمین"
BTN_LEAVE_ALL_CHATS = "🗑 خروج همگانی از چت‌ها" # ✅ اضافه شد
BTN_BACKUP_RESTORE = "💾 پشتیبان‌گیری و بازیابی" # فیچر بازگردانده شده
BTN_PREMIUM_EMOJI = "💎 ایموجی پریمیوم"  # پنل ایموجی پریمیوم (Custom Emoji)

REGEX_BACK = r".*بازگشت.*" 
REGEX_CANCEL = r".*انصراف.*"
REGEX_MAIN_MENU = r".*منوی اصلی.*"
REGEX_EXIT_ADMIN = r".*خروج از پنل.*"

# ===================== MENUS =====================

# ✅ اطمینان از وجود دکمه پشتیبانی برای کاربر
USER_MAIN_MENU = [
    ["🛍 خرید سرویس", "💰 کیف پول من"],
    ["📦 سفارشات من", "🆘 پشتیبانی"]
]
MAIN_MENU = USER_MAIN_MENU 

# ✅ اطمینان از وجود دکمه مدیریت تیکت برای ادمین
ADMIN_MAIN_MENU = [
    ["📩 مدیریت تیکت‌ها", "📢 پیام همگانی"],
    ["👤 مدیریت کاربران", "📉 آمار کل ربات"],
    ["📋 مدیریت پلن‌ها", "⚙️ تنظیمات سیستم"],
    ["👥 مدیریت اکانت‌های ربات", BTN_EXIT_ADMIN]
]

RESELLER_MANAGEMENT_MENU = [
    ["➕ افزودن نماینده جدید", "📋 لیست نمایندگان"],
    [BTN_BACK]
]

ADMIN_SETTINGS_MENU = [
    ["🔒 تنظیمات امنیتی", "💳 مدیریت درگاه پرداخت"], 
    ["📝 تنظیم متن پشتیبانی", "📝 تنظیم متن استارت"],
    ["🆔 تنظیم کانال‌های لاگ", "🆔 متن احراز هویت"],
    ["🤖 مدیریت نمایندگی‌ها", "🩺 تنظیمات بررسی سلامت"],
    [BTN_PREMIUM_EMOJI, "📊 گزارش کلی"],
    [BTN_BACK]
]

SECURITY_SETTINGS_MENU = [
    ["🔗 اجبار حضور در کانال"],
    ["📱 اجبار ارسال شماره", "🇮🇷 اجبار شماره ایرانی"],
    ["🔐 اجبار احراز هویت (KYC)"],
    [BTN_BACK]
]

PLAN_MANAGEMENT_MENU = [
    ["➕ ایجاد پلن جدید", "✏️ ویرایش پلن"],
    ["❌ حذف پلن", "📋 لیست پلن‌ها"],
    [BTN_BACK]
]

PLAN_TYPES_MENU = [["🎙 ویس‌کال", "👥 عضویت گروه"], ["📢 عضویت کانال", BTN_CANCEL]]
USER_MANAGEMENT_MENU = [["🔎 جستجوی کاربر (پیشرفته)", "➕ افزودن ادمین جدید"], ["➖ حذف ادمین", "📋 لیست ادمین‌ها"], [BTN_BACK]]

ACCOUNT_MENU = [
    ["➕ افزودن اکانت (شماره)", "📥 افزودن با سشن (String)"], 
    ["📋 لیست اکانت‌ها", "📩 دریافت کد ورود"], 
    ["🔧 تنظیمات پروفایل", "❌ حذف اکانت"],
    [BTN_LEAVE_ALL_CHATS], # دکمه خروج همگانی
    [BTN_BACK]
]

PROFILE_MENU = [
    ["✏️ تغییر نام", "✏️ تغییر نام خانوادگی"],
    ["🆔 تنظیم یوزرنیم", "📝 تغییر بیوگرافی"],
    ["🖼️ تغییر عکس پروفایل", "📸 مدیریت عکس‌ها (اسلایدر)"],
    ["📹 ارسال استوری (Premium)", "🔒 حریم خصوصی"],
    ["🔙 بازگشت به مدیریت اکانت‌ها"]
]

PRIVACY_MENU = [
    ["👁️ عکس پروفایل", "🕒 آخرین بازدید"],
    ["📞 تماس صوتی", "🔄 فوروارد پیام"],
    ["🔙 بازگشت به منوی پروفایل"]
]

PRIVACY_LEVEL_MENU = [
    ["✅ همه (Everyone)", "👤 مخاطبین (Contacts)"],
    ["🚫 هیچکس (Nobody)", "🔙 بازگشت"]
]
ORDER_MENU = [["🎙 ویس‌کال", "👥 عضویت گروه"], ["📢 عضویت کانال", BTN_BACK_MAIN]]
WALLET_MENU = [["💳 شارژ حساب", "📈 تراکنش‌های اخیر"], [BTN_BACK_MAIN]]

ORDER_TIMING_MENU = [
    ["🚀 شروع آنی (همین الان)"],
    ["📅 زمان‌بندی شده (رزرو آینده)"],
    [BTN_CANCEL]
]

CANCEL_KB = [[BTN_CANCEL]]
BACK_KB = [[BTN_BACK]]
BACK_MAIN_KB = [[BTN_BACK_MAIN]]

SYSTEM_LIMITS = {"min_charge": 10000, "max_charge_amount": 50000000}