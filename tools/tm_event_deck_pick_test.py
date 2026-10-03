"""THE TOURNAMENT DECK PICK MUST LET THE PLAYER INTO THE EVENT ROOM.

2026-10-03 01:58: the first live pick uploaded its five cards, got @Select=/A=0,
and sat on "loading" for good. After the card scene closes, the client's entry
helper waits (sub-state 5, no timeout) for (0xB2, 0x17) @EQuit from the same
sender with the same shop byte. Replays that upload.

    python tools/tm_event_deck_pick_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-deck-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
import tmeventstate                                                # noqa: E402
from tmgame import dispatch                                        # noqa: E402

FAILS = []

# the live upload, 2026-10-03T01:58:06Z (member 3, shop 2)
UPLOAD = (b"B2000702@CardSelect=/C=5@N0=/D=237|43|1|54|68|15|16|255"
          b"@N1=/D=226|35|1|25|65|11|14|255@N2=/D=202|53|1|44|24|11|243|255"
          b"@N3=/D=142|22|1|42|58|11|55|255@N4=/D=141|45|1|53|45|13|180|255")


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main():
    saved = {}
    tmeventstate.set_deck = lambda ws, mid, rows: saved.update({mid: rows})
    out = dispatch.handle_line(UPLOAD, member_id=3)
    lines = out if isinstance(out, list) else [out]
    lines = [l for l in lines if l]
    sel = [l for l in lines if b"@Select=" in l]
    eq = [l for l in lines if b"@EQuit=" in l]
    check(len(saved.get(3, [])) == 5, "the five picked cards are saved as the tournament deck")
    check(len(sel) == 1 and sel[0][:2] == b"B2" and sel[0][2:4] == b"00"
          and sel[0][4:6] == b"07" and sel[0][6:8] == b"02",
          "(0xB2, 7, shop 2) @Select=/A=0 answers the upload", repr(sel))
    check(len(eq) == 1 and eq[0][:2] == b"B2" and eq[0][4:6] == b"17" and eq[0][6:8] == b"02"
          and b"@EQuit=/D=0/Stat=0" in eq[0],
          "(0xB2, 0x17, shop 2) @EQuit lets the entry helper into the room", repr(eq))
    check(bool(sel and eq) and lines.index(sel[0]) < lines.index(eq[0]),
          "@Select comes first (the card scene polls msgid 7 before 0x17)")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all deck pick checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
