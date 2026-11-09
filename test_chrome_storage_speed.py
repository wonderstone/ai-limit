"""Chrome Local Storage reads stay fast enough for the OpenAI credit probe.

Regression: the pure-Python snappy decoder and per-edit namedtuple classes made
one read take ~26s, so every refresh failed with browser_storage_limit and the
menu bar kept an old OpenAI promo balance.
"""
import io
import json
import pathlib
import struct
import tempfile
import types
import unittest
from unittest import mock

from ai_limit import openai_platform as p
from ai_limit._vendor import ccl_leveldb as db, ccl_simplesnappy as snappy


def _varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def _block(entries):
    body = b"".join(_varint(0) + _varint(len(k)) + _varint(len(v)) + k + v for k, v in entries)
    return body + struct.pack("<I", 0) + struct.pack("<I", 1)


def _internal(user_key, seq, live=True):
    return user_key + struct.pack("<Q", (seq << 8) | (1 if live else 0))


def _write_table(path, blocks):
    """Uncompressed leveldb table; each block is a list of (internal key, value)."""
    data, index = bytearray(), []
    for entries in blocks:
        raw = _block(entries)
        index.append((entries[-1][0], _varint(len(data)) + _varint(len(raw))))
        data += raw + b"\x00" + b"\x00" * 4
    meta = _block([])
    meta_handle = _varint(len(data)) + _varint(len(meta))
    data += meta + b"\x00" * 5
    idx = _block(index)
    idx_handle = _varint(len(data)) + _varint(len(idx))
    data += idx + b"\x00" * 5
    footer = (meta_handle + idx_handle).ljust(40, b"\x00") + struct.pack("<Q", db.LdbFile.MAGIC)
    path.write_bytes(bytes(data + footer))


class SnappyTests(unittest.TestCase):
    def test_literals_and_copies(self):
        # "abcd" literal, then a 1-byte-offset overlapping copy (len 8, offset 4),
        # then a 2-byte-offset copy (len 3, offset 12).
        stream = _varint(15) + bytes([3 << 2]) + b"abcd" + bytes([((8 - 4) << 2) | 1, 4]) + bytes([(2 << 2) | 2, 12, 0])
        self.assertEqual(snappy.decompress(io.BytesIO(stream)), b"abcdabcdabcdabc")

    def test_long_literal_length_byte(self):
        payload = bytes(range(100))
        stream = _varint(100) + bytes([60 << 2, 99]) + payload
        self.assertEqual(snappy.decompress(io.BytesIO(stream)), payload)

    def test_run_length_copy(self):
        stream = _varint(10) + bytes([0]) + b"z" + bytes([((9 - 4) << 2) | 1, 1])
        self.assertEqual(snappy.decompress(io.BytesIO(stream)), b"z" * 10)

    def test_bad_streams_raise(self):
        for stream in (
            _varint(4) + bytes([1, 1]),                    # copy before any output
            _varint(3) + bytes([3 << 2]) + b"abcd",         # literal past declared size
            _varint(4) + bytes([3 << 2]) + b"ab",           # truncated literal
            _varint(5) + bytes([3 << 2]) + b"abcd",         # short output
            _varint(8) + bytes([3 << 2]) + b"abcd" + bytes([2]),  # truncated copy
        ):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                snappy.decompress(io.BytesIO(stream))


class TableFilterTests(unittest.TestCase):
    def test_filtered_read_matches_full_scan(self):
        blocks = [
            [(_internal(b"a", 1), b"1"), (_internal(b"c", 2), b"2")],
            [(_internal(b"k", 9), b"new"), (_internal(b"k", 3), b"old")],
            [(_internal(b"k", 2), b"older"), (_internal(b"m", 4), b"4")],
            [(_internal(b"x", 5), b"5")],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "000007.ldb"
            _write_table(path, blocks)
            table = db.LdbFile(path)
            try:
                full = [(r.user_key, r.seq, r.value) for r in table]
                for targets in ({b"k"}, {b"a", b"x"}, {b"b", b"zz"}, {b"c", b"m"}):
                    with self.subTest(targets=targets):
                        got = [(r.user_key, r.seq, r.value) for r in table.iter_user_keys(targets)]
                        self.assertEqual(got, [row for row in full if row[0] in targets])
            finally:
                table.close()


class TableMemoTests(unittest.TestCase):
    def tearDown(self):
        p._TABLE_MEMO.clear()

    def test_unchanged_table_is_parsed_once(self):
        prefix = b"_https://platform.openai.com\x00\x01"
        auth = b"\x01" + json.dumps({"accessToken": "tok"}).encode("latin1")
        org = b'\x01"org-x"'
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            store = root / "Local Storage" / "leveldb"
            store.mkdir(parents=True)
            (store / "CURRENT").write_text("MANIFEST-000001")
            (store / "MANIFEST-000001").touch()
            _write_table(store / "000005.ldb", [[(_internal(prefix + b"oai-auth:onboard", 4), auth),
                                                 (_internal(prefix + b"oai/activeOrg", 5), org)]])
            edit = types.SimpleNamespace(deleted_files=[], new_files=[types.SimpleNamespace(file_no=5)],
                                         log_number=10, prev_log_number=0)

            def manifest(*_args):
                reader = mock.MagicMock()
                reader.__iter__.return_value = iter([edit])
                reader.damage = []
                return reader

            with mock.patch.object(p.providers, "_chromium_profile_cookie_files", return_value=[("Chosen", root / "Cookies")]), \
                    mock.patch.object(p.ccl_leveldb, "ManifestFile", side_effect=manifest), \
                    mock.patch.object(db.LdbFile, "iter_user_keys", autospec=True,
                                      side_effect=db.LdbFile.iter_user_keys) as table:
                self.assertEqual(p.read_platform_session("Chosen"), ("tok", "org-x"))
                self.assertEqual(p.read_platform_session("Chosen"), ("tok", "org-x"))
            self.assertEqual(table.call_count, 1)


if __name__ == "__main__":
    unittest.main()
