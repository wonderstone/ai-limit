"""
Copyright 2020, CCL Forensics

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import sys
import struct
import io
import typing
import enum

__version__ = "0.4"
__description__ = "Pure Python reimplementation of Google's Snappy decompression"
__contact__ = "Alex Caithness"


DEBUG = False
FRAME_MAGIC = bytes.fromhex("73 4E 61 50 70 59")


def make_crc_table(poly):
    table = []
    for i in range(256):
        crc = 0
        for _ in range(8):
            if (i ^ crc) & 1:
                crc = (crc >> 1) ^ poly
            else:
                crc >>= 1
            i >>= 1
        table.append(crc)
    return table


CRC_POLY = 0x82F63B78
CRC_QUICK_TABLE = tuple(make_crc_table(CRC_POLY))


def crc32c(data, xor_value=0xffffffff):
    value = 0xffffffff
    for b in data:
        value = CRC_QUICK_TABLE[(b ^ value) & 0xff] ^ (value >> 8)

    value ^= xor_value
    return value


# def log(msg):
#     if DEBUG:
#         print(msg)


class NoMoreData(Exception):
    ...


class ElementType(enum.IntEnum):
    """Run type in the compressed snappy data (literal data or offset to backreferenced data_"""
    Literal = 0
    CopyOneByte = 1
    CopyTwoByte = 2
    CopyFourByte = 3


def _read_le_varint(stream: typing.BinaryIO) -> typing.Optional[typing.Tuple[int, bytes]]:
    """Read varint from a stream.
    If the read is successful: returns a tuple of the (unsigned) value and the raw bytes making up that varint,
    otherwise returns None"""
    # this only outputs unsigned
    i = 0
    result = 0
    underlying_bytes = []
    while i < 10:  # 64 bit max possible?
        raw = stream.read(1)
        if len(raw) < 1:
            return None
        tmp, = raw
        underlying_bytes.append(tmp)
        result |= ((tmp & 0x7f) << (i * 7))
        if (tmp & 0x80) == 0:
            break
        i += 1
    return result, bytes(underlying_bytes)


def read_le_varint(stream: typing.BinaryIO) -> typing.Optional[int]:
    """Convenience version of _read_le_varint that only returns the value or None"""
    x = _read_le_varint(stream)
    if x is None:
        return None
    else:
        return x[0]


def read_uint16(stream: typing.BinaryIO) -> int:
    """Reads an Uint16 from stream"""
    return struct.unpack("<H", stream.read(2))[0]


def read_uint24(stream: typing.BinaryIO) -> int:
    """Reads an Uint24 from stream"""
    return struct.unpack("<I", stream.read(3) + b"\x00")[0]


def read_uint32(stream: typing.BinaryIO) -> int:
    """Reads an Uint32 from stream"""
    return struct.unpack("<I", stream.read(4))[0]


def read_byte(stream: typing.BinaryIO) -> typing.Optional[int]:
    """Reads a single byte from stream (or returns None if EOD is met)"""
    x = stream.read(1)
    if x:
        return x[0]

    return None


def decompress(data: typing.BinaryIO) -> bytes:
    """Decompresses the snappy compressed data stream.

    Indexes a bytes buffer instead of reading the stream one byte at a time:
    the stream version took ~90s on a 17MB Chrome Local Storage database.
    Same bounds checks as before; consumes the rest of the stream.
    """
    uncompressed_length = read_le_varint(data)
    if uncompressed_length is None or not 0 <= uncompressed_length <= 8 * 1024 * 1024:
        raise ValueError("Snappy block exceeds local read limit")

    src = data.read()
    n = len(src)
    total = uncompressed_length
    out = bytearray(total)
    pos = 0  # bytes written so far
    i = 0
    from_bytes = int.from_bytes
    while i < n:
        type_byte = src[i]
        i += 1
        tag = type_byte & 0x03
        if tag == 0:  # literal
            length = (type_byte >> 2) + 1
            if length > 60:
                extra = length - 60  # 1..4 length bytes
                if i + extra > n:
                    raise ValueError("Couldn't read enough literal data")
                length = from_bytes(src[i:i + extra], "little") + 1
                i += extra
            end = pos + length
            if end > total:
                raise ValueError("Snappy literal exceeds declared output")
            if i + length > n:
                raise ValueError("Couldn't read enough literal data")
            out[pos:end] = src[i:i + length]
            i += length
            pos = end
            continue
        if tag == 1:
            if i >= n:
                raise ValueError("Truncated snappy copy")
            length = ((type_byte >> 2) & 0x07) + 4
            offset = ((type_byte & 0xE0) << 3) | src[i]
            i += 1
        elif tag == 2:
            if i + 2 > n:
                raise ValueError("Truncated snappy copy")
            length = (type_byte >> 2) + 1
            offset = src[i] | (src[i + 1] << 8)
            i += 2
        else:
            if i + 4 > n:
                raise ValueError("Truncated snappy copy")
            length = (type_byte >> 2) + 1
            offset = from_bytes(src[i:i + 4], "little")
            i += 4
        end = pos + length
        if end > total:
            raise ValueError("Snappy copy exceeds declared output")
        if offset == 0:
            raise ValueError("Offset cannot be 0")
        start = pos - offset
        if start < 0:
            raise ValueError("Snappy copy offset before start of output")
        if offset >= length:
            out[pos:end] = out[start:start + length]
        else:
            # Overlapping copy repeats the last `offset` bytes.
            out[pos:end] = (out[start:pos] * (length // offset + 1))[:length]
        pos = end

    if pos != total:
        raise ValueError("Wrong data length in uncompressed data")
    return bytes(out)


def check_masked_crc(crc, data, xor_value=0xffffffff):
    check = crc32c(data, xor_value=xor_value)

    check = ((check >> 15) | (check << 17)) & 0xffffffff  # rotate
    check += 0xa282ead8  # add constant
    check %= 0x100000000  # wraparound as an uint32

    return crc == check


def read_frame(frame_stream: typing.BinaryIO):
    frame_header = frame_stream.read(4)
    if not frame_header:
        raise NoMoreData()
    if len(frame_header) < 4:
        raise ValueError("Could not read entire frame header")

    frame_id = frame_header[0]
    frame_length, = struct.unpack("<I", frame_header[1:] + b"\x00")

    data = frame_stream.read(frame_length)
    if len(data) != frame_length:
        raise ValueError(f"Could not read all data; wanted: {frame_length}; got: {len(data)}")

    return frame_id, data


def decompress_framed(frame_stream: typing.BinaryIO, out_stream: typing.BinaryIO, *, mozilla_mode=False):
    """
    Decompresses a Snappy framed format stream into another stream.

    :param frame_stream: Stream containing the Snappy Framed data
    :param out_stream: Stream that the decompressed data will be written to.
    :param mozilla_mode: If True, use the (non-standard) checksum format used by Mozilla
    :return:
    """
    header_type, header_raw = read_frame(frame_stream)
    if header_type != 0xff or header_raw != FRAME_MAGIC:
        raise ValueError("Invalid magic")

    while True:
        frame_offset = frame_stream.tell()
        try:
            frame_type, frame_data = read_frame(frame_stream)
        except NoMoreData:
            break

        if frame_type == 0x00:  # compressed
            crc_raw = frame_data[0:4]
            with io.BytesIO(frame_data[4:]) as compressed:
                decompressed = decompress(compressed)
            stored_crc, = struct.unpack("<I", crc_raw)
            crc_match = check_masked_crc(stored_crc, decompressed, xor_value=0x0 if mozilla_mode else 0xffffffff)
            if not crc_match:
                raise ValueError(f"CRC mismatch in frame starting at {frame_offset}")

            out_stream.write(decompressed)
        elif frame_type == 0x01:  # decompressed
            crc_raw = frame_data[0:4]
            stored_crc, = struct.unpack("<I", crc_raw)
            crc_match = check_masked_crc(stored_crc, frame_data[4:], xor_value=0x0 if mozilla_mode else 0xffffffff)
            if not crc_match:
                raise ValueError(f"CRC mismatch in frame starting at {frame_offset}")
            out_stream.write(frame_data[4:])
        elif frame_type == 0xfe:  # padding
            pass
        elif 0x02 <= frame_type <= 0x7f:  # reserved, unskippable
            raise ValueError("Reserved unskippable data")
        elif 0x80 <= frame_type <= 0xfe:  # reserved, skippable
            pass
        else:
            raise ValueError("unexpected frame")


def _main(in_path, out_path):
    import pathlib

    in_path = pathlib.Path(in_path)
    out_path = pathlib.Path(out_path)
    with in_path.open("rb") as f:
        with out_path.open("wb") as out:
            decompress_framed(f, out)


if __name__ == "__main__":
    _main(sys.argv[1], sys.argv[2])
