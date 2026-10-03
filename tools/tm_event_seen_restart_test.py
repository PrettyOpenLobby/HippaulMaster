"""THE TOURNAMENT-SCREEN LIST MUST SURVIVE AN AUTHSESS RESTART.

2026-10-03 test cup: authsess restarted at 04:12, the in-memory list of members
who had heard the event phase was gone, the clients never re-asked, and at 04:15
nobody was sent "time is up" -- every client said it could not exit the
tournament correctly. Replays: hear 'running', restart, the phase moves on.

    python tools/tm_event_seen_restart_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-seen-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
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
    kv = FakeKV()
    tmstore.kv = kv
    pushed = []
    T.pushqueue._queue_push = lambda mid, body, why, after=0: pushed.append((mid, body))

    phase = ["running"]
    T.event_phase = lambda now=None: (phase[0], 0, 0)

    T.note_seen(3, "running")
    T.note_seen(52, "running")
    check(kv.d.get(T._SEEN_KEY) == {"3": "running", "52": "running"},
          "hearing the phase is saved outside the process", str(kv.d.get(T._SEEN_KEY)))

    # authsess restarts: the module state is gone, Valkey is not
    T._EVENT_SEEN.clear()
    T._SEEN_LOADED[0] = False

    phase[0] = "over"
    T._event_phase_push(3)
    check(any(mid == 3 and b"/End=0" in body for mid, body in pushed),
          "after a restart, 'time is up' still reaches a member on the tournament screen")
    check(52 in T._EVENT_SEEN, "the other member is back on the list too")
    check(kv.d[T._SEEN_KEY].get("3") == "over", "the new phase is saved", str(kv.d[T._SEEN_KEY]))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all restart checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
