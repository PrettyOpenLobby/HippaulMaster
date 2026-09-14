#!/usr/bin/env python3
"""Build Tetra Master's `b/g/ZL` zone list -- the multiplayer lobby's front door.

WHERE THIS CAME FROM (2026-08-15). Clicking "player vs. player" on TM's main menu
fetches lobby 03:00 path `b/g/ZL`; we were answering the 664-byte fallback and the
screen stayed empty. Two separate faults, both fixed here and in `_FETCH_PATHLEN`:
the read is **2120 bytes**, and an all-zero blob is a valid list of ZERO zones.

Everything below is read off the decrypted PS2 `TMaster.pex` (base 0x00280000),
never guessed. The fetch site is 0x00410720:

    0x00410768  jal 0x0040a0d8        ; sprintf the literal "b/g/ZL"
    0x00410784  addiu a2, zero, 2120  ; <-- the length
    0x0041078c  jal 0x00409c38        ; sqMgCpReadFile
    0x004107cc  sw v1, 336(s0)        ; 2120 recorded again

and SE's own symbol for its caller is `sqMgCpLoadZoneList` (0x00485e80), which
reads into the buffer at **0x0058f2c0**. The chain it starts is named by the
symbol table too: LoadZoneList -> LoadRoomList -> EnterRoom2 -> EnterTable.

THE LAYOUT IS PINNED BY FIVE LEAF ACCESSORS, not by inference. Each is a
two-instruction function that indexes the same buffer, so the stride and every
field offset are read directly:

    0x0041fbe0   lw  [0x0058f300]                  no index  -> ZONE COUNT
    0x0041fc50   lw  [0x0058f2c0 + idx*64 + 72]              -> record +0x00
    0x0041fc74       0x0058f2c0 + idx*64 + 84                -> record +0x0C
    0x003a7320       0x0058f2c0 + idx*64 + 116               -> record +0x2C
    0x0041fc10   lb  [0x0058f2c0 + idx*64 + 132]             -> record +0x3C

`sll v1, a1, 6` in each gives the 64-byte stride. Taking the records to start at
buffer+0x48 puts all four field offsets inside one record (0x00, 0x0C, 0x2C,
0x3C) and the totals close EXACTLY:

    0x48 header + 32 records * 64 = 72 + 2048 = 2120

No slack, no padding to explain away -- that exactness is the check that the
reading is right. The count sits at +0x40, in the last 8 bytes of the header.

WHAT +0x2C IS, and it is not what it looks like. The accessor at 0x003a7320 hands
that string to 0x00299220, which is **IRC session start** -- its own error string
is 'IRC Session start already.' (0x0047b340) and it returns 1000 in that case.
So +0x2C is the zone's **IRC channel**, and "entering a zone" is joining a channel
on the auth band. It is NOT a numeric field; do not feed it a count.

STATUS: the count/name half is measured. The remaining fields are a FIRST CUT --
+0x00 and +0x3C are non-zero placeholders, because a zero id is what made the
group list unusable once before. Iterate against the live client.

    python tools/tmzonelist.py --out data/resources/8.b_g_ZL.bin
    python tools/tmzonelist.py --dump data/resources/8.b_g_ZL.bin
"""
import argparse
import struct

TOTAL = 2120           # a2 at TMaster.pex 0x00410784
HDR = 0x48             # records start here
REC = 64               # sll a1, 6 in every accessor
COUNT_OFF = 0x40       # 0x0058f300 - 0x0058f2c0
MAX_ZONES = (TOTAL - HDR) // REC        # 32, and 72 + 32*64 == 2120 exactly

F_ID = 0x00            # lw,  accessor 0x0041fc50
F_NAME = 0x0C          # ptr, accessor 0x0041fc74
F_CHANNEL = 0x2C       # ptr, accessor 0x003a7320 -> IRC session start
F_BYTE = 0x3C          # lb,  accessor 0x0041fc10

#: Field widths are bounded by the NEXT field in the record, so a name can run to
#: 0x2C-0x0C = 32 bytes and a channel to 0x3C-0x2C = 16, each NUL-terminated.
NAME_MAX = F_CHANNEL - F_NAME
CHANNEL_MAX = F_BYTE - F_CHANNEL


def _text(s, limit):
    """Shift-JIS, NUL-terminated, hard-truncated to the field width."""
    b = s.encode("cp932", "replace")[:limit - 1]
    return b.ljust(limit, b"\x00")


def build(zones):
    """`zones` is a list of (id, name, channel, byte) -- at most MAX_ZONES."""
    if len(zones) > MAX_ZONES:
        raise ValueError("%d zones; the buffer holds %d" % (len(zones), MAX_ZONES))
    buf = bytearray(TOTAL)
    struct.pack_into("<I", buf, COUNT_OFF, len(zones))
    for i, (zid, name, channel, flag) in enumerate(zones):
        o = HDR + i * REC
        struct.pack_into("<I", buf, o + F_ID, zid)
        buf[o + F_NAME:o + F_NAME + NAME_MAX] = _text(name, NAME_MAX)
        buf[o + F_CHANNEL:o + F_CHANNEL + CHANNEL_MAX] = _text(channel, CHANNEL_MAX)
        buf[o + F_BYTE] = flag & 0xFF
    assert len(buf) == TOTAL
    return bytes(buf)


def dump(blob):
    n = struct.unpack_from("<I", blob, COUNT_OFF)[0]
    print("total %d B, count = %d (max %d)" % (len(blob), n, MAX_ZONES))
    for i in range(min(n, MAX_ZONES)):
        o = HDR + i * REC
        name = blob[o + F_NAME:o + F_CHANNEL].split(b"\x00")[0]
        chan = blob[o + F_CHANNEL:o + F_BYTE].split(b"\x00")[0]
        print("  [%2d] id=%d name=%r channel=%r byte=%d" % (
            i, struct.unpack_from("<I", blob, o + F_ID)[0],
            name.decode("cp932", "replace"), chan.decode("cp932", "replace"),
            blob[o + F_BYTE]))


#: One zone. Deliberately minimal: the open question is whether the list RENDERS,
#: and one row answers it as well as thirty-two while keeping every other field a
#: single known value if it does not.
#: WARNING: A TOOL DEFAULT, AND IT MUST NOT READ LIKE CONTENT. SE's real English
#: zones are known from period screenshots and are
#: what `data/resources/1.b_g_ZL.bin` actually serves -- `Mermaids'
#: Dreamworld` and `Dragon King's Dance Hall`, 7 rooms each. This default
#: exists only so `--out` with no arguments produces something valid; it
#: used to say "Test Zone", which would have published a fixture name over
#: recovered content if anyone ran it that way.
DEFAULT = [(1, "Mermaids' Dreamworld", "#TM0Z001", 1)]

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the blob here")
    ap.add_argument("--dump", help="decode an existing blob instead")
    a = ap.parse_args()
    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
    else:
        blob = build(DEFAULT)
        dump(blob)
        if a.out:
            with open(a.out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (a.out, len(blob)))
