"""A TOURNAMENT START THAT BOUNCES MUST NOT LOCK BOTH PLAYERS.

2026-10-03, first full cup: twice (02:22, 02:38) both players accepted, went to
the table ('E') and came straight back to the room ('A') with "Could not start
game". The match was 'started', and a started match was never declined or
expired, so neither player could be paired again until authsess restarted.
Replays that status sequence, the good one beside it (E -> B -> A), the
2-minute backstop, and the hold that keeps a failed pair from being re-paired
while someone else is waiting.

    python tools/tm_event_start_failed_test.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-start-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster as T                                            # noqa: E402
from tmgame import tournament                                      # noqa: E402

FAILS = []
ROOM = 0x2000000051


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def block(letter):
    s = list("I" + "A" * 15)
    s[13] = letter
    return "Player         " + "".join(s)


def status(mid, letter):
    vals = ["0x0", "0", "0", "0", "", "2", "0", "0", "0", "0x0000002000000051", block(letter)]
    T.note_event_status(mid, vals)


def started(who, t=None):
    now = time.time() if t is None else t
    m = {"who": list(who), "room": ROOM, "index": 1, "tblid": 0x2100001001,
         "ans": set(who), "t": now, "t_start": now, "started": True, "playing": False}
    for mid in who:
        tournament._EVENT_MATCH[mid] = m
    return m


def reset():
    tournament._EVENT_MATCH.clear()
    tournament._EVENT_STATUS.clear()
    tournament._EVENT_FAILED.clear()


def main():
    pushed = []
    tournament.pushqueue._queue_push = lambda mid, body, why: pushed.append((mid, why))
    tournament._event_present = lambda room: None
    tournament.event_phase = lambda now=None: ("running", 0, 0)

    reset()
    m = started([3, 52])
    status(3, "A"); status(52, "A")      # repeats of the pre-start letter
    check(T._in_event_match(3) and T._in_event_match(52),
          "an 'A' repeat right after the accept does NOT release anyone")
    status(3, "E"); status(52, "E")
    status(3, "A")
    check(not T._in_event_match(3), "02:38 replay: back in the room without playing -> released")
    status(52, "A")
    check(not tournament._EVENT_MATCH, "...and the second bounce ends the match")
    check(frozenset([3, 52]) in tournament._EVENT_FAILED, "the pair is remembered as failed")

    reset()
    m = started([3, 52])
    status(3, "E"); status(52, "E"); status(3, "B"); status(52, "B")
    check(m["playing"] and T._in_event_match(3), "02:14 replay: E -> B is a game, still matched")
    status(3, "A")
    check(T._in_event_match(52), "...and the ordinary release still waits for the other player")

    reset()
    started([3, 52], t=time.time() - tournament._EVENT_START_TTL - 1)
    tournament._event_try_pair(ROOM)
    check(not tournament._EVENT_MATCH, "a started match that never plays is cleared after the TTL")

    reset()
    old = time.time() - 600
    for mid in (3, 52, 64):
        tournament._EVENT_STATUS[mid] = ("A", ROOM, old)
    tournament._EVENT_FAILED[frozenset([3, 52])] = time.time()
    pushed.clear()
    tournament._event_try_pair(ROOM)
    who = tournament._EVENT_MATCH.get(3) or tournament._EVENT_MATCH.get(52)
    check(who is not None and set(who["who"]) != {3, 52},
          "a failed pair is skipped while a third player is waiting",
          str(who and who["who"]))

    reset()
    for mid in (3, 52):
        tournament._EVENT_STATUS[mid] = ("A", ROOM, old)
    tournament._EVENT_FAILED[frozenset([3, 52])] = time.time()
    tournament._event_try_pair(ROOM)
    check(3 in tournament._EVENT_MATCH, "...but with nobody else, the same pair is tried again")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all event start-failure checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
