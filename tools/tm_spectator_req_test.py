"""A SPECTATOR'S BARE @Req= AT A LIVE TABLE IS NOT A VS. COM ENTRY.

2026-10-03: a player walking up to watch a live tournament game sent a bare
@Req= to that table's peer; the server took it for the PS2 VS. COM entry,
queued a COM board + @GameEA=/Exit=1, and the @Data=/Watch= that followed found
that fake COM game instead of the match -- the spectator hung on loading.

    python tools/tm_spectator_req_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-spectator-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)

import tetramaster  # noqa: E402,F401
from tmgame import boardrules, dispatch, matchmaking, pushqueue, vscom   # noqa: E402

FAILS = []
TABLE = ("#TM0R081", 33)


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main():
    matchmaking._peer_table = lambda peer: TABLE if peer == b"UTABLEPEER" else (None, None)
    boardrules._MATCH_TURN[TABLE] = {"turn": 2}
    out = dispatch.handle_line(b"01000000@Req=/NN=0000000000000000/HID=0/Tm0=0/Tm1=0/Dm=0/Vol=0/CN=/HN=",
                               member_id=52, peer_nick=b"UTABLEPEER")
    lines = [l for l in (out if isinstance(out, list) else [out]) if l]
    check(any(b"@PLAYACK=/EN=1" in l for l in lines), "the spectator still gets @PLAYACK", repr(lines))
    check(not vscom._COM_GAME.get(pushqueue._push_key(52)), "no VS. COM game is made for them")
    q = [bytes(e[1]) for e in pushqueue._PUSHES.get(pushqueue._push_key(52)) or []]
    check(not any(b"@ComGameInit" in b or b"@GameEA=/Exit" in b for b in q),
          "no COM board or leave ack is queued", repr(q))

    boardrules._MATCH_TURN.pop(TABLE, None)
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all spectator checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
