#!/usr/bin/env python3
"""A DECK KEEPS ITS OWN COPY OF A CARD AFTER THE CARD GROWS.

The slot map records a card by the stats it had when it was placed; cards
grow as they are played. With no exact match `_deck_slot_bytes` took the
FIRST copy of the id, so a player's 9M56 Hades came back as their 7M56 one
after a game, and the deck's order shuffled.

    python tools/tm_deck_slot_growth_test.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tm_testenv                                                  # noqa: E402

tm_testenv.setup(need_core=False)
TMP = tempfile.mkdtemp(prefix="tm-deck-growth-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)

import tetramaster as T                                            # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


HADES, ZIDANE = 97, 90


def main():
    # rows: [id, attack, type, pdef, mdef, power, arrows]
    cards = [
        [HADES, 70, 1, 50, 60, 10, 0x55],     # the weaker Hades, first in the collection
        [ZIDANE, 90, 2, 70, 80, 12, 0x81],
        [HADES, 93, 1, 52, 64, 14, 0x33],     # the player's Hades, grown since it was placed
    ]
    data = {"deck_slots": {"3": [HADES, 90, 1, 50, 60, 0x33],     # placed when it read 9M56
                           "4": [ZIDANE, 90, 2, 70, 80, 0x81]}}
    out = T._deck_slot_bytes(data, cards)
    check(out[2] == 3 and out[0] == T.DECK_SLOT_NONE,
          "the grown Hades keeps its deck slot, the weaker copy stays out", repr(out))
    check(out[1] == 4, "Zidane keeps his slot", repr(out))
    check(data["deck_slots"]["3"] == [HADES, 93, 1, 52, 64, 0x33],
          "the slot map now names the card as it is", repr(data["deck_slots"]["3"]))
    # an exact match still wins over a nearer same-arrows copy
    data2 = {"deck_slots": {"0": [HADES, 70, 1, 50, 60, 0x55]}}
    check(T._deck_slot_bytes(data2, cards)[0] == 0, "an exact match is still taken")
    # a slot recorded by id alone still lands on a copy of that id
    data3 = {"deck_slots": {"1": [ZIDANE]}}
    check(T._deck_slot_bytes(data3, cards)[1] == 1, "a slot with no stats matches by id")
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all deck growth checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
