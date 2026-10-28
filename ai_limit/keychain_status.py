"""Read lock status without retrieving secrets or prompting for access."""
from __future__ import annotations

import ctypes
import ctypes.util
import sys


def default_keychain_locked() -> bool | None:
    """Return a verified lock state, or None when macOS cannot report it.

    An unlocked default keychain does not prove access to a particular item.
    Cookie decryption errors alone never establish a locked keychain.
    """
    if sys.platform != "darwin":
        return None
    keychain = ctypes.c_void_p()
    core = None
    try:
        security_path = ctypes.util.find_library("Security")
        core_path = ctypes.util.find_library("CoreFoundation")
        if not security_path or not core_path:
            return None
        security = ctypes.CDLL(security_path)
        core = ctypes.CDLL(core_path)
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease.restype = None
        security.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        security.SecKeychainCopyDefault.restype = ctypes.c_int32
        security.SecKeychainGetStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        security.SecKeychainGetStatus.restype = ctypes.c_int32
        if security.SecKeychainCopyDefault(ctypes.byref(keychain)) != 0 or not keychain.value:
            return None
        status = ctypes.c_uint32()
        if security.SecKeychainGetStatus(keychain, ctypes.byref(status)) != 0:
            return None
        return not bool(status.value & 1)  # kSecUnlockStateStatus
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    finally:
        if keychain.value and core is not None:
            core.CFRelease(keychain)
