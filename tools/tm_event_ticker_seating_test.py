"""NO TOURNAMENT TICKER PAGE MAY RIDE A SEATING REPLY.

2026-10-03 15:10:24: after a finished game the result ticker's pages were still
queued when the next match formed; one rode the table's @GameEA reply, the
client queued Pong + @CheckJoinTable, wrote only the Pong, and said "Could not
start game" (narration log). Every start after a finished game failed this way;
a fresh client (nothing queued) always started.

    python tools/tm_event_ticker_seating_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-ticker-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
from tmgame import pushqueue, tournament as T                      # noqa: E402

FAILS = []
ROOM = 0x2000000051


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def queued(mid):
    return [bytes(e[1]) for e in pushqueue._PUSHES.get(pushqueue._push_key(mid)) or []]


def main():
    T._event_present = lambda room: None
    T.event_phase = lambda now=None: ("running", time.time() - 600, time.time() + 600)
    T._EVENT_SEEN.update({3: "running", 15: "running"})
    T._SEEN_LOADED[0] = True
    old = time.time() - 600
    for mid in (3, 15):
        T._EVENT_STATUS[mid] = ("A", ROOM, old)
        for pg in range(3):     # the result ticker, still trickling out
            pushqueue._queue_push(mid, b"D7000000@ETelop=/ID=585/Lp=3/Pg=%d|5/St=41" % pg,
                                  "tournament ticker (game result)", after=pg)
        pushqueue._queue_push(mid, b"D1000200@EventTime=/End=0", "not a ticker page")

    T._event_try_pair(ROOM)
    check(3 in T._EVENT_MATCH and 15 in T._EVENT_MATCH, "the pair is matched")
    for mid in (3, 15):
        q = queued(mid)
        check(not any(b[:2] == b"D7" for b in q),
              f"member {mid}: queued ticker pages are dropped at the match", repr(q))
        check(any(b"@EventTime" in b for b in q), f"member {mid}: other pushes are kept")

    aud = T._ticker_audience(ROOM)
    check(3 not in aud and 15 not in aud, "a ticker broadcast skips players being seated", repr(aud))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all ticker seating checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
