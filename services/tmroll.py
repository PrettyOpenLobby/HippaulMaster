#!/usr/bin/env python3
"""Tetra Master card ROLL and GROWTH -- the per-instance stat model.

WHAT THIS IS. A card is NOT its `CardPrm` row. `CardPrm` is the card's CEILING;
every real instance is ROLLED strictly below it and then GROWS toward it through
use -- "not only will your card collection and your funds grow, but the cards you
use will grow too" (SE's own guidebook, `in_mnd.pml`). Both game clients confirm
this and NEITHER computes it:

  * the PC (`TM.dll` 0xB2CF0) and the PS2 (`TMaster.pex` 0x2b1c80) carry ONE
    byte-identical instantiation roll and ZERO growth arithmetic (verified by
    exhaustive sweep of every CardPrm consumer in both binaries, 2026-09-02);
  * both clients only STORE and RENDER the ABSOLUTE stats the server sends over
    `@Card` (`/D=[id,atk,type,pdef,mdef,arrows]`) and recompute the derived grade
    locally. The "+N" growth the results screen shows is the client diffing the
    server's new absolute record against the one it held.

So the SERVER owns both the roll and the growth. This module is the pure math for
both; the wiring (roll at acquisition, grow the used cards at match end, push via
`@Card`) lives in `tetramaster`.

WHAT IS MEASURED AND WHAT IS OURS
---------------------------------
  * MEASURED -- the client's own roll, byte-for-byte: a fresh combat stat is
    ``floor(base/2) + floor(rand()%base / 2)``, range ``[base/2, base-1]`` (see
    `roll_stat`). A stat can never reach the ceiling by rolling; the ceiling is
    where the client draws the "MAX" tile.
  * OURS, a POLICY -- how much a stat GAINS per use. SE's growth curve lived only
    in their server and is unrecoverable from anything we hold, so `grow_stat`
    is a tunable knob (POL_TM_GROW_*), exactly like `POL_TM_LUCKY_POINTS`. The
    MECHANISM (roll low, grow toward the ceiling, never exceed it) is measured;
    the per-use NUMBERS are the server owner's to set.
"""
import os
import random

import tm_cardprm


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)), 0)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 1. THE ROLL (measured -- the client's own 0xB2CF0 / 0x2b1c80)
# --------------------------------------------------------------------------

def roll_stat(rnd, base):
    """One combat stat, rolled strictly below its `CardPrm` ceiling.

    ``floor(base/2) + floor(rand()%base / 2)`` -- byte-for-byte the client's own
    per-instance roll, range ``[base//2, base-1]``. The client clamps a base to
    >= 1 before rolling; a base of 0 or 1 has nothing to roll and is returned as
    is (a degenerate stat, but a real card id never has one).
    """
    base = int(base)
    if base <= 1:
        return max(0, base)
    return base // 2 + rnd.randrange(base) // 2


def roll_card(rnd, card_id):
    """A freshly-acquired instance of `card_id` -- combat stats rolled below the
    ceiling, type kept, the derived grade left for the client to recompute, and
    the flag byte kept.

    Returns the 8-value stored row ``[id, atk, type, pdef, mdef, grade, arrows,
    flag]``. Arrows are the caller's concern (`_card_arrows`), passed in `arrows`
    so this module does not import the battle table; when omitted the CardPrm
    level byte is used (the old behaviour) so the function is still total.
    """
    b = tm_cardprm.row(card_id)
    return [int(card_id), roll_stat(rnd, b[0]), b[1],
            roll_stat(rnd, b[2]), roll_stat(rnd, b[3]),
            b[4], b[5], b[6]]


def apply_roll(rnd, row):
    """Roll an already-built 8-value row IN PLACE of its combat stats, keeping
    everything else (arrows, flag, the id the caller chose). For a card whose
    arrows were drawn elsewhere -- the acquisition paths build the row first,
    then hand it here so the arrows they rolled survive.
    """
    out = list(row)
    cid = out[0]
    b = tm_cardprm.row(cid)
    out[1] = roll_stat(rnd, b[0])
    out[3] = roll_stat(rnd, b[2])
    out[4] = roll_stat(rnd, b[3])
    return out


# --------------------------------------------------------------------------
# 2. THE GROWTH (ours -- a tunable policy; the mechanism is measured)
# --------------------------------------------------------------------------

def _grow_params():
    """The growth knobs, read per call so they retune with no restart.

    POL_TM_GROW_RATE  percent of the remaining gap gained per use (default 15)
    POL_TM_GROW_FLAT  a flat minimum step when below the ceiling  (default 1)
    POL_TM_GROW_JITTER +/- random spread on the step             (default 2)
    """
    return (_env_int("POL_TM_GROW_RATE", 15),
            _env_int("POL_TM_GROW_FLAT", 1),
            _env_int("POL_TM_GROW_JITTER", 2))


def grow_stat(rnd, cur, ceiling):
    """Grow one stat one use toward its ceiling. Returns the NEW value (>= cur,
    <= ceiling). Policy, not measurement -- see the banner.

    ``gain = flat + floor(gap * rate/100)`` then +/- jitter, floored at 0 and at
    the ceiling. A stat already at (or above) its ceiling never moves.
    """
    cur, ceiling = int(cur), int(ceiling)
    gap = ceiling - cur
    if gap <= 0:
        return min(cur, ceiling)         # already MAX -- and clamp a stray over-max
    rate, flat, jitter = _grow_params()
    gain = flat + gap * max(0, rate) // 100
    if jitter:
        gain += rnd.randint(-jitter, jitter)
    gain = max(0, gain)
    return min(ceiling, cur + gain)


def grow_card(rnd, row):
    """Grow one card's three combat stats toward their `CardPrm` ceilings.

    Returns ``(new_row, deltas)`` where `deltas` is ``{'atk':d,'pdef':d,'mdef':d}``
    for the stats that actually moved (the "+N" the results screen would show).
    Only atk/pdef/mdef grow; type, arrows, grade and flag are untouched. The
    derived grade (idx 5) is left for the client to recompute.
    """
    out = list(row)
    cid = out[0]
    b = tm_cardprm.row(cid)
    ceil = {"atk": b[0], "pdef": b[2], "mdef": b[3]}
    idx = {"atk": 1, "pdef": 3, "mdef": 4}
    deltas = {}
    for name in ("atk", "pdef", "mdef"):
        i, c = idx[name], ceil[name]
        new = grow_stat(rnd, out[i], c)
        if new != out[i]:
            deltas[name] = new - out[i]
            out[i] = new
    return out, deltas


def is_maxed(row):
    """True when every combat stat is at (or above) its CardPrm ceiling -- the
    card the client draws as all-MAX. What serving the CardPrm row makes EVERY
    card, which is the bug this module exists to end."""
    b = tm_cardprm.row(row[0])
    return row[1] >= b[0] and row[3] >= b[2] and row[4] >= b[3]


# --------------------------------------------------------------------------
# 3. SELFTEST
# --------------------------------------------------------------------------

def selftest(say=print):
    ok = True

    def check(cond, what):
        nonlocal ok
        if not cond:
            say("FAIL: " + what)
            ok = False
        return bool(cond)

    rnd = random.Random(12345)

    # -- the roll is measured: [base/2, base-1], never the ceiling -----------
    for base in (2, 6, 40, 75, 99, 100):
        vals = [roll_stat(rnd, base) for _ in range(2000)]
        check(min(vals) >= base // 2,
              "roll of base %d never below floor(base/2)=%d (got %d)"
              % (base, base // 2, min(vals)))
        check(max(vals) <= base - 1,
              "roll of base %d never reaches the ceiling (got %d, ceiling %d)"
              % (base, max(vals), base))
        check(max(vals) == base - 1,
              "roll of base %d should reach base-1 over 2000 draws" % base)
    check(roll_stat(rnd, 1) == 1 and roll_stat(rnd, 0) == 0,
          "a degenerate base (0/1) has nothing to roll")

    # -- a rolled card is strictly below its ceiling, hence NOT maxed --------
    for cid in (0, 7, 42, 160, 166, 249):
        b = tm_cardprm.row(cid)
        c = roll_card(rnd, cid)
        check(c[0] == cid and c[2] == b[1],
              "roll_card keeps id and type for %d" % cid)
        # Only assert 'below' where the ceiling actually leaves room (>1).
        if b[0] > 1:
            check(c[1] < b[0], "rolled atk below ceiling for %d" % cid)
        if b[2] > 1:
            check(c[3] < b[2], "rolled pdef below ceiling for %d" % cid)
        if b[3] > 1:
            check(c[4] < b[3], "rolled mdef below ceiling for %d" % cid)
        if b[0] > 1 and b[2] > 1 and b[3] > 1:
            check(not is_maxed(c),
                  "a freshly rolled card must NOT read as MAX (%d)" % cid)

    # -- serving the CardPrm row IS the all-MAX bug --------------------------
    b = tm_cardprm.row(160)
    nominal = [160, b[0], b[1], b[2], b[3], b[4], 20, b[6]]
    check(is_maxed(nominal),
          "the nominal CardPrm row reads as MAX -- the bug we are ending")

    # -- growth moves toward the ceiling and NEVER past it -------------------
    os.environ["POL_TM_GROW_RATE"] = "20"
    os.environ["POL_TM_GROW_FLAT"] = "1"
    os.environ["POL_TM_GROW_JITTER"] = "0"
    try:
        b = tm_cardprm.row(160)          # atk 84, pdef 99, mdef 75
        row = roll_card(rnd, 160)
        row[6] = 20
        seen_delta = False
        for _ in range(500):             # many uses -> converges on the ceiling
            row, deltas = grow_card(rnd, row)
            seen_delta = seen_delta or bool(deltas)
            check(row[1] <= b[0] and row[3] <= b[2] and row[4] <= b[3],
                  "a growing stat must never exceed its ceiling")
        check(seen_delta, "growth must actually move a stat")
        check(row[1] == b[0] and row[3] == b[2] and row[4] == b[3],
              "enough uses must bring every stat to its ceiling (MAX)")
        _, d2 = grow_card(rnd, row)
        check(d2 == {}, "a fully-grown card must not grow further")

        # a genuinely below-ceiling card grows on the very next use
        young = roll_card(rnd, 160)
        _, d3 = grow_card(rnd, young)
        check(d3 != {}, "a young card gains on use")
    finally:
        for k in ("POL_TM_GROW_RATE", "POL_TM_GROW_FLAT", "POL_TM_GROW_JITTER"):
            os.environ.pop(k, None)

    say("tmroll selftest %s" % ("OK" if ok else "FAILED"))
    return ok


if __name__ == "__main__":
    import sys
    raise SystemExit(0 if selftest() else 1)
