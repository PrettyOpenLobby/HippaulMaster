"""Readers for the Tetra Master PC client's own file formats, for the tools
that build server data and board art from YOUR install:

    load(path)          a `data/*.BIN` table, decoded (tmdata_build.py's LZSS)
    slot_strings(buf)   the strings a decoded table's records point at
    read_pack(path)     a `data/gW*.dat` texture pack: its groups and assets

THE PACK (identical in shape on the PS2, where it is `gP*.dat`): two levels of
32-byte directory entries.

    outer entry `<I 4s I I I I I I>`
        +0x00 u32   0x00008000
        +0x04 char4 group tag, ASCII ("0090", "1017", ...)
        +0x08 u32   platform (0x01560000 on the PC)
        +0x0c u32   2
        +0x10 u32   offset of the group payload, from the start of the file
        +0x14 u32   its size
        +0x18, +0x1c   flags
    The directory length is not stored: entry 0's offset IS the end of it.

    inner entry `<I 12s I I I I>`, in the group payload
        +0x00 u32    0x00004000
        +0x04 char12 asset name, NUL-padded ("menu", "cad00000", ...)
        +0x10 u32    offset of the asset, from the start of the payload
        +0x14 u32    its stored size
        +0x18 u32    encoding: 2 = a plain PNG (the PC), 3 = compressed (PS2)
        +0x1c u32    its decoded size

The board tools only take the PC's plain-PNG assets (kind 2).

THE STRING SLOTS of a decoded table: the loader walks dwords from +0x10 to
the pool offset; a dword whose low two bytes are ff ff and whose byte 2 has
low nibble f is a slot, filled with consecutive pointers into the pool,
advancing by `(dword >> 20) + 1` bytes (the string plus its NUL).
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from tmdata_build import decompress, load, MAGIC        # noqa: E402,F401

OUTER = "<I4sIIIIII"
INNER = "<I12sIIII"


class Asset:
    def __init__(self, magic, name, kind, usz, blob):
        self.magic, self.name, self.kind, self.usz, self.blob = magic, name, kind, usz, blob


class Group:
    def __init__(self, magic, tag, plat, kind, f1, f2, assets):
        self.magic, self.tag, self.plat, self.kind = magic, tag, plat, kind
        self.f1, self.f2, self.assets = f1, f2, assets


def read_group(pay):
    n = struct.unpack_from("<I", pay, 0x10)[0] // 32
    out = []
    for i in range(n):
        magic, name, off, size, kind, usz = struct.unpack_from(INNER, pay, i * 32)
        out.append(Asset(magic, name, kind, usz, pay[off:off + size]))
    return out


def read_pack(path):
    with open(path, "rb") as f:
        d = f.read()
    n = struct.unpack_from("<I", d, 0x10)[0] // 32
    groups = []
    for i in range(n):
        magic, tag, plat, kind, off, size, f1, f2 = struct.unpack_from(OUTER, d, i * 32)
        groups.append(Group(magic, tag, plat, kind, f1, f2, read_group(d[off:off + size])))
    return groups


def name_of(a):
    return a.name.split(b"\x00")[0].decode("latin1")


def png_assets(path):
    """{asset name: PNG bytes} for the plain-PNG assets of one pack; the first
    group carrying a name wins."""
    out = {}
    for g in read_pack(path):
        for a in g.assets:
            if a.kind == 2 and name_of(a) not in out:
                out[name_of(a)] = a.blob
    return out


def slot_strings(buf):
    """[(record index, field offset, text bytes)] in the loader's own order."""
    pool, recs = struct.unpack_from("<II", buf, 0)[:2]
    rsize = (pool - 0x10) // recs if recs else 4
    out, p = [], pool
    for off in range(0x10, pool, 4):
        if buf[off] != 0xFF or buf[off + 1] != 0xFF or (buf[off + 2] & 0x0F) != 0x0F:
            continue
        n = (struct.unpack_from("<I", buf, off)[0] >> 20) + 1
        out.append(((off - 0x10) // rsize, (off - 0x10) % rsize, buf[p:p + n - 1]))
        p += n
    return out
