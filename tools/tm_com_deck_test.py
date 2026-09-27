"""EVERY VS. COM OPPONENT PLAYS ITS OWN DECK -- PlPrm.BIN +0x0B.

Until 2026-09-26 the deck was guessed as `25 + index - 1` wrapped inside the
25..44 ladder, so every opponent from 21 on (Vivi, Kuja, Garnet, Beatrix,
Regent Cid...) was dealt a beginner's deck. The client's own PlPrm.BIN names
each opponent's PackPrm record at +0x0B; two of them were already known
independently (Quina's mascots 27, Cid's airships 12), which is what pins the
reading.

Needs PlPrm.BIN and PackPrm.BIN in services/tmdata/ (tools/tmdata_build.py);
SKIPs without them.

    python tools/tm_com_deck_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "services"))
TMP = tempfile.mkdtemp(prefix="tm-com-deck-")
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ.setdefault("POL_DATA_DIR", TMP)

import tetramaster as T                                            # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def tiers(no):
    return sorted({d[0] for d in (T._pack_rec(no) or {}).get("draw") or [] if d[3] > 0 and d[0] != 255})


def main():
    if not T._plprm() or not T._packprm():
        print("[SKIP] tm_com_deck_test: services/tmdata/PlPrm.BIN or PackPrm.BIN "
              "missing (run tools/tmdata_build.py)")
        return 0
    decks = {i: T._com_deck_record(i) for i in range(1, 31)}
    check(all(T._is_com_deck(T._pack_rec(n)) for n in decks.values()),
          "all thirty opponents get a real COM deck (no prize record, no fallback)", repr(decks))
    check(decks[3] == 27 and decks[30] == 12, "Quina plays her mascots (27), Regent Cid his airships (12)")
    check(decks[22] == 11 and min(tiers(11)) >= 7, "Vivi plays his own deck, tiers 7+", f"{decks[22]} {tiers(decks[22])}")
    late = [i for i in range(21, 31)]
    # (Trade King Gohn's 13 is all named cards -- tier 255 rows -- so no tiers)
    check(all(decks[i] not in (25, 26, 27, 28, 29) for i in late),
          "no late opponent (21-30) is dealt a first-rung beginner's deck",
          repr({i: (decks[i], tiers(decks[i])) for i in late}))
    rows = T._com_deck_rows(22)
    check(len(rows) == 5, "a COM hand is five cards", repr(rows))
    os.environ["POL_TM_COM_DECKS"] = "ladder"
    try:
        check(T._com_deck_record(22) == 26, "POL_TM_COM_DECKS=ladder restores the old guess (Vivi -> 26)")
    finally:
        del os.environ["POL_TM_COM_DECKS"]
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all COM deck checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
