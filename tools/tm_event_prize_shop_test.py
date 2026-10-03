"""THE TOURNAMENT PRIZE @EcmInit MUST NOT USE SHOP BYTE 1.

2026-10-03, first full cup: the winner's prize was credited (1979 -> 61979, +3
cards) but their client sat on a loading screen forever and never sent @Get= or
@Quit=. The Event Shop waits for the 0x13 arm to return exactly 1, and that arm
returns -1 for shop byte 1; we sent sh=1 /N=1.

    python tools/tm_event_prize_shop_test.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-event-prize-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ["POL_TM_EVENT_ZONE_MEMBERS"] = "*"

import tetramaster  # noqa: E402,F401
import tmeventstate                                                # noqa: E402
from tmgame import tournament as T                                 # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main():
    tmeventstate.standings = lambda ws: {"52": {"steps": 6, "missions": 0b11}}
    tmeventstate.mark_paid = lambda ws, mid: None
    T.purse.money_of = lambda mid: 1979
    T.purse._set_money = lambda mid, v, why: None
    T.matchend._bump_prize = lambda mid, v, why: None
    T.collection._collection_add_cards = lambda mid, cards: None
    T.cardshop._shopbuy_cards = lambda pack: [[137, 58, 0, 20, 17, 8, 137]] * 5
    T.cardtables.card_level_of = lambda mid: 390

    for env in (None, "1"):
        if env is None:
            os.environ.pop("POL_TM_EVENT_PRIZE_SHOP", None)
        else:
            os.environ["POL_TM_EVENT_PRIZE_SHOP"] = env
        lines = T._event_prize_lines(52)
        ecm = next((l for l in lines if b"@EcmInit=" in l), b"")
        hdr, body = ecm[:8], ecm[8:]
        shop = int(hdr[6:8], 16) if len(hdr) == 8 else None
        n = re.search(rb"/N=(\d+)", body)
        n = int(n.group(1)) if n else None
        check(shop not in (None, 1), f"@EcmInit shop byte is not 1 (POL_TM_EVENT_PRIZE_SHOP={env})",
              f"header {hdr!r}")
        check(shop == n, "header shop byte matches /N=", f"sh={shop} /N={n}")
        pz = T.event_prizes()
        want = pz["money"][0] + 2 * pz["mission_money"] + pz["class_money"][1]   # 6 steps = Silver
        check(b"/PM=%d" % want in body and b"/C=3" in body,
              "prize body carries 1st + 2 missions + the Silver class prize, and the cards",
              "want /PM=%d" % want)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all prize shop checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
