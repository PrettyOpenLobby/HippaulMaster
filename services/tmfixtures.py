"""The server-side lobby and default-data blobs Tetra Master fetches, built
here instead of shipped as files.

The client reads a set of fixed-size blobs over the lobby band's 3:0 fetch --
the zone list, the room list per zone, the room's member/table list, the
auction's price-band counts, the ranking header and lists, the event
schedule. Nothing in them comes from Square Enix: every byte is either a
layout the client's own readers dictate (sizes, offsets, the letter
encodings) or content this server authors (zone and room names, the tables
in a room). So they are built from those layouts and this module's tables,
once per process, and the live patchers in tmtitle.py (player counts, the
zone's dial address, the room's live roster, the auction counts) write the
moving parts in at serve time exactly as they did over a file.

    template(path)  -> bytes or None    what a member with no stored copy is served
    ptl()           -> bytes            the room member/table list (tables only)
    python tmfixtures.py --write DIR    materialise every blob into DIR
    python tmfixtures.py --compare DIR  byte-compare against blobs in DIR

The builders for the two big lists live in tools/ (tmptl.py, tmroomlist.py,
tmevent.py) because they double as command-line inspectors; this module
imports them from there.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# the tools directory: beside services/ in a checkout, under the code in the
# image (the Dockerfile copies tools/ to /app/tools/)
for _TOOLS in (os.path.normpath(os.path.join(HERE, os.pardir, "tools")),
               os.path.join(HERE, "tools")):
    if os.path.isdir(_TOOLS) and _TOOLS not in sys.path:
        sys.path.append(_TOOLS)      # append, not insert: services/ stays first

import tmrank                                                    # noqa: E402
import tmroom                                                    # noqa: E402

# --- the zones and their rooms (server-authored content) ---------------------
#: (zone id byte, name, room count). The zone id byte names the room list the
#: client fetches next (`b/g/RL%03d`); the name carries the client's five
#: letter prefix the way the authored list always did. Two zones, as SE's
#: English service had (period screenshots), seven rooms each.
ZONES = (
    (0, "ENAAAMermaids' Dreamworld"),
    (1, "ENAAADragon King's Dance Hall"),
)
#: The serving address goes into every zone row at serve time (tmtitle's
#: `_zone_host_live`, from POL_TM_ZONE_HOST or POL_ADVERTISE); this is the
#: placeholder it replaces, never what a client dials.
ZONE_HOST_PLACEHOLDER = "0.0.0.0"

#: (type, vscom, name, rules letters at +0x49, table letters at +0x6A). Type
#: 'A' = Set Rules, 'B' = Free Rules; +0x29 'A' opens VS. COM in that room
#: (the client stores the byte raw and compares it to 'A'). The letter blocks
#: are this server's authored rank gates and table parameters, in the
#: client's own letter encoding ('A' = 0).
ROOMS = (
    ("B", "A", "Freewheeler Room 1", "AAAAAAA", "ABAACAAAAAAAA"),
    ("B", "A", "Freewheeler Room 2", "AAAAAAA", "ABAACAAAAAAAA"),
    ("B", "A", "Freewheeler Room 3", "AAAAAAA", "ABAACAAAAAAAA"),
    ("A", "A", "Novice Hall",        "AAAKBCA", "ABAACAAAAAAAA"),
    ("A", "A", "Veteran Arena",      "AAGEFDB", "ABAACAAAAAAAA"),
    ("A", "A", "Master Arena",       "ADOIPEC", "ABAACAAAAAAAA"),
    ("B", "A", "Player Showdowns",   "AAAAAAA", "ABAACAAAAAAAA"),
)
#: Room ids and channels: zone z's rooms are ids 10*z + 1..7, channels
#: `#TM0R%03d` of the same number (zone 0 = #TM0R001.., zone 1 = #TM0R011..).
ROOMS_PER_ZONE = len(ROOMS)


def room_rows(zone):
    out = []
    for i, (typ, vscom, name, rules, tbl) in enumerate(ROOMS):
        no = 10 * zone + i + 1
        out.append({"id": no, "type": typ, "vscom": vscom, "name": name,
                    "rules": rules, "tables": tbl, "channel": "#TM0R%03d" % no})
    return out


def zone_list():
    """`b/g/ZL`: 2120 bytes, count at +0x40, 64-byte rows from +0x48."""
    buf = bytearray(tmroom.ZL_TOTAL)
    struct.pack_into("<I", buf, tmroom.ZL_COUNT_OFF, len(ZONES))
    for i, (zid, name) in enumerate(ZONES):
        o = tmroom.ZL_HDR + i * tmroom.ZL_REC
        struct.pack_into("<I", buf, o + tmroom.ZL_F_PLAYERS, 0)      # live-patched
        struct.pack_into("<I", buf, o + tmroom.ZL_F_ROOMS, ROOMS_PER_ZONE)
        n = name.encode("cp932")[:0x2C - 0x0C - 1]
        buf[o + 0x0C:o + 0x0C + len(n)] = n
        h = ZONE_HOST_PLACEHOLDER.encode("ascii")
        buf[o + tmroom.ZL_F_HOST:o + tmroom.ZL_F_HOST + len(h)] = h
        buf[o + tmroom.ZL_F_ZONEID] = zid
    return bytes(buf)


def room_list(zone):
    """`b/g/RL%03d` for `zone`, or None for a zone this server does not have."""
    if not any(z == zone for z, _n in ZONES):
        return None
    import tmroomlist
    return tmroomlist.build(room_rows(zone))


def ptl():
    """`b/g/PTL`: the table half only, no members, serial 1. A member with no
    stored blob is served this and `tmroom.build_ptl` fills the member half in
    from the live roster; a baked-in member row would be a phantom player in
    every room."""
    import tmptl
    return tmptl.build([], tmptl.DEFAULT_TABLES, 1)


def event_members():
    """`[(member_id, name, score, flag)]` for the event board, from
    POL_TM_EVENT_MEMBERS = "hexid:name:score,..." -- nobody by default, so an
    event opens with an empty board rather than a stranger on top of it."""
    rows = []
    for item in os.environ.get("POL_TM_EVENT_MEMBERS", "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        try:
            mid = int(parts[0], 16)
            name = parts[1] if len(parts) > 1 else ""
            score = int(parts[2]) if len(parts) > 2 else 1
        except (ValueError, IndexError):
            continue
        rows.append((mid, name, score, 0))
    return rows


def event_blob(path):
    import tmevent
    if path == "b/g/TM0EventList":
        return tmevent.build_list(tmevent.DEFAULT_LIST)
    if path == "b/g/TM0EventDataList":
        return tmevent.build_data(tmevent.DEFAULT_DATA)
    if path == "b/g/TM0EventMemberList":
        return tmevent.build_members(event_members())
    return None


_CACHE = {}


def template(path):
    """The blob a member with no stored copy is served for `path`, or None
    for a path this server does not author. Built once per process."""
    if path in _CACHE:
        return _CACHE[path]
    blob = None
    if path == "b/g/ZL":
        blob = zone_list()
    elif path.startswith("b/g/RL") and path[6:].isdigit():
        blob = room_list(int(path[6:]))
    elif path == "b/g/PTL":
        blob = ptl()
    elif path == "b/g/TM0AucData":
        blob = bytes(20)                    # five u32 band counts, live-patched
    elif path == tmrank.RKDATA:
        blob = tmrank.build_rkdata({}, 0)   # the weekly job publishes the real one
    elif tmrank.is_list_path(path):
        # ONE ZERO ROW, NOT AN EMPTY LIST: the client's rankings scene bails on a
        # row count of 0 before it ever reads the file (TM.dll rva 0x1750A8).
        blob = bytes(tmrank.REC)
    elif path.startswith("b/g/TM0Event"):
        blob = event_blob(path)
    if path.startswith("b/g/TM0Event") and blob is not None:
        return blob                         # env-dependent: not cached
    _CACHE[path] = blob
    return blob


PATHS = ("b/g/ZL", "b/g/RL000", "b/g/RL001", "b/g/PTL", "b/g/TM0AucData",
         "b/g/TM0RkData", "U/g/TM0_RANKLIST", "U/g/TM0_RANKLIST0",
         "U/g/TM0_RANKLIST1", "U/g/TM0_RANKLIST2", "U/g/TM0_RANKLIST3",
         "U/g/TM0_RANKLIST4", "b/g/TM0EventList", "b/g/TM0EventDataList",
         "b/g/TM0EventMemberList")


def file_name(path):
    return path.replace("/", "_") + ".bin"


def write_all(out):
    os.makedirs(out, exist_ok=True)
    for p in PATHS:
        b = template(p)
        if b is None:
            continue
        with open(os.path.join(out, file_name(p)), "wb") as f:
            f.write(b)
        print("  %-26s %6d B" % (p, len(b)))


def compare(other):
    """Rows of (path, ours, theirs, verdict, differing offsets) against blobs
    named `<path with _>.bin` in `other`."""
    rows = []
    for p in PATHS:
        b = template(p)
        fn = os.path.join(other, file_name(p))
        if not os.path.isfile(fn):
            rows.append((p, len(b) if b else 0, None, "no file to compare", []))
            continue
        with open(fn, "rb") as f:
            o = f.read()
        if b == o:
            rows.append((p, len(b), len(o), "IDENTICAL", []))
            continue
        diffs = [i for i in range(min(len(b or b""), len(o))) if b[i] != o[i]]
        rows.append((p, len(b) if b else 0, len(o),
                     "differs at %d byte(s)" % (len(diffs) + abs(len(b or b"") - len(o))),
                     diffs))
    return rows


def _main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", metavar="DIR")
    ap.add_argument("--compare", metavar="DIR")
    a = ap.parse_args()
    if a.write:
        write_all(a.write)
    if a.compare:
        for p, ours, theirs, verdict, diffs in compare(a.compare):
            print("  %-26s ours=%6s theirs=%6s  %s%s" % (
                p, ours, theirs, verdict,
                ("  first at " + ", ".join("0x%x" % d for d in diffs[:6])) if diffs else ""))
    if not (a.write or a.compare):
        for p in PATHS:
            b = template(p)
            print("  %-26s %s" % (p, "%d B" % len(b) if b else "-"))


if __name__ == "__main__":
    _main()
