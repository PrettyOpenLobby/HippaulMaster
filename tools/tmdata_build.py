#!/usr/bin/env python3
"""Build services/tmdata/ from your own Tetra Master client install.

    python tools/tmdata_build.py --client "<your TetraMaster install directory>"

(on a default PC install that is the `TetraMaster` folder under `Program Files
(x86)`, `PlayOnline`, `SquareEnix`)

The server needs four of the client's parameter tables -- the card stats, the
card price ladder, the VS. COM opponents and the card packs -- and none of them
ship with this repository. This reads them out of YOUR install, decodes the
client's own container format, and writes the plain tables where the server
looks for them:

    data/CardPri.BIN   -> services/tmdata/CardPri.BIN    the sell-price ladder
    data/CardPrm.BIN   -> services/tmdata/CardPrm.BIN    one 12-byte row per card
    data/CoPrm.BIN     -> services/tmdata/CoPrm.BIN      VS. COM opponents
    data/PackPrm.BIN   -> services/tmdata/PackPrm.BIN    the card packs
                       -> services/tmdata/card_names_en.txt  the card names, one
                          per line, from CardPrm's own string pool when the
                          install is an English one (US/EU); a Japanese install
                          keeps whatever names file is already there

Idempotent: a table already present with the same bytes is left alone.
`--compare DIR` prints a byte comparison of the decoded tables against another
set (useful for checking a decode against another install's copies).

THE CONTAINER. Every `data/*.BIN` in the PC install is LZSS-compressed, not
encrypted, and the decoder is the client's own (TM.dll: loader 0x05119120,
decoder init 0x0510b6c0, loop 0x0510b4c0; transcribed 2026-09):

    +0x00 u32  decoded size
    +0x04 u32  0x00204040            the container mark
    +0x08 u32  a build stamp (constant within a build; NOT a key)
    +0x0c u32  0
    +0x10      the bitstream, every field MSB-first:
                 1 -> literal: 8 bits, emit
                 0 -> match: 8 bits window index (0 ENDS the stream), 4 bits n,
                      copy n+2 bytes from window[(index+i) & 0xff]
               a 256-byte zero-filled window; every emitted byte is appended
               at wpos, which starts at 1 and wraps at 256.

THE DECODED TABLE: `u32 pool offset, u32 record count, u32 platform (0x0f on
the PC), u32 hash (0 on the PC)`, then the records from +0x10, then a
NUL-separated string pool at the pool offset. CardPrm's records are 12 bytes:
attack, type, physical defence, magic defence, ?, level, group, category, then
a 4-byte string slot naming the card.
"""
import argparse
import hashlib
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DEFAULT = os.path.normpath(os.path.join(HERE, os.pardir, "services", "tmdata"))
MAGIC = 0x00204040
TABLES = ("CardPri.BIN", "CardPrm.BIN", "CoPrm.BIN", "PackPrm.BIN")
NAMES_FILE = "card_names_en.txt"
CARD_COUNT = 250        # the card-number ceiling the client enforces
CARD_REC = 12


class _Bits:
    """MSB-first bit reader; a fresh source byte is fetched when the mask wraps."""

    def __init__(self, buf):
        self.b, self.pos, self.cur, self.mask = buf, 0, 0, 0x80

    def bit(self):
        if self.mask == 0x80:
            self.cur = self.b[self.pos] if self.pos < len(self.b) else 0
            self.pos += 1
        v = 1 if (self.cur & self.mask) else 0
        self.mask >>= 1
        if self.mask == 0:
            self.mask = 0x80
        return v

    def bits(self, n):
        v, m = 0, 1 << (n - 1)
        for _ in range(n):
            if self.bit():
                v |= m
            m >>= 1
        return v


def decompress(src, want=None):
    """The client's LZSS, as its decoder loop runs it."""
    st = _Bits(src)
    win = bytearray(256)
    wpos = 1
    out = bytearray()
    while True:
        if st.bit():
            byte = st.bits(8)
            out.append(byte)
            win[wpos] = byte
            wpos = (wpos + 1) & 0xFF
        else:
            idx = st.bits(8)
            if idx == 0:
                break
            for i in range(st.bits(4) + 2):
                byte = win[(idx + i) & 0xFF]
                out.append(byte)
                win[wpos] = byte
                wpos = (wpos + 1) & 0xFF
        if want is not None and len(out) >= want:
            break
    return bytes(out[:want] if want is not None else out)


def load(path):
    """The plain table: decoded if the file is a container, as-is if it is
    already plain (the PS2 disc ships plain files)."""
    with open(path, "rb") as f:
        blob = f.read()
    if len(blob) >= 16 and struct.unpack_from("<I", blob, 4)[0] == MAGIC:
        want = struct.unpack_from("<I", blob, 0)[0]
        return decompress(blob[16:], want)
    return blob


def describe(plain):
    pool, count, plat, h = struct.unpack_from("<4I", plain, 0)
    return pool, count, plat, h


def pool_strings(plain):
    pool = struct.unpack_from("<I", plain, 0)[0]
    if not 0 < pool < len(plain):
        return []
    return plain[pool:].split(b"\x00")


def find_table(client, name):
    """data/<name> under the install, or the first match anywhere below it."""
    p = os.path.join(client, "data", name)
    if os.path.isfile(p):
        return p
    for dirpath, _dirs, files in os.walk(client):
        for fn in files:
            if fn.lower() == name.lower():
                return os.path.join(dirpath, fn)
    return None


def sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


def check_shape(name, plain):
    """A cheap sanity check that this is the table the server expects."""
    if len(plain) < 16:
        return "too short"
    pool, count, _plat, _h = describe(plain)
    if name == "CardPrm.BIN":
        if count != CARD_COUNT or pool != 16 + CARD_COUNT * CARD_REC:
            return "expected %d rows of %d bytes (count=%d pool=%d)" % (
                CARD_COUNT, CARD_REC, count, pool)
        types = [plain[16 + i * CARD_REC + 1] for i in range(count)]
        if max(types) > 3:
            return "a card type above 3 (%d): not a CardPrm table" % max(types)
    elif name == "CardPri.BIN":
        if count != 25 or len(plain) < 16 + 25 * 4:
            return "expected a 25-entry price ladder (count=%d)" % count
    elif pool > len(plain) or count <= 0:
        return "pool offset %d / count %d out of range" % (pool, count)
    return None


def build(client, out, compare=None):
    os.makedirs(out, exist_ok=True)
    rows, ok = [], True
    cardprm = None
    for name in TABLES:
        src = find_table(client, name)
        if src is None:
            rows.append((name, "NOT FOUND under %s" % client))
            ok = False
            continue
        try:
            plain = load(src)
        except Exception as exc:
            rows.append((name, "decode failed: %r" % (exc,)))
            ok = False
            continue
        why = check_shape(name, plain)
        if why:
            rows.append((name, "REJECTED: " + why))
            ok = False
            continue
        dst = os.path.join(out, name)
        state = "written"
        if os.path.isfile(dst):
            with open(dst, "rb") as f:
                state = "unchanged" if f.read() == plain else "updated"
        if state != "unchanged":
            with open(dst, "wb") as f:
                f.write(plain)
        pool, count, plat, _h = describe(plain)
        note = "%5d B, %3d records, platform 0x%02x, sha %s, %s" % (
            len(plain), count, plat, sha(plain), state)
        if compare:
            other = os.path.join(compare, name)
            if os.path.isfile(other):
                with open(other, "rb") as f:
                    o = f.read()
                note += "; vs %s: %s" % (compare, "IDENTICAL" if o == plain else
                                          "differs (%d B there)" % len(o))
        rows.append((name, note))
        if name == "CardPrm.BIN":
            cardprm = plain
    if cardprm is not None:
        names = [s.decode("latin1") for s in pool_strings(cardprm)[:CARD_COUNT]]
        ascii_names = len(names) == CARD_COUNT and all(
            n and all(32 <= ord(c) < 127 for c in n) for n in names)
        dst = os.path.join(out, NAMES_FILE)
        if ascii_names:
            text = "\n".join(names) + "\n"
            state = "written"
            if os.path.isfile(dst):
                with open(dst, encoding="utf-8") as f:
                    state = "unchanged" if f.read() == text else "updated"
            if state != "unchanged":
                with open(dst, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
            rows.append((NAMES_FILE, "%d names from the table's own string pool, %s"
                         % (len(names), state)))
        else:
            rows.append((NAMES_FILE, "the install's string pool is not English; "
                         + ("keeping the existing file" if os.path.isfile(dst)
                            else "NOT written -- card names will come from the "
                                 "table's own pool (Japanese)")))
    print("tmdata_build: %s -> %s" % (client, out))
    for name, note in rows:
        print("  %-18s %s" % (name, note))
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--client", required=True,
                    help="your Tetra Master install (the directory holding data/)")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--compare", help="another set of decoded tables to byte-compare against")
    a = ap.parse_args()
    if not os.path.isdir(a.client):
        raise SystemExit("not a directory: %s" % a.client)
    sys.exit(0 if build(a.client, a.out, a.compare) else 1)


if __name__ == "__main__":
    main()
