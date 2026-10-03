"""TOURNAMENT PAIRING ROTATES OPPONENTS INSTEAD OF REMATCHING THE LOWEST IDS.

2026-10-03 17:11: with three players in the room, members 3 and 15 were paired
again as soon as both were free (the two lowest ids) while member 52 waited.
Now the longest-waiting player goes first, against the opponent they have
faced least recently.

    python tools/tm_event_fair_pairing_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-fair-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
from tmgame import tournament as T                                 # noqa: E402

FAILS = []
ROOM = 0x2000000051


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def reset():
    T._EVENT_MATCH.clear()
    T._EVENT_FAILED.clear()
    T._EVENT_LAST_MET.clear()
    T._EVENT_STATUS.clear()


def pair(waits):
    """waits: member -> seconds they have been waiting. Returns the pair made."""
    now = time.time()
    for mid, w in waits.items():
        T._EVENT_STATUS[mid] = ("A", ROOM, now - w)
    T._event_try_pair(ROOM)
    m = next(iter(T._EVENT_MATCH.values()), None)
    who = sorted(m["who"]) if m else None
    T._EVENT_MATCH.clear()
    return who


def main():
    T._event_present = lambda room: None
    T.event_phase = lambda now=None: ("running", time.time() - 3000, time.time() + 600)
    T.pushqueue._queue_push = lambda *a, **k: None

    reset()
    T._EVENT_LAST_MET[frozenset((3, 15))] = time.time() - 30   # they just played
    got = pair({3: 25, 15: 22, 52: 300})                      # ruuko waited longest
    check(got is not None and 52 in got, "the longest-waiting player is matched first", str(got))

    reset()
    T._EVENT_LAST_MET[frozenset((3, 15))] = time.time() - 30
    got = pair({3: 300, 15: 25, 52: 22})                      # 3 waited longest, just met 15
    check(got == [3, 52], "they get the opponent they have not just played", str(got))

    reset()
    T._EVENT_LAST_MET[frozenset((3, 15))] = time.time() - 30
    got = pair({3: 300, 15: 25})                              # nobody else in the room
    check(got == [3, 15], "a rematch only when there is no one else", str(got))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all fair pairing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
