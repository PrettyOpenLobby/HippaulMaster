#!/usr/bin/env python3
"""`@GameEN=` refusals: a full table gets /EN=-32870, a repeat (opt-in) -32869.

    python tools/tm_gameea_refuse_test.py

Codes after Project Crystal Server's Table.cs; TM.dll's reserve scene has a
dialog per code (see tetramaster's `_gameea_refusal` banner). Everything runs
in a temp dir with the room registry sandboxed the way tetramaster's own
seating selftests do it. The member ids and their 64-bit app ids are made up
here; the app ids sit in the client key's coset the way a real `@Init=/NN=`
value does.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tm_testenv                                                  # noqa: E402

tm_testenv.setup(need_core=False)

tmp = tempfile.mkdtemp(prefix="gameea-")
for k, sub in (("POL_DATA_DIR", "data"), ("POL_RESOURCE_DIR", "res"),
               ("POL_LOG_DIR", "logs")):
    os.environ[k] = os.path.join(tmp, sub)
    os.makedirs(os.environ[k], exist_ok=True)
os.environ["POL_TM_TEACH_FILE"] = os.path.join(tmp, "logs", "teach.txt")
for k in ("POL_TM_GAMEEA_REFUSE", "POL_TM_GAMEEA_REFUSE_DUP",
          "POL_TM_TABLE_MAX_SEATS", "POL_TM_GAMEEA_EN", "POL_TM_GAMEEA"):
    os.environ.pop(k, None)

import tmroom  # noqa: E402
import tetramaster as tm  # noqa: E402

tm._say = lambda *a, **k: None          # quiet: the handler logs every step

bad = 0


def chk(what, got, want):
    global bad
    ok = got == want
    bad += not ok
    print("  %s %s: %r%s" % ("ok  " if ok else "FAIL", what, got,
                             "" if ok else "  (want %r)" % (want,)))


TABLE_PEER = "UE7QN1N9G"                 # table 1 of #TM0R001 (tm selftests)
ROOM_PEER = "UKXDBA266"                  # the room peer of #TM0R001
#: five members: a small member id each, and an app id in K's coset
MEMBERS = [(0x1001 + i, tmroom.app_id_for_guid(0x00E100000100 + i))
           for i in range(5)]


def reset():
    tmroom._FILE = os.path.join(tempfile.mkdtemp(dir=tmp), "r.json")
    tmroom._OWNER[0] = True
    for d in (tmroom._TABLES, tmroom._PEERS, tmroom._RECORDS,
              tmroom._ROOMS_SEQ, tmroom._SEATS, tm._SEATED):
        d.clear()
    tm._SEATED_ADOPTED.clear()
    tmroom._CACHE["mtime"] = -1.0
    for mid, _ident in MEMBERS:
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
    tmroom.note_room_peer("#TM0R001", tm.peer_guid(ROOM_PEER))


def reserve(i):
    mid, ident = MEMBERS[i]
    out = tm.handle_line(b"41000100@GameEN=/NN=%016X/L=1" % ident,
                         peer_nick=TABLE_PEER, member_id=mid)
    if isinstance(out, (list, tuple)):      # the reply plus any riding push
        out = b" ".join(x for x in out if isinstance(x, bytes))
    m = re.search(rb"@GameEA=/EN=(-?\d+)", out or b"")
    return int(m.group(1)) if m else None


def seated():
    return sorted(m for m, _i in (tm._seats_of("#TM0R001").get(1) or []))


print("normal joins keep the seat-count verdict")
reset()
chk("first joiner", reserve(0), 1)
chk("second joiner", reserve(1), 2)
chk("third joiner", reserve(2), 3)
chk("three seated", len(seated()), 3)

print("a full table is refused and nobody moves")
chk("fourth joiner", reserve(3), tm.GAMEEA_TABLE_FULL)
chk("still three seated", seated(), sorted(m for m, _ in MEMBERS[:3]))

print("a repeat by a seated member: default re-seats as before")
chk("member 0 again", reserve(0), 3)
chk("still three seated", len(seated()), 3)

print("a repeat with POL_TM_GAMEEA_REFUSE_DUP=1 is -32869")
os.environ["POL_TM_GAMEEA_REFUSE_DUP"] = "1"
chk("member 1 again", reserve(1), tm.GAMEEA_ALREADY_REGISTERED)
chk("member 1 keeps its seat", MEMBERS[1][0] in seated(), True)
os.environ.pop("POL_TM_GAMEEA_REFUSE_DUP")

print("POL_TM_TABLE_MAX_SEATS=2 fills at two")
reset()
os.environ["POL_TM_TABLE_MAX_SEATS"] = "2"
chk("first", reserve(0), 1)
chk("second", reserve(1), 2)
chk("third refused", reserve(2), tm.GAMEEA_TABLE_FULL)
os.environ.pop("POL_TM_TABLE_MAX_SEATS")

print("POL_TM_GAMEEA_REFUSE=0 restores the old seat-anyone answer")
reset()
os.environ["POL_TM_GAMEEA_REFUSE"] = "0"
for i in range(4):
    reserve(i)
chk("fourth joiner seated", len(seated()), 4)
os.environ.pop("POL_TM_GAMEEA_REFUSE")

print("an @GameEN= we cannot place is never refused")
reset()
chk("no table peer", tm._gameea_refusal(MEMBERS[0][0], None), None)
chk("no member", tm._gameea_refusal(None, TABLE_PEER), None)

print("%s" % ("ALL OK" if not bad else "%d FAILED" % bad))
sys.exit(1 if bad else 0)
