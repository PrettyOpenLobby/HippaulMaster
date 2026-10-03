#!/usr/bin/env python3
"""Build Tetra Master's `b/g/TM0EventList`, `TM0EventDataList`, `TM0EventMemberList`.

THREE files, and only ONE of them is reachable in the retail US client. The
event subsystem is a chain of scenes (bridge loader 0x39xxx -> deep scenes
0x66000+) driven by a per-scene message queue; it is fully present, not removed,
and gated entirely server-side (entry mechanism traced 2026-08-22).

  * `TM0EventList`       -- THE SCHEDULE / GATE. Reader 0x8B5D0, called from the
                           REACHABLE loader pump 0x39590 (0x0395BD) at its first
                           step. This is the file that decides whether an event
                           exists at all; the other two are read only from inside
                           the event scenes (their readers 0x8C670/0x8C700 are
                           orphaned outside the event span). Built here for the
                           first time -- this is the actual door.
  * `TM0EventDataList`   -- inner: the event-name/divisor table (reader 0x8C670).
  * `TM0EventMemberList` -- inner: the ranking whose top three places are the
                           only door into the **Event Shop** (reader 0x8C700).

None has ever been requested on our server, because the event screen has never
opened; this tool exists so the first fetch returns a
real list rather than zeros.

    python tools/tmevent.py --out-list   data/resources/1.b_g_TM0EventList.bin
    python tools/tmevent.py --out-data   data/resources/1.b_g_TM0EventDataList.bin
    python tools/tmevent.py --out-member data/resources/1.b_g_TM0EventMemberList.bin \
                            --winner AB12CEB20D9067C4
    python tools/tmevent.py --dump-list   data/resources/1.b_g_TM0EventList.bin
    python tools/tmevent.py --dump-member data/resources/1.b_g_TM0EventMemberList.bin

    # a marked file for the shim measurement rig (distinctive bytes per field):
    python tools/tmevent.py --out-list markers.bin --markers

ADDRESSING. Every offset below is an RVA into `TM.dll.unpacked`, the unpacked
client image, which is a flat memory image (file offset == RVA) whose pointers
assume base 0x04F90000.

THE MEMBER LIST, and why its layout is worth stating twice. The file's records
are 0x28 bytes, but the array the entry gate actually ranks is a 0x70-stride
vector at RVA 0x24EEB0. They are not two lists: the event main scene's own
constructor loop (0x782B8) builds each vector element out of one file record and
pushes it onto a temp vector with `insert(end, ...)` -- so **nothing sorts**, and

    file +0x00/+0x04 -> elem +0x00/+0x04    the 64-bit member id
    file +0x08 (16B) -> elem +0x10          the name
    file +0x1C       -> elem +0x5C          THE SCORE
                        elem +0x60 = 0      the scorer writes the placing here
                        elem +0x68 = 0

`0x6C5C0` then walks the vector and bumps a placing counter every time `+0x5C`
CHANGES, recording ours into `0x2B5FAC` and our score into `0x2B5FA8`. The gate
at `0x783A3` is then literally `1 <= placing <= 3 && score != 0`.

So THREE things have to hold for the Event Shop to open, and this tool is how you
make them hold:

  * our own 64-bit member id appears in a row,
  * that row's `+0x1C` (the score) is non-zero,
  * at most two DISTINCT higher scores are listed before it -- which, since
    nothing sorts, means literally "in file order".

A row whose `+0x24` is negative is skipped entirely by the consumer (`0x782C8`),
so that field is a per-row kill switch, not a flag.

WARNING: NOT MEASURED. Nothing in this file has been on a wire. Every offset is read
off the client, but "the client parses this" and "the client is happy with this"
are different claims, and only the first is supported. The cheap tell-tale is
the fetch itself: `b/g/TM0EventDataList` appearing in authserv.log at all is the
first evidence in the project's history that the event screen opened.
"""
import argparse
import struct

#: `b/g/TM0EventDataList` -- reader 0x8C670, buffer 0x2795E8, size arg 0x1308.
#: Count at +0x54 (the global the scene reads as [scene+0x1418]); records from
#: +0x60, stride 0x28. Record +0x00 is a display string the client truncates to
#: 7 or 8 bytes with a Shift-JIS-safe cut (0x9F640), and record 0's +0x20 is read
#: as the divisor [scene+0x141C] in one arm of the entry gate -- so it must not
#: be zero if that arm is ever taken.
#:
#: THE COUNT AND RECORD 0's +0x20 ARE THE CHOCOBO TRACK. Read off the unpacked
#: PC TM.dll in the 2026-10-03 cup crash dump: the tournament scene's init
#: (RVA 0x728FD) picks the track's icon table from (count, record 0 +0x20) --
#: count 2 needs 4..8, count 3 needs 1..5, count 4 needs 1..4 (tables at
#: RVA 0x221FF0/0x221ED4/0x221DF8, each count*sub positions long), an
#: out-of-range +0x20 stores NULL, and ANY OTHER COUNT LEAVES THE POINTER
#: UNINITIALISED. The row painter (RVA 0x72FC6) dereferences it as soon as a
#: player's steps are non-zero, so count 1 crashed every PC on the tournament
#: screen the moment the first game of the cup paid out steps (AV in TM.dll,
#: pointer = leftover heap text). The record names label the track's groups
#: (RVA 0x7818D copies `count` of them, 8 bytes each).
DATA_SIZE = 0x1308
DATA_COUNT_OFF = 0x54
DATA_REC_OFF = 0x60
DATA_REC_STRIDE = 0x28
TRACK_LAYOUTS = {2: range(4, 9), 3: range(1, 6), 4: range(1, 5)}

#: `b/g/TM0EventMemberList` -- reader 0x8C700, buffer 0x2923C8, size arg 0x2808.
#: Count at +0x04, records from +0x08, stride 0x28, and 8 + 256*0x28 == 0x2808
#: exactly, so 256 is the file's own ceiling rather than a guess.
MEMBER_SIZE = 0x2808
MEMBER_COUNT_OFF = 0x04
MEMBER_REC_OFF = 0x08
MEMBER_REC_STRIDE = 0x28
MEMBER_MAX = (MEMBER_SIZE - MEMBER_REC_OFF) // MEMBER_REC_STRIDE   # == 256

#: `b/g/TM0EventList` -- THE SCHEDULE/GATE. Reader 0x8B5D0 loads the filename
#: string at 0x51B2DFC ("b/g/TM0EventList", verified) into buffer 0x52136D0 with
#: size arg 0x2C08; count is read straight out of the loaded buffer at +0x04 (no
#: code writer), so it IS the file's own field. Records start at +0x08, stride
#: 0x160, and 8 + 32*0x160 == 0x2C08 exactly -- 32 is the file's own ceiling.
#: Per-record layout, every offset read off the parse loop at 0x3968F and the
#: keyed lookup at 0x8B670:
#:   +0x00  u32         event id / number     (-> parsed elem +0x00)
#:   +0x04  u8 x4       b04..b07              (copied verbatim; date/flag bytes)
#:   +0x08  u32         seconds value A       (split into H:M:S -- /3600, /60)
#:   +0x0C  u32         seconds value B       (split into H:M:S)
#:   +0x10  char[0x20]  key1  -- the per-event key the list scene looks up by
#:   +0x30  char[0x20]  key2  -- matched against a FIXED runtime key (global
#:                              0x5245324). A record whose key2 != that key is
#:                              marked not-found (0xffffffff) and skipped. That
#:                              key lives in .bss and is built at runtime, so it
#:                              is UNKNOWN statically and MUST be measured off a
#:                              live client (the shim dump captures 0x5245324).
#:   +0x50  char[...]   event name (copied into the parsed record)
#: The /3600 and /60 magic-division constants (0x91A2B3C5>>11, 0x88888889>>5)
#: are verified numerically, so A/B are plain second counts. Which of b04..b07 /
#: A / B forms the "active now" window is NOT yet located -- that is the second
#: thing the measurement rig is for.
LIST_SIZE = 0x2C08
LIST_COUNT_OFF = 0x04
LIST_REC_OFF = 0x08
LIST_REC_STRIDE = 0x160
LIST_MAX = (LIST_SIZE - LIST_REC_OFF) // LIST_REC_STRIDE   # == 32
LIST_R_ID = 0x00
LIST_R_BYTES = 0x04
LIST_R_SECA = 0x08
LIST_R_SECB = 0x0C
LIST_R_KEY1 = 0x10
LIST_R_KEY2 = 0x30
LIST_R_NAME = 0x50


def build_list(entries, header0=0):
    """entries: list of dicts with keys id, bytes4, seca, secb, key1, key2, name
    (all optional). Nothing sorts; the caller owns the order the client sees."""
    if len(entries) > LIST_MAX:
        raise ValueError("%d records exceeds the file's %d slots"
                         % (len(entries), LIST_MAX))
    blob = bytearray(LIST_SIZE)
    struct.pack_into("<I", blob, 0x00, header0)
    struct.pack_into("<I", blob, LIST_COUNT_OFF, len(entries))
    for i, e in enumerate(entries):
        base = LIST_REC_OFF + i * LIST_REC_STRIDE
        struct.pack_into("<I", blob, base + LIST_R_ID, e.get("id", i + 1) & 0xFFFFFFFF)
        b4 = e.get("bytes4", b"\0\0\0\0")
        blob[base + LIST_R_BYTES:base + LIST_R_BYTES + 4] = bytes(b4[:4]).ljust(4, b"\0")
        struct.pack_into("<I", blob, base + LIST_R_SECA, e.get("seca", 0) & 0xFFFFFFFF)
        struct.pack_into("<I", blob, base + LIST_R_SECB, e.get("secb", 0) & 0xFFFFFFFF)
        for off, key in ((LIST_R_KEY1, "key1"), (LIST_R_KEY2, "key2")):
            v = e.get(key, b"")
            if isinstance(v, str):
                v = v.encode("cp932")
            blob[base + off:base + off + min(len(v), 0x1F)] = v[:0x1F]
        name = e.get("name", b"")
        if isinstance(name, str):
            name = name.encode("cp932")
        # name occupies +0x50 up to the end of the record; keep room for a NUL.
        room = LIST_REC_STRIDE - LIST_R_NAME - 1
        blob[base + LIST_R_NAME:base + LIST_R_NAME + min(len(name), room)] = name[:room]
    return bytes(blob)


def _hms(sec):
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def dump_list(blob):
    h0 = struct.unpack_from("<I", blob, 0x00)[0]
    n = struct.unpack_from("<i", blob, LIST_COUNT_OFF)[0]
    print("b/g/TM0EventList  %d B  header0=0x%X  count=%d  (max %d)"
          % (len(blob), h0, n, LIST_MAX))
    for i in range(max(0, min(n, LIST_MAX))):
        base = LIST_REC_OFF + i * LIST_REC_STRIDE
        eid = struct.unpack_from("<I", blob, base + LIST_R_ID)[0]
        b4 = blob[base + LIST_R_BYTES:base + LIST_R_BYTES + 4]
        seca = struct.unpack_from("<I", blob, base + LIST_R_SECA)[0]
        secb = struct.unpack_from("<I", blob, base + LIST_R_SECB)[0]
        key1 = blob[base + LIST_R_KEY1:base + LIST_R_KEY1 + 0x20].split(b"\0")[0]
        key2 = blob[base + LIST_R_KEY2:base + LIST_R_KEY2 + 0x20].split(b"\0")[0]
        name = blob[base + LIST_R_NAME:base + LIST_REC_STRIDE].split(b"\0")[0]
        print("  %2d id=0x%08X b4=%s A=%s B=%s key1=%r key2=%r name=%r"
              % (i, eid, b4.hex(), _hms(seca), _hms(secb),
                 key1.decode("cp932", "replace"), key2.decode("cp932", "replace"),
                 name.decode("cp932", "replace")))


#: A marked record: every field a distinctive value so the shim buffer dump makes
#: each on-disk offset legible at a glance (id ramps 0xA1A1A1A1.., seconds are
#: recognizable H:M:S, keys/name are ASCII). Purely a measurement aid.
def build_markers():
    return build_list([{
        "id": 0xA1A1A1A1,
        "bytes4": b"\xB1\xB2\xB3\xB4",
        "seca": 1 * 3600 + 2 * 60 + 3,     # 1:02:03
        "secb": 4 * 3600 + 5 * 60 + 6,     # 4:05:06
        "key1": "KEY1MARKER",
        "key2": "KEY2MARKER",
        "name": "TESTEVENT-NAME",
    }], header0=0xDEADBEEF)


def build_data(entries):
    """entries: [(name, divisor)] -- name is bytes/str, divisor goes to +0x20."""
    blob = bytearray(DATA_SIZE)
    if len(entries) * DATA_REC_STRIDE + DATA_REC_OFF > DATA_SIZE:
        raise ValueError("too many event-data records for a %d-byte file" % DATA_SIZE)
    if not entries or entries[0][1] not in TRACK_LAYOUTS.get(len(entries), ()):
        raise ValueError("no chocobo track for %d record(s) with +0x20=%s; the "
                         "client crashes on it (see TRACK_LAYOUTS)"
                         % (len(entries), entries[0][1] if entries else None))
    struct.pack_into("<i", blob, DATA_COUNT_OFF, len(entries))
    for i, (name, divisor) in enumerate(entries):
        off = DATA_REC_OFF + i * DATA_REC_STRIDE
        if isinstance(name, str):
            name = name.encode("cp932")
        blob[off:off + min(len(name), 0x1F)] = name[:0x1F]
        struct.pack_into("<i", blob, off + 0x20, divisor)
    return bytes(blob)


def build_members(rows):
    """rows: [(member_id, name, score, flag)] in the ORDER the client will rank
    them -- nothing sorts, so the caller owns the placings."""
    if len(rows) > MEMBER_MAX:
        raise ValueError("%d rows exceeds the file's %d slots" % (len(rows), MEMBER_MAX))
    blob = bytearray(MEMBER_SIZE)
    struct.pack_into("<i", blob, MEMBER_COUNT_OFF, len(rows))
    for i, (member_id, name, score, flag) in enumerate(rows):
        off = MEMBER_REC_OFF + i * MEMBER_REC_STRIDE
        struct.pack_into("<Q", blob, off, member_id)
        if isinstance(name, str):
            name = name.encode("cp932")
        # The consumer memcpy's exactly 16 bytes, so a 16-byte name arrives
        # unterminated. Keep 15 + NUL.
        blob[off + 0x08:off + 0x08 + min(len(name), 15)] = name[:15]
        struct.pack_into("<i", blob, off + 0x1C, score)
        struct.pack_into("<i", blob, off + 0x24, flag)
    return bytes(blob)


def dump_members(blob):
    n = struct.unpack_from("<i", blob, MEMBER_COUNT_OFF)[0]
    print("b/g/TM0EventMemberList  %d B  count=%d" % (len(blob), n))
    placing, prev = 0, None
    for i in range(max(0, min(n, MEMBER_MAX))):
        off = MEMBER_REC_OFF + i * MEMBER_REC_STRIDE
        mid = struct.unpack_from("<Q", blob, off)[0]
        name = blob[off + 0x08:off + 0x18].split(b"\0")[0]
        score = struct.unpack_from("<i", blob, off + 0x1C)[0]
        flag = struct.unpack_from("<i", blob, off + 0x24)[0]
        if score != prev:
            placing += 1
            prev = score
        skip = "  SKIPPED (+0x24 < 0)" if flag < 0 else ""
        gate = "  <- Event Shop OPENS" if (1 <= placing <= 3 and score != 0
                                           and flag >= 0) else ""
        print("  %3d  place %-3d id=%016X  %-16s score=%-8d flag=%d%s%s"
              % (i, placing, mid, name.decode("cp932", "replace"), score, flag,
                 skip, gate))


def dump_data(blob):
    n = struct.unpack_from("<i", blob, DATA_COUNT_OFF)[0]
    print("b/g/TM0EventDataList  %d B  count=%d" % (len(blob), n))
    for i in range(max(0, n)):
        off = DATA_REC_OFF + i * DATA_REC_STRIDE
        if off + DATA_REC_STRIDE > len(blob):
            break
        name = blob[off:off + 0x20].split(b"\0")[0]
        print("  %3d  %-20s +0x20=%d"
              % (i, name.decode("cp932", "replace"),
                 struct.unpack_from("<i", blob, off + 0x20)[0]))


#: No default winner: a ranking with a made-up member on top of it is a
#: stranger on everyone's event board. Name one with --winner (or, on the
#: server, POL_TM_EVENT_MEMBERS -- see services/tmfixtures.py).
DEFAULT_WINNER = 0

#: Two groups of eight: the longest track the client has (16 steps).
DEFAULT_DATA = [("STAGE 1", 8), ("STAGE 2", 8)]

#: One candidate active event. key2 is now MEASURED, not guessed: a live memory
#: read of global 0x5245324 (RVA 0x2B5324) returned "Mermaids' Dreamworld" -- the
#: WORLD name. The record-by-key lookup (0x8B670) matches key2 against it, so a
#: record whose key2 != the client's current world is marked not-found and
#: skipped. key1 is still the per-event key (unmeasured). seca/secb span a full
#: day so whatever window semantics they carry, this record errs toward "open".
DEFAULT_LIST_KEY2 = "Mermaids' Dreamworld"   # measured 2026-08-22 off the live client
DEFAULT_LIST = [{
    "id": 1,
    "bytes4": b"\x01\x00\x00\x00",
    "seca": 0,                       # 0:00:00
    "secb": 23 * 3600 + 59 * 60 + 59,  # 23:59:59
    "key1": "EVENT01",
    "key2": DEFAULT_LIST_KEY2,
    "name": "Tetra Master Event",
}]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-list", help="write b/g/TM0EventList (the schedule/gate) here")
    ap.add_argument("--out-data", help="write b/g/TM0EventDataList here")
    ap.add_argument("--out-member", help="write b/g/TM0EventMemberList here")
    ap.add_argument("--dump-list", help="decode an existing schedule blob")
    ap.add_argument("--dump-data", help="decode an existing data-list blob")
    ap.add_argument("--dump-member", help="decode an existing member-list blob")
    ap.add_argument("--markers", action="store_true",
                    help="with --out-list, emit a marked file for the shim rig")
    ap.add_argument("--winner", type=lambda s: int(s, 16), default=DEFAULT_WINNER,
                    help="member id (hex) to place FIRST, default %016X"
                         % DEFAULT_WINNER)
    ap.add_argument("--name", default="Lex", help="that member's displayed name")
    ap.add_argument("--score", type=int, default=100,
                    help="its +0x1C; must be non-zero or the gate fails")
    a = ap.parse_args()

    if a.dump_list:
        with open(a.dump_list, "rb") as f:
            dump_list(f.read())
    if a.dump_data:
        with open(a.dump_data, "rb") as f:
            dump_data(f.read())
    if a.dump_member:
        with open(a.dump_member, "rb") as f:
            dump_members(f.read())

    if a.out_list:
        blob = build_markers() if a.markers else build_list(DEFAULT_LIST)
        dump_list(blob)
        with open(a.out_list, "wb") as f:
            f.write(blob)
        print("wrote %s (%d B)" % (a.out_list, len(blob)))

    if a.out_data or not (a.dump_data or a.dump_member or a.out_member):
        blob = build_data(DEFAULT_DATA)
        dump_data(blob)
        if a.out_data:
            with open(a.out_data, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (a.out_data, len(blob)))

    if a.out_member or not (a.dump_data or a.dump_member or a.out_data):
        # Two also-rans BELOW us, so the placing arithmetic is exercised without
        # anyone outranking the named winner. Distinct scores, descending.
        rows = [
            (a.winner, a.name, a.score, 0),
            (0x0000001000000002, "MEMBER-B", max(a.score - 10, 1), 0),
            (0x0000001000000003, "MEM-C", max(a.score - 20, 1), 0),
        ]
        blob = build_members(rows)
        dump_members(blob)
        if a.out_member:
            with open(a.out_member, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (a.out_member, len(blob)))
