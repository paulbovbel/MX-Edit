#!/usr/bin/env python3
"""Write an xz file like the stock MX5 rootfs using only Python's lzma module:
one stream, CRC32, LZMA2 with a 64 MiB dictionary (preset 6e), 128 MiB blocks
with compressed/uncompressed sizes in their headers (like
'xz -T2 --block-size=134217728'). Python lzma cannot write block header sizes,
so compress each block with FORMAT_RAW and assemble the xz container (stream
header, block headers, index, footer) following xz specification 1.0.4.
Blocks run in parallel threads because lzma releases the GIL."""
import lzma, struct, zlib
from concurrent.futures import ThreadPoolExecutor

BLOCK = 128 << 20
DICT = 64 << 20
CHECK_CRC32 = 0x01


def _vli(n):
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _dict_prop(size):
    # LZMA2 property byte: smallest value whose dictionary is >= size
    for p in range(40):
        if (2 | (p & 1)) << (p // 2 + 11) >= size:
            return p
    return 40


def _raw(chunk):
    filters = [{"id": lzma.FILTER_LZMA2, "preset": 6 | lzma.PRESET_EXTREME, "dict_size": DICT}]
    return lzma.compress(chunk, format=lzma.FORMAT_RAW, filters=filters)


def _block(chunk, comp):
    flags = 0x00 | 0x40 | 0x80              # One filter, compressed and uncompressed sizes
    body = bytes([flags]) + _vli(len(comp)) + _vli(len(chunk))
    body += _vli(0x21) + _vli(1) + bytes([_dict_prop(DICT)])   # LZMA2 filter, one property byte
    size = 1 + len(body) + 4
    size = (size + 3) & ~3
    hdr = bytes([size // 4 - 1]) + body
    hdr += bytes(size - 4 - len(hdr))
    hdr += struct.pack("<I", zlib.crc32(hdr))
    pad = bytes((4 - len(comp) % 4) % 4)
    check = struct.pack("<I", zlib.crc32(chunk))
    unpadded = len(hdr) + len(comp) + len(check)
    return hdr + comp + pad + check, unpadded


def compress(data, threads=3):
    chunks = [data[i:i + BLOCK] for i in range(0, len(data), BLOCK)] or [b""]
    with ThreadPoolExecutor(max_workers=threads) as ex:
        comps = list(ex.map(_raw, chunks))
    flags = bytes([0, CHECK_CRC32])
    out = bytearray(b"\xfd7zXZ\x00" + flags + struct.pack("<I", zlib.crc32(flags)))
    records = []
    for chunk, comp in zip(chunks, comps):
        blk, unpadded = _block(chunk, comp)
        out += blk
        records.append((unpadded, len(chunk)))
    index = bytearray(b"\x00" + _vli(len(records)))
    for unpadded, usize in records:
        index += _vli(unpadded) + _vli(usize)
    index += bytes((4 - len(index) % 4) % 4)
    index += struct.pack("<I", zlib.crc32(index))
    out += index
    backward = len(index) // 4 - 1
    foot = struct.pack("<I", backward) + flags
    out += struct.pack("<I", zlib.crc32(foot)) + foot + b"YZ"
    return bytes(out)
