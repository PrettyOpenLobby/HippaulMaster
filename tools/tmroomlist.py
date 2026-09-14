#!/usr/bin/env python3
"""Dump / patch Tetra Master's `b/g/RL%03d` room list fixture.

    python tools/tmroomlist.py --dump  services/tmdata/b_g_RL000.bin
    python tools/tmroomlist.py --set-vscom services/tmdata/b_g_RL*.bin

WARNING: THIS IS *TM'S* READING OF THE FILE, NOT JANHOUROU'S. `tools/janroomlist.py`
maps the same 200-byte record for the JAN client, where `+0x28` is a 128-byte
`TitleSet` name. TM reads the SAME BYTES completely differently -- `+0x28` is a
one-byte TYPE and the name lives at `+0x2A`. Two clients, one file family, two
field maps; do not "fix" one against the other.

THE RECORD, as TM reads it (display loop 0x8E0B0..0x8E468,
whose `ebx` = entry+0x6D). Count u32 at `+0x40`, entries from `+0x48`, stride
200 (0xC8):

    +0x00  u64   room id
    +0x10, +0x14 u32 pair, summed = players
    +0x18  u32   used tables (Open Table = capacity - this)
    +0x28  u8    TYPE: 'A' = Set Rules, 'B' = Free Rules; anything else and the
                 ENTRY IS SKIPPED (0x8E1E7/0x8E1EE).  -> row+0x18C, as type-'A'
    +0x29  u8    ** THE VS. COM GATE ** -- see below.                -> row+0x19C
    +0x2A  32 B  NAME, terminated by '*' or length
    +0x6A  3 L   table capacity, letter-encoded
    +0x6D..+0x70 four letters: max players -> row+0x1A2, chat flag -> row+0x1A0
    +0xB8  13 B  IRC channel name

## `+0x29` IS THE VS. COM GATE, AND IT IS SERVER DATA (measured 2026-08-22)

`0x45D78 cmp dword [0x52454FC], 0x41` is condition (3) of the VS. COM gate, and
`0x52454FC` is **row+0x19C of the current room's 472-byte row**, which is a
verbatim copy of this byte:

    0x8E1C8  movsx ecx, byte [ebx-0x44]   ; ebx = entry+0x6D, so this is +0x29
    0x8E1D9  movsx eax, byte [ebx-0x45]   ; = entry+0x28, the TYPE byte
    0x8E1DD  sub   eax, 0x41              ; -'A'  -> row+0x18C
    0x8E1E0  mov   [esp+0x1dc], ecx       ; row+0x19C := entry+0x29, RAW

The stack row base is `esp+0x40` (which puts players at `[esp+0x1d0]` =
row+0x190, matching the independently-read map). The row then reaches the gate
through TWO block copies -- selected row -> staging `0x51DEC68` -> globals
`0x5245360` (`rep movsd`, 0x76 dwords = 472 B, at rva 0x3074F and friends).

**Stored RAW, with no `-0x41`**, so the served byte must literally be `'A'`
(0x41). We shipped `0x00` in every room, which is exactly what a live dump read
out of `[0x52454FC]`, and why VS. COM answered Lobby.BIN 454 "You cannot play
against the computer in this room" even in a Set-Rules room.

WARNING: **THE VALUE IS OURS, NOT SE'S.** No capture tells us which rooms SE enabled,
so `--set-vscom` turns it on EVERYWHERE. If a capture ever shows SE gating it
per room, this is the one byte to change.

WARNING: **AND `row+0x19C` MAY HAVE READERS BESIDES THE GATE.** Within the copied
globals it does not -- `0x52454FC` occurs exactly ONCE image-wide, the compare
at `0x45D78` -- but the ROW ARRAY copy of it is not audited, so a room-row
renderer could read it too. 0x00 -> 0x41 is a display risk of exactly one byte;
watch the room list after changing it.
"""
import argparse
import struct
import sys

HDR = 0x48              # first entry
REC = 200               # 0xC8 stride
COUNT_OFF = 0x40
TOTAL = 51272
F_TYPE = 0x28
F_VSCOM = 0x29          # THE GATE
F_NAME = 0x2A
F_CHAN = 0xB8
VSCOM_ON = 0x41         # 'A'


F_U04 = 0x04            # u32, 32 in every authored room (unread as far as is known)
F_RULES = 0x49          # 7 letters: the room's authored rank gates
F_TABLES = 0x6A         # 13 letters: capacity (3) then the table parameters
NAME_MAX = 32
CHAN_MAX = 13


def build(rooms):
    """`b/g/RL%03d` from room dicts {id, type, vscom, name, rules, tables,
    channel}: count at +0x40, 200-byte records from +0x48, everything not
    named here zero (the live patchers write players and open tables)."""
    if len(rooms) > (TOTAL - HDR) // REC:
        raise ValueError("%d rooms; the list holds %d" % (len(rooms), (TOTAL - HDR) // REC))
    buf = bytearray(TOTAL)
    struct.pack_into("<I", buf, COUNT_OFF, len(rooms))
    for i, r in enumerate(rooms):
        e = HDR + i * REC
        struct.pack_into("<I", buf, e, int(r["id"]))
        struct.pack_into("<I", buf, e + F_U04, 32)
        buf[e + F_TYPE] = ord(r.get("type", "B"))
        buf[e + F_VSCOM] = ord(r.get("vscom", "A"))
        n = r["name"].encode("cp932")[:NAME_MAX]
        buf[e + F_NAME:e + F_NAME + len(n)] = n
        for off, key, width in ((F_RULES, "rules", 7), (F_TABLES, "tables", 13)):
            v = r.get(key, "A" * width).encode("ascii")[:width]
            buf[e + off:e + off + len(v)] = v
        c = r["channel"].encode("ascii")[:CHAN_MAX]
        buf[e + F_CHAN:e + F_CHAN + len(c)] = c
    return bytes(buf)


def _rooms(blob):
    """(index, offset) for each entry the count declares, bounds-checked."""
    n = struct.unpack_from("<I", blob, COUNT_OFF)[0]
    if not 0 < n <= (TOTAL - HDR) // REC:
        return None, n
    return [(i, HDR + i * REC) for i in range(n)], n


def _name(blob, e):
    raw = bytes(blob[e + F_NAME:e + F_NAME + 32])
    return raw.split(b"*")[0].split(b"\x00")[0].decode("latin1", "replace")


def dump(path):
    blob = open(path, "rb").read()
    rooms, n = _rooms(blob)
    print("%s  %d bytes, count=%d" % (path, len(blob), n))
    if rooms is None:
        print("  refusing to walk: count %d is out of range" % n)
        return 1
    for i, e in rooms:
        chan = bytes(blob[e + F_CHAN:e + F_CHAN + 13]).split(b"\x00")[0]
        print("  %2d  %-22s %-10s type=%r  +0x29=0x%02X %s"
              % (i, _name(blob, e), chan.decode("latin1", "replace"),
                 chr(blob[e + F_TYPE]), blob[e + F_VSCOM],
                 "VS. COM ON" if blob[e + F_VSCOM] == VSCOM_ON else
                 "-- gate FAILS (454)"))
    return 0


def set_vscom(path, value=VSCOM_ON):
    blob = bytearray(open(path, "rb").read())
    rooms, n = _rooms(blob)
    if rooms is None:
        print("%s: count %d out of range -- NOT touched" % (path, n))
        return 1
    # WARNING: REFUSE ANY FILE THAT IS NOT TETRA MASTER'S. `b/g/RL%03d` is a SHARED
    # namespace: `services/tmdata/` holds RL000 for TM (#TM0R...) and RL001..004
    # for JANHOUROU (#MJS0R...), and JAN reads the SAME 200-byte record with a
    # different field map -- its `+0x28` is a 128-byte name string, so `+0x29` is
    # the SECOND LETTER OF THE ROOM NAME. Writing 'A' there turned "Test Room
    # 1-1" into "TAst Room 1-1" across all four JAN files (2026-08-22, caught
    # and reverted before commit). The docstring already warned about exactly
    # this and the warning did not stop a `RL*.bin` glob, so the check is now
    # CODE. The IRC channel at +0xB8 is the discriminator -- it is at the same
    # offset in both maps and it names the game.
    bad = [(_name(blob, e),
            bytes(blob[e + F_CHAN:e + F_CHAN + 13]).split(b"\x00")[0]
            .decode("latin1", "replace"))
           for _i, e in rooms
           if not bytes(blob[e + F_CHAN:e + F_CHAN + 4]) == b"#TM0"]
    if bad:
        print("%s: NOT a Tetra Master room list -- refusing to touch it.\n"
              "    %d of %d rooms are on another game's channel, e.g. %r (%s).\n"
              "    +0x29 is TM's VS. COM gate but somebody else's name byte."
              % (path, len(bad), n, bad[0][1], bad[0][0]))
        return 1
    changed = []
    for _i, e in rooms:
        if blob[e + F_VSCOM] != value:
            changed.append("%s(0x%02X->0x%02X)"
                           % (_name(blob, e), blob[e + F_VSCOM], value))
            blob[e + F_VSCOM] = value
    if changed:
        open(path, "wb").write(bytes(blob))
    print("%s  %d room(s): %s"
          % (path, n, ", ".join(changed) if changed else "already set"))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--dump", action="store_true", help="print the room table")
    ap.add_argument("--set-vscom", action="store_true",
                    help="set entry+0x29 = 'A' in every room (opens the "
                         "VS. COM gate; see the module docstring)")
    ap.add_argument("--value", default=None,
                    help="override the byte written by --set-vscom, e.g. 0x00 "
                         "to put it back")
    a = ap.parse_args()
    if not (a.dump or a.set_vscom):
        a.dump = True
    rc = 0
    for f in a.files:
        if a.set_vscom:
            v = int(a.value, 0) if a.value else VSCOM_ON
            rc |= set_vscom(f, v & 0xFF)
        if a.dump:
            rc |= dump(f)
    return rc


if __name__ == "__main__":
    sys.exit(main())
