import unittest
from unittest import mock
from ai_limit import keychain_status


class KeychainStatusTests(unittest.TestCase):
    def probe(self, flags=1, copy_status=0, status_code=0):
        security = mock.MagicMock()
        core = mock.MagicMock()

        def copy(out):
            if copy_status == 0:
                out._obj.value = 123
            return copy_status

        def status(key, out):
            out._obj.value = flags
            return status_code

        security.SecKeychainCopyDefault.side_effect = copy
        security.SecKeychainGetStatus.side_effect = status
        with mock.patch.object(keychain_status.sys, "platform", "darwin"), \
                mock.patch.object(keychain_status.ctypes.util, "find_library", side_effect=["Security", "CoreFoundation"]), \
                mock.patch.object(keychain_status.ctypes, "CDLL", side_effect=[security, core]):
            result = keychain_status.default_keychain_locked()
        self.assertEqual(core.CFRelease.call_count, int(copy_status == 0))
        return result

    def test_locked_unlocked_and_unknown_are_distinct(self):
        self.assertFalse(self.probe(flags=7))
        self.assertTrue(self.probe(flags=6))
        self.assertIsNone(self.probe(copy_status=-1))
        self.assertIsNone(self.probe(status_code=-1))

    def test_unsupported_platform_is_unknown(self):
        with mock.patch.object(keychain_status.sys, "platform", "linux"), \
                mock.patch.object(keychain_status.ctypes, "CDLL") as load:
            self.assertIsNone(keychain_status.default_keychain_locked())
            load.assert_not_called()

    def test_missing_framework_is_unknown(self):
        with mock.patch.object(keychain_status.sys, "platform", "darwin"), \
                mock.patch.object(keychain_status.ctypes.util, "find_library", return_value=None):
            self.assertIsNone(keychain_status.default_keychain_locked())
