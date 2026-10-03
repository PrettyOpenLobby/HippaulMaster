"""A DECLINED OR OLD MATCH OFFER MUST NOT SPLIT TWO PLAYERS ACROSS TABLES.

2026-10-03 05:00 test event: the first offer (table 1) went out with the
"games begin" push; one client declined it, the other accepted it a second
later, was never told it was off, and walked to table 1 while the re-pair sent
the first to table 2 -- the server even counted the table-1 accept for table 2.
Both sat alone: black screen. Replays that, plus the start-of-event hold.

    python tools/tm_event_stale_answer_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-stale-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
from tmgame import tournament as T                                 # noqa: E402

FAILS = []
ROOM = 0x2000000051
OFF = b"@MuchMake=/Start=-1"


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main():
    pushed = []
    T.pushqueue._queue_push = lambda mid, body, why, after=0: pushed.append((mid, body))
    T._event_present = lambda room: None
    start = [time.time() - 600]
    T.event_phase = lambda now=None: ("running", start[0], start[0] + 600)
    old = time.time() - 600

    # the start-of-event hold
    start[0] = time.time() - 2
    for mid in (3, 52):
        T._EVENT_STATUS[mid] = ("A", ROOM, old)
    T._event_try_pair(ROOM)
    check(not T._EVENT_MATCH, "no offer in the first seconds of the event")
    start[0] = time.time() - 600

    T._event_try_pair(ROOM)
    m1 = T._EVENT_MATCH.get(3)
    check(m1 is not None and m1["index"] == 1, "first offer at table 1")
    pushed.clear()
    T._event_match_answer(3, 0, 1)                              # 05:00:05 decline
    check((52, T._event_call_off_body()) in pushed,
          "the decline calls the match off for the player who had NOT answered yet")
    r = T._event_match_answer(52, 1, 1) if 52 in T._EVENT_MATCH else T.event_stale_answer(52, 1, 1)
    check(r is None, "their late accept of the dead offer gets no SECOND call-off", repr(r))

    T._event_try_pair(ROOM)                                     # 05:00:07 re-pair
    m2 = T._EVENT_MATCH.get(3)
    check(m2 is not None and m2["index"] != 1, "the re-pair is on another table",
          "table %s" % (m2 and m2["index"]))
    r = T._event_match_answer(52, 1, 1)                          # an old table-1 accept
    check(r == T._event_call_off_body() and 52 not in m2["ans"],
          "an accept naming the OLD table is not counted and is called off", repr(r))
    T._event_match_answer(52, 1, m2["index"])
    out = T._event_match_answer(3, 1, m2["index"])
    check(m2.get("started") and b"/Start=1" in (out or b""),
          "accepts naming the current table start the match")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all stale answer checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
