"""
services/payment_service.py
اصلاح شده برای دریافت تمام جزئیات تراکنش (شماره کارت، RefID، کارمزد)
"""
import logging
import json
import time
import aiohttp
from abc import ABC, abstractmethod
from typing import Tuple, Any, Optional, Dict
from database import DatabaseManager
from config import Config
from constants import GATEWAY_SLUG_AGHAYE_PARDAKHT, GATEWAY_SLUG_ZARINPAL

logger = logging.getLogger(__name__)

# کدهای خطای وب‌سرویس آقای پرداخت (API V2) برای پیام‌های خوانا در لاگ/کاربر.
AP_ERROR_CODES = {
    "-1": "amount نمی‌تواند خالی باشد",
    "-2": "کد پین درگاه نمی‌تواند خالی باشد",
    "-3": "callback نمی‌تواند خالی باشد",
    "-4": "amount باید عددی باشد",
    "-5": "amount باید بین ۱٬۰۰۰ تا ۴۰۰٬۰۰۰٬۰۰۰ تومان باشد",
    "-6": "کد پین درگاه اشتباه است",
    "-7": "transid نمی‌تواند خالی باشد",
    "-8": "تراکنش مورد نظر وجود ندارد",
    "-9": "کد پین درگاه با درگاه تراکنش مطابقت ندارد",
    "-10": "مبلغ با مبلغ تراکنش مطابقت ندارد",
    "-11": "درگاه در انتظار تایید و یا غیرفعال است",
    "-12": "امکان ارسال درخواست برای این پذیرنده وجود ندارد",
    "-13": "شماره کارت باید ۱۶ رقم چسبیده به‌هم باشد",
    "-14": "درگاه بر روی سایت دیگری در حال استفاده است",
    "-15": "آدرس کال‌بک ارسال‌شده با دامنهٔ تاییدشدهٔ درگاه مغایرت دارد",
    "-16": "ارجاع‌دهنده نامعتبر است (Referrer ارسال نشده است)",
    "-17": "مقدار callback_method باید POST یا GET باشد",
    "0": "پرداخت انجام نشد",
}


def _payment_proxy() -> Optional[str]:
    """آدرس پروکسی HTTP برای درخواست‌های درگاه پرداخت (بدون WARP).

    درگاه‌های ایرانی اتصال از IP خارجیِ WARP را نمی‌پذیرند؛ این پروکسی از IP
    ایرانیِ هاست عبور می‌کند. None یعنی بدون پروکسی (مستقیم).
    """
    return getattr(Config, "PAYMENT_HTTP_PROXY", None)

class BasePaymentGateway(ABC):
    def __init__(self, slug: str, name: str):
        self.slug = slug
        self.name = name
    
    @abstractmethod
    async def create_payment_link(self, user_id: int, amount: int, mobile: Optional[str], email: Optional[str], config: dict) -> Tuple[bool, str, Optional[str]]:
        pass
        
    @abstractmethod
    async def verify_payment(self, verification_data: dict, config: dict) -> Tuple[bool, Dict[str, Any]]:
        """
        Return: (Success, Data_Dict)
        Data_Dict must contain: 'ref_id', 'card_pan', 'fee'
        """
        pass

class AghayePardakhtGateway(BasePaymentGateway):
    # وب‌سرویس آقای پرداخت (API V2). مبلغ‌ها بر پایهٔ «تومان» هستند.
    API_URL_REQUEST = "https://panel.aqayepardakht.ir/api/v2/create"
    API_URL_VERIFY = "https://panel.aqayepardakht.ir/api/v2/verify"
    START_PAY_URL = "https://panel.aqayepardakht.ir/startpay/"
    START_PAY_SANDBOX_URL = "https://panel.aqayepardakht.ir/startpay/sandbox/"

    def __init__(self):
        super().__init__(GATEWAY_SLUG_AGHAYE_PARDAKHT, "آقای پرداخت")

    @staticmethod
    def _ap_error(data: dict) -> str:
        """پیام خطای خوانا از پاسخ آقای پرداخت (بر اساس code)."""
        code = str(data.get('code', '')).strip()
        return AP_ERROR_CODES.get(code, f"کد خطا: {code or 'نامشخص'}")

    async def create_payment_link(self, user_id: int, amount: int, mobile: Optional[str], email: Optional[str], config: dict) -> Tuple[bool, str, Optional[str]]:
        # مبلغ در آقای پرداخت «تومان» است؛ مبلغ داخلی هم تومان است ⇒ بدون تبدیل.
        pin = config.get('pin', 'sandbox')
        is_sandbox = str(pin).lower() == 'sandbox'
        callback_url = Config.AGHAYE_PARDAKHT_CALLBACK_URL
        final_mobile = str(mobile) if mobile else ""
        invoice_id = f"{user_id}-{int(time.time())}"

        payload = {
            "pin": pin,
            "amount": amount,  # تومان
            "callback": f"{callback_url}?user_id={user_id}",
            # بازگشت به سایت با GET (توصیهٔ خودِ مستندات) تا هندلر ساده‌تر و سازگارتر شود.
            "callback_method": "GET",
            "invoice_id": invoice_id,
            "mobile": final_mobile,
            "email": email or "",
            "description": f"شارژ کیف پول کاربر {user_id}",
        }

        logger.info(
            f"AghayePardakht create: amount={amount} تومان, sandbox={is_sandbox}, "
            f"callback={payload['callback']}, proxy={_payment_proxy() or 'direct'}"
        )
        if "localhost" in payload['callback'].lower() or "127.0.0.1" in payload['callback'].lower():
            logger.warning(
                "⚠️ callback آقای پرداخت روی localhost است؛ کاربر پس از پرداخت به سرور "
                "بازنمی‌گردد. متغیر محیطی SERVER_URL را به آدرس عمومی سرور تنظیم کنید."
            )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL_REQUEST, data=payload, timeout=15, proxy=_payment_proxy()) as response:
                    data = await response.json(content_type=None)
                    if data.get('status') == 'success' and data.get('transid'):
                        trans_id = str(data['transid'])
                        base = self.START_PAY_SANDBOX_URL if is_sandbox else self.START_PAY_URL
                        link = f"{base}{trans_id}"
                        return True, link, trans_id
                    err = self._ap_error(data)
                    logger.error(f"AghayePardakht Create Error: {err} | raw={data}")
                    return False, f"خطای درگاه: {err}", None
        except Exception as e:
            logger.error(f"AP Create Error: {e}")
            return False, "خطا در اتصال به درگاه.", None

    async def verify_payment(self, verification_data: dict, config: dict) -> Tuple[bool, Dict[str, Any]]:
        # مبلغ در وریفای هم «تومان» است ⇒ بدون تبدیل.
        pin = config.get('pin', 'sandbox')
        trans_id = verification_data.get('trans_id')
        amount = verification_data.get('amount')
        card_pan = verification_data.get('card_pan') or '---'
        tracking_number = verification_data.get('tracking_number')

        payload = {"pin": pin, "transid": trans_id, "amount": amount}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL_VERIFY, data=payload, timeout=15, proxy=_payment_proxy()) as response:
                    data = await response.json(content_type=None)
                    code = str(data.get('code', '')).strip()
                    # طبق مستندات: code=1 موفق، code=2 «قبلاً وریفای و پرداخت شده»
                    # (این هم موفق است و نباید تراکنش را ناموفق بزنیم).
                    if data.get('status') == 'success' and code in ('1', '2'):
                        return True, {
                            "ref_id": tracking_number or trans_id,
                            "card_pan": card_pan,
                            "already_verified": code == '2',
                            "fee": 0,
                        }
                    err = self._ap_error(data)
                    logger.error(f"AghayePardakht Verify Error: {err} | raw={data}")
                    return False, {"error": err}
        except Exception as e:
            logger.error(f"AP Verify Error: {e}")
            return False, {"error": str(e)}

class ZarinPalGateway(BasePaymentGateway):
    API_URL_REQUEST = "https://payment.zarinpal.com/pg/v4/payment/request.json"
    API_URL_VERIFY = "https://payment.zarinpal.com/pg/v4/payment/verify.json"
    START_PAY_URL = "https://payment.zarinpal.com/pg/StartPay/"
    
    def __init__(self):
        super().__init__(GATEWAY_SLUG_ZARINPAL, "زرین‌پال")
        
    async def create_payment_link(self, user_id: int, amount: int, mobile: Optional[str], email: Optional[str], config: dict) -> Tuple[bool, str, Optional[str]]:
        merchant_id = config.get('merchant_id', Config.ZARINPAL_MERCHANT_ID)
        callback_url = Config.ZARINPAL_CALLBACK_URL
        amount_rial = amount * 10 
        
        full_callback = f"{callback_url}?user_id={user_id}"
        # لاگِ آدرس بازگشت تا در صورت مشکلِ «بازنگشتن به ربات» به‌راحتی قابل بررسی باشد.
        logger.info(
            f"ZarinPal create: amount={amount} تومان ({amount_rial} ریال), "
            f"callback_url={full_callback}, proxy={_payment_proxy() or 'direct'}"
        )
        if "localhost" in full_callback.lower() or "127.0.0.1" in full_callback.lower():
            logger.warning(
                "⚠️ callback_url زرین‌پال روی localhost است؛ کاربر پس از پرداخت به "
                "سرور بازنمی‌گردد. متغیر محیطی SERVER_URL را به آدرس عمومی سرور تنظیم کنید."
            )

        metadata = {}
        if mobile: metadata["mobile"] = mobile
        if email: metadata["email"] = email

        payload = {
            "merchant_id": merchant_id,
            "amount": amount_rial,
            "currency": "IRR", 
            "description": f"شارژ کیف پول کاربر {user_id}",
            "callback_url": full_callback,
            "metadata": metadata
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL_REQUEST, json=payload, timeout=15, proxy=_payment_proxy()) as response:
                    data = await response.json()
                    if response.status == 200 and data.get('data', {}).get('code') == 100:
                        authority = data['data']['authority']
                        link = f"{self.START_PAY_URL}{authority}"
                        return True, link, authority
                    else:
                        errors = data.get('errors', [])
                        logger.error(f"ZarinPal Create Error: {errors}")
                        return False, f"خطا درگاه: {errors}", None
        except Exception as e:
            logger.error(f"ZarinPal Connection Error: {e}")
            return False, "خطا در اتصال.", None

    async def verify_payment(self, verification_data: dict, config: dict) -> Tuple[bool, Dict[str, Any]]:
        merchant_id = config.get('merchant_id', Config.ZARINPAL_MERCHANT_ID)
        authority = verification_data.get('authority')
        # مبلغ به تومان از سرویس دریافت و به ریال تبدیل می‌شود
        amount_rial = verification_data.get('amount', 0) * 10

        payload = {
            "merchant_id": merchant_id,
            "amount": amount_rial,
            "authority": authority
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL_VERIFY, json=payload, timeout=15, proxy=_payment_proxy()) as response:
                    data = await response.json()
                    
                    # بررسی موفقیت آمیز بودن تراکنش
                    if response.status == 200 and data.get('data') and data.get('data', {}).get('code') in [100, 101]:
                        resp_data = data['data']
                        return True, {
                            "ref_id": resp_data.get('ref_id'),
                            "card_pan": resp_data.get('card_pan', '---'),
                            "fee": resp_data.get('fee', 0),
                            "fee_type": resp_data.get('fee_type', 'Unknown'),
                            "card_hash": resp_data.get('card_hash', '')
                        }
                    
                    # در صورت خطا، استخراج پیام دقیق از پاسخ درگاه
                    error_block = data.get('errors', {})
                    if not isinstance(error_block, dict): error_block = {}
                    error_code = error_block.get('code', 'N/A')
                    error_message = error_block.get('message', 'خطای نامشخص از درگاه')
                    
                    logger.error(f"ZarinPal Verify Failed. Code: {error_code}, Message: {error_message}, Full Response: {data}")
                    return False, {"error": f"کد خطا: {error_code} - {error_message}"}
        except Exception as e:
            logger.error(f"ZarinPal Verify Connection Error: {e}", exc_info=True)
            return False, {"error": f"خطا در ارتباط با درگاه: {str(e)}"}

class PaymentService:
    def __init__(self):
        self.gateways = {
            GATEWAY_SLUG_AGHAYE_PARDAKHT: AghayePardakhtGateway(),
            GATEWAY_SLUG_ZARINPAL: ZarinPalGateway(),
        }

    async def create_payment_link(self, user_id: int, amount: int, mobile: Optional[str], email: Optional[str] = None, bot_id: int = 1) -> Tuple[bool, str]:
        active_gw_db = await DatabaseManager.get_active_gateway(bot_id=bot_id)
        if not active_gw_db:
            return False, "درگاه پرداخت برای این ربات فعال نیست."
            
        slug = active_gw_db['slug']
        gateway = self.gateways.get(slug)
        
        if not gateway: return False, "Gateway Error"
        
        config = {}
        if active_gw_db.get('config_json'):
            try: config = json.loads(active_gw_db['config_json'])
            except: pass
            
        success, link, trans_id = await gateway.create_payment_link(user_id, amount, mobile, email, config)
        
        if success and trans_id:
            # لینک واقعی درگاه (link) را ذخیره می‌کنیم و به‌جای آن، آدرس صفحهٔ
            # میانیِ خودمان را به کاربر می‌دهیم. این صفحه (روی دامنهٔ اصلی) کاربر
            # را به درگاه هدایت می‌کند تا Referrer با دامنهٔ اصلی تطابق داشته باشد
            # (الزام شاپرک برای پرداخت از طریق بات‌ها).
            await DatabaseManager.create_payment_transaction(
                user_id, amount, trans_id, slug, bot_id=bot_id, pay_url=link
            )
            intermediate_url = f"{Config.SERVER_URL}/pay/{trans_id}"
            return True, intermediate_url
        elif success and not trans_id:
            return False, "خطای داخلی: شناسه تراکنش دریافت نشد."
            
        return False, link

    async def verify_payment(self, trans_id: str, amount: int, gateway_slug: str, bot_id: int = 1, extra: Optional[dict] = None) -> Tuple[bool, Dict[str, Any]]:
        gateway = self.gateways.get(gateway_slug)
        if not gateway: return False, {"error": "Gateway not found"}
            
        gw_db = await DatabaseManager.get_gateway(gateway_slug, bot_id=bot_id)
        config = {}
        if gw_db and gw_db.get('config_json'):
            try: config = json.loads(gw_db['config_json'])
            except: pass

        verification_data = {
            "authority": trans_id,
            "trans_id": trans_id,
            "amount": amount 
        }
        # اطلاعات تکمیلیِ کال‌بک (مثل شماره کارت و شماره پیگیریِ آقای پرداخت که
        # در پاسخِ وریفای وجود ندارند و باید از خودِ کال‌بک منتقل شوند).
        if extra:
            verification_data.update(extra)

        # بازگرداندن دیکشنری کامل اطلاعات
        return await gateway.verify_payment(verification_data, config)

payment_service = PaymentService()