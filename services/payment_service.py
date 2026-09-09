"""
services/payment_service.py
اصلاح شده برای دریافت تمام جزئیات تراکنش (شماره کارت، RefID، کارمزد)
"""
import logging
import json
import aiohttp
from abc import ABC, abstractmethod
from typing import Tuple, Any, Optional, Dict
from database import DatabaseManager
from config import Config
from constants import GATEWAY_SLUG_AGHAYE_PARDAKHT, GATEWAY_SLUG_ZARINPAL

logger = logging.getLogger(__name__)

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
    API_URL_REQUEST = "https://panel.aqayepardakht.ir/api/v2/create"
    API_URL_VERIFY = "https://panel.aqayepardakht.ir/api/v2/verify"
    
    def __init__(self):
        super().__init__(GATEWAY_SLUG_AGHAYE_PARDAKHT, "آقای پرداخت")
    
    async def create_payment_link(self, user_id: int, amount: int, mobile: Optional[str], email: Optional[str], config: dict) -> Tuple[bool, str, Optional[str]]:
        api_key = config.get('pin', 'sandbox')
        callback_url = Config.AGHAYE_PARDAKHT_CALLBACK_URL 
        amount_rial = amount * 10
        final_mobile = str(mobile) if mobile else ""
        invoice_id = f"{user_id}-{int(aiohttp.helpers.time.time())}"
        
        payload = {
            "key": api_key,
            "amount": amount_rial,
            "callback_url": f"{callback_url}?user_id={user_id}",
            "invoice_id": invoice_id,
            "mobile": final_mobile,
            "email": email or ""
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL_REQUEST, json=payload, timeout=15) as response:
                    data = await response.json()
                    if response.status == 200 and data.get('status') == 'success':
                        trans_id = data.get('transid') 
                        link = data.get('payment_link')
                        if not trans_id and link:
                            trans_id = link.split("/")[-1]
                        return True, link, trans_id
                    else:
                        return False, f"Error: {data}", None
        except Exception as e:
            logger.error(f"AP Create Error: {e}")
            return False, "خطا در اتصال به درگاه.", None

    async def verify_payment(self, verification_data: dict, config: dict) -> Tuple[bool, Dict[str, Any]]:
        api_key = config.get('pin', 'sandbox')
        trans_id = verification_data.get('trans_id')
        amount_rial = verification_data.get('amount') * 10
        
        payload = {"key": api_key, "transid": trans_id, "amount": amount_rial}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL_VERIFY, json=payload, timeout=15) as response:
                    data = await response.json()
                    if response.status == 200 and str(data.get('code')) == '1':
                        # آقای پرداخت معمولا کارت را برمی‌گرداند اگر بانک ساپورت کند
                        return True, {
                            "ref_id": trans_id,
                            "card_pan": data.get('card_number', '---'),
                            "fee": 0
                        }
                    return False, {"error": data.get('text', 'Failed')}
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
        
        metadata = {}
        if mobile: metadata["mobile"] = mobile
        if email: metadata["email"] = email

        payload = {
            "merchant_id": merchant_id,
            "amount": amount_rial,
            "currency": "IRR", 
            "description": f"شارژ کیف پول کاربر {user_id}",
            "callback_url": f"{callback_url}?user_id={user_id}",
            "metadata": metadata
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.API_URL_REQUEST, json=payload, timeout=15) as response:
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
                async with session.post(self.API_URL_VERIFY, json=payload, timeout=15) as response:
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
            await DatabaseManager.create_payment_transaction(user_id, amount, trans_id, slug, bot_id=bot_id)
            return True, link
        elif success and not trans_id:
            return False, "خطای داخلی: شناسه تراکنش دریافت نشد."
            
        return False, link

    async def verify_payment(self, trans_id: str, amount: int, gateway_slug: str, bot_id: int = 1) -> Tuple[bool, Dict[str, Any]]:
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
        
        # بازگرداندن دیکشنری کامل اطلاعات
        return await gateway.verify_payment(verification_data, config)

payment_service = PaymentService()