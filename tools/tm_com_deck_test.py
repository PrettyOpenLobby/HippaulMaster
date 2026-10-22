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


def skill_and_elo_checks():
    """The COM AI dial and the COM Elo read PlPrm.BIN's average rank (+0x04),
    so with the real file both must VARY by opponent -- before PlPrm.BIN was
    on prod every opponent read rank 200 and the dial topped out at 0.75."""
    from tmgame import careerstats, comai, vscom
    table = vscom._plprm()
    check(len(table) == 31, "PlPrm.BIN: 30 opponents + the placeholder row", str(len(table)))
    ranks = {i: vscom._com_avg_rank(i) for i in range(1, 31)}
    check(len(set(ranks.values())) >= 25 and 200 not in (ranks[1], ranks[30]),
          "average ranks differ per opponent (none of the ends reads the 200 fallback)",
          repr(ranks))
    check(ranks[1] == 300 and ranks[2] == 293 and ranks[29] == 111 and ranks[30] == 100,
          "Natasha 3.00, Mick 2.93, ... the two strongest 1.11 and 1.00")
    skills = {i: comai.skill_of(i) for i in range(1, 31)}
    check(all(skills[i] == 0.0 for i in range(1, 10)),
          "opponents 1-9 keep the random moves (skill 0)", repr(skills))
    # d4c20b97 capped the strongest at POL_TM_COM_AI_CEILING (default 0.85):
    # at 1.0 it never slipped, and players found that unfair
    check(abs(skills[30] - 0.85) < 1e-9 and 0.75 < skills[29] < skills[30],
          "the strongest opponent plays at the 0.85 ceiling, the next just below"
          " it (live never went above 0.75)",
          "%.2f / %.2f" % (skills[29], skills[30]))
    order = sorted(range(1, 31), key=lambda i: ranks[i], reverse=True)
    check(all(skills[a] <= skills[b] for a, b in zip(order, order[1:])),
          "skill rises as the average rank falls")
    check(len({round(skills[i], 3) for i in range(10, 31)}) >= 18,
          "opponents 10-30 each get their own skill",
          repr(sorted({round(skills[i], 2) for i in range(10, 31)})))
    elos = {i: careerstats._com_elo(i) for i in range(1, 31)}
    check(len(set(elos.values())) == 30 and min(elos, key=elos.get) == 1
          and max(elos, key=elos.get) == 30,
          "COM Elo: thirty different ratings, Natasha lowest, the strongest highest",
          repr(elos))
    # the house prize follows the same ranks: a stake-0 win over Mick is 153
    check(vscom._house_prize(0, [ranks[2]], [6, 4], 0) == 153
          and vscom._house_prize(0, [ranks[30]], [6, 4], 0) == 250,
          "the house prize reads the same ranks (Mick 153, the strongest 250)")


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
    skill_and_elo_checks()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        return 1
    print("all COM deck checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
