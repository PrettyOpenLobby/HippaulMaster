"""A FINISHED TOURNAMENT GAME RELEASES EACH PLAYER ON THEIR OWN RETURN.

Until 2026-09-26 the first player back in the event room (status 'A' after
'B') cleared the match for BOTH, so the slower player's @Ready= was no longer
"in an event match": it took the ordinary PvP path and never got the event
@Continue= that the PC board polls with no timeout (80% bar). Measured on a
test host: the second @Ready= came 4 s after the first player had gone back.

    python tools/tm_event_release_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-release-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster as T                                            # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def block(letter):
    # value 10: 15-char name + 16 status letters; letter 13 is the event state
    s = list("I" + "A" * 15)
    s[13] = letter
    return "Player         " + "".join(s)


def status(mid, letter):
    vals = ["0x0", "0", "0", "0", "", "2", "0", "0", "0", "0x0000002000000051", block(letter)]
    T.note_event_status(mid, vals)


def main():
    m = {"who": [7, 8], "index": 1, "started": True, "t": 0, "ans": {}}
    T._EVENT_MATCH.clear()
    T._EVENT_MATCH[7] = m
    T._EVENT_MATCH[8] = m
    status(7, "B"); status(8, "B")
    check(m.get("playing"), "both at the table: the match is being played")
    status(7, "A")
    check(not T._in_event_match(7), "the first player back is released")
    check(T._in_event_match(8), "...and the other is STILL in the event match (their @Ready= gets the event @Continue=)")
    status(8, "A")
    check(not T._in_event_match(8) and not T._EVENT_MATCH, "the second player back ends it")
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all event release checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
