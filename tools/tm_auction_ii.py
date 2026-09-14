#!/usr/bin/env python3
"""Decode Tetra Master auction `<II>` groups straight out of `authserv.log`.

`<II>` is the CARD the player is listing, and it is the player's own collection
entry copied verbatim -- `0x12D997` does `mov ecx,8 / rep movsd`, i.e. 0x20
bytes off the selected card and onto the stack. So this is not an auction
format; whatever is decoded here is true of the collection too.

**THE WIDTHS ARE NOT ON THE WIRE.** All 16 values arrive as plain decimal,
`\\x06`-separated, and only the reader's store instruction says how wide each
one is (TM.dll `0x1A54B0`, and its exact inverse the writer `0x1A58D0`):

    values 0-3    mov [ebx], eax               +0x00   4 x u32
    values 4-7    mov word [ebx], ax           +0x10   4 x u16
    values 8-15   mov byte [edx+ebx+0x18], al  +0x18   8 x u8
                                               = 0x20 = 32 bytes

That total is also the proof the 192-byte auction record is right: `<II>` sits
at record `+0xA0` and `0xA0 + 0x20 == 0xC0`, the same 192
`sqMgAccpReadExhibitList` (`n*3<<6`) and `ExhibitRequestCheck` (0x30 dwords)
reach independently. The record has no slack.

WHAT IS MEASURED, AND WHAT IS NOT (live, 2026-08-20, four listings):

  * `u32[0]` IS THE CARD ID -- 75 -> Cinna, 57 -> Garland, both exactly
    `tm_cardprm.NAMES[id]`.
  * `u32[1]` IS THE CARD'S SELL PRICE, and it is the strongest result here:
    `tetramaster._sell_price()` returns 218 for Cinna and 656 for Garland, and
    the client sent 218 and 656. **That measures the price ladder for the
    first time** -- until then it had only ever been derived from disassembly
    and stood as "the one unmeasured number". Two cards, two
    types (0 and 1), two levels (4 and 6), both exact. It also means the game
    PRE-FILLS the asking price with this value: Garland's `<SP>` was 656, the
    untouched default, while Cinna's was hand-typed.
  * the `u8` block is the card's STAT ROW, but WARNING: **NOT in `CardPrm` order --
    1 and 2 are TRANSPOSED**, confirmed on both cards and not a zero
    coincidence, because Garland's type is 1:
        Cinna    wire [54,32,0,15,9,4]   CardPrm [54, 0,32,15, 9,4]
        Garland  wire [65,35,1,45,13,6]  CardPrm [65, 1,35,45,13,6]
    so `<II>` reads **attack, physDEF, TYPE, magicdef, [4], [5]**. That is a
    THIRD card ordering, different again from the stored row and the `@Sell=`
    wire row, which 16 already found diverge from each other. Do not reuse
    either of those orderings here.
  * `u8[6]` is 0 in every listing while `CardPrm[6]` is 3 / 2 -- so it is NOT
    that stat, and only `u8[0..5]` are card stats.
  * `u16[2]` IS THE PLAYER'S **CARD LEVEL** -- the very number the client
    publishes in its profile on class P as `<CR> ... <CI>(1,"Card Level N")`.
    Measured, not fitted: that account published 185 at 15:41:18 and the
    15:45:27 listing carried 185; it published 274 at 16:19:17 and all three
    later listings carried 274. Its history that day ran
    120 -> 251 -> 322 -> 304 -> 185 -> 274, so those are not two constants
    that happen to match. Card-independent, which is why Cinna and Garland
    carry the same value at the same moment.
  * OK: `u8[7]` IS THE PLAYER'S **TITLE** -- a row index into `CoPrm.BIN`.
    `0x12451A` reads the byte and indexes a 12-byte-record table through the
    pointer at `0x2BB634`, filled by `0x188C7C` right after `CoPrm.BIN` loads.
    That file is the title ladder (money>= / avg rank<= / card level>= / name;
    its records start at file offset **16**, not 8). The
    three observed values land exactly on the next 100-band above the player's
    Card Level, which is `u16[2]` in this same struct:

        11 -> row cardlvl>=200   (card level 185)
        16 -> row cardlvl>=300   (card level 274)
        21 -> row cardlvl>=400   (card level 322)

    The table runs 5 rows per 100-band, which is why it stepped by 5 and made
    "+5 per pack" look like a tally. WARNING: HOW IT WAS GOT MATTERS: two curve fits
    died first (below). What settled it was finding the byte's READER.

  * WARNING: THE FALSIFIED HISTORY, kept as the lesson. It was labelled
    "cards owned (deliveries+1)" on a
    two-point fit; **the fifth listing falsified that and every replacement I
    had ready**, and it stayed unknown until its READER was found rather than
    being re-fitted -- which is the whole point of this bullet. Predicted
    23 (total cards), 22 (distinct types) or 26 (deliveries+1); it read **21**.
    The series is 11, 16, 16, 16, 21 and the tester was holding 23 cards of
    22 types at the last reading, so it is none of those three.
    WARNING: THE FACT THAT KILLED IT: the 4th pack delivered **10** cards
    (`@Card=/S=10`, every earlier one was `/S=5`) and `u8[7]` still moved by
    only **+5**. So it does not count delivered cards.
    WARNING: AND DO NOT "FIX" IT WITH `5*packs+1`, which also fits all three values --
    that is a 3-point fit with no mechanism, offered by the same reasoning that
    just produced a wrong answer from 2 points. Find the WRITER of this byte in
    the collection entry before naming it. There is a real candidate mechanism
    to check first: the client's documented discard gate
    (`cmp id, [0x528F4FC]; jge discard`) means the client keeps fewer cards
    than it is handed, so "cards accepted" and "cards delivered" genuinely
    differ -- but 21 != 23 either, so that alone is not the answer.
  * OK: `u16[0]` IS THE "Cards" COUNT ON THE SELLER PROFILE. Right-clicking a
    listing draws the SELLER'S PROFILE out of that listing's `<II>` SNAPSHOT --
    which is why it differs card to card and why it does not match the seller's
    real collection. Measured 2026-08-20: a Tidus listing carrying
    `u16 = [4, 1, 299, 0]` rendered "Card Level: 299" and "Cards: 4", and the
    same screen printed a real `CoPrm.BIN` title name for `u8[7]`. That screen
    confirms THREE fields at once and is a better oracle than the list row.
    WARNING: It counted 2 -> 3 -> 4 across three successive listings, so it GROWS as
    cards are listed -- it is not the collection size.
  * `u32[2]`, `u32[3]`, `u16[3]` and `u8[6]` are 0, and `u16[1]` is
    1, in every listing. Unknown rather than absent -- listings from ONE
    account cannot tell those apart.
  * WARNING: **`<II>` IS DETERMINISTIC per (card, collection state)** -- listings 5
    and 6 are BYTE-IDENTICAL, 11 minutes apart, while `<SP>`/`<BI>`/`<ET>`/
    `<AM>` all changed around them. So **no unknown field is a per-listing
    nonce, serial, sequence or timestamp**, `u8[7]` included. That is a cheap
    negative worth keeping: it rules the whole family out without a probe.

    python tm_auction_ii.py --log authserv.log      # every <ER> in a log
    python tm_auction_ii.py --values 75,218,0,...   # one group by hand
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services"))
try:
    import tm_cardprm
except Exception:                                    # tool stays usable without it
    tm_cardprm = None

#: (label, count, byte width) -- straight off the three store loops in 0x1A54B0.
BLOCKS = (("u32", 4, 4), ("u16", 4, 2), ("u8", 8, 1))

#: wire value index -> field name. Only names what is measured or strongly
#: indicated; a "?" field is one four listings from one account never moved,
#: which is NOT the same as knowing it is unused.
FIELDS = {
    0:  "card id",
    1:  "sell price",
    4:  "cards",
    6:  "CARD LEVEL",
    8:  "attack",
    9:  "phys def",
    10: "type",
    11: "magic def",
    12: "CardPrm[4]",
    13: "CardPrm[5]",
    15: "title",     # CoPrm.BIN row index -- see the docstring
}

#: `polpro A <- <ER>(...) ... <II>(v,v,...)`, as responders.py renders it.
_II = re.compile(r"<II>\(([^)]*)\)")
_TS = re.compile(r"^(\S+)")


def _sell_price(cid):
    """`tetramaster._sell_price` for a baseline card, or None if unavailable.

    Imported lazily and defensively: this tool is often run against a log on a
    machine that is not the server, and a missing import must not cost the
    whole decode.
    """
    try:
        import tetramaster
        typ, lvl = tetramaster._cardprm(cid)
        row = list(tm_cardprm.row(cid))
        return tetramaster._sell_price([cid, row[0], typ, row[2], row[3], lvl])
    except Exception:
        return None


def split_blocks(vals):
    """The 16 wire values as [(label, [values...]), ...]. Raises on a bad count."""
    if len(vals) != sum(n for _, n, _ in BLOCKS):
        raise ValueError("expected %d values, got %d"
                         % (sum(n for _, n, _ in BLOCKS), len(vals)))
    out, i = [], 0
    for label, n, _ in BLOCKS:
        out.append((label, vals[i:i + n]))
        i += n
    return out


def overflows(vals):
    """Values that do NOT fit the width the reader stores them at.

    A u16 slot carrying > 65535, or a u8 slot carrying > 255, would mean the
    block boundaries above are wrong -- which is exactly how the original
    "16-byte array" reading was caught (a 274 in a byte slot).
    """
    bad, i = [], 0
    for label, n, width in BLOCKS:
        for k in range(n):
            v = vals[i + k]
            if v < 0 or v >= 1 << (8 * width):
                bad.append((i + k, label, v))
        i += n
    return bad


def render(vals, ts=None):
    lines = []
    if ts:
        lines.append(ts)
    i = 0
    for label, chunk in split_blocks(vals):
        lines.append("  %-3s  %s" % (label, "  ".join("%6d" % v for v in chunk)))
        lines.append("       %s" % "  ".join("%6s" % FIELDS.get(i + k, "-")[:6]
                                             for k in range(len(chunk))))
        i += len(chunk)
    cid = vals[0]
    if tm_cardprm and 0 <= cid < tm_cardprm.CARD_COUNT:
        lines.append("  card id %d = %s" % (cid, tm_cardprm.name(cid)))
        row = list(tm_cardprm.row(cid))
        lines.append("  CardPrm   %s" % "  ".join("%6d" % v for v in row))
        # The u8 block is CardPrm[0..5] with 1 and 2 TRANSPOSED -- measured on
        # two cards. Rebuild what it SHOULD be and say so, because a mismatch
        # here means either a rolled (non-baseline) card or a wrong reading,
        # and those want telling apart.
        want = [row[0], row[2], row[1], row[3], row[4], row[5]]
        got = vals[8:14]
        lines.append("  expect    %s   (CardPrm 0,2,1,3,4,5 -- the transposition)"
                     % "  ".join("%6d" % v for v in want))
        lines.append("  stats %s" % ("MATCH baseline"
                                     if got == want else
                                     "DIFFER at %s -- rolled card, or re-read 0x1A54B0"
                                     % [i for i, (a, b) in enumerate(zip(got, want)) if a != b]))
        sell = _sell_price(cid)
        if sell is not None:
            lines.append("  sell price %d  vs  u32[1]=%d   %s"
                         % (sell, vals[1],
                            "MATCH" if sell == vals[1] else "MISMATCH -- 16b's ladder?"))
    else:
        lines.append("  card id %d = <outside CardPrm>" % cid)
    for idx, label, v in overflows(vals):
        lines.append("  !! value %d (%s slot) = %d does NOT fit -- the block "
                     "layout is wrong, re-read 0x1A54B0" % (idx, label, v))
    return "\n".join(lines)


def diff(groups):
    """Which value indexes vary across listings -- the open-field hunt."""
    if len(groups) < 2:
        return "  (need two or more listings to diff)"
    out = ["  varying value indexes across %d listings:" % len(groups)]
    labels = []
    for label, n, _ in BLOCKS:
        labels.extend([label] * n)
    steady = []
    for i in range(len(groups[0])):
        seen = [g[i] for g in groups]
        if len(set(seen)) > 1:
            out.append("    [%2d] %-3s  %s" % (i, labels[i],
                                               " -> ".join(str(v) for v in seen)))
        else:
            steady.append(i)
    out.append("  steady: %s" % (", ".join(str(i) for i in steady) or "none"))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", help="authserv.log to scan for <ER> listings")
    ap.add_argument("--values", help="one <II> group, comma or space separated")
    args = ap.parse_args()

    groups = []
    if args.values:
        vals = [int(x) for x in re.split(r"[,\s]+", args.values.strip()) if x]
        print(render(vals))
        groups.append(vals)
    if args.log:
        with open(args.log, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "polpro A <- <ER>" not in line:
                    continue
                m = _II.search(line)
                if not m:
                    print("  !! <ER> with no <II>: %s" % line.strip()[:160])
                    continue
                vals = [int(x) for x in m.group(1).split(",")]
                ts = _TS.match(line).group(1)
                try:
                    print(render(vals, ts))
                except ValueError as exc:
                    print("%s  !! %s" % (ts, exc))
                    continue
                print()
                groups.append(vals)
    if not args.log and not args.values:
        ap.error("give --log or --values")
    if len(groups) > 1:
        print(diff(groups))


if __name__ == "__main__":
    main()
