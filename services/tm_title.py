#!/usr/bin/env python3
"""Tetra Master's TITLE -- `CoPrm.BIN` and the client's own picker, ported.

WHAT A TITLE IS. `Pauper`, `Rich Newbie`, `Super Card Baron`, `Almighty`: 126 of
them, and the player does not choose one. It is a pure FUNCTION of three
numbers -- money, card level and average rank -- evaluated against the shipped
ladder in `data/CoPrm.BIN`. Tetra Master computes it locally for its own Player
Data screen; the POL Viewer's per-Content-ID profile does NOT, and expects the
SERVER to send the index (`z_attrss0`, schema field 24). This module is what
lets us send the same number the game itself would have shown.

THE TABLE, `CoPrm.BIN` -- 16-byte header, count at +0x04, then 126 records of
12 bytes, and the name pool after them. Decompressed with the BIN extractor
(the file ships LZSS-compressed, exactly like `Friend.BIN`) and stored in
`tmdata/` alongside `CardPrm.BIN` / `PackPrm.BIN`, which the client's own
loader at rva 0x188C49 reads the same way:

    +0x00  u32  MONEY       threshold, `>=`
    +0x04  u16  AVERAGE RANK threshold, `<=`   (x100; 300 == 3.00, LOW IS GOOD)
    +0x06  u16  CARD LEVEL  threshold, `>=`
    +0x08  u32  the name slot

WARNING: The u32 reading of money is load-bearing and was itself a retraction:
`Almighty` needs 99,999,999, which no u16 can hold.

*** THE PICKER, rva 0x110B10, PORTED INSTRUCTION BY INSTRUCTION. *** This is
the whole function; it is short enough to quote, and quoting it is the point,
because three separate details of it decide what we must send:

    0x110B10  push esi
    0x110B11  mov  esi, [0x528F4B8]      ; the record COUNT, CoPrm's own
    0x110B17  xor  al, al                ; best = 0
    0x110B19  xor  edx, edx              ; i    = 0
    0x110B1D  jle  0x110B59              ; empty table -> fall to the +1
    0x110B1F  mov  ecx, [0x524B634]      ; the record base...
    0x110B30  add  ecx, 4                ; ...biased by 4, so [ecx-4] is +0x00
    0x110B26  mov  ebx, [esp+0x10]       ; arg1  MONEY        (dword)
    0x110B33  mov  bp,  [esp+0x14]       ; arg2  CARD LEVEL   (word)
    0x110B2C  mov  edi, [esp+0x1C]       ; arg3  AVERAGE RANK (dword)
    loop:
    0x110B38  cmp  bp, [ecx+2]  / jb  skip    ; card level >= rec+0x06
    0x110B3E  cmp  ebx, [ecx-4] / jl  skip    ; money      >= rec+0x00  (SIGNED)
    0x110B45  mov  bp, [ecx]                  ; rec+0x04
    0x110B48  cmp  edi, ebp     / ja  skip    ; avg rank   <= rec+0x04  (UNSIGNED)
    0x110B4C  mov  al, dl                     ; best = i   -- NO break
    skip:
    0x110B4E  inc  edx / add ecx, 0xC / cmp edx, esi / jl loop
    0x110B59  inc  eax                        ; *** the return is 1-BASED ***
    0x110B5B  ret

THREE THINGS FALL OUT OF THAT, and each one is a bug if you get it wrong:

  1. **THE LAST MATCH WINS, NOT THE FIRST.** There is no `break`; `al` is
     overwritten by every record that passes. The ladder is ordered, so this is
     "the best title you qualify for" -- but only because of the ORDER, and a
     first-match port would silently hand everybody `Pauper`.
  2. **THE RESULT IS 1-BASED**, and `prof_002.pfb`'s enum agrees exactly: its
     126 entries carry VALUES 1..126, `Pauper` first and `Almighty` last.
     **Value 0 is not in that table**, which is why an unset slot 24 renders as
     `Unknown` on the Viewer's profile -- an out-of-range enum, not a blank.
  3. **NOTHING MATCHING STILL RETURNS 1.** `al` starts at 0 and the tail adds
     one unconditionally, so the floor is `Pauper`, never "no title".

WARNING: **AND THE AVERAGE-RANK TEST IS WHY A ZERO SAVE OVER-PROMOTES.** `<=` against
zero passes on EVERY record, so a save whose +0x30 has never been written
(which is every save this server has ever produced -- see `tmsave.py`) leaves
money and card level as the only real gates. The game's own Player Data screen
is computing its title from that same zero, so a client showing a flattering
title is not evidence that the number behind it is right.

    python tm_title.py --selftest     the ladder anchors + the picker's shape
    python tm_title.py 51000 5000 116 money / card level / avg rank -> a title
"""
import os
import struct
import sys

#: The shipped ladder, `tmdata/CoPrm.BIN`, decompressed. Same directory and the
#: same 16/count-at-+4 container as `CardPrm.BIN` and `PackPrm.BIN`.
TABLE_FILE = "CoPrm.BIN"
HEADER, RECORD = 16, 12

#: What the picker can return: 1..126, and 0 only as "we could not compute one".
#: `prof_002.pfb`'s Title enum is exactly this range, which is the cross-check.
TITLE_MIN, TITLE_MAX = 1, 126

#: Average rank is fixed point x100, the same encoding the save uses at +0x30
#: and the ranking rows use for VS. Rating. `PlPrm.BIN` gives the COM opponents
#: 300 down to 100, i.e. 3.00 down to 1.00, and LOWER IS BETTER.
RANK_SCALE = 100

_TABLE = None


def _say(msg):
    sys.stderr.write(msg + "\n")


def table():
    """[(money, avg_rank, card_level, name)] in ladder order, index 0 == title 1.

    Empty if the file is missing or malformed -- every caller then degrades to
    "no title", which is the same thing we served before this module existed.
    """
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    _TABLE = []
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tmdata", TABLE_FILE), "rb") as fh:
            blob = fh.read()
        n = struct.unpack_from("<I", blob, 4)[0]
        if not (0 < n <= 1024) or len(blob) < HEADER + RECORD * n:
            raise ValueError("count %d does not fit %d bytes" % (n, len(blob)))
        # The name pool follows the records, NUL separated, in ladder order.
        names = [s.decode("cp932", "replace")
                 for s in blob[HEADER + RECORD * n:].split(b"\x00") if s]
        for i in range(n):
            o = HEADER + RECORD * i
            money, rank, level = struct.unpack_from("<IHH", blob, o)
            _TABLE.append((money, rank, level,
                           names[i] if i < len(names) else ""))
    except Exception as exc:
        _TABLE = []
        _say("tm_title: %s unreadable (%r) -- no title can be computed"
             % (TABLE_FILE, exc))
    return _TABLE


def title_index(money, card_level, avg_rank):
    """The 1-based title, exactly as rva 0x110B10 computes it. 0 if unknowable.

    WARNING: `avg_rank` is the x100 fixed point, NOT a placement -- pass 250 for an
    average finish of 2.50. Passing 2 would claim a 0.02 average, which clears
    every threshold in the table and awards `Almighty` to a beginner.

    Returns 0, never a made-up title, when the table did not load or an input
    is missing: the callers' contract is that 0 means "leave the field unset",
    which the Viewer renders as `Unknown` -- honest, where a fabricated
    `Almighty` is not.
    """
    rows = table()
    if not rows:
        return 0
    try:
        money = int(money)
        card_level = int(card_level)
        avg_rank = int(avg_rank)
    except (TypeError, ValueError):
        return 0
    if money < 0 or card_level < 0 or avg_rank < 0:
        return 0
    # The client compares card level as a WORD and money as a SIGNED dword;
    # clamping here keeps a wild stored value from wrapping into a better title
    # than it earned rather than a worse one.
    card_level = min(card_level, 0xFFFF)
    money = min(money, 0x7FFFFFFF)
    best = 0                                    # 0x110B17 `xor al, al`
    for i, (m_min, r_max, l_min, _name) in enumerate(rows):
        if card_level < l_min:                  # 0x110B38 `jb`
            continue
        if money < m_min:                       # 0x110B3E `jl`
            continue
        if avg_rank > r_max:                    # 0x110B48 `ja`
            continue
        best = i                                # 0x110B4C -- and NO break
    return best + 1                             # 0x110B59 `inc eax`


def title_name(index):
    """The ladder's own English name for a 1-based index, or "" .

    WARNING: FOR LOGS AND TOOLS ONLY. The Viewer draws the profile's title from
    `prof_002.pfb`, not from here, and the two files do not always agree on the
    wording -- index 6 is `Apt Newbie` in `CoPrm.BIN` and `Clever Newbie` in
    `prof_002.pfb`. Two English renderings of one Japanese string; neither is
    ours to reconcile, and the wire carries the INDEX, so it never matters on
    screen. It would matter a great deal in a log that claimed to quote the
    screen.
    """
    rows = table()
    try:
        i = int(index) - 1
    except (TypeError, ValueError):
        return ""
    return rows[i][3] if 0 <= i < len(rows) else ""


def selftest(say=print):
    """0 = pass. Anchors are the ladder's own ends plus the picker's shape."""
    bad = []

    def check(cond, what):
        if not cond:
            bad.append(what)
            say("FAIL: " + what)

    rows = table()
    check(len(rows) == TITLE_MAX,
          "CoPrm.BIN must ship and decode to %d records -- got %d"
          % (TITLE_MAX, len(rows)))
    if not rows:
        say("FAIL: nothing further can be checked without the table")
        return 1

    # The two ends of the table -- these are the values
    # that pinned the u32 money reading, so a change here means the container
    # moved, not that a threshold was retuned.
    check(rows[0][:3] == (0, 300, 0),
          "title 1 must be money>=0 / rank<=300 / level>=0 -- got %r"
          % (rows[0][:3],))
    check(rows[0][3] == "Pauper", "title 1 must be Pauper -- got %r" % rows[0][3])
    check(rows[TITLE_MAX - 1][:3] == (99999999, 100, 10000),
          "title 126 must be money>=99999999 / rank<=100 / level>=10000 -- "
          "got %r" % (rows[TITLE_MAX - 1][:3],))
    check(rows[TITLE_MAX - 1][3] == "Almighty",
          "title 126 must be Almighty -- got %r" % rows[TITLE_MAX - 1][3])

    # 0x110B59: the tail adds one unconditionally, so nothing ever returns 0
    # through the loop and the floor is Pauper.
    check(title_index(0, 0, 99999) == 1,
          "a player matching NO record must still get title 1 (0x110B59 adds "
          "one to a zeroed al) -- got %d" % title_index(0, 0, 99999))
    check(title_index(0, 0, 300) == 1,
          "money 0 / level 0 / rank 3.00 is Pauper -- got %d"
          % title_index(0, 0, 300))
    check(title_index(99999999, 10000, 100) == TITLE_MAX,
          "the maxed player must reach Almighty -- got %d"
          % title_index(99999999, 10000, 100))

    # *** THE LAST MATCH WINS. *** A first-match port passes every test above,
    # because Pauper matches everybody. This is the one that catches it.
    top = title_index(99999999, 10000, 100)
    check(top > 1, "a maxed player must NOT come back as Pauper -- a "
                   "first-match loop is the bug this catches (got %d)" % top)

    # *** AND THE AVERAGE-RANK GATE IS REAL, which is what makes the zero save
    # over-promote. Same money and cards, a bad average must not do better. ***
    good = title_index(51000, 5000, 116)
    poor = title_index(51000, 5000, 9999)
    check(good >= poor,
          "a WORSE average rank must never earn a better title -- 116 gave %d, "
          "9999 gave %d" % (good, poor))
    check(title_index(51000, 5000, 0) >= good,
          "a ZERO average rank clears every threshold, so it must rank at "
          "least as high as a real one -- this is the over-promotion the "
          "unwritten save +0x30 causes, asserted so it cannot be forgotten")

    # The contract every caller depends on: 0 means "unset", never a title.
    check(title_index(None, 5000, 116) == 0,
          "a missing input must return 0 (leave the field unset), not a title")
    check(title_index(-1, 5000, 116) == 0, "a negative input must return 0")

    say("tm_title: %d check(s) failed" % len(bad) if bad
        else "tm_title: PASS -- %d titles, picker matches rva 0x110B10" % len(rows))
    return 1 if bad else 0


def main(argv):
    if not argv or "--selftest" in argv:
        return selftest()
    if len(argv) != 3:
        sys.stderr.write("usage: tm_title.py <money> <card_level> <avg_rank_x100>\n")
        return 2
    idx = title_index(*argv)
    print("%d\t%s" % (idx, title_name(idx)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
