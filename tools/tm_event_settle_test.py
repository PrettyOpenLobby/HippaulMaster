"""THE RESULTS CLOSE AS SOON AS NO TOURNAMENT GAME IS LEFT, NOT AFTER 3 MINUTES.

2026-10-03: a fixed 180 s "over" phase kept every client on "Tallying
tournament results... Retry/Exit" when nothing was being played. Now "over"
lasts only while a game is live, capped at POL_TM_EVENT_SETTLE.

    python tools/tm_event_settle_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-settle-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
import tmcup                                                       # noqa: E402
import tmstore                                                     # noqa: E402
from tmgame import tournament as T                                 # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


class FakeKV:
    def __init__(self):
        self.d = {}

    def get_json(self, k):
        return self.d.get(k)

    def set_json(self, k, v, ttl=None):
        self.d[k] = v


def main():
    tmstore.kv = FakeKV()
    now = time.time()
    start, end = now - 600, now - 10            # time ran out 10 s ago
    tmcup.event_window = lambda now=None: (start, end)

    def phase(t=None):
        tmcup._GAMES_CACHE[0] = 0.0             # no 2 s cache inside the test
        return tmcup.event_phase(t if t is not None else now)[0]

    check(phase() == "over", "no record yet: stays 'over' (never close early on missing data)")
    tmcup.note_games_live(end, 1, now)
    check(phase() == "over", "a game still being played: 'over'")
    tmcup.note_games_live(end, 0, now)
    check(phase() == "closed", "no game left: 'closed' right away, not at +180 s")
    tmcup.note_games_live(end, 1, now)
    check(phase(end + 181) == "closed", "a game still live at the cap: 'closed' anyway")
    tmcup.note_games_live(end - 3600, 0, now)
    check(phase() == "over", "a record for ANOTHER event does not close this one")

    # the game server's side: started matches count, offers do not
    T.event_window = lambda now=None: (start, end)
    T._EVENT_MATCH.clear()
    a = {"who": [3, 15], "index": 1, "started": True}
    b = {"who": [52, 64], "index": 2, "started": False}
    T._EVENT_MATCH.update({3: a, 15: a, 52: b, 64: b})
    T._publish_games_live()
    check(tmstore.kv.d[tmcup.games_live_key()]["n"] == 1,
          "one started match counts once; an unanswered offer does not",
          str(tmstore.kv.d[tmcup.games_live_key()]))
    T._EVENT_MATCH.clear()
    T._publish_games_live()
    check(phase() == "closed", "the last game ending closes the results")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all settle checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
