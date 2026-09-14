#!/usr/bin/env python3
"""Author and decode Tetra Master's `U/g/TM0DataFile` -- THE PLAYER SAVE.

THE FILE SE'S SERVER OWNED. TM.dll has no save-WRITE api: it only ever calls
`sqMgCpLoadPlayerSaveData`, so this blob is authored server-side and served, and
whatever is not in it did not happen. We served 12328 zero bytes for months,
which is why every launch started with an empty collection -- and why the free
Pauper's Pack was buyable again every time, which is how the recorded
collections in `*.tm_collection.json` inflated past anything a real player
could hold.

MEASURED 2026-08-18 off the unpacked TM.dll (image base 0x04F90000, so
file offset == rva). The chain, end to end:

    rva 0x85920   the READ    `U/g/TM0DataFile` (string 0x51B2844) -> buffer
                              0x5224BD0, length 0x3028 == 12328
    rva 0x90740   the STARTUP state machine (state at [esi+0x144], case table
                              0x9092C, jump table 0x90908). State 1 reads the
                              file, state 3 parses it:
    rva 0x9084E   `mov ecx, 0x52463E0` / `call 0x50911F0`
    rva 0x1011F0  the PARSER, filling the struct at 0x52463E0

and the two collection globals every other reader uses are fields OF that
struct, which is what settles that the save carries the collection:

    0x52464CC  the count  == struct+0xEC     (u16 -- `mov word`, never dword)
    0x5246F10  the array  == struct+0xB30    (16 bytes per card in MEMORY)

THE LAYOUT

    +0x000..0x147   328 B   header / profile, copied field by field
    +0x03C          u16     THE COLLECTION COUNT, clamped to 0x406 == 1030
    +0x148..0x3027  1000 x 12   the collection

and that is an exact fit, which is the check that the offsets are right:

    12328 - 0x148 == 12000 == 1000 * 12,  no remainder

WARNING: THE COUNT IS THE WHOLE GATE. `0x5091355` compares it against zero and jumps
the entire copy loop when it is 0 -- so a save whose cards are perfect but whose
+0x3C is zero loads NOTHING, silently. That is exactly the state we were in.

THE RECORD, 12 bytes, from the copy loop at 0x509136D..0x50913EA. The loop
walks the source with `edi` starting at buf+0x14A -- i.e. record+2, not
record+0 -- so every displacement below is `edi` relative minus 2:

    +0x00  u16  CARD ID    `mov ax,[edi-2]`; 0x509137F rejects id >= the limit
                           at [0x528F4FC], so an out-of-range id is DROPPED and
                           the count still consumed -- cards silently vanish
    +0x02  u8   attack     -> dest+0
    +0x03  u8   type       -> dest+1   0x5091383 rejects type >= 4
    +0x04  u8   phys def   -> dest+2
    +0x05  u8   magic def  -> dest+3
    +0x06  u8   ...        -> dest+4, and on
    +0x07..0x0B            the rest of the row

WARNING: AND A ROW OF ZEROS IS NOT AN EMPTY SLOT, IT IS A REJECT. 0x509138D sums
attack + physdef + magicdef and 0x509139D drops the record when that is <= 0.
So padding rows are fine (they are skipped) but a real card with all-zero stats
is thrown away exactly like a bad id -- which is what serving placeholder 1s
against CardPrm's 6..90 baseline would have produced.

WARNING: WHAT IS NOT MEASURED: the last five bytes, +0x07..0x0B. dest+0..3 are pinned
because they land on `tm_cardprm`'s documented in-memory record (attack, type,
phys, magic) and agree with the `@Card=` /D= order, but the tail is written in
recorded order on the assumption that the save row and the wire row agree past
the fields both name. One launch settles it: if a card's stats read right on
the collection screen the tail is right too.

    python tmsave.py --dump  data/resources/16.U_g_TM0DataFile.bin
    python tmsave.py --from-collection data/resources/16.tm_collection.json \\
                     --base data/resources/16.U_g_TM0DataFile.bin --out NEW.bin
"""
import argparse
import json
import os
import struct
import sys

TOTAL = 12328                   #: 0x3028, the length rva 0x85920 asks for
COUNT_OFF = 0x3C                #: u16 -- 0x50912D9 `mov cx, word [0x5224C0C]`

#: *** MONEY -- u16 at +0x38, MEASURED 2026-08-18. ***
#: The parser at 0x1011F0 copies the file buffer (0x5224BD0) into the struct
#: field by field, so a file offset is just the source VA minus that base:
#:
#:     0509124D  mov dx,  word [0x5224c08]     file +0x38
#:     05091256  mov word [esi], dx            -> struct +0x00
#:
#: and the same routine's `mov cx, word [0x5224C0C]` -> struct+0xEC is the
#: COLLECTION COUNT this file already documents at +0x3C. Money and the count are
#: neighbours, four bytes apart.
#:
#: CONFIRMED AGAINST A LIVE CLIENT (a memory probe): struct+0x00 read
#: **111** in a running Tetra Master whose header was otherwise entirely zeros
#: apart from a pointer, and whose collection count read 6 -- the number of cards
#: that client actually held. A lone non-zero integer in an empty header, at the
#: offset next to the count, is the money.
#:
#: WARNING: AND IT IS A **u16**, so the ceiling is 65535. `mov word`, never dword --
#: the same care the count needed.
#:
#: WHY IT MATTERED: we wrote this header as zeros, so every launch started the
#: player at 0 money. SHOPINIT's `/M=` does NOT feed the shop's Money display --
#: measured, by serving `/M=4321` and watching the screen stay at 87 across a
#: shop exit and re-entry.
#: WARNING: CORRECTED 2026-08-20: MONEY IS +0x34 AND A DWORD. The +0x38 u16 above is
#: **Card Points**, not money, and writing the balance there is why every player
#: has always had zero money -- `+0x34` reads 0 in every save on the server.
#:
#: The save parser at 0x1011F0 copies the file buffer (0x294BD0) field by field,
#: and the two are adjacent, which is how they were conflated:
#:
#:     0x101234  mov  eax, dword [0x294C04]    file **+0x34**
#:     0x101239  test eax, eax
#:     0x10123B  mov  dword [esi+0xC8], eax    -> struct +0xC8
#:     0x101241  jge  0x10124D
#:     0x101243  mov  dword [esi+0xC8], 0      <- clamp NEGATIVE to zero
#:
#:     0x10124D  mov  dx, word [0x294C08]      file +0x38
#:     0x101256  mov  word [esi], dx           -> struct +0x00
#:
#: **struct +0xC8 is money**, and the proof is that the same field is where
#: `/M=` lands in BOTH GameInits -- `@VsGameInit` 0x1028CE and `@ComGameInit`
#: 0x102DA9 -- each with that identical clamp-to-zero. struct +0x00 is where
#: `/CP=` lands (0x102DCA, and the shop arm's 0x1052B1), i.e. Card Points.
#:
#: WARNING: The old note's own evidence should have caught it: serving `/M=4321` left
#: the screen at **87**, a Card-Points-shaped number, not a wallet.
#: WARNING: And it is a DWORD, so the ceiling is not 65535. `mov dword`, never word.
MONEY_OFF = 0x34
MONEY_MAX = 0x7FFFFFFF          #: signed -- 0x101239's `test`/`jge` clamps < 0

#: The u16 at +0x38 -> struct +0x00. NOT money; `/CP=` writes the same field.
#:
#: KEY: **AND IT IS DEAD FOR PERSISTENCE -- DO NOT AUTHOR IT** (2026-08-24). The
#: parser loads it into struct +0x00 at `0x101256` and then **overwrites that
#: slot, unconditionally, with the DERIVED card level** at `0x10147F` (the
#: return of `0xB9040` over the freshly parsed collection, which is also what
#: it pushes to the title picker). So whatever we write here is discarded four
#: hundred instructions later. `+0x3E` is dead the same way -- the parser zeroes
#: it at `0x101449` and the shop zeroes it again at `0x17964E`.
#:
#: That is the practical half of the "struct +0x00 is contested" question
#: (one reading has it as Card Points because `/CP=` writes it; the load path makes
#: it the card level). Which LABEL owns the struct slot at runtime is still
#: open; which FILE BYTE we should author is not -- neither.
CARDPOINTS_OFF = 0x38
CARDPOINTS_MAX = 0xFFFF
#: *** THE CAREER STATISTICS BLOCK -- Player Data -> Status, and every one of
#: these was ZERO in every save this server has ever written. ***
#:
#: MEASURED 2026-08-16 by the self-locating save: every
#: aligned dword was set to ITS OWN OFFSET and the Player Data screen was read,
#: so each number on screen names the byte it came from. That is a stronger
#: reading than a disassembly sweep -- the screen is the consumer -- and every
#: hit landed 4-byte aligned, which is the cross-check that they are real.
#:
#: WARNING: `MONEY_OFF` above is part of this block and is the ONLY member of it this
#: module has ever written. The rest are the "stats we keep and do nothing with"
#: a tester asked about on 2026-08-24: the server computes `rating`,
#: `games`, `score_total`, `prize_total` and the prize-point balance and stores
#: them in the collection's `rank` block and in `tmprize`'s ledger -- and then
#: never delivers any of them to the one file the client reads. The Status
#: screen has therefore shown zeros for numbers we hold.
#:
#: ENCODING: `AVG_RANK_OFF` and `RATING_OFF` are int x100 (2.96 is 296, the same
#: fixed point `tmrank`'s VS. Rating column uses); the rest are plain integers.
#: WARNING: LOWER IS BETTER for average rank -- it is a mean finishing POSITION, and
#: `CoPrm.BIN` gates every title on `<=` it (see `tm_title.py`).
#:
#: KEY: **THE WIDTHS ARE THE PARSER'S, NOT THE MARKER SAVE'S** (read 2026-08-24
#: straight off `0x1011F0`, which copies the file buffer at VA `0x5224BD0` into
#: the struct field by field -- so a file offset is just the source VA minus
#: that base and the `mov` names the width). The self-locating save could only
#: ever see DWORDS, and three of these are not dwords:
#:
#:     +0x30  dword  0x1011FD -> struct +0x04     Average Rank
#:     +0x34  dword  0x101234 -> struct +0xC8     Money  (clamped >= 0)
#:     +0x48  dword  0x101296 -> struct +0x78     Biggest Prize
#:     +0x4C  dword  0x10128D -> struct +0x7C     Average Prize
#:     +0x50  dword  0x10129F -> struct +0x80     Grand Total
#:     +0x78  **word**  0x1012CC -> struct +0x88  Consecutive Wins
#:     +0x7A  **word**  0x1012BB -> struct +0x8A  its NEIGHBOUR -- see below
#:     +0x128 dword  0x10121D -> struct +0xB0     VS. Rating
#:     +0x130 **word**  0x101259 -> struct +0xB8  VS. Player Games
#:     +0x134 dword  0x101228 -> struct +0xB4     Prize Points
AVG_RANK_OFF = 0x30             #: Average Rank, x100        dword
OPPONENTS_OFF = 0x40            #: opponents faced           WARNING: WORD -- see below
RING_INDEX_OFF = 0x44           #: the result ring's index   WARNING: WORD
BIGGEST_PRIZE_OFF = 0x48        #: Biggest Prize             dword
AVERAGE_PRIZE_OFF = 0x4C        #: Average Prize             dword
PRIZE_TOTAL_OFF = 0x50          #: Grand Total               dword
CONSEC_WINS_OFF = 0x78          #: Consecutive Wins          WARNING: WORD, **SIGNED**
STREAK_OFF = 0x7A               #: Winning Streak            WARNING: WORD
RATING_OFF = 0x128              #: VS. Rating, x100          dword
VS_GAMES_OFF = 0x130            #: VS. Player Games          WARNING: WORD
PRIZE_POINTS_OFF = 0x134        #: Prize Points Acquired     dword
#: `Most Combos` -- a career MAX (`0xD029B`) over a per-seat byte at
#: `48*seat + struct+0x19C` that the battle code increments. WARNING: The FIELD is
#: measured; the UNIT is not -- see `tetramaster._note_combo`. Nothing writes
#: this unless `POL_TM_MOST_COMBOS=1`.
MOST_COMBOS_OFF = 0x7C          #: Most Combos              WARNING: BYTE, and gated

#: KEY: **`OPPONENTS_OFF` IS NOT A GAME COUNT.** `0xCC7AC` does
#: `add word [struct+0x84], (N - 1)` where N is the match's player count, so it
#: accumulates OPPONENTS FACED -- and `0xCC7E4` divides Grand Total by it to get
#: `Average Prize`, which is therefore prize money **per opponent, not per
#: match**. The two agree only while every match is two-player. It is also the
#: divisor `0x10F6A5` uses, i.e. the one behind the result-screen
#: divide-by-zero, so a save that carries prize money and a zero here is a
#: crash waiting on a screen.
#:
#: WARNING: **`CONSEC_WINS_OFF` IS ONE SIGNED FIELD CARRYING TWO PLAYDAT LABELS.**
#: `0xCC643..0xCC6BA`: a win run counts UP, a loss run counts DOWN through zero
#: (`0xCC68A` stores 0xFFFF), and the screen draws `Consecutive Wins` (Playdat
#: 39) or `Consecutive Losses` (40) depending on the sign. `STREAK_OFF` is its
#: running maximum -- `Winning Streak` (41 / 82).

#: The stat fields, `{offset: name}`, for callers that want to write or dump the
#: whole block.
STAT_FIELDS = {
    AVG_RANK_OFF: "avg_rank", OPPONENTS_OFF: "opponents",
    BIGGEST_PRIZE_OFF: "biggest_prize",
    AVERAGE_PRIZE_OFF: "average_prize", PRIZE_TOTAL_OFF: "prize_total",
    CONSEC_WINS_OFF: "consec_wins", STREAK_OFF: "streak",
    MOST_COMBOS_OFF: "most_combos",
    RATING_OFF: "rating",
    VS_GAMES_OFF: "vs_games", PRIZE_POINTS_OFF: "prize_points",
}

#: WARNING: **THE WORD FIELDS MUST BE WRITTEN AS WORDS.** `+0x78` is the sharp one: the
#: very next word, `+0x7A`, is a SEPARATE field the parser copies to struct
#: `+0x8A`, so a dword write at `+0x78` silently zeroes Winning Streak. This is
#: why `write_field` grew width 2 (2026-08-24); before that there was no way to
#: touch either without destroying the other.
STAT_FIELDS_WORD = (OPPONENTS_OFF, RING_INDEX_OFF, CONSEC_WINS_OFF,
                    STREAK_OFF, VS_GAMES_OFF)

#: WARNING: **THE EARLIER READING "Card Level, Winning Streak and Title are single BYTES at an
#: offset that is a multiple of 0x100" IS RETRACTED (2026-08-24).** The
#: inference was: all three read `0`/`Pauper` against the self-locating save,
#: and a dword read anywhere in that file returns a non-zero offset, therefore
#: they cannot be dwords, therefore they are bytes whose low byte is zero. Two
#: steps of that are wrong, and `0x1011F0` says so:
#:
#:  * **CARD LEVEL AND TITLE ARE NOT IN THE SAVE AT ALL. They are DERIVED at
#:    load.** `0x10146E` calls `0xB9040` over the freshly parsed COLLECTION and
#:    that return value IS the card level (`mov word [esi], ax`); `0x101482`
#:    then feeds it, money and the average rank to the title picker `0x110B10`
#:    and stores the result as a byte at struct `+0xAD`/`+0xDA`. They read
#:    0/`Pauper` in the marker save because that save had **no cards and no
#:    money**, which is the correct answer for it -- not because they are
#:    hiding somewhere.
#:  * **THE WORD CASE WAS SKIPPED.** A 16-bit field at an offset `2 (mod 4)`
#:    reads as the HIGH half of the marker's dword, i.e. **0**, in exactly the
#:    way a "byte at a multiple of 0x100" would. `+0x7A` is such a field and is
#:    the neighbour of Consecutive Wins, which makes it the strongest candidate
#:    for **Winning Streak** (or Consecutive Losses -- Playdat.BIN carries both,
#:    at records 41 and 40).
#:
#: So the honest open list is smaller and better shaped than it was: five header
#: fields the parser copies whose LABEL is not yet known -- `+0x3E` (word),
#: `+0x40` (word -> struct `+0x84`, and `0x10F6A5` divides Grand Total by it, so
#: it is a GAMES COUNT), `+0x44` (word), `+0x7A` (word), `+0x7C` (byte) -- plus
#: a **32-byte STRING at `+0x7D`** (`0x1012B6` copies 0x20 bytes to struct
#: `+0x128`), which sits beside the streak counters and is the obvious shape for
#: `Memorable Win`. Pinning them is a renderer read, not another marker save.
STAT_FIELDS_UNLABELLED = (0x3E, 0x40, 0x44, 0x7A, 0x7C, 0x7D)

CARDS_OFF = 0x148               #: 0x5091362 `mov edi, 0x5224D1A` is this + 2
REC = 12                        #: 0x50913EA `add edi, 0xc`
REC_MEM = 16                    #: 0x50913E7 `add ebp, 0x10` -- the MEMORY row
MAX_CARDS = (TOTAL - CARDS_OFF) // REC          #: 1000, and it divides exactly
COUNT_CAP = 0x406               #: 1030 -- 0x509133D clamps the count to this
TYPE_MAX = 4                    #: 0x5091383 `cmp byte [edi+1], 4` / `jae`

assert CARDS_OFF + MAX_CARDS * REC == TOTAL, "the layout must fill the file"


def encode_card(vals):
    """One 12-byte save row from a recorded `[id, attack, type, pdef, mdef, ...]`.

    Raises rather than emitting a row the client will silently drop -- see the
    two rejection gates in the module docstring. A dropped row is invisible on
    screen (the card is simply absent), which is the worst possible failure mode
    to debug, so it is refused here instead.
    """
    if not vals:
        raise ValueError("empty card record")
    v = list(vals) + [0] * (8 - len(vals))
    cid, attack, ctype, pdef, mdef = v[0], v[1], v[2], v[3], v[4]
    if not 0 <= cid <= 0xFFFF:
        raise ValueError("card id %r does not fit the u16 at +0x00" % (cid,))
    if not 0 <= ctype < TYPE_MAX:
        raise ValueError("type %r is rejected by 0x5091383 (needs < %d)"
                         % (ctype, TYPE_MAX))
    if attack + pdef + mdef <= 0:
        raise ValueError(
            "card %d has attack+pdef+mdef == 0, which 0x509139D DROPS -- a row "
            "of zeros is a reject, not an empty slot" % cid)
    rec = bytearray(REC)
    struct.pack_into("<H", rec, 0x00, cid)
    for i, b in enumerate((attack, ctype, pdef, mdef, v[5], v[6], v[7])):
        rec[0x02 + i] = b & 0xFF
    return bytes(rec)


def decode_card(rec):
    """Inverse of `encode_card`, plus WHY the client would drop this row."""
    cid = struct.unpack_from("<H", rec, 0)[0]
    attack, ctype, pdef, mdef = rec[2], rec[3], rec[4], rec[5]
    why = []
    if ctype >= TYPE_MAX:
        why.append("type %d >= %d, dropped by 0x5091383" % (ctype, TYPE_MAX))
    if attack + pdef + mdef <= 0:
        why.append("attack+pdef+mdef == 0, dropped by 0x509139D")
    return {"id": cid, "attack": attack, "type": ctype, "pdef": pdef,
            "mdef": mdef, "tail": list(rec[6:]), "dropped": why}


#: *** THE DEFAULT HEADER, AND WHY A MINTED SAVE MUST CARRY ONE. ***
#:
#: Every save this server has ever written had a header of ZEROS, and the client
#: reads its settings straight out of it -- so every setting whose default is not
#: zero read as the first entry in its list, for every member, for ever. Live
#: testing found it as "the chat window shows 0 lines where the default is 5"
#: and, correctly, as a CLASS of bugs rather than one setting.
#:
#: The offsets are `tetramaster.OPT_FIELD_OFFSETS`, measured off the binary long
#: before this; what was missing was what the fields MEAN, and that came from the
#: client's own Options screen under its `Default` button (screenshots,
#: 2026-08-20). CONFIRMED LIVE the same night: serving the four values below made
#: the screen read Medium / High / 5 lines / Fairly dark (60%).
#:
#: The three clamps `tetramaster` documents are what let us read the encoding
#: without disassembling the option lists, because each one turned a marker pass
#: into a legible answer:
#:
#:   +0x102 folds anything above 0x14 to 8   -> a marker of 147 showed as "8",
#:                                              which is how CL was identified
#:   +0x103 outside 0x1E..0x46 becomes 0x3C  -> 60, i.e. "Fairly dark (60%)"
#:   +0x100/+0x101 are masked & 1            -> booleans, matching Off/Off
#:
#: WARNING: VOLUME 1 IS "LOW", NOT "OFF" -- measured, and it is the reason this went
#: unnoticed: the two live members carried `01 01` at +0xB3/+0xB4, so the volumes
#: were quietly wrong rather than obviously absent.
#:
#: WARNING: ONLY MINTED SAVES GET THIS. A save that already exists keeps its header --
#: see `build`. Zero is a LEGAL value for several of these (Off, muted, 0 lines),
#: so "heal every zero" would overwrite a deliberate choice, which is exactly the
#: trap `iniheal` documents for the shim's ini. `--heal` is the explicit,
#: per-save, logged way to repair one that predates this.
#: VERIFIED: COMPLETE, AND THE CLIENT HANDED IT OVER IN ONE MESSAGE. A tester
#: pressed the Options screen's own **Default** button and then **Confirm**,
#: which makes the client send `@Opt=` carrying all twelve fields at their
#: factory values (prod, 2026-08-20T03:21:25Z):
#:
#:     Ar=0 Cu=0 Vi=0 Se=2 Bgm=3 Per=1 Ran=0 CMD=0 CAD=0 CL=5 CT=60 HNSS=0
#:
#: So only FIVE of the twelve are non-zero, and a zero header is genuinely
#: correct for the other seven -- which is why this bug looked like scattered
#: unrelated faults instead of one. It also settles three readings that guessing
#: would have got wrong: `Ran=0` IS "Display name" (an earlier 1 was a tester
#: changing it AWAY from the default), `Ar=0` is "Normal", `HNSS=0` is
#: "Don't skip".
#:
#: WARNING: ASK THE CLIENT, DO NOT INFER. Two rounds of marker passes and a disassembly
#: hunt produced four of these values; the Default button produced all twelve,
#: exactly, in one send. When a client can be made to state its own defaults,
#: that beats reading them out of the binary.
DEFAULT_HEADER = {
    0x0B3: 2,       #: Se   SE Volume            -> Medium   (1=Low 2=Med 3=High)
    0x0B4: 3,       #: Bgm  BGM Volume           -> High
    0x0B5: 1,       #: Per  (screen row unknown) -> 1
    0x102: 5,       #: CL   Chat Window Size     -> 5 lines
    0x103: 0x3C,    #: CT   Chat Window Transp.  -> Fairly dark (60%)
}

#: Measured as ZERO by the same send, so they are correct in a zero header and
#: deliberately absent above rather than forgotten: `Ar` (+0xB0, Card Placement
#: "Normal"), `Cu` (+0xB1), `Vi` (+0xB2), `Ran` (+0xB6, Rankings Display
#: "Display name"), `CMD` (+0x100, Off), `CAD` (+0x101, Off) and `HNSS` (+0x126,
#: Linking Handles "Don't skip").
DEFAULT_HEADER_MEASURED_ZERO = (0x0B0, 0x0B1, 0x0B2, 0x0B6, 0x100, 0x101, 0x126)


#: *** THE TABLE SETTINGS PRESET -- THE SAME CHANNEL, ONE SCREEN OVER. ***
#:
#: VERIFIED: MEASURED 2026-08-20 off `TM.dll.unpacked`, by opening the functions rather
#: than matching addresses. This is the answer to "the table settings do not
#: persist, and half of them are blank when I change page".
#:
#: `apply_options_from_save` (`0x14D9F0`) does not stop at the options. Its tail,
#: `0x14DAE3..0x14DB9B`, copies a second run of save bytes into the globals the
#: table-settings dialog seeds from -- the SAME save buffer at VA `0x5224BD0`
#: that gave the options block its offsets, twenty-four bytes further along:
#:
#:     save +0xB8 (dword) -> 0x2B5ED4      save +0xD8 (byte)  -> 0x2B5EE0
#:          +0xBC (byte)  -> 0x2B5ED8           +0xDC (dword) -> 0x2B5EE4
#:          +0xC0 .. +0xD0                      +0xE0 .. +0xF0
#:                                              +0xF4 (10 B)  -> 0x2B5EF7
#:
#: `0x2B5ED4` and `0x2B5EE0` are exactly the two structs the `@Tet=`/`@Tab=`
#: BUILDER at `0x87410` serialises (args 1 and 2), and exactly the two blocks the
#: settings dialog's two pages seed their widgets from (`0x4ECA0` the rules page,
#: `0x515C0` the restrictions page). So the round trip is:
#:
#:     save  --0x14D9F0-->  globals  --the dialog-->  widgets
#:     widgets  --the dialog-->  globals  --0x87410-->  `@Save=` (code 0x24)
#:
#: and the server closes it by writing what `@Save=` carries back here. That is
#: the identical shape to `@Opt=`, which already works -- see `OPT_FIELD_OFFSETS`
#: in `tetramaster`.
#:
#: WARNING: THE FIELD NAMES ARE THE CLIENT'S OWN, not ours: the builder pushes the
#: literals `/bm= /du= /st= /cb= /ca= /gs= /tl=` and `/in= /lu= /ll= /au= /al=
#: /co= /pa=` from the string table at rva 0x2229A0..0x222A18, in this order.
#:
#: WARNING: THE BYTE FIELDS SIT ON A 4-BYTE STRIDE and the client reads ONE byte from
#: each (`mov dl, byte ptr [0x5224C8C]`). Write the byte it reads and nothing
#: else; the three that follow are not ours to interpret.
#:
#: WARNING: `pa` IS THE PASSWORD SWITCH, not a rule. `0x515D2`..`0x51605` tests +0xF0
#: and only then copies the ten bytes at +0xF4 into the dialog's two password
#: fields. The password itself is `TABLE_PRESET_PW` and is NOT part of the
#: numeric set.
TABLE_PRESET = (
    # name, save offset, width -- the order the client writes them on the wire
    (b"bm", 0x0B8, 4),      #: `@Tet=` -- the rules page (`@Save=/Game=1`)
    (b"du", 0x0BC, 1),
    (b"st", 0x0C0, 1),
    (b"cb", 0x0C4, 1),
    (b"ca", 0x0C8, 1),
    (b"gs", 0x0CC, 1),
    (b"tl", 0x0D0, 1),
    (b"in", 0x0D8, 1),      #: `@Tab=` -- the restrictions page (`@Save=/Tbl=`>0)
    (b"lu", 0x0DC, 4),
    (b"ll", 0x0E0, 4),
    (b"au", 0x0E4, 4),
    (b"al", 0x0E8, 4),
    (b"co", 0x0EC, 1),
    (b"pa", 0x0F0, 1),
)

#: The ten-byte table password at +0xF4, gated by `pa` (+0xF0). Carried by the
#: client only as `@Tab=`'s `/pw=`, which -- per `0x87090` -- it RECEIVES and
#: never sends, so nothing writes this yet. Named here so the next reader does
#: not mistake it for spare header.
TABLE_PRESET_PW = (0x0F4, 10)

#: `{name: (offset, width)}`, for callers that resolve by field name.
TABLE_PRESET_BY_NAME = {n: (o, w) for n, o, w in TABLE_PRESET}

#: Which page owns which half. `@Save=` carries BOTH halves every time, but the
#: dialog only ever fills in the half whose page sent it -- the other half is
#: whatever the globals already held, which on a zero save is zeros.
#:
#: WARNING: THIS IS WHY PERSISTING THE WHOLE MESSAGE WOULD BE WORSE THAN PERSISTING
#: NOTHING. Measured on the wire (`logs/authserv.log`, 2026-08-18):
#:
#:     06:32:11  @Save=/Tbl=1/Game=0  @Tet= ALL ZERO   @Tab=/in=0/lu=90/...
#:     06:45:57  @Save=/Tbl=0/Game=1  @Tet=/st=1/...   @Tab= ALL ZERO
#:
#: Saving both halves of either one would blank the other half of the preset.
#: The discriminator is the builder's own two arguments: `0x512ED` passes
#: `Game=1` and is the rules page, `0x53674` passes `Tbl=1` and is the
#: restrictions page, and `0x87410` emits `@Save=` when EITHER is non-zero.
TABLE_PRESET_TET = tuple(n for n, _o, _w in TABLE_PRESET[:7])
TABLE_PRESET_TAB = tuple(n for n, _o, _w in TABLE_PRESET[7:])


#: The table-preset half of the factory header, in the client's own numbers.
#:
#: VERIFIED: MEASURED, not invented: these are the values the client itself put on the
#: wire after a tester pressed the settings screen's **Default** button
#: (`logs/authserv.log` 2026-08-18T06:33:02Z, code 0x14):
#:
#:     @Tet=/bm=0/du=0/st=0/cb=0/ca=0/gs=0/tl=0
#:     @Tab=/in=0/lu=99999/ll=0/au=300/al=100/co=0/pa=0
#:
#: so eleven of the fourteen are correct at zero and only three are not. `lu`,
#: `au` and `al` are the three, and `au`/`al` are the pair an earlier capture
#: measured as REFUSING every reservation while they were 0/0 -- "no member
#: meets this table's restrictions" is literally true of a 0-0 rank band.
#:
#: WARNING: THESE CAME FROM THE **TABLE** RECORD, NOT THE PRESET. Default+Confirm sends
#: code 0x14, which `0x53491` builds from `0x2B61E0`/`0x2B61EC` -- the open
#: table's own settings -- while the preset lives in `0x2B5ED4`/`0x2B5EE0`. The
#: widgets are shared, so the same numbers should be the preset's factory values
#: too, but that step is REASONED and not watched. It is why these are separate
#: from `DEFAULT_HEADER` below and why a live read of the settings screen is the
#: thing that confirms them.
#: VERIFIED: THE RULES PAGE IS NOW MEASURED ON THE PRESET TIER (2026-08-20T20:0xZ). A
#: tester pressed **Default** on page 1 and saved, and the client stated its own
#: factory rules in a code 0x24 `@Save=`, which is the tier these defaults are
#: actually read back on:
#:
#:     @Save=/Tbl=0/Game=1 @Tet=/bm=995/du=0/st=1/cb=1/ca=1/gs=1/tl=3
#:
#: Five of the seven are NON-ZERO, and a zero header was wrong on every one of
#: them. Corroborated independently: `logs/authserv.log` 2026-08-18T06:45:57Z, a
#: different member on a different session, carries `st=1/cb=1/ca=1/gs=1/tl=3`
#: byte-identical. Two measurements, no inference.
DEFAULT_TABLE_PRESET = {
    # `@Tet=`, the rules page -- MEASURED, twice.
    b"st": 1,
    b"cb": 1,       #: Chance Blocks -- ON by default (seen on screen)
    b"ca": 1,
    b"gs": 1,
    b"tl": 3,
    # `@Tab=`, the restrictions page -- MEASURED 2026-08-20, and it RETRACTED the
    # reasoned pair that stood here for an hour. Default+Save on page 2 sent:
    #
    #     @Tab=/in=0/lu=99999/ll=0/au=0/al=0/co=0/pa=0
    #
    # The server owner: "it's just the 99,999 card level maximum." So ONE field is
    # non-zero and `au`/`al` are **0/0 by default**, not the 300/100 that was
    # carried over from a code 0x14 Confirm. That inference was flagged in this
    # file as unwatched; it was watched, and it was wrong. See the banner below.
    b"lu": 99999,   #: Card Level ceiling -- the only non-zero restriction
}

#: WARNING: `au`/`al` ARE 0/0 HERE, AND THAT RETRACTS AN INFERENCE OF OURS -- twice
#: over, so read this before putting 300/100 back.
#:
#: An earlier capture measured a reservation being REFUSED with `au=0/al=0`
#: and ACCEPTED with `au=300/al=100`, and concluded the band was the
#: discriminator. 300/100 was then carried into these defaults on the reasoning
#: that the settings dialog's widgets are shared between the two tiers, so the
#: preset's factory values should match the table's. Measured now: they do not.
#: The client's own Default on the PRESET tier is 0/0.
#:
#: Which means one of two things, and NEITHER is "put 300/100 in the save":
#:
#:   * the 300/100 belongs to the TABLE tier only -- `0x53491` builds the 0x14
#:     Confirm from `0x2B61E0`/`0x2B61EC`, which `0x8FA0C` fills out of the
#:     client's per-table record, NOT out of this preset; or
#:   * that capture's discriminator reading was incomplete and something else refused
#:     that reservation, since 0/0 cannot both be SE's default and always refuse.
#:
#: WARNING: AND IT NARROWS AN EARLIER CLAIM, WHICH OVERREACHED. That claim said `au`/`al` "were
#: never going to be in the 104-byte table record". True of the PRESET, which is
#: what it had measured -- but the table's own copy reaches the dialog through
#: the per-table record, so the record is exactly where the table tier's band
#: could live. The two tiers were collapsed into one claim. Settle which before
#: authoring a band anywhere.
DEFAULT_TABLE_PRESET_RETRACTED = (b"au", b"al")

#: WARNING: `bm` IS NOT A DEFAULT, IT IS DERIVED -- and baking one would pin every new
#: player's wager to a stranger's wallet.
#:
#: A tester, 2026-08-20: "The wager field, by default, is 10% of however much
#: you have in gold. It's not so much a fixed field." Confirmed to the unit: the
#: Default press sent `bm=995` while that client held **9950** gold (10000 less a
#: 50-gold auction bid), and 9950/10 = 995 exactly.
#:
#: The binary agrees. The rules page's seeder `0x4ECA0` does not just copy `bm`:
#:
#:     0x4ECB2  call 0xA7E90          ; the cap -- a getter for global 0x2B64A8
#:     0x4ECB7  mov ecx, [0x2B5ED4]   ; the STORED bm
#:     0x4ECC3  cmp eax, ecx / jge    ; widget = min(stored, cap)
#:
#: so the field is clamped against a live, money-derived global on every seed.
#:
#: WARNING: AND THE SERVER DOES NOT OWN THAT BALANCE. `money_of` falls back to
#: `_start_money()` (10000) whenever a member's collection carries no `money`,
#: which was true of the very member measured here -- their client said 9950 and
#: ours would have said 10000. A default computed from our number would be wrong
#: for the player looking at it. Leave the byte at zero and let the client's own
#: clamp put a sane number in the box.
DEFAULT_TABLE_PRESET_DERIVED = (b"bm",)

#: Measured as ZERO by the same Default press, so a zero header is correct for
#: these and they are deliberately absent above: `du` (+0xBC).
DEFAULT_TABLE_PRESET_MEASURED_ZERO = (b"du",)


def default_header_writes():
    """`{offset: (width, value)}` for the whole factory header, both blocks.

    One place that knows what a minted save should contain, so `build`,
    `apply_defaults` and `tetramaster`'s mint-on-first-write cannot drift apart
    -- they had three copies of the byte loop between them.
    """
    out = {off: (1, val) for off, val in DEFAULT_HEADER.items()}
    for name, value in DEFAULT_TABLE_PRESET.items():
        off, width = TABLE_PRESET_BY_NAME[name]
        out[off] = (width, value)
    return out


#: `{width: (unpack code, mask)}`. **Width 2 was added 2026-08-24** and it is not
#: a convenience: the parser `0x1011F0` reads `+0x78`, `+0x7A`, `+0x40`, `+0x44`,
#: `+0x3E` and `+0x130` with `mov <r16>, word ptr`, and `+0x78`'s NEIGHBOUR
#: `+0x7A` is a live field (Winning Streak). Writing the pair as one dword --
#: the only option this function used to offer -- destroys it. See
#: `STAT_FIELDS_WORD`.
_WIDTHS = {1: ("<B", 0xFF), 2: ("<H", 0xFFFF), 4: ("<I", 0xFFFFFFFF)}


def write_field(buf, off, width, value):
    """Write one header field. Returns (old, new), or None if it did not move.

    `buf` is a bytearray. Width is 1, 2 or 4, all little-endian, matching how
    the client's own parser reads each field.

    WARNING: **VALUES ARE MASKED, NOT CLAMPED, AND NEGATIVES ARE TWO'S COMPLEMENT.**
    `Consecutive Wins` (+0x78) is a SIGNED word -- the client runs it negative to
    count consecutive LOSSES (`0xCC68A` writes 0xFFFF for -1) -- so -3 has to
    store as 0xFFFD and read back as -3. `(old, new)` are the RAW stored words;
    a caller that wants the signed sense converts.
    """
    if isinstance(value, (bytes, bytearray)):
        # THE STRING CASE. The two 17-byte name slots (`STRING_FIELDS`) are
        # server-authored -- the client only ever copies them OUT of the save
        # (`0x101508` / `0x10151E` -> the `0x1E2060` memcpy) and never writes
        # them back -- so something has to put them there, and it is not a
        # 1/2/4-byte integer. Padded with NULs and truncated to `width`;
        # returns the raw old/new bytes, so a caller that logs `%d` must
        # branch. No `_WIDTHS` entry is involved.
        if not (0 <= off and off + width <= len(buf)):
            return None
        raw = bytes(value)[:width].ljust(width, b"\x00")
        old = bytes(buf[off:off + width])
        if old == raw:
            return None
        buf[off:off + width] = raw
        return old, raw
    try:
        code, mask = _WIDTHS[width]
    except KeyError:
        raise ValueError("header fields are 1, 2 or 4 bytes, not %r" % (width,))
    if not (0 <= off and off + width <= len(buf)):
        return None
    old = struct.unpack_from(code, buf, off)[0]
    new = int(value) & mask
    if old == new:
        return None
    struct.pack_into(code, buf, off, new)
    return old, new


def apply_defaults(blob, only_zero=True):
    """Write the factory header into `blob`. Returns {offset: (old, new)} moved.

    Covers BOTH blocks -- the options (`DEFAULT_HEADER`) and the table-settings
    preset (`DEFAULT_TABLE_PRESET`) -- via `default_header_writes`, so healing an
    old save no longer leaves it with `au=0/al=0` and a table nobody can join.

    `only_zero` leaves any field the player (or `@Opt=`/`@Save=`) has already set,
    which is what makes this safe to run against a save that has a history.
    """
    out = bytearray(blob)
    moved = {}
    for off, (width, val) in sorted(default_header_writes().items()):
        if only_zero:
            cur = (out[off] if width == 1
                   else struct.unpack_from("<I", out, off)[0])
            if cur != 0:
                continue
        got = write_field(out, off, width, val)
        if got:
            moved[off] = got
    return bytes(out), moved


def build(cards, base=None, money=None):
    """A full 12328-byte save carrying `cards`, and optionally `money`.

    `base` is an existing save whose HEADER is kept -- rank and the shop flags
    live in +0x00..0x147, and the Pauper's Pack "already bought" bit is among
    them. Rebuilding a save without a base resets those, which is what made the
    free pack buyable on every launch.

    `money` is written at `MONEY_OFF` when given, and left alone when not. It is
    the field the player's balance actually comes from -- see the banner above
    `MONEY_OFF`. Passing None is the old behaviour exactly, so a caller that does
    not track money cannot zero somebody's balance by omission.
    """
    # WARNING: THE FILE ON DISK IS 12332, NOT 12328. `responders._FETCH_PATHLEN` declares
    # `12328 + 4` and the signer writes a checksum over the last 4 bytes of the
    # reply, so the stored blob carries a 4-byte TRAILER SLOT past the content.
    # Both lengths are accepted and the slot is preserved -- trimming it to 12328
    # would serve a reply 4 bytes short, which is the wrong-length read class
    # that already stalled this very path at 664.
    out = bytearray(base if base else b"\x00" * TOTAL)
    if len(out) not in (TOTAL, TOTAL + 4):
        raise ValueError("base save is %d bytes; expected %d, or %d with the "
                         "trailer slot" % (len(out), TOTAL, TOTAL + 4))
    if base is None:
        # MINTING. A zero header is not a neutral starting point -- it is the
        # bug. See the banner above `DEFAULT_HEADER`. An EXISTING base is left
        # alone: its zeros may be somebody's actual choices.
        for off, (width, val) in default_header_writes().items():
            write_field(out, off, width, val)
    if len(cards) > MAX_CARDS:
        raise ValueError("%d cards exceeds the %d the file holds"
                         % (len(cards), MAX_CARDS))
    struct.pack_into("<H", out, COUNT_OFF, min(len(cards), COUNT_CAP))
    if money is not None:
        struct.pack_into("<I", out, MONEY_OFF,
                         max(0, min(int(money), MONEY_MAX)))
    for i, c in enumerate(cards):
        o = CARDS_OFF + i * REC
        out[o:o + REC] = encode_card(c)
    # Everything past the last card stays as the base had it; the client never
    # reads beyond the count, so old rows there are inert.
    return bytes(out)


#: *** THE OPTIONS BLOCK, AND WHY A MARKER SAVE IS THE ONLY WAY TO READ IT. ***
#:
#: MEASURED 2026-08-20: every save this server has ever written has a header of
#: ZEROS. Members 3, 5 and 6 on prod carry 8, 7 and 6 non-zero bytes out of 328,
#: and all of them are `MONEY_OFF` and `COUNT_OFF` -- the only two fields this
#: module knows how to write. Everything else has never been anything but zero.
#:
#: THAT IS ONE ROOT CAUSE UNDER A WHOLE CLASS OF BUGS. The client reads about
#: fifty distinct header fields (found by scanning TM.dll for absolute references
#: into the save buffer at VA **0x5224BD0** -- the same anchor that gave
#: `COUNT_OFF` its `mov cx, word [0x5224C0C]`), and the run of single-byte reads
#: from +0xB0 to +0xD8 has exactly the shape of one byte per option. A setting
#: whose default is not zero therefore reads as zero, for every member, for ever.
#: A tester hit it as "the chat window shows 0 lines when the default is 5",
#: and noticed the general case before this module did.
#:
#: WARNING: AND THERE IS NO DEFAULT HEADER TO COPY. Checked, three ways: TM.dll writes
#: only TWO immediates into the save buffer (+0x000 = 300 and +0x0B6 = 0/1), so
#: it carries no defaults table; `Playdat.BIN` is 1173 bytes of packed UI data,
#: not a save; and `data/_tm-marker-archive/` is the literal bytes `TMDT`
#: repeated, a fill pattern that can only answer "is this region read at all".
#: SE's server supplied an initialised save on first login and we never had one.
#:
#: SO THE CLIENT HAS TO TELL US ITS OWN LAYOUT. Give each byte a value that names
#: its own offset, serve it, and the Options screen becomes a decoder ring: read a
#: number out of a box and `marker_offset()` says which byte it came from. No
#: disassembly, and it maps every visible setting in one pass instead of fifty.
#:
#: The default range is the options block alone. Marking the whole header would
#: put nonsense in money and the card count and make the save incoherent, and the
#: point is to read the SETTINGS.
MARKER_LO = 0xB0                #: the options block, per the read-site scan
#: ...through the END of the table-settings preset (`pa`, +0xF0), which the old
#: 0xE0 ceiling stopped four fields short of -- it predates `TABLE_PRESET` and
#: could not see `au`/`al`/`co`/`pa` at all. The ten password bytes at +0xF4 are
#: deliberately still outside: they are a STRING, and a marker ramp there shows
#: up as mojibake rather than as a readable number.
MARKER_HI = 0xF2
MARKER_START = 1                #: 0 is "unset"; start at 1 so a blank box is not
                                #: mistaken for a hit


def build_marker(base=None, lo=MARKER_LO, hi=MARKER_HI, start=MARKER_START):
    """A save whose bytes in `[lo, hi)` each carry a number naming their offset.

    Values run `start, start+1, ...` in offset order and are kept inside 1..255,
    so a box showing N means the byte at `lo + N - start`. Small consecutive
    numbers are deliberate: a setting that CLAMPS its value to a legal range
    still shows something, and a clamped box is obvious (several boxes reading
    the same ceiling) rather than silently wrong.

    WARNING: USE A SECOND PASS WITH A DIFFERENT `start` TO CONFIRM. One pass can be
    fooled by a clamp: if a box reads its maximum, that may be the marker or may
    be the clamp. Re-run with `start` shifted and a real reading MOVES by the
    same amount.

    `base` is preserved outside the marked range, so money, the card count and
    the cards themselves survive -- this is a settings probe, not a wipe.
    """
    if not (0 <= lo < hi <= CARDS_OFF):
        raise ValueError("marker range must sit inside the header, 0..0x%X"
                         % CARDS_OFF)
    out = bytearray(base if base else b"\x00" * TOTAL)
    if len(out) not in (TOTAL, TOTAL + 4):
        raise ValueError("base save is %d bytes; expected %d, or %d with the "
                         "trailer slot" % (len(out), TOTAL, TOTAL + 4))
    for i, off in enumerate(range(lo, hi)):
        v = start + i
        if v > 0xFF:
            raise ValueError("marker range %#x..%#x overflows a byte at start=%d"
                             % (lo, hi, start))
        out[off] = v
    return bytes(out)


#: *** THE THREE STRING FIELDS, AND THE ONLY WAY LEFT TO NAME THEM. ***
#:
#: `(file offset, byte width)`, straight off the parser's three copy calls
#: (`0x1E2060(dst, src, len)`), two of which were missed on the first read of
#: `0x1011F0` because the disassembly window stopped before the function did:
#:
#:     0x1012B6   src +0x07D  len 0x20  -> struct +0x128
#:     0x10150E   src +0x104  len 0x11  -> struct +0x104
#:     0x101524   src +0x115  len 0x11  -> struct +0x116
#:
#: WARNING: **AN XREF CANNOT FIND THEIR CONSUMERS.** All three are read through a
#: POINTER, not an absolute address, so the sweep that named every numeric field
#: returns nothing for them -- only `+0x128` has even one absolute reference
#: (`0x138C28`, a `std::string` assign). That is a limit of the instrument, not
#: evidence of absence, and it is why this exists: **the SCREEN is the only
#: reader we can interrogate.**
#:
#: `Memorable Win` (Playdat 43 / 84) is the standing candidate for the 17-byte
#: pair -- 17 bytes is 16 chars plus a NUL, the shape of every player NAME in
#: this game -- but which field is which is NOT established, and none of them is
#: authored. One launch with this save settles it.
STRING_FIELDS = ((0x07D, 32), (0x104, 17), (0x115, 17))


def string_marker_text(off, width):
    """The text written into one string field: its own offset, then a RULER.

    `104:5678901234` -- the prefix names the offset the reader should look up,
    and the digits are position markers, so a screen that TRUNCATES says exactly
    where it truncated instead of merely looking short. The numeric marker
    needed `marker_offset()` to decode a box; this needs nothing, because the
    field says its own name out loud.
    """
    label = "%03X:" % off
    ruler = "".join(str(i % 10) for i in range(width - 1 - len(label)))
    return (label + ruler)[:width - 1]


def build_string_marker(base=None, fields=STRING_FIELDS):
    """A save whose STRING fields each announce their own offset.

    The string twin of `build_marker`, and the same contract: everything outside
    the marked fields is preserved, so money, the card count and the collection
    survive. This is a probe, not a wipe.

    WARNING: Each string is written NUL-TERMINATED and strictly inside its field. The
    parser's copies are fixed-length (`0x20`/`0x11`/`0x11`), so a string that
    filled its field would leave the client scanning into whatever follows --
    which for `+0x104` is the OTHER 17-byte field and would read as one long
    value, exactly the confusion this probe exists to remove.
    """
    out = bytearray(base if base else b"\x00" * TOTAL)
    if len(out) not in (TOTAL, TOTAL + 4):
        raise ValueError("base save is %d bytes; expected %d, or %d with the "
                         "trailer slot" % (len(out), TOTAL, TOTAL + 4))
    for off, width in fields:
        if not (0 <= off and off + width <= CARDS_OFF):
            raise ValueError("string field %#x/%d is not inside the header"
                             % (off, width))
        raw = string_marker_text(off, width).encode("ascii")
        out[off:off + width] = raw + b"\x00" * (width - len(raw))
    return bytes(out)


def read_strings(blob, fields=STRING_FIELDS):
    """`{offset: text}` for the string fields -- what a save actually carries.

    Reads to the first NUL, the way the client's own display will, so a field
    that was written without one shows the overrun rather than hiding it.
    """
    out = {}
    for off, width in fields:
        raw = bytes(blob[off:off + width])
        text = raw.split(b"\x00")[0]
        try:
            out[off] = text.decode("cp932")
        except UnicodeDecodeError:
            out[off] = repr(text)
    return out


def selftest(say=print):
    """0 = pass. Guards the layout constants and the two probe builders."""
    bad = []

    def check(cond, what):
        if not cond:
            bad.append(what)
            say("FAIL: " + what)

    # 1. THE STRING FIELDS MUST NOT COLLIDE WITH ANYTHING WE AUTHOR. This is
    #    the assertion that makes the probe safe to serve: a marker that landed
    #    on the options block or on a stat would corrupt the very save it is
    #    meant to interrogate, and a tester would read the damage as a
    #    finding.
    claimed = {}
    for off, name in STAT_FIELDS.items():
        width = 2 if off in STAT_FIELDS_WORD else (1 if off == MOST_COMBOS_OFF
                                                   else 4)
        for b in range(off, off + width):
            claimed[b] = name
    for off in (MONEY_OFF, COUNT_OFF, CARDPOINTS_OFF):
        for b in range(off, off + 4):
            claimed.setdefault(b, "header")
    for off in list(DEFAULT_HEADER) + list(DEFAULT_HEADER_MEASURED_ZERO):
        claimed.setdefault(off, "option")
    for name, off, width in TABLE_PRESET:
        for b in range(off, off + width):
            claimed.setdefault(b, "preset")
    for soff, swidth in STRING_FIELDS:
        hit = {b: claimed[b] for b in range(soff, soff + swidth)
               if b in claimed}
        check(not hit, "string field +0x%X/%d overlaps %r -- the probe would "
                       "corrupt what it is measuring" % (soff, swidth, hit))

    # 2. ...AND THE PROBE PRESERVES EVERYTHING ELSE.
    base = bytearray(TOTAL)
    write_field(base, AVG_RANK_OFF, 4, 233)
    write_field(base, CONSEC_WINS_OFF, 2, -4)
    write_field(base, STREAK_OFF, 2, 9)
    struct.pack_into("<H", base, COUNT_OFF, 7)
    marked = build_string_marker(bytes(base))
    s = read_stats(marked)
    check(s["avg_rank"] == 233 and s["consec_wins"] == -4 and s["streak"] == 9,
          "the string marker must preserve the career block -- got %r" % (s,))
    check(struct.unpack_from("<H", marked, COUNT_OFF)[0] == 7,
          "...and the collection count")
    got = read_strings(marked)
    for off, _w in STRING_FIELDS:
        check(got[off].startswith("%03X:" % off),
              "+0x%X must announce its own offset -- got %r" % (off, got[off]))
    check(len(set(got.values())) == len(STRING_FIELDS),
          "the three strings must be DISTINGUISHABLE on screen -- got %r"
          % (sorted(got.values()),))
    for off, width in STRING_FIELDS:
        check(marked[off + width - 1] == 0,
              "+0x%X must stay NUL-terminated inside its field, or the client "
              "reads on into the next one" % off)

    # 3. WIDTHS. The pair that a dword write would destroy, and the signed one.
    b = bytearray(64)
    write_field(b, 0, 2, -3)
    write_field(b, 2, 2, 7)
    check(struct.unpack_from("<h", b, 0)[0] == -3
          and struct.unpack_from("<H", b, 2)[0] == 7,
          "a width-2 write must store two's complement and leave its NEIGHBOUR "
          "alone -- got %r" % (bytes(b[:4]),))
    try:
        write_field(b, 0, 3, 1)
        check(False, "an unsupported width must raise, not silently no-op")
    except ValueError:
        pass
    check(CONSEC_WINS_OFF + 2 == STREAK_OFF,
          "Consecutive Wins and Winning Streak must stay ADJACENT WORDS -- the "
          "whole reason write_field has width 2")

    say("tmsave: %d check(s) failed" % len(bad) if bad
        else "tmsave: PASS -- layout, probes and widths")
    return 1 if bad else 0


def marker_offset(value, lo=MARKER_LO, start=MARKER_START):
    """The save offset a marker `value` came from, or None if it is out of range."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    off = lo + v - start
    return off if lo <= off < lo + 0x100 and v >= start else None


def decode(blob):
    n = struct.unpack_from("<H", blob, COUNT_OFF)[0]
    eff = min(n, COUNT_CAP, MAX_CARDS)
    rows = [decode_card(blob[CARDS_OFF + i * REC:CARDS_OFF + (i + 1) * REC])
            for i in range(eff)]
    return {"count": n, "effective": eff, "cards": rows}


def read_stats(blob):
    """`{name: value}` for the career block, each at ITS OWN width.

    The instrument for a live test: read this beside Player Data -> Status and
    the two must agree field for field. Reading the block as dwords -- the
    obvious thing, and what a hex editor invites -- silently merges
    `Consecutive Wins` with `Winning Streak` and reports one number where the
    client sees two.
    """
    out = {}
    for off, name in sorted(STAT_FIELDS.items()):
        if off in STAT_FIELDS_WORD:
            v = struct.unpack_from("<H", blob, off)[0]
            if off == CONSEC_WINS_OFF and v >= 0x8000:
                v -= 0x10000               # the one SIGNED field
        else:
            v = struct.unpack_from("<I", blob, off)[0]
        out[name] = v
    return out


def dump_strings(blob):
    """Print the three string fields, unidentified on purpose."""
    got = read_strings(blob)
    if not any(got.values()):
        return got
    print("  string fields (UNIDENTIFIED -- `Memorable Win` is a candidate):")
    for off, width in STRING_FIELDS:
        print("    +0x%-4X %2dB  %r" % (off, width, got[off]))
    return got


def dump_stats(blob):
    """Print the career block the way the Status screen renders it."""
    s = read_stats(blob)
    x100 = {"avg_rank", "rating"}
    print("  career stats (Player Data -> Status):")
    for off, name in sorted(STAT_FIELDS.items()):
        v = s[name]
        shown = ("%d.%02d" % (v // 100, v % 100)) if name in x100 else str(v)
        note = ""
        if name == "avg_rank" and v and v < 100:
            note = "   !! below 1.00 -- the client cannot produce this"
        if name == "avg_rank" and not v:
            note = "   (unset: no placements recorded)"
        if name == "consec_wins" and v < 0:
            note = "   (negative == a LOSING run; the screen says " \
                   "'Consecutive Losses %d')" % -v
        if name == "average_prize" and s["opponents"]:
            note = "   (= %d / %d opponents faced)" % (s["prize_total"],
                                                       s["opponents"])
        if name == "average_prize" and not s["opponents"] and v:
            note = "   !! a quotient with a ZERO divisor stored -- the result " \
                   "screen divides by +0x40 and would fault"
        print("    +0x%-4X %-15s %s%s" % (off, name, shown, note))
    return s


def dump(blob, names=True):
    try:
        import tm_cardprm          # a sibling in services/, so no path games
    except Exception:
        tm_cardprm = None
    d = decode(blob)
    print("save %d B, collection count = %d%s" % (
        len(blob), d["count"],
        "" if d["count"] == d["effective"]
        else " (clamped to %d)" % d["effective"]))
    dump_stats(blob)
    dump_strings(blob)
    if d["count"] == 0:
        print("  !! COUNT IS ZERO -- 0x5091355 skips the ENTIRE card loop, so "
              "the client loads no cards at all no matter what follows.")
    for i, c in enumerate(d["cards"]):
        nm = ""
        if names and tm_cardprm is not None:
            nm = "  %s" % tm_cardprm.name(c["id"])
        print("  [%3d] id=%-4d atk=%-3d type=%d pdef=%-3d mdef=%-3d tail=%s%s%s"
              % (i, c["id"], c["attack"], c["type"], c["pdef"], c["mdef"],
                 c["tail"], nm,
                 "   <-- DROPPED: " + "; ".join(c["dropped"]) if c["dropped"] else ""))
    kept = sum(1 for c in d["cards"] if not c["dropped"])
    print("Tetra Master will load %d of %d card(s)." % (kept, d["effective"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dump", metavar="SAVE", help="decode a save and stop")
    ap.add_argument("--from-collection", metavar="JSON",
                    help="a `*.tm_collection.json` written by tetramaster.py")
    ap.add_argument("--base", metavar="SAVE",
                    help="keep this save's HEADER (money, rank, shop flags)")
    ap.add_argument("--out", metavar="SAVE", help="write the new save here")
    ap.add_argument("--selftest", action="store_true",
                    help="assert the layout constants and the probes")
    ap.add_argument("--string-marker", action="store_true",
                    help="write a save whose three STRING fields each announce "
                         "their own offset -- one launch names them on screen")
    ap.add_argument("--marker", action="store_true",
                    help="write a MARKER save: each byte in the options block "
                         "carries a number naming its own offset, so the "
                         "Options screen reads back as a field map")
    ap.add_argument("--marker-range", metavar="LO:HI",
                    default="%#x:%#x" % (MARKER_LO, MARKER_HI),
                    help="the byte range to mark (default the options block)")
    ap.add_argument("--marker-start", type=int, default=MARKER_START,
                    help="first marker value; shift it for a confirming pass")
    ap.add_argument("--decode-marker", metavar="N", type=int,
                    help="a number read off the Options screen -> its offset")
    ap.add_argument("--heal", metavar="SAVE",
                    help="repair an EXISTING save's settings defaults in place "
                         "(only bytes still zero are touched); needs --out")
    a = ap.parse_args()

    if a.selftest:
        raise SystemExit(selftest())

    if a.dump:
        with open(a.dump, "rb") as f:
            dump(f.read())
        return

    lo, _, hi = a.marker_range.partition(":")
    lo, hi = int(lo, 0), int(hi, 0)

    if a.heal:
        with open(a.heal, "rb") as f:
            blob = f.read()
        healed, moved = apply_defaults(blob)
        if not moved:
            print("%s: nothing to heal -- every default is already set or the "
                  "player has chosen their own" % a.heal)
        for off, (old, new) in sorted(moved.items()):
            print("  +0x%03X  %d -> %d" % (off, old, new))
        if a.out:
            with open(a.out, "wb") as f:
                f.write(healed)
            print("wrote %s (%d B)" % (a.out, len(healed)))
        else:
            print("(no --out; nothing written)")
        return

    if a.decode_marker is not None:
        off = marker_offset(a.decode_marker, lo, a.marker_start)
        if off is None:
            print("%d is outside the marked range -- that box is NOT reading a "
                  "marked byte (a real default, a clamp, or a field outside "
                  "%#x..%#x)" % (a.decode_marker, lo, hi))
        else:
            print("%d  ->  save +0x%03X" % (a.decode_marker, off))
        return

    if a.string_marker:
        base = None
        if a.base:
            with open(a.base, "rb") as f:
                base = f.read()
        blob = build_string_marker(base)
        print("string-marker save: each of the THREE string fields now says its "
              "own offset.")
        for off, text in sorted(read_strings(blob).items()):
            print("    +0x%03X  %r" % (off, text))
        print("Serve it, open Player Data -> Status, and read which field shows "
              "which prefix.\n"
              "The prefix IS the offset -- no decoder needed. `Memorable Win` "
              "is the standing candidate;\n"
              "a field that shows NOTHING is one this client does not render, "
              "which is also an answer.\n"
              "!! Note where each string TRUNCATES: the digits are a position "
              "ruler, so a box ending\n"
              "   at `...345` is showing 8 characters, not 16.")
        if a.out:
            with open(a.out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (a.out, len(blob)))
        else:
            print("(no --out; nothing written)")
        return

    if a.marker:
        base = None
        if a.base:
            with open(a.base, "rb") as f:
                base = f.read()
        blob = build_marker(base, lo, hi, a.marker_start)
        print("marker save: %#x..%#x carry %d..%d"
              % (lo, hi, a.marker_start, a.marker_start + hi - lo - 1))
        print("read a number N off the Options screen, then: "
              "tmsave.py --decode-marker N")
        if a.out:
            with open(a.out, "wb") as f:
                f.write(blob)
            print("wrote %s (%d B)" % (a.out, len(blob)))
        else:
            print("(no --out; nothing written)")
        return

    cards = []
    if a.from_collection:
        with open(a.from_collection, "r", encoding="utf-8") as f:
            cards = json.load(f).get("cards", [])
    base = None
    if a.base:
        with open(a.base, "rb") as f:
            base = f.read()
    blob = build(cards, base)
    dump(blob)
    if a.out:
        with open(a.out, "wb") as f:
            f.write(blob)
        print("wrote %s (%d B)" % (a.out, len(blob)))
    else:
        print("(no --out; nothing written)")


if __name__ == "__main__":
    main()
