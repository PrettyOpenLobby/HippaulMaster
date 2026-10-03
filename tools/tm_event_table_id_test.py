"""A TOURNAMENT MATCH MUST OFFER A TABLE THE CLIENT'S PTL LISTS, BY THAT ID.

2026-10-03 (TM.dll @PLAYACK arm, RVA 0x86AC9): the client looks the offered
TblId up in its PTL table array; not found -> index -1 -> it writes a table slot
0x34 bytes before the array, setting the global behind "/Dm=" to 2 and
corrupting its neighbours, and every seating after that first game failed with
"Could not start game". We offered the raw fixture id (0x21_0000n00n) while the
live PTL publishes canonical_table_id(n-1, room), and tables from #TM0T005 on
are event-host rows.

    python tools/tm_event_table_id_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-tblid-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
import tmroom                                                      # noqa: E402
from tmgame import tournament as T                                 # noqa: E402
from tmplugin import ptl                                           # noqa: E402

FAILS = []
ROOM = 0x2000000051


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main():
    T._event_present = lambda room: None
    T.event_phase = lambda now=None: ("running", time.time() - 600, time.time() + 600)
    T.pushqueue._queue_push = lambda *a, **k: None
    old = time.time() - 600
    seen = set()
    for _round in range(6):
        T._EVENT_MATCH.clear()
        T._EVENT_FAILED.clear()
        for mid in (3, 15):
            T._EVENT_STATUS[mid] = ("A", ROOM, old)
        T._event_try_pair(ROOM)
        m = T._EVENT_MATCH.get(3)
        if not m:
            check(False, "a pair is matched"); break
        n = m["index"]
        seen.add(n)
        check(m["tblid"] == tmroom.canonical_table_id(n - 1, ROOM),
              f"table {n}: the offered TblId is the PTL's id for it", "%016X" % m["tblid"])
        check(m["tblid"] != tmroom.canonical_table_id(n - 1, 0),
              f"table {n}: not the raw fixture id")
        T._event_match_clear(m, "test")
    hosts_from = ptl._PTL_EVENT_HOST_FROM - 2       # two service rows lead the table block
    check(max(seen) <= hosts_from, "only tables the event PTL keeps as tables are offered",
          "used %s, PTL keeps 1..%d" % (sorted(seen), hosts_from))
    check(len(seen) > 1, "rotation still spreads over those tables", str(sorted(seen)))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all table id checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
