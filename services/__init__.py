"""
پکیج services — importهای lazy برای جلوگیری از circular import.
از Python 3.7+ پشتیبانی می‌کند (PEP 562: module __getattr__).
"""

__all__ = [
    "order_executor",
    "order_executor_class",
    "voice_call_manager",
    "VoiceCallManager",
    "health_checker_service",
    "AccountManager",
    "backup_manager",
    "BackupManager",
]

def __getattr__(name: str):
    # lazy-import to avoid circular imports
    if name == "order_executor":
        from .order_executor import order_executor
        return order_executor
    if name == "order_executor_class" or name == "OrderExecutor":
        from .order_executor import OrderExecutor
        return OrderExecutor
    if name == "voice_call_manager":
        from .voice_call_manager import voice_call_manager
        return voice_call_manager
    if name == "VoiceCallManager":
        from .voice_call_manager import VoiceCallManager
        return VoiceCallManager
    if name == "health_checker_service":
        from .health_checker import health_checker_service
        return health_checker_service
    if name == "AccountManager":
        from .account_manager import AccountManager
        return AccountManager
    if name == "backup_manager":
        from .backup_manager import backup_manager
        return backup_manager
    if name == "BackupManager":
        from .backup_manager import BackupManager
        return BackupManager
    raise AttributeError(f"module {__name__} has no attribute {name}")