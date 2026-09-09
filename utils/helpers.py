"""
utils/helpers.py
توابع کمکی برای تبدیل زمان، فرمت‌دهی و تولید تقویم شمسی
"""
import re
from datetime import datetime, timedelta
import jdatetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def clean_number(text):
    """تبدیل اعداد فارسی/عربی به انگلیسی و حذف کاراکترهای اضافه"""
    if not text: return ""
    text = str(text)
    persian_digits = "۰１２３４５６７８９"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    english_digits = "0123456789"
    
    trans_table = str.maketrans(persian_digits + arabic_digits, english_digits * 2)
    cleaned = text.translate(trans_table)
    # برای ساعت (:) و تاریخ (/) را نگه دار، بقیه را پاک کن اگر لازم بود (اینجا ساده برمی‌گردانیم)
    return cleaned.strip()

def get_tehran_time():
    """دریافت زمان فعلی به وقت تهران (UTC+3:30)"""
    return datetime.utcnow() + timedelta(hours=3, minutes=30)

def get_jalali_month_name(month_index):
    """دریافت نام ماه شمسی"""
    months = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
    try:
        return months[int(month_index) - 1]
    except:
        return ""

def format_jalali_datetime(dt_obj):
    """تبدیل تاریخ میلادی به شمسی با ساعت دقیق ایران"""
    if not dt_obj: return "---"
    
    # تبدیل به وقت تهران
    tehran_dt = dt_obj + timedelta(hours=3, minutes=30)
    
    j_date = jdatetime.date.fromgregorian(date=tehran_dt.date())
    time_str = tehran_dt.strftime("%H:%M")
    
    month_name = get_jalali_month_name(j_date.month)
    
    return f"{j_date.day} {month_name} {j_date.year}، ساعت {time_str}"

def format_price(amount):
    """فرمت‌دهی قیمت به تومان"""
    try:
        return f"{int(amount):,}"
    except:
        return str(amount)

def generate_jalali_calendar(year=None, month=None):
    """
    تولید کیبورد شیشه‌ای تقویم شمسی
    - روزهای گذشته غیرفعال می‌شوند.
    - قابلیت ورق زدن ماه‌ها.
    """
    now_tehran = datetime.utcnow() + timedelta(hours=3, minutes=30)
    now_j = jdatetime.datetime.fromgregorian(datetime=now_tehran)
    
    if year is None: year = now_j.year
    if month is None: month = now_j.month
    
    # تنظیم تاریخ اول ماه انتخابی
    current_j_date = jdatetime.date(year, month, 1)
    first_day_weekday = current_j_date.weekday() # 0=Shanbe, ..., 6=Jome
    
    # تعداد روزهای ماه
    if month <= 6: days_in_month = 31
    elif month <= 11: days_in_month = 30
    else: days_in_month = 29 if not current_j_date.isleap() else 30
    
    keyboard = []
    
    # هدر: نام ماه و سال
    month_name = get_jalali_month_name(month)
    keyboard.append([InlineKeyboardButton(f"{month_name} {year}", callback_data="ignore")])
    
    # روزهای هفته
    week_days = ["ش", "ی", "د", "س", "چ", "پ", "ج"]
    keyboard.append([InlineKeyboardButton(day, callback_data="ignore") for day in week_days])
    
    row = []
    # خانه‌های خالی قبل از شروع ماه
    for _ in range(first_day_weekday):
        row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        
    for day in range(1, days_in_month + 1):
        # بررسی اینکه آیا روز گذشته است؟
        is_past = False
        if year < now_j.year:
            is_past = True
        elif year == now_j.year and month < now_j.month:
            is_past = True
        elif year == now_j.year and month == now_j.month and day < now_j.day:
            is_past = True
            
        if is_past:
            # نمایش به صورت ضربدر یا غیرفعال (دکمه ignore)
            row.append(InlineKeyboardButton("❌", callback_data="ignore"))
        else:
            # فرمت دیتای بازگشتی: cal_selected_YEAR_MONTH_DAY
            row.append(InlineKeyboardButton(str(day), callback_data=f"cal_sel_{year}_{month}_{day}"))
            
        if len(row) == 7:
            keyboard.append(row)
            row = []
            
    if row:
        while len(row) < 7:
            row.append(InlineKeyboardButton(" ", callback_data="ignore"))
        keyboard.append(row)
        
    # دکمه‌های نویگیشن (ماه قبل / ماه بعد)
    nav_row = []
    
    # ماه قبل
    prev_year, prev_month = (year, month - 1) if month > 1 else (year - 1, 12)
    # اگر ماه قبل کلاً در گذشته است، دکمه‌اش را نشان نده (یا غیرفعال کن)
    # ساده‌ترین منطق: اگر ماه فعلی همان ماه جاری است، دکمه قبل را غیرفعال کن
    if year == now_j.year and month == now_j.month:
        nav_row.append(InlineKeyboardButton("⛔️", callback_data="ignore"))
    else:
        nav_row.append(InlineKeyboardButton("◀️ ماه قبل", callback_data=f"cal_nav_{prev_year}_{prev_month}"))
        
    # ماه بعد
    next_year, next_month = (year, month + 1) if month < 12 else (year + 1, 1)
    nav_row.append(InlineKeyboardButton("ماه بعد ▶️", callback_data=f"cal_nav_{next_year}_{next_month}"))
    
    keyboard.append(nav_row)
    
    return InlineKeyboardMarkup(keyboard)