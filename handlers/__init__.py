"""
Handlers Package
"""
# به جای ایمپورت تک تک توابع، فقط ماژول‌ها را ایمپورت می‌کنیم تا از چرخه جلوگیری شود
# یا اگر نیاز به توابع است، باید مطمئن شویم که هیچ وابستگی چرخشی در سطح بالا وجود ندارد.

# فعلاً برای حل مشکل شما، این فایل را خالی یا ساده نگه می‌داریم و ایمپورت‌ها را در main.py مستقیم انجام می‌دهیم.
# اما اگر می‌خواهید از ساختار فعلی استفاده کنید، این نسخه ایمن است:

from handlers import general_handlers
from handlers import admin_handlers
from handlers import order_handlers
from handlers import wallet_handlers
from handlers import profile_handlers
from handlers import account_management
from handlers import menu_handlers
from handlers import kyc_handlers

__all__ = [
    'general_handlers',
    'admin_handlers',
    'order_handlers',
    'wallet_handlers',
    'profile_handlers',
    'account_management',
    'menu_handlers',
    'kyc_handlers'
]