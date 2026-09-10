# -*- coding: utf-8 -*-
"""Startup shim: guarantee every pyrogram error alias is importable.

Legacy code (and old py-tgcalls examples) sometimes imports
``GroupCallInvalid`` / ``GroupCallForbidden`` directly from
``pyrogram.errors``, but Pyrogram only ships ``GroupCallInvalid`` — so on
any version where the alias is missing that import raises ImportError and
the whole voice stack dies at client init.  Python imports this module
automatically at startup (sitecustomize), so the aliases exist before any
application code runs — no app file needs to change.
"""
try:
    import pyrogram.errors as _errors
    _BASE = (getattr(_errors, "GroupCallInvalid", None)
             or getattr(_errors, "RPCError", None)
             or Exception)
    for _alias in ("GroupCallInvalid", "GroupCallForbidden"):
        if not hasattr(_errors, _alias):
            setattr(_errors, _alias, _BASE)
except Exception:
    pass
