"""NOTHING THE CLIENT NEVER READS MAY BE LEFT IN ITS STORE AFTER A TOURNAMENT GAME.

2026-10-03 (narration log): after every finished tournament game, the client's
receive store kept two replies it never read -- @Ready=/Go=1 (looked up 0 times)
and the return-from-game @EventEn -- and with them there the next seating sent
only a Pong, never @CheckJoinTable: "Could not start game". A fresh client
(empty store) always started. Neither reply is sent there now.

    python tools/tm_event_unread_replies_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-unread-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
from tmgame import dispatch, matchmaking, tournament as T          # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def lines_of(out):
    return [l for l in (out if isinstance(out, list) else [out]) if l]


def main():
    T.event_phase = lambda now=None: ("running", time.time() - 600, time.time() + 600)
    dispatch.event_phase = T.event_phase
    T.note_seen = lambda mid, ph: None
    m = {"who": [3, 15], "room": 0x2000000051, "index": 2, "tblid": 0x2100002002,
         "ans": {3, 15}, "t": time.time(), "started": True, "playing": True}
    T._EVENT_MATCH.update({3: m, 15: m})
    matchmaking._match_of = lambda mid: ("#TM0R081", 34, [3, 15])

    out = lines_of(dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id=3))
    check(not any(b"@Ready=" in l for l in out),
          "a tournament game's @Ready= gets no /Go= answer", repr(out))

    out = lines_of(dispatch.handle_line(b"D1000400@EventTimeReq=/NN=00000000000000AA", member_id=3))
    check(any(b"@EventTimeReqCheck" in l for l in out), "back from the game: the time answer still goes")
    check(not any(b"@EventEn" in l for l in out), "back from the game: no @EventEn", repr(out))

    T._EVENT_MATCH.clear()
    out = lines_of(dispatch.handle_line(b"D1000400@EventTimeReq=/NN=00000000000000AA", member_id=3))
    check(any(b"@EventEn=/Ans=1" in l for l in out), "a real entry still gets @EventEn", repr(out))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all unread-reply checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
