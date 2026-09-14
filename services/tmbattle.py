"""Tetra Master's board geometry, arrow rules and battle resolver.

VERIFIED: PORTED FROM TM.dll, FUNCTION BY FUNCTION, 2026-08-20. Every rule below names
the RVA it was read from. Nothing here is fitted to an observation; where a
value could not be measured it is named as INFERRED and given a knob.

WHY A SEPARATE MODULE. `services/tetramaster.py` owns the wire; this owns the
GAME. The split matters because this half is pure -- board in, board out, no
sockets, no globals -- so `selftest()` can drive a whole match through it and a
wrong flip shows up as a failing assertion instead of a screenshot.

THE FOUR THINGS THE SERVER OWES A MATCH, and where each is measured:

  1. WHICH TILES TOUCH WHICH -- `_NEIGHBOURS`, against the client's own table
     at rva 0x233748, and the tile counts at rva 0x23359E.
  2. WHETHER A PLACEMENT FIGHTS OR JUST FLIPS -- `contest`, ported from
     **0xD3760**, the arrow engine.
  3. WHAT A FIGHT ROLLS -- `resolve`, ported from **0xD31D0**, and
  4. WHO WON -- `winner`, ported from **0xD34D0**, which is a DIFFERENT
     function and does not agree with 0xD31D0's own return value. 0xD31D0's
     return is dead (its single caller at 0xCEE90 discards it); 0xD34D0 is the
     one the scene branches on, and only 0xD34D0 has a DRAW.
"""

# --------------------------------------------------------------------------
# 1. THE BOARD
# --------------------------------------------------------------------------

#: How many tiles a board has, and its shape. The counts are read straight out
#: of the image at **rva 0x23359E**: {16, 25, 233, 0, ...}. Two players is 16 --
#: a 4x4 board, which is Tetra Master's -- and three is 25. The third entry is
#: nonsense, so only these two are real. `TILES_BY_PLAYERS` in tetramaster.py
#: is the same table read for the wire; this copy is the geometry's own.
TILE_COUNTS = {16: (4, 4), 25: (5, 5)}

#: The eight arrow directions, in BIT ORDER: bit i of a card's arrow mask is
#: direction i. Derived from the client's neighbour table, not assumed --
#: `selftest()` re-derives the whole table from this ordering and compares it
#: against the 16 rows dumped out of TM.dll, so a wrong order fails loudly.
#:
#: Reading tile 5 of the 4x4 board (row 1, col 1 -- every neighbour on the
#: board) the shipped table gives [1, 2, 6, 10, 9, 8, 4, 0], which is
#: (r-1,c) (r-1,c+1) (r,c+1) (r+1,c+1) (r+1,c) (r+1,c-1) (r,c-1) (r-1,c-1):
#: clockwise from north.
DIRECTIONS = ((-1, 0), (-1, 1), (0, 1), (1, 1),
              (1, 0), (1, -1), (0, -1), (-1, -1))
DIR_NAMES = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def opposite(d):
    """WARNING: THE OPPOSITE OF DIRECTION d IS (d + 4) & 7, AND THAT IS WHAT MAKES A
    BATTLE. 0xD3836 loads the neighbour's arrow byte and computes
    `(a >> 4) | (a << 4)` -- a rotate by four -- before testing the SAME bit it
    tested on the attacker. Rotating a byte by 4 and testing bit d is testing
    bit (d+4)&7 of the original.
    """
    return (d + 4) & 7


def _build_neighbours(tiles):
    """tile -> the eight neighbouring tiles (None off the board).

    Tile index is `row * width + col`, row 0 at the top -- the reading the
    dumped table confirms in `selftest()`.
    """
    w, h = TILE_COUNTS[tiles]
    out = []
    for t in range(tiles):
        r, c = divmod(t, w)
        row = []
        for dr, dc in DIRECTIONS:
            nr, nc = r + dr, c + dc
            row.append(nr * w + nc if 0 <= nr < h and 0 <= nc < w else None)
        out.append(tuple(row))
    return tuple(out)


_NEIGHBOURS = {n: _build_neighbours(n) for n in TILE_COUNTS}


def neighbours(tiles, tile):
    """The eight neighbours of `tile` on a `tiles`-tile board, by direction."""
    return _NEIGHBOURS[tiles][tile]


#: The client's own table, verbatim, for the 16-tile board -- dumped from
#: **rva 0x233748**, eight bytes per tile, stride 25 tiles per board kind, with
#: "off the board" written as the tile count itself. Kept so `selftest()` can
#: check the derivation against the shipped data rather than against itself.
_NEIGHBOURS_16_MEASURED = (
    (16, 16, 1, 5, 4, 16, 16, 16), (16, 16, 2, 6, 5, 4, 0, 16),
    (16, 16, 3, 7, 6, 5, 1, 16), (16, 16, 16, 16, 7, 6, 2, 16),
    (0, 1, 5, 9, 8, 16, 16, 16), (1, 2, 6, 10, 9, 8, 4, 0),
    (2, 3, 7, 11, 10, 9, 5, 1), (3, 16, 16, 16, 11, 10, 6, 2),
    (4, 5, 9, 13, 12, 16, 16, 16), (5, 6, 10, 14, 13, 12, 8, 4),
    (6, 7, 11, 15, 14, 13, 9, 5), (7, 16, 16, 16, 15, 14, 10, 6),
    (8, 9, 13, 16, 16, 16, 16, 16), (9, 10, 14, 16, 16, 16, 12, 8),
    (10, 11, 15, 16, 16, 16, 13, 9), (11, 16, 16, 16, 16, 16, 14, 10),
)

# --------------------------------------------------------------------------
# 2. THE CARD ROW
# --------------------------------------------------------------------------

#: The eight `|` fields of a wire card row, in the order `@PutCard` /
#: `@CardSelect=` / `@Card=` all parse them (0x103951..0x103AAA), with the
#: record offset each lands at once 0xB3200 copies the 15-byte staging area
#: into a 156-byte card record:
#:
#:     slot 0  word  -> rec+0x0E  card id     (< [0x2FF4FC], 250)
#:     slot 1  byte  -> rec+0x08  ATTACK
#:     slot 2  byte  -> rec+0x09  TYPE        (< 4, else Error:CardNum)
#:     slot 3  byte  -> rec+0x0A  PHYS DEF
#:     slot 4  byte  -> rec+0x0B  MAG DEF
#:     slot 5  byte  -> rec+0x0C  a POWER byte, and it is DERIVED
#:     slot 6  byte  -> rec+0x0D  the ARROW MASK
#:     slot 7  byte  -> rec+0x16  unread; compared against 0x32 and indexes a
#:                                word table at 0x2BB560
#:
#: WARNING: SLOT 6 IS THE ARROW MASK AND SLOT 5 IS NOT THE LEVEL. Both readings in
#: this project's history were wrong and both are retracted here; see
#: `ARROW_COUNT_WEIGHTS` for slot 6 and `power_byte` for slot 5.
ROW_FIELDS = ("id", "attack", "type", "pdef", "mdef", "power", "arrows", "flag")

#: The arrow-count distribution, 100 entries, **rva 0x2334A0**. The client's own
#: card generator (0xB2DF4..0xB2E9A) shuffles the eight directions, draws
#: `n = TABLE[rand() % 100]` of them and ORs their bits into rec+0x0D:
#:
#:     0 arrows  1%     3 arrows 31%     6 arrows  6%
#:     1 arrow   4%     4 arrows 27%     7 arrows  3%
#:     2 arrows 16%     5 arrows 10%     8 arrows  2%
#:
#: WARNING: THIS IS THE PROOF THAT SLOT 6 IS NOT THE LEVEL, and it matters because
#: this server has been sending the level there. A per-card RANDOM count of
#: arrows cannot be a column of `CardPrm.BIN`, and CardPrm byte 5 is pinned as
#: the level by six measured sell prices. The two
#: readings collided because our own shop put CardPrm byte 5 into slot 6, so
#: the one captured hand -- `161|100|2|90|99|31|21|255` -- is the client
#: echoing OUR level back at us, not evidence about SE's format.
#:
#: Two independent confirmations that rec+0x0D is the mask, neither of which
#: can be read any other way:
#:
#:   * 0xB4D0D, the card RENDERER: `eax = 1 << bit; test [esi+0x0D], al` in a
#:     loop over eight bits, drawing one arrow sprite per set bit.
#:   * 0xD3760, the adjacency engine: the same byte, tested per direction.
#:
#: and the constructor for pseudo-cards writes it directly -- 0xB304D sets
#: 0xFF (all eight) and 0xB3095 sets `1 << (rand() & 7)` (exactly one).
ARROW_COUNT_WEIGHTS = (
    0, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    3, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
    4, 4, 4, 4, 4, 4,
    5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6, 7, 7, 7, 8, 8,
)


def draw_arrows(rnd):
    """One card instance's arrow mask, drawn the way the client draws one.

    0xB2DF4: fill a list with 0..7, take `n = ARROW_COUNT_WEIGHTS[rand()%100]`,
    then n times pick a random remaining entry, OR `1 << it` into the mask and
    compact the list over the entry taken. Reproduced exactly, because picking
    WITHOUT REPLACEMENT is what stops a 4-arrow card ever having fewer than
    four -- with replacement the distribution above would not be the one on
    screen.
    """
    pool = list(range(8))
    mask = 0
    for _ in range(ARROW_COUNT_WEIGHTS[rnd.randrange(100)]):
        mask |= 1 << pool.pop(rnd.randrange(len(pool)))
    return mask


def power_byte(base_power, attack, pdef, mdef, base_attack, base_pdef,
               base_mdef):
    """Wire slot 5 -- rec+0x0C, and it is DERIVED, not a table column.

    0xB37CF, inside the card builder:

        esi = CardPrm[3] + CardPrm[2] + CardPrm[0]      the BASELINE total
        eax = rec[0x0B] + rec[0x0A] + rec[0x08]         THIS card's total
        rec[0x0C] = (eax * CardPrm[4]) / esi

    so it is CardPrm byte 4 scaled by how much better than nominal this
    instance rolled -- which is why a card with nominal stats carries CardPrm
    byte 4 unchanged, and why we have been getting it right by accident.
    """
    base_total = base_attack + base_pdef + base_mdef
    if base_total <= 0:
        return 0
    return min(255, (attack + pdef + mdef) * base_power // base_total)


def unpack(row):
    """A wire card row (bytes, str or sequence) as a dict of `ROW_FIELDS`."""
    if isinstance(row, dict):
        return dict(row)
    if isinstance(row, (bytes, bytearray)):
        vals = [int(v) for v in bytes(row).split(b"|") if v != b""]
    elif isinstance(row, str):
        vals = [int(v) for v in row.split("|") if v != ""]
    else:
        vals = [int(v) for v in row]
    vals = (vals + [0] * 8)[:8]
    return dict(zip(ROW_FIELDS, vals))


# --------------------------------------------------------------------------
# 3. WHAT A CARD ON A TILE IS
# --------------------------------------------------------------------------

class Card(object):
    """One card sitting on one tile.

    WARNING: `ability` (rec+0x24) AND `modifier` (rec+0x26) ARE ZERO FOR EVERY CARD
    THIS SERVER CAN PUT ON A BOARD, AND THAT IS BY CONSTRUCTION, NOT A GUESS.
    An earlier inference had them as wire slots 5 and 7; they
    are neither, and the read that settles it is 0xB3200 itself:

      * 0xB3200 -- the copy that MAKES a tile record out of a wire row -- writes
        `[rec+0x24] = 0` (0xB32A9) and `[rec+0x26] = 0` (0xB32AC). So do all the
        other card constructors: 0xB2B00, 0xB3400, 0xB34B0, 0xB35C0, 0xB9000.
      * The ONLY non-zero writes to either field in the whole image are
        0xB3043 / 0xB305B / 0xB30BC / 0xB30CF, inside the arm for pseudo card
        ids **0x8003..0x800B** (0xB301D computes `id - 0x8003`, bound 8) --
        UI placeholders, which `@PutCard` cannot carry because 0x103982
        refuses any id >= [0x2FF4FC] = 250.
      * On the board `+0x24` belongs to the TILE and not the card: 0xC7015
        saves it across a placement and 0xC703C puts it back. Tiles are zeroed
        at 0xD249B / 0xD2526, and the only writer (0xD1A2E) is the pre-placed
        board setup this server never uses.

    So the multiplier is always 1 here and the stat modifier always 0, and the
    roll's divisor collapses to `stat + 1`. They stay PARAMETERS of everything
    below anyway, because the moment a mode with special tiles exists they are
    the one input that changes the arithmetic, and a hardcoded 1 would hide it.
    """

    __slots__ = ("row", "owner", "ability", "modifier", "placer")

    def __init__(self, row, owner, ability=0, modifier=0):
        self.row = unpack(row)
        self.owner = owner              # absolute seat, or None for nobody
        self.ability = ability          # rec+0x24
        self.modifier = modifier        # rec+0x26, SIGNED
        # WHO PLAYED IT, which is NOT `owner` once a battle is lost: `owner`
        # moves, this never does. It is not a client field -- the client has no
        # use for it, because it settles up on the result screen and forgets --
        # but the server cannot tell "cards you TOOK" from "cards you played"
        # without it, and `owner != placer` is exactly what a take is. Set
        # once, at construction; nothing below writes it.
        self.placer = owner

    @property
    def arrows(self):
        """The mask the engine actually tests.

        0xD378D: an ability of 3 means ALL EIGHT arrows, and the renderer
        agrees (0xB4D1F draws every direction for the same value).
        """
        return 0xFF if (self.ability & 0xF) == 3 else self.row["arrows"] & 0xFF

    def __repr__(self):
        return "Card(#%d owner=%s arrows=0x%02X)" % (
            self.row["id"], self.owner, self.arrows)


# --------------------------------------------------------------------------
# 4. THE ARROW ENGINE -- 0xD3760
# --------------------------------------------------------------------------

BATTLE, FLIP = 1, 2             #: the two values 0xD3760 writes to rec+0x20


#: The owner byte a board OBJECT carries in `rec+0x1D`, and NEITHER IS A SEAT.
#: The client's materialiser writes 4 for every block (0xC1B12, the tail every
#: block-family code reaches) and 8 for a special tile (0xC198E), and the arrow
#: engine branches on exactly those two values -- see `contest`.
OWNER_BLOCK, OWNER_SPECIAL = 4, 8

#: The seat count `contest` compares an owner against. The client reads the
#: real N out of [0x52464C5]; 4 is the safe default here because the only
#: boards that exist are 2- and 3-player, so every real seat is < 4 and both
#: object owners are >= 4. Pass the match's own N to make the comparison the
#: client's exactly.
DEFAULT_PLAYERS = 4


def contest(board, tiles, tile, actor, players=DEFAULT_PLAYERS):
    """What a card just placed on `tile` by `actor` touches. Ported 0xD3760.

    Returns `{neighbour tile: BATTLE or FLIP}`.

    For each of the eight directions the placed card has an arrow in:

        0xD37F1  the neighbour comes out of the table; off the board -> skip
        0xD3811  its OWNER is rec+0x1D
        0xD3818  owner >= the player count -> the NEUTRAL arm at 0xD386B
        0xD3820  it is the actor's own card -> skip
        0xD3849  it points BACK (bit (d+4)&7) -> **BATTLE** (0xD3882 writes 1)
        0xD3855  ...or its ability is 3, which also forces a battle
        0xD3859  ...or it is already flagged 1, which is left alone
        0xD3862  otherwise -> **FLIP**, taken with no fight at all (writes 2)

    WARNING: THE NEUTRAL ARM IS NOW MODELLED, AND IT IS WHAT MAKES A BLOCK A BLOCK.
    0xD386B, reached whenever the neighbour's owner is not a seat, is three
    tests and no reverse-arrow test at all:

        0xD386D  owner != 4            -> skip entirely
        0xD386F  already flagged 2     -> skip
        0xD387E  its arrow mask is 0   -> skip
        0xD3882  otherwise             -> **BATTLE**

    So of the two object owners the materialiser writes, only `OWNER_BLOCK`
    (4) is reachable: a **special tile is never contested at all** (owner 8
    fails 0xD386D), which is right, because a special tile is a MODIFIER ON AN
    EMPTY TILE and not an occupant -- see `special_ability`. A block is
    battled *without* pointing back, so a chance block (all eight arrows) or a
    rotating block (exactly one) fights anything that points at it, while a
    plain block (mask 0) is inert. And a neutral tile is never FLIPPED: the arm
    writes only 1.
    """
    me = board.get(tile)
    if me is None:
        return {}
    out = {}
    for d in range(8):
        if not (me.arrows >> d) & 1:
            continue
        u = _NEIGHBOURS[tiles][tile][d]
        if u is None:
            continue
        occ = board.get(u)
        if occ is None or occ.owner is None:
            continue
        if occ.owner >= players:
            # 0xD386B, the neutral arm.
            if occ.owner != OWNER_BLOCK or out.get(u) == FLIP or not occ.arrows:
                continue
            out[u] = BATTLE
            continue
        if occ.owner == actor:
            continue
        if (occ.arrows >> opposite(d)) & 1 or (occ.ability & 0xF) == 3:
            out[u] = BATTLE
        elif out.get(u) != BATTLE:
            out[u] = FLIP
    return out


def next_defender(marks):
    """The tile `-BattleCheck` picks when it does not ask the player.

    0xD38F0 walks the tiles in INDEX order collecting everything flagged 1 and
    returns the LAST one it saw (`mov al, bl` at 0xD394E, inside the branch).
    0xCE47B stores that in [obj+0x17A] and 0xCE483 branches on the count: none
    means the placement is over, exactly one is taken automatically, more than
    one raises `-BattleSelect:Target=%d` and the player chooses.

    WARNING: So a MULTI-battle placement is the one case that needs the client's
    answer first. See `POL_TM_BATTLE` in tetramaster.py: the reply to
    `@BattleSelect=` is the same message with the same body.
    """
    battles = sorted(t for t, kind in marks.items() if kind == BATTLE)
    return battles[-1] if battles else None


# --------------------------------------------------------------------------
# 5. THE ROLL -- 0xD31D0
# --------------------------------------------------------------------------

#: The three `+0x29` selector values, as the resolver writes them. They name
#: WHICH stat the number on screen came from, which is why they have to be sent
#: rather than derived: the card renderer highlights that stat.
SEL_ATTACK, SEL_PDEF, SEL_MDEF = 1, 4, 8


def _stats(att, dfn):
    """(attacker stat, defender stat, attacker selector, defender selector).

    The four arms of the jump table at **rva 0xD34B8**, dispatched on the
    ATTACKER'S type (0xD31EA), decoded entry by entry:

        type 0 (P)  0xD31F1  attack   vs  phys def          sel 1 / 4
        type 1 (M)  0xD3216  attack   vs  mag def           sel 1 / 8
        type 2 (X)  0xD323B  attack   vs  min(pdef, mdef)   sel 1 / 4 or 8
        type 3 (A)  0xD327D  max(atk,pdef,mdef) vs min(...) both computed

    and it agrees with `CardPrm.BIN`, which carries types 0-3 only.

    WARNING: EVERY TIE GOES TO THE MAGIC SIDE, and that is measured, not tidied:
    0xD326B is `setae` (>=, so pdef == mdef takes mdef, selector 8) and the
    type-3 arms at 0xD32E5 / 0xD32F6 / 0xD331F / 0xD3336 use `setle` / `setge`
    the same way.
    """
    a, p, m = att["attack"], att["pdef"], att["mdef"]
    da, dp, dm = dfn["attack"], dfn["pdef"], dfn["mdef"]
    t = att["type"]
    if t == 0:
        return a, dp, SEL_ATTACK, SEL_PDEF
    if t == 1:
        return a, dm, SEL_ATTACK, SEL_MDEF
    if t == 2:
        return (a, min(dp, dm), SEL_ATTACK,
                SEL_MDEF if dp >= dm else SEL_PDEF)
    # type 3: the attacker's best against the defender's worst, and each side's
    # selector says which stat that turned out to be.
    if a > p:
        a_sel = SEL_MDEF if a <= m else SEL_ATTACK
    else:
        a_sel = SEL_MDEF if p <= m else SEL_PDEF
    if da < dp:
        d_sel = SEL_MDEF if da >= dm else SEL_ATTACK
    else:
        d_sel = SEL_MDEF if dp >= dm else SEL_PDEF
    return max(a, p, m), min(da, dp, dm), a_sel, d_sel


def _multiplier(ability, want, rnd):
    """`rec+0x2A`. 0xD3375 (attacker) / 0xD339E (defender).

    An ability of 1 on the ATTACKER, or 2 on the DEFENDER, gives that side
    `rand() % 4 + 2` -- so 2..5 -- and every other card gets 1. See the `Card`
    banner for why every card this server deals gets 1.
    """
    return rnd.randrange(4) + 2 if (ability & 0xF) == want else 1


def _roll(raw, mult, rnd):
    """`rec+0x8C`, the number the card counts DOWN to. 0xD33E5 / 0xD3427.

    Instruction for instruction, which is what was required before a
    line of Python:

        imul ecx, edx      N = raw * multiplier
        inc  ecx           N = N + 1                <- the +1 is real
        idiv ecx  x2       two independent rand() values, each taken MOD N
        add / cdq / sub / sar eax, 1
                           their sum, halved -- the `sub eax,edx` before the
                           `sar` is MSVC's round-toward-zero fixup, and both
                           operands are non-negative here, so it is a floor

    So the roll is the AVERAGE OF TWO uniform draws over 0..raw*mult: a strong
    card rarely rolls terribly, and no card ever rolls above its own stat.
    """
    n = raw * mult + 1
    return (rnd.randrange(0x8000) % n + rnd.randrange(0x8000) % n) // 2


def resolve(att, dfn, rnd, att_ability=0, dfn_ability=0,
            att_mod=0, dfn_mod=0):
    """One battle. Ported from **0xD31D0**; returns the ten wire values.

    `att` / `dfn` are unpacked card rows. The result keys are named for the
    `/A=` and `/D=` occurrences `@BattleData` carries and for the record fields
    0xCF1D6 scatters them into:

        raw   occ1  rec+0x8E   the stat, clamped to >= 1  (0xD334D)
        roll  occ2  rec+0x8C   what it rolled             (0xD33E5)
        sel   occ3  rec+0x29   which stat that was        (0xD3207 &c)
        mult  occ4  rec+0x2A   the ability multiplier     (0xD339B)

    WARNING: FOUR `rand()` CALLS UP FRONT, IN THIS ORDER (0xD33C5..0xD33DC), the
    attacker consuming the first two and the defender the last two. The order
    changes neither roll -- both are sums -- but it is written down because the
    client's vs-COM path draws from the same CRT stream (`0x1E2E44` is the
    textbook MSVC LCG, `seed*0x343FD + 0x269EC3 >> 16 & 0x7FFF`) and anyone
    diffing the two side by side will need it.

    WARNING: WE DO NOT HAVE TO REPRODUCE THE CLIENT'S SEQUENCE, only its SHAPE. In a
    networked match the client never rolls: 0xCEE51 tests the network flag and
    jumps straight past the resolver, and the ten values arrive from here. The
    entropy is ours; the arithmetic is theirs.
    """
    a_stat, d_stat, a_sel, d_sel = _stats(att, dfn)
    a_raw = max(1, a_stat + att_mod)            # 0xD334D..0xD336E
    d_raw = max(1, d_stat + dfn_mod)
    a_mult = _multiplier(att_ability, 1, rnd)
    d_mult = _multiplier(dfn_ability, 2, rnd)
    a_roll = _roll(a_raw, a_mult, rnd)
    d_roll = _roll(d_raw, d_mult, rnd)
    if a_roll + d_roll == 0:
        # 0xD347D: two zero rolls are re-thrown as a coin each. It can still
        # come up 0-0, and then 0xD34D0 calls it a draw and the scene re-runs
        # the whole battle -- which is retail behaviour, not a bug.
        a_roll = rnd.randrange(0x8000) & 1
        d_roll = rnd.randrange(0x8000) & 1
    return {"a_raw": a_raw, "a_roll": a_roll, "a_sel": a_sel, "a_mult": a_mult,
            "d_raw": d_raw, "d_roll": d_roll, "d_sel": d_sel, "d_mult": d_mult}


ATTACKER, DEFENDER, DRAW = 1, 2, 8      #: 0xD34D0's three return values


def winner(res):
    """Who took the tile. Ported from **0xD34D0**, the tail at 0xD3553.

    WARNING: NOT 0xD31D0'S OWN RETURN VALUE, which is dead code -- its single caller
    (0xCEE90) discards it in the next instruction, and it has no draw case
    because its `setle` hands a tie to the defender. The scene branches on
    0xD34D0 instead (0xCFA33), and 0xD34D0 checks equality FIRST:

        0xD3569  attacker roll == defender roll -> **8, a DRAW**
        0xD357B  setle / inc                    -> 1 attacker, 2 defender

    The rest of 0xD34D0 is the countdown animation: each frame it ticks
    `rec+0x8E` down towards `rec+0x8C` and returns 0 until both have arrived.
    That half is the client's business; only the verdict is ours.

    A draw sends the client to state 0xC and then back to state 7
    (`-BattleDecide`), so it fights the SAME battle again and asks for another
    `@BattleData`. See `POL_TM_BATTLE_TIES` in tetramaster.py.
    """
    if res["a_roll"] == res["d_roll"]:
        return DRAW
    return ATTACKER if res["a_roll"] > res["d_roll"] else DEFENDER


# --------------------------------------------------------------------------
# 6. THE BOARD OBJECTS -- blocks, chance blocks, rotating blocks, special tiles
# --------------------------------------------------------------------------

#: VERIFIED: PORTED FROM TM.dll 2026-09-06, and it closes the three rules the room
#: panel has been advertising since 2026-08-16 with nothing behind them.
#:
#: WHERE THE BOARD LIVES ON THE WIRE. `@StartData` carries three fields whose
#: meaning the deal builder called unknown, and all three are this:
#:
#:     /F= xM  -> [obj+0x1F5+i], ONE OBJECT CODE PER TILE, verbatim -- the
#:                parser at 0x10360F stores the byte with no mask at all.
#:     /B=     -> [obj+0x174], how many of those codes are BLOCK-family
#:     /E=     -> [obj+0x175], how many of those codes are SPECIAL TILES
#:
#: The client's opening animation (0xC1860, and its twin at 0xC2AF0) then
#: materialises them: pick a random tile whose code is non-zero (0xD39C0 in
#: mode 1), build the object, clear the code (0xC1BCF / 0xC2E33), decrement
#: `/B=` or `/E=`, and repeat until both counters reach zero. The reveal ORDER
#: is the client's own `rand()`, but every code lands on the tile the server
#: named, so two clients converge on the same board -- which is the whole
#: reason this can be driven from here.
#:
#: WARNING: `/B=` AND `/E=` MUST MATCH WHAT `/F=` ACTUALLY CONTAINS. The animation
#: loops while a counter is non-zero and divides by the number of tiles the
#: collector found; over-count it and the collector returns 0, `idiv ecx`
#: divides by zero and the client dies. `board_counts` derives both from the
#: array so they cannot drift -- never send a rolled count.
#:
#: WHICH RULE MAKES WHICH. The generator at 0xC0F16 reads three rule bytes off
#: the match object, and they are the `@Tet=` keys we already serve:
#:
#:     st -> [obj+0xE0]  Special Tiles    gates the /E= roll     (0xC0F72)
#:     cb -> [obj+0xE1]  Chance Blocks    gates codes 6/7/8/17   (0xC1048)
#:     ca -> [obj+0xE2]  Rotating Blocks  carves blocks >> 2 off (0xC0F40)
#:
#: `ca` had never been named before this; it is Rotating Blocks by measurement
#: -- it is the flag that moves a quarter of the block count into the 9..16
#: codes, and 0xC1140 adds that quarter back so `/B=` is the TOTAL.

#: How many BLOCKS a board starts with: a 100-entry weighted table indexed by
#: `rand() % 100`, one row per board kind, **rva 0x2335B8** (stride 100).
#: 4x4: 0:1% 1:2% 2:3% 3:5% 4:9% 5:30% 6:50%
BLOCK_COUNT_WEIGHTS_16 = (
    0, 1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 4, 5,
    5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
    5, 5, 5, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
    6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
)
#: 5x5: 0:1% 1:1% 2:1% 3:3% 4:2% 5:3% 6:9% 7:15% 8:15% 9:25% 10:25%
BLOCK_COUNT_WEIGHTS_25 = (
    0, 1, 2, 3, 3, 3, 4, 4, 5, 5, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 7,
    7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 8, 8, 8, 8, 8, 8, 8,
    8, 8, 8, 8, 8, 8, 8, 8, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9,
    9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 10, 10, 10, 10, 10, 10, 10,
    10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10,
    10, 10,
)
#: How many SPECIAL TILES, same shape, **rva 0x233680**. The two tables sit
#: back to back and the second ends exactly where the neighbour table at
#: 0x233748 begins, which is what pins the stride and the row count at two.
#: 4x4: 0:3% 1:20% 2:70% 3:5% 4:2%
SPECIAL_COUNT_WEIGHTS_16 = (
    0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 4, 4,
)
#: 5x5: 0:18% 1:2% 2:3% 3:70% 4:3% 5:2% 6:2%. WARNING: The fifteen zeros in the tail
#: are SHIPPED DATA, not padding -- the row runs right up to 0x233748.
SPECIAL_COUNT_WEIGHTS_25 = (
    0, 0, 0, 1, 1, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3,
    3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 5, 5, 6,
    6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
)

BLOCK_COUNT_WEIGHTS = {16: BLOCK_COUNT_WEIGHTS_16, 25: BLOCK_COUNT_WEIGHTS_25}
SPECIAL_COUNT_WEIGHTS = {16: SPECIAL_COUNT_WEIGHTS_16,
                         25: SPECIAL_COUNT_WEIGHTS_25}

#: The object codes, and what each becomes. A code `c` is turned into pseudo
#: card id `0x8000 + c` (0xC1AC6), dispatched through the jump table at rva
#: 0xB31D4 which covers ids **0x8003..0x800B** only -- so 1 and 2 fall to the
#: default arm at 0xB30DF, which zeroes the stats AND the arrow mask.
#:
#:   1, 2       plain block     no arrows, no stats -- an inert dead tile
#:   3, 4, 5    SPECIAL TILE    ability 1 / 2 / 3   (0xB303C: `id - 2`)
#:   6, 7, 8    chance block    ability 0x10/0x20/0x30, arrows 0xFF (0xB304B)
#:   9 .. 16    rotating block  one arrow, `1 << (code - 9)`        (0xB3065)
#:   17         chance block    ability 0x40, arrows 0xFF           (0xB30BC)
#:   18         chance block    ability 0x50, arrows 0xFF           (0xB30CF)
#:
#: WARNING: 18 IS NOT ROLLED AT THE DEAL, AND THE CLIENT CANNOT PRODUCE ONE EITHER.
#: An exhaustive sweep for writers of `[obj+0x1F5+i]` finds six: the three
#: generator loops (which top out at 17), two clears, and the two message
#: parsers. So a code 18 can ONLY arrive from a server -- `object_card` handles
#: it because the client will happily RENDER one (0xC19FA), not because this
#: server ever sends one.
EMPTY = 0
PLAIN_CODES = (1, 2)
SPECIAL_CODES = (3, 4, 5)
CHANCE_CODES = (6, 7, 8, 17, 18)
ROTATING_CODES = tuple(range(9, 17))
#: Everything `/B=` counts: every code that is not empty and not a special.
BLOCK_CODES = PLAIN_CODES + CHANCE_CODES + ROTATING_CODES


#: WARNING: A FLIP IS REFUSED ABOVE THIS CARD ID, AND THAT IS A REAL GAME RULE.
#: The flip-application loop at **0xD2FF5** walks the board, takes every tile
#: whose mark (`card+0x20`) is 2, and hands it to the current player -- but
#: `0xD301B` compares the tile's CARD ID against 0x8009 first and `jae` skips
#: it. So of the things that can sit on a board:
#:
#:   * a real card (id < 250) and a PLAIN or low CHANCE block (0x8001, 0x8002,
#:     0x8006..0x8008) can be flipped;
#:   * a ROTATING block (0x8009) and the two high chance blocks (0x800A,
#:     0x800B) can be taken in BATTLE but **never flipped**.
#:
#: It only bites once a block has been captured: until then its owner is 4 and
#: the arrow engine's neutral arm never marks it 2 in the first place (see
#: `contest`). After a capture the tile is an ordinary seat-owned card to the
#: engine, and this is the one thing that still tells it apart.
FLIP_ID_CEILING = 0x8009


def flippable(card):
    """Can `card` be taken by a FLIP? 0xD301B -- see `FLIP_ID_CEILING`."""
    return int(card.row["id"]) < FLIP_ID_CEILING


def object_kind(code):
    """"plain", "special", "chance", "rotating", or None for an empty tile."""
    code = int(code)
    if code in SPECIAL_CODES:
        return "special"
    if code in CHANCE_CODES:
        return "chance"
    if code in ROTATING_CODES:
        return "rotating"
    if code in PLAIN_CODES:
        return "plain"
    return None


def special_ability(code):
    """The ability a SPECIAL TILE hands to whatever is played on it.

    WARNING: A SPECIAL TILE IS NOT AN OCCUPANT, IT IS A PROPERTY OF AN EMPTY TILE,
    and the placement path is what proves it. 0xC7015, three instructions
    before the client copies the played card into the tile record, saves
    `[tile.card + 0x24]` into `bl`; 0xB3200 then builds the played card there
    and zeroes +0x24 like every other card constructor; 0xC703C writes `bl`
    straight back. So the tile's ability SURVIVES the placement and belongs to
    the card that landed on it -- which is exactly what `resolve`'s
    `att_ability` / `dfn_ability` already consume.

    That is also why the arrow engine skips owner 8 entirely (`contest`): a
    special tile is never fought, it is played ON. Keep these OUT of the board
    dict or they will occupy a tile nobody can use.

    Ability 1 multiplies the ATTACKER's roll by `rand()%4+2`, 2 does the same
    for the DEFENDER, and 3 gives the card ALL EIGHT ARROWS -- see
    `_multiplier` and `Card.arrows`, both of which have always been able to do
    this and have never been handed a non-zero ability.
    """
    return {3: 1, 4: 2, 5: 3}.get(int(code), 0)


def object_card(code, owner=None):
    """The `Card` a BLOCK-family code puts on the board, or None.

    Returns None for empty tiles and for special tiles -- neither is an
    occupant. Every block carries `owner` 4 (`OWNER_BLOCK`), which is the byte
    the materialiser writes at 0xC1B12 and the one the arrow engine's neutral
    arm tests for.

    The stats are all zero and that is measured, not a simplification: every
    pseudo-card case except 3/4/5 jumps to 0xB315D, which zeroes rec+0x08
    (attack), +0x09 (type), +0x0A (phys def) and +0x0B (mag def). So a block
    defends with a raw stat of 1 (`resolve` clamps at 0xD334D) and is close to
    a free capture -- which is what makes fighting a chance block worth doing.
    """
    code = int(code)
    kind = object_kind(code)
    if kind is None or kind == "special":
        return None
    if kind == "chance":
        arrows = 0xFF
        ability = {6: 0x10, 7: 0x20, 8: 0x30, 17: 0x40, 18: 0x50}[code]
    elif kind == "rotating":
        # WARNING: EVERY ROTATING BLOCK IS CARD ID 0x8009, whatever its code. The
        # materialiser pushes the literal 0x8009 (0xC1A2B) and uses the code
        # ONLY for the phase; `0x8000 + code` was this module's invention and it
        # would have mattered the moment anything keyed on the id -- the client
        # compares against 0x8009 in SEVEN places.
        arrows = 1 << (code - 9)
        ability = 0
        row = [ROTATING_ID, 0, 0, 0, 0, 0, arrows, 0]
        return Card(row, OWNER_BLOCK if owner is None else owner)
    else:
        arrows = 0
        ability = 0
    # id, attack, type, pdef, mdef, power, arrows, flag -- `ROW_FIELDS`. The id
    # is the pseudo id for the log's benefit only; nothing puts it on a wire
    # (`@PutCard` refuses anything >= 250, 0x103982).
    row = [0x8000 + code, 0, 0, 0, 0, 0, arrows, 0]
    return Card(row, OWNER_BLOCK if owner is None else owner, ability=ability)


def roll_board(tiles, rnd, special=True, chance=True, rotating=True,
               chance_codes=None):
    """Roll one match's board. Ported from **0xC0F16..0xC11E5**, in order.

    Returns a list of `tiles` object codes, index = tile. Feed it to
    `board_counts` for `/B=` and `/E=`; never count them any other way.

    The client's own generator is gated on `[obj+0x171]` -- "settle this match
    locally" -- which the network scenes clear (0xBA140, 0xBA2B9), so on our
    path it never runs and the server owes the whole thing. Instruction for
    instruction:

        0xC0F16  blocks   = BLOCK_COUNT_WEIGHTS[kind][rand() % 100]
        0xC0F40  if `ca`:   rotating = blocks >> 2; blocks -= rotating
        0xC0F72  if `st`:   specials = SPECIAL_COUNT_WEIGHTS[kind][rand()%100]
        0xC1029  per block: a free tile, uniformly among those still free
        0xC1051  if `cb` and rand()%100 < 50 -> a CHANCE block:
        0xC106C     rand()%100 -> <25: 6, <50: 7, <75: 8, else 17
        0xC10A2  otherwise -> a plain block, `(rand() & 1) + 1`
        0xC1123  per rotating: `(rand() & 7) + 9`
        0xC1180  per special:  rand()%100 -> <25: 3, >=75: 5, else 4
        0xC1140  blocks += rotating   -- so `/B=` is the TOTAL block count

    The free list is rebuilt from scratch on every single placement (0xD39C0
    in mode 0, called fresh at 0xC1029 / 0xC10FF / 0xC1175), so a uniform draw
    over the tiles still free is exact, not an approximation.

    WARNING: The three flags are the rule bytes, not our preference: pass `st` / `cb`
    / `ca` through. With all three off this returns a board of plain blocks
    only, which is retail behaviour and not a degenerate case.
    """
    tiles = int(tiles)
    if tiles not in BLOCK_COUNT_WEIGHTS:
        raise ValueError("no shipped weights for a %d-tile board" % tiles)
    # Resolved here, not in the signature: section 7 is defined below this function.
    if chance_codes is None:
        chance_codes = MODELLED_CHANCE_CODES
    codes = [EMPTY] * tiles

    def _free_tile():
        """A uniform pick among the still-free tiles, or None."""
        free = [t for t in range(tiles) if codes[t] == EMPTY]
        return free[rnd.randrange(len(free))] if free else None

    n_block = BLOCK_COUNT_WEIGHTS[tiles][rnd.randrange(100)]
    n_rot = 0
    if rotating and MODEL_ROTATING:
        n_rot = n_block >> 2
        n_block -= n_rot
    n_spec = (SPECIAL_COUNT_WEIGHTS[tiles][rnd.randrange(100)]
              if special else 0)

    for _ in range(n_block):
        t = _free_tile()
        if t is None:
            break
        if chance and rnd.randrange(100) < 50:
            r = rnd.randrange(100)
            _c = 6 if r < 25 else 7 if r < 50 else 8 if r < 75 else 17
            # WARNING: A CODE WE CANNOT REPRODUCE BECOMES A PLAIN BLOCK. This is a
            # DELIBERATE DEVIATION from the client's own ladder, and it skews
            # the mix toward plain blocks -- which is the correct trade, because
            # the alternative is dealing an effect the client applies and the
            # server does not, and that hangs the match (see
            # `MODELLED_CHANCE_CODES`). Widen `chance_codes` as each effect
            # lands, and the ladder goes back to retail's on its own.
            codes[t] = _c if _c in chance_codes else (rnd.randrange(0x8000) & 1) + 1
        else:
            codes[t] = (rnd.randrange(0x8000) & 1) + 1
    for _ in range(n_rot):
        t = _free_tile()
        if t is None:
            break
        codes[t] = (rnd.randrange(0x8000) & 7) + 9
    for _ in range(n_spec):
        t = _free_tile()
        if t is None:
            break
        r = rnd.randrange(100)
        codes[t] = 3 if r < 25 else 5 if r >= 75 else 4
    return codes


def board_counts(codes):
    """`(/B=, /E=)` for a code array -- DERIVED, never rolled.

    See the section banner: a counter that outruns the array is a divide by
    zero in the client's reveal loop, so these two numbers are only ever read
    off the thing that was actually sent.
    """
    b = sum(1 for c in codes if int(c) in BLOCK_CODES)
    e = sum(1 for c in codes if int(c) in SPECIAL_CODES)
    return b, e



# --------------------------------------------------------------------------
# 7. WHAT A CHANCE BLOCK DOES -- the ability HIGH nibble, 0xD07F5
# --------------------------------------------------------------------------

#: WARNING: RETRACTS section 6's "the high nibble is read by nothing" (2026-09-07, same
#: night). It is read, once, and it is the whole mechanism:
#:
#:     0xD07EA   mov al, [ebp + tile*156 + 0x368]   ; the ability byte
#:     0xD07F5   shr eax, 4                         ; <<< THE HIGH NIBBLE
#:     0xD07F8   == 1 -> scene state 0x12
#:     0xD080B   == 2 -> 0x14
#:     0xD081E   == 3 -> 0x16
#:     0xD0889   == 4 -> 0x19 ;  anything else -> 0x1B
#:
#: The earlier sweep missed it because it searched the CARD alias
#: `[reg + 0x24]` and this is the TILE alias `[ebp + ecx*4 + 0x368]` loaded
#: into a dword before the shift. Worse, the displacement scan DID surface
#: 0xD07EB -- one byte in, decoded as `test byte ptr [ebp+0x368], cl` -- and
#: the whole 0xD0xxx cluster was written off as decode noise on the strength
#: of it. The instrument was misaligned and its output was taken as a result;
#: a misaligned instrument reads as a result.
#:
#: WHEN IT FIRES. The chain is state 0x08 (which prints the client's own
#: `-BattleSet:OK!`) -> 0x09 -> 0xCF6C7 -> state 0x11, whose arm is the
#: dispatch above. Nothing between the battle and the shift tests WHO WON, so
#: the effect belongs to the battle RESOLVING against that tile, not to the
#: attacker winning it. The ability comes from `[obj+0x17A]`, the battle
#: target.
#:
#: WARNING: AND A NORMAL CARD GOES DOWN THE SAME PATH. Nibble 0 fails every compare
#: and lands on 0x1B via the `setne` at 0xD088C -- so 0x1B is the ORDINARY
#: post-battle continuation, and nibble 5 (code 18) reaching the same state
#: means **code 18 has no effect at all**. That is a real negative, not a gap.
#:
#: VERIFIED: ALL FOUR DEALT KINDS CROSS-CHECKED AGAINST A LIVE GAME 2026-09-07T00:25Z,
#: on a board this server chose, against a tester's running commentary:
#: code 17 on tile 9 -> "scramble"; code 7 on tile 4 -> "power up for their
#: cards"; code 6 on tile 1 -> "power down"; code 8 on tile 6 -> "couldn't
#: tell what that one did", which is exactly what a silent change of owner
#: looks like. Four for four, with the RE and the screen agreeing and neither
#: fitted to the other.
CHANCE_EFFECT_STATES = {6: 0x12, 7: 0x14, 8: 0x16, 17: 0x19, 18: 0x1B}

#: code -> what the block does. None = nothing (it is the ordinary path).
CHANCE_EFFECTS = {6: "power_down", 7: "power_up", 8: "take",
                  17: "scramble", 18: None}

#: How much a power up/down moves `rec+0x26`. 0xD0ED1 `add dl, 0x0A` and
#: 0xD0B88 `add bl, 0xF6` -- +10 and -10 on a SIGNED byte.
POWER_STEP = 10

#: WARNING: THE CODES THIS SERVER MAY DEAL, and it is a SHORT LIST ON PURPOSE.
#: An effect the client applies and the server does not is not a cosmetic
#: difference -- it is a board divergence, and a board divergence presents as
#: a turn that never ends (the client marks a battle its board says exists,
#: the server never fights one its board says does not, and the client is
#: server-authoritative for battles, so it waits for ever). That is exactly
#: how 2026-09-07T00:27Z hung.
#:
#: So: 6, 7, 8 and 17 are modelled below and may be dealt -- which is the
#: client's own ladder in full (0xC106C deals 6/7/8/17 at 25% each), so the mix
#: is retail's again and no longer skewed toward plain blocks. 18 does nothing
#: and is harmless, but the client never generates one either, so it stays out.
#:
#: KEY: 17 (scramble) TOOK TWO LIVE HANGS TO GET RIGHT and the fault was the
#: same both times: the CANDIDATE LIST, not the effect. 0xD19DF passes arg2=1
#: to the collector, so the client's candidates are the tiles whose `card+0x1C`
#: is 8 -- and an unplayed SPECIAL TILE is 4 there while being owned by nobody
#: (`+0x1D` = 8), which is the one place the two bytes disagree. See the 17
#: banner below and `apply_scramble`. `POL_TM_SCRAMBLE=0` withdraws it again
#: from the deal without a code change.
#:
#: WARNING: "Modelled" is still not "measured against the client": 6 and 7 were
#: proved live by a tester naming the effect off their own screen, 8 by the
#: client printing COLORSHIFT, and 17 by neither. An effect that MOVES cards
#: cannot be signed off by a server-side selftest, so 17 is modelled AND
#: unproved until a two-human match runs a scramble and does not park.
MODELLED_CHANCE_CODES = (6, 7, 8, 17)

#: VERIFIED: 8 = TAKE -- the client calls it **COLORSHIFT**, and that name is the
#: mechanic: every adjacent card changes COLOUR, i.e. changes owner. Confirmed
#: live 2026-09-07T03:31Z, a tester reading the word off their own screen
#: while the log showed `/R= occ 1 = 84` taking the 70% branch to the attacker.
#: On that trigger the server converted NOTHING and nothing changed on screen
#: either -- tile 9's neighbours were the attacker's own card and two blocks,
#: which 0xD2A47 and 0xD2A3B both skip. Guards confirmed by a negative.
#:
#: See `take_owner` and `apply_take`. The draw at
#: 0xD0823 picks a player out of `[obj+0x1A9]` (index 1 of our own `/R=`), and
#: state 0x17 copies that player onto every card ADJACENT to the block.
#:
#: WARNING: The obvious reading -- "somebody takes the block" -- is WRONG, and the
#: thing that disproves it is the consumption: 0x16 -> 0x17 -> 0x1D restores the
#: tile to empty, so the owner the dispatch writes onto the BLOCK cannot be the
#: point. It is a courier for the neighbours.
#:
#: VERIFIED: 17 = SCRAMBLE, and it is BACK ON after being withdrawn twice. Every
#: card is lifted off the board and redealt from `/R=`; blocks stay put and
#: ownership travels with each card, so the SCORE never moves and only the
#: adjacencies do.
#:
#: WHAT WAS WRONG BOTH TIMES WAS THE CANDIDATE LIST, NOT THE MECHANICS. State
#: 0x19's arm at 0xD1990 lifts the real cards and re-places them on
#: `candidates[R[i & 7] % count]`, copying `+0x24`/`+0x26` across
#: (0xD1A2E/0xD1A37); `R` is `@TurnData`'s own `/R=` and therefore ours. But
#: `candidates` came from 0xD3A60 with **arg2 = 1** (0xD19DF `push 1`), which
#: takes the 0xD3ABC arm testing `card+0x1C` -- NOT the `card+0x1D` owner byte
#: the ray and colorshift use. A displacement sweep for every writer of
#: `+0x1C` (see `apply_scramble`) says the two bytes differ in exactly one
#: place: an **UNPLAYED SPECIAL TILE** is `+0x1D` 8 (nobody owns it) and
#: `+0x1C` 4, so the client never redeals onto one and this module used to.
#: That made our list longer than the client's, and `R % count` picked a
#: different tile: 04:20Z, `183 % 10 = 3` put card 33 on tile 7 while the
#: client put it on tile 2. With the specials removed the list is 7 long,
#: `183 % 7 = 1`, and index 1 IS tile 2 -- the arithmetic and the disassembly
#: reached the same answer independently.
#:
#: WARNING: IT STILL HAS NOT BEEN WATCHED ON A CLIENT. `POL_TM_SCRAMBLE=0` takes
#: 17 straight back out of the deal with no code change and no deploy, and
#: that is the switch to reach for if a match parks on a placement again.
#: KEY: An effect that MOVES cards cannot be signed off by a server-side
#: selftest -- 6 and 7 were proved by a tester naming the effect off their
#: own screen, 8 by the client printing COLORSHIFT, and 17 by neither.
UNMODELLED_CHANCE_CODES = ()

#: Every rotating block is this ONE card id; the code carries only the phase.
ROTATING_ID = 0x8009

#: WARNING: ROTATING BLOCKS ARE NOT DEALT, AND THIS IS THE SECOND TIME THE SAME RULE
#: HAS EARNED ITSELF: an effect the client applies and the server does not is a
#: board divergence, and a board divergence hangs the match.
#:
#: A battle whose DEFENDER is a rotating block does not take the ordinary
#: post-battle path at all. 0xCF6C7 compares the defender's card id against
#: 0x8009 and, on a match, branches to scene state **0x1E** (0xD25E2) or
#: **0x0A** (0xCF71F) depending on whether either side's ability multiplier
#: (`card+0x2A`) exceeded 1 -- never to 0x11, the state every other battle
#: reaches. This server models neither state, and in particular assumes the
#: attacker simply CAPTURES the block the way it captures a chance block.
#:
#: Live 2026-09-07T02:37Z: a placement took both rotating blocks on our board
#: (`the ATTACKER takes it`, twice) and the match ran two more turns before the
#: client stopped acking -- on a COM turn, where no player input is involved.
#: The tester's own read was "I played a card while the arrow from the
#: rotating block was facing it", which is the same story from the screen.
#:
#: VERIFIED: SOLVED 2026-09-07 -- see section 9. State 0x1E collects the battled blocks, 0x1F
#: sets the step count to 1 and 0x20 fires each block's one-tile ray, converting
#: an OPPONENT'S card on the landing tile and leaving everything else alone. The
#: block is never captured and never consumed. `apply_rotating` models it and
#: this switch is back ON; set it False to stop dealing them again.
MODEL_ROTATING = True


#: WARNING: A CHANCE BLOCK IS CONSUMED BY ITS OWN EFFECT -- the tile goes back to
#: EMPTY, it is not captured. Traced end to end 2026-09-07 and then confirmed
#: on the tester's screen, which showed all four triggered tiles blank while
#: this server still held them as cards:
#:
#:     0x12 / 0x14  the effect state
#:       -> 0x13 / 0x15   the power loop
#:       -> 0xD0F25       common tail, state 0x18 (the animation)
#:       -> 0xD18A3       when the timer passes 0x30, state 0x1D
#:       -> 0xD250E       [target.card + 0x20] = 0     the mark
#:          0xD2526       [target.card + 0x24] = 0     the ability
#:          0xD2543       0xB2CF0(&target.card, 0xFFFF, ..)
#:
#: and card id **0xFFFF** is the empty-tile constructor: 0xB314B zeroes the
#: arrows, the ability, +0x1F and all four stats.
#:
#: KEY: IT IS SPECIFIC TO THE CHANCE PATH, which is what makes it safe to model.
#: State 0x1D has exactly ONE entry in the whole image -- 0xD18A3, the end of
#: the effect animation -- so an ordinary battle never clears its target, and
#: a captured card stays captured. Nibble 5 (code 18) never gets here either:
#: it lands on 0x1B, the ordinary post-battle state.
#:
#: VERIFIED: AND "CONSUMED" MEANS "RESTORED TO A FRESH EMPTY TILE", PROVED rather than
#: inferred: the board's own init loop at 0xC0E57 does the SAME THREE THINGS in
#: the same order --
#:
#:     0xC0E67  0xB2CF0(&tile.card, 0xFFFF, 0)   the empty-tile constructor
#:     0xC0E6C  [card+0x1E] = 8
#:     0xC0E70  [card+0x1D] = 8      the OWNER
#:     0xC0E73  [card+0x1C] = 8
#:
#: against state 0x1D's 0xD2543 / 0xD255C / 0xD2573 / 0xD2588. Byte for byte the
#: same tile. So a triggered block is PLAYABLE again, scores for nobody (owner 8
#: is >= the player count, and the tally at 0xC742B skips it) and renders as
#: nothing -- which is exactly what the tester saw. Modelling it as absence
#: from the board dict is therefore exact, not an approximation.
#:
#: WARNING: ALL FOUR EFFECTS CONSUME; the list is short because of what is MODELLED,
#: not because the others survive. 6/7 reach 0x1D via 0x18; **8 also reaches it**
#: (0x16 -> 0x17 -> 0xD1849 -> 0x1D), which means its 70/30 owner-set at dispatch
#: is WIPED by the consumption and cannot be the point of the effect; 17 runs
#: 0x19 -> 0x1A -> 0x24 and builds its own 0xFFFF tiles (0xD1966, 0xD205A,
#: 0xD2215) as it moves cards. Widen this tuple only with the matching effect.
CONSUMED_CHANCE_CODES = (6, 7, 8, 17)


def chance_effect(code):
    """What a battle resolving against `code` does, or None."""
    return CHANCE_EFFECTS.get(int(code))


def apply_power(board, actor, delta, players=DEFAULT_PLAYERS):
    """Power up (+10) or down (-10) every card `actor` holds. Returns the tiles.

    The two loops are the same shape, 0xD0B58 (down) and 0xD0EB8 (up): walk
    every tile, compare `card+0x1D` against `[obj+0x178]` -- the CURRENT
    PLAYER, not the attacker and not the block's owner -- and add the step to
    `card+0x26`.

    KEY: `rec+0x26` is `Card.modifier`, which `resolve` has threaded into
    `att_mod`/`dfn_mod` since the port was written and which nothing has ever
    made non-zero. So the whole of a power up/down is one field this module
    already had; `resolve` clamps the result at 1 (0xD334D) exactly as the
    client does.

    WARNING: It hits the ACTOR'S OWN cards either way -- a power down on your own turn
    weakens your own board. That is not a misreading; it is why a tester
    saw "power down" fire on their own attack.
    """
    hit = []
    for t, card in sorted(board.items()):
        if card.owner is None or int(card.owner) != int(actor):
            continue
        if int(card.owner) >= players:      # a block is nobody's card
            continue
        # A plain byte add in the client, so it wraps; kept signed here.
        card.modifier = ((card.modifier + delta + 128) & 0xFF) - 128
        hit.append(t)
    return hit


def take_owner(r, actor, players):
    """Who a code-8 block hands its neighbours to. Ported from **0xD0823**.

    `r` is `[obj+0x1A9]` -- index **1** of the shared random table, which is
    `@TurnData`'s own `/R=` and therefore ours to generate. That is what makes
    this reproducible on both sides with no extra message.

    KEY: AND THE CLIENT DOES NOT CLOBBER IT ON OUR PATH, which is the thing that
    makes the above true rather than merely hoped. State 7 (the battle) writes
    two fresh `rand()` values straight into `[ebp+0x1A8]` and `[ebp+0x1A9]` at
    0xCEF66/0xCEF71 -- R[0] and R[1], the very byte this function reads. But
    those writes sit inside the LOCAL-ONLY branch: 0xCEE51 loads the network
    flag `[0x52464CA]`, and `jne 0xCEFDE` jumps the whole block when it is set.
    A networked match therefore keeps the table we sent. Checked 2026-09-07
    after the clobber turned up by accident; had it been on the shared path,
    every code-8 outcome would have diverged.

        r <  0xB3                     -> the current player   (about 70%)
        r >= 0xD9 and 3 players       -> (current + 2) % N
        otherwise                     -> (current + 1) % N
    """
    r = int(r) & 0xFF
    if r < 0xB3:
        return int(actor)
    if players >= 3 and r >= 0xD9:
        return (int(actor) + 2) % players
    return (int(actor) + 1) % players


def apply_take(board, tiles, target, owner, players=DEFAULT_PLAYERS):
    """Code 8: every card ADJACENT to the block becomes `owner`. State 0x17.

    WARNING: THIS IS NOT "SOMEBODY TAKES THE BLOCK", which is what the dispatch's
    owner-set at 0xD0837 looks like in isolation. That owner is wiped moments
    later when the block is consumed (`CONSUMED_CHANCE_CODES`); what it is
    actually for is to be COPIED OUT to the neighbours here.

    State 0x17 loops `esi` over the eight directions (`0xD1786 inc esi` is the
    continue), and for each:

        0xD16CC  N = neighbour[kind][target][esi]     the shipped table
        0xD1623  N off the board            -> skip
        0xD1648  N.owner >= player count    -> skip   (a block, or empty)
        0xD16C1  dl = the BLOCK's owner
        0xD16DD  N.card+0x1E = dl                     via N's own scratch
        0xD1718  N.card+0x1D = that                   -> N CHANGES HANDS
        0xD174B  N.card+0x20 = 0                      the mark
        0xD177E  N.card+0x22 = 0x80

    So a code-8 block converts everything around it to one player -- and it can
    just as easily be the opponent, including the very card that attacked it.
    That is the "chance".

    WARNING: It skips neighbours whose owner is not a seat, so an adjacent block or
    special tile is untouched, and an EMPTY neighbour has nothing to convert --
    which is why it can look like nothing happened at all.
    """
    hit = []
    for d in range(8):
        u = _NEIGHBOURS[tiles][int(target)][d]
        if u is None:
            continue
        occ = board.get(u)
        if occ is None or occ.owner is None or int(occ.owner) >= players:
            continue
        if int(occ.owner) == int(owner):
            continue                       # already theirs; nothing to see
        occ.owner = int(owner)
        hit.append(u)
    return hit


def apply_scramble(board, tiles, rand, players=DEFAULT_PLAYERS, specials=()):
    """Code 17: lift every real card off the board and redeal it. State 0x19.

    WARNING: THE ONLY EFFECT THAT MOVES CARDS. Ownership travels with each card, so
    the SCORE does not change -- but every adjacency on the board does at once,
    which is why nothing else in this module can stand in for it.

    Phase 1, 0xD1942, walks the tiles in index order:

        0xD1948  card id >= 0x8000  -> SKIP      a block; it stays put
        0xD1957  0xB2A80(staging, card)          copy the card out
        0xD195C  keep card+0x26 (the modifier) and card+0x24 (the ability)
        0xD196F  0xB2CF0(card, 0xFFFF, 0)        blank the tile
        0xD1974  [card+0x1E] = 8                 back to a fresh empty tile

    Phase 2, 0xD19D3, walks the staging array:

        0xD19D9  an empty staging slot -> skip
        0xD19DF  0xD3A60(tiles, 0)               collect the tiles whose owner
                                                 byte is 8 -- i.e. EMPTY ones
        0xD19FE  [obj+0x1A8 + (i & 7)]           the SHARED random table
        0xD1A07  % that count                    -> a candidate index
        0xD1A24  0xB2A80(tile.card, staging)     place it
        0xD1A2E  the ability and 0xD1A37 the modifier travel with it

    KEY: The tile is drawn from `R[i & 7]` -- index `i & 7` of `@TurnData`'s own
    `/R=`, which this server generates. Same source as colorshift's draw, and
    the reason the shuffle is reproducible on both sides with no new message.

    WARNING: The candidate list is RECOLLECTED for every card (0xD19DF is inside the
    loop), so a tile just filled is no longer a candidate for the next one.

    WARNING: `specials` IS THE WHOLE FIX FOR 2026-09-07T04:20Z, AND IT IS A DIFFERENT
    BYTE. 0xD19DF pushes **arg2 = 1** into the collector, so 0xD3A60 takes its
    0xD3ABC arm and tests `[tile + 0x360]` = **`card+0x1C`**; the rotating ray
    and colorshift take the 0xD3A84 arm and test `card+0x1D`, the OWNER. Every
    writer of `+0x1C` in the image, swept by displacement (`0x360 + i*0x9C`,
    and 0x9C is 0x27*4 so they all read `[reg + idx*4 + 0x360]`):

        0xC0E73   board init                 8      a fresh empty tile
        0xD20B5   the scramble's own 0x1A    8      and 0xD197C in phase 1
        0xD2588   chance consumption 0x1D    8      cl = 8 at 0xD254A
        0xC1976   a SPECIAL TILE is dealt    4      +0x1D = 8 (0xC1996)
        0xC2BCE   the same, second arm       4      +0x1D = 8 (0xC2BE6)
        0xC1AFA   a BLOCK is dealt           4      +0x1D = 4 too (0xC1B12)
        0xC2D5E   the same, second arm       4      bl = 4
        0xC7098   a card is PLAYED           the OWNER, copied from +0x1D
        0xC1DEF   a PRE-PLACED card          the owner
        0xB2AC2   the card COPY (0xB2A80)    it travels with the card

    So `+0x1C == 8` means GENUINELY EMPTY, and the one thing that separates it
    from this module's "no card on this tile" is a **SPECIAL TILE**: a special
    is owned by nobody (`+0x1D` = 8) but stamped 4 in `+0x1C`, so the client
    will never redeal onto one and `t not in board` will, every time.

    WARNING: ONLY AN *UNPLAYED* SPECIAL. Phase 1 writes all three bytes back to 8
    (0xD1974/0xD1978/0xD197C) on every tile it lifts a card off, so a special
    that has been played on IS a candidate again -- its 4 was overwritten by
    the owner at 0xC7098 the moment the card landed. `specials` must therefore
    be the tiles still carrying an unplayed special, not every tile the deal
    put one on; `tetramaster._unplayed_specials` clears the deal entry when a
    card lands, which is the same thing the client does to the tile record.

    KEY: AND IT RE-DERIVES THE LIVE SAMPLE, which is the only reason it is back
    on. 04:20Z: `R[0] = 183`, this module's free list was 10 long and picked
    `183 % 10 = 3` -> tile 7; the client used tile **2**. 183 = 3 x 61, so
    `183 % N` can only land on index 1 for N in {7, 13, 14} -- and the client's
    list is a SUBSET of ours, which leaves **N = 7**: three of our ten
    candidates were unplayed special tiles. That is exactly the board a tester was
    asked to force next ("force a board carrying 3 and 5"), one of each
    special. Mechanism and arithmetic agree and neither was fitted to the other.
    """
    blocked = {int(t) for t in (specials or ())}
    order = sorted(t for t, c in board.items()
                   if int(c.row["id"]) < 0x8000)
    staged = [board[t] for t in order]
    for t in order:
        del board[t]                       # 0xB2CF0(card, 0xFFFF) -- blanked
    moved = []
    rand = list(rand)
    for src_tile, card in zip(order, staged):
        # `card+0x1C == 8`: empty AND carrying no unplayed special (which is
        # stamped 4 at 0xC1976 and would make this list too long by one tile).
        free = [t for t in range(tiles)
                if t not in board and t not in blocked]
        if not free:
            break
        # ...and the draw is `R[SOURCE TILE & 7]` (0xD1ACD increments the
        # counter on the skip path too), not R[position in the lifted list].
        u = free[int(rand[src_tile & 7]) % len(free)]
        board[u] = card                    # owner, ability and modifier ride along
        moved.append((src_tile, u))
    return moved


def apply_chance(board, code, actor, players=DEFAULT_PLAYERS,
                 tiles=None, target=None, rand=None, specials=()):
    """Run the effect of a battle resolving against a chance block.

    Returns `(effect name, tiles touched)`, or `(None, [])` when the code has
    no effect or this server does not model it. A code it does not model must
    never have been dealt in the first place -- see `MODELLED_CHANCE_CODES`.
    """
    eff = chance_effect(code)
    if eff == "power_up":
        return eff, apply_power(board, actor, POWER_STEP, players)
    if eff == "power_down":
        return eff, apply_power(board, actor, -POWER_STEP, players)
    if eff == "scramble" and tiles is not None and rand is not None:
        # `specials` = the tiles still carrying an UNPLAYED special tile, which
        # the client's candidate collector excludes and this module used not to.
        return eff, apply_scramble(board, tiles, rand, players, specials)
    if eff == "take" and tiles is not None and target is not None             and rand is not None:
        # `rand` is the match's `/R=`; index 1 is the byte 0xD0823 reads.
        who = take_owner(list(rand)[1], actor, players)
        return eff, apply_take(board, tiles, target, who, players)
    return eff, []



# --------------------------------------------------------------------------
# 9. WHAT A ROTATING BLOCK DOES -- the ray, states 0x1E..0x22
# --------------------------------------------------------------------------

#: WARNING: A BATTLE AGAINST A ROTATING BLOCK DOES NOT CAPTURE IT. That assumption --
#: that it changes hands like any other defender -- hung a live match on
#: 2026-09-07T02:37Z, and a tester read it off the screen before the log
#: did: "I played a card while the arrow from the rotating block was facing
#: it."
#:
#: `0xCF6C7` compares the defender's card id against 0x8009 and, on a match,
#: leaves the ordinary post-battle path entirely -- to state **0x1E** when
#: neither side's ability multiplier (`card+0x2A`) exceeded 1, else **0x0A**.
#: State 0x11, which every other battle reaches, is never entered. The chain
#: from there is 0x1E -> 0x1F -> 0x20 -> 0x21/0x22 -> back to 0x1E, and
#: **nothing in it writes the block's own owner or rebuilds its tile**. The
#: block simply stays where it is.
#:
#: WHAT IT DOES INSTEAD, and every step is measured:
#:
#:   0x1E  0xD262F  walk the board, collect every tile whose card id is 0x8009
#:                  and whose mark is 3 (in battle) into the +0x1DC list
#:   0x1F  0xD277D  set the step count [obj+0x16C] to **1**
#:   0x20  0xD2971  for each collected block, walk that many steps from it in
#:                  direction `(phase - 4) & 7` through the shipped neighbour
#:                  table -- so ONE tile, the one the arrow points at
#:         0xD2A3B  landing owner >= player count -> skip (empty/block/special)
#:         0xD2A47  landing owner == the current player -> skip
#:         0xD2B22  otherwise **[landing.card+0x1D] = the current player**
#:         0xD2B97  and on EITHER SKIP, if the landing tile is itself a
#:                  rotating block, mark it 4 -- that is how the beam RELAYS
#:   0x21  0xD2F31  back to 0x1E for the next round
#:
#: KEY: THE RAY GOES THE OPPOSITE WAY TO THE ARROW BIT. The mask is `1 << phase`
#: (0xC1A7B) but both the ray (0xD299C) and the sprite angle (0xC1A9C) use
#: `(phase - 4) & 7`, which is `opposite(phase)`. So the direction the block
#: VISIBLY points is the direction it fires, and that is the opposite of the
#: bit the arrow engine tests. Getting that backwards would send every ray to
#: the wrong tile.
#:
#: WARNING: NOTHING HERE TESTS WHO WON. Like the chance dispatch, the branch is taken
#: on the battle RESOLVING, and 0xD2B0A reads `[obj+0x178]` -- the CURRENT
#: player -- so the ray converts for the attacker even on a loss. Modelled as
#: measured; if a live game contradicts it, this is the line to revisit.


def rotating_phase(card, advance=0):
    """The phase of a rotating block, recovered from its arrow mask.

    The materialiser stores the phase in `tile+0x36C` and sets the mask to
    `1 << phase`, so the mask is the only part of it this server keeps. A
    rotating block always has exactly one arrow, which makes this exact.

    WARNING: IT ROTATES. `advance` is how many steps the block has turned since the
    deal -- one per TURN, measured 2026-09-07T14:23Z:
    a code-15 block (deal phase 6) fired SE on turn 1 (empty), SW on turn 3
    (took a tester's card on tile 9 on BOTH clients; this server fired E
    and the boards diverged into a freeze) and showed W on turn 4 -- exactly
    `(6 + turn) & 7` fired at `(phase - 4) & 7`. The server had held the
    deal phase for the whole match. `tetramaster` passes `turn *
    POL_TM_ROTATING_STEP`; 0 restores the static block.
    """
    mask = int(card.row["arrows"]) & 0xFF
    if not mask or (mask & (mask - 1)):
        return None
    return ((mask.bit_length() - 1) + int(advance)) & 7


def ray_target(tiles, tile, phase):
    """The tile a rotating block at `tile` fires at. 0xD299C, one step."""
    return _NEIGHBOURS[tiles][int(tile)][(int(phase) - 4) & 7]


def is_rotating(card):
    """Is this card a rotating block? The client tests the id in seven places."""
    return card is not None and int(card.row["id"]) == ROTATING_ID


def apply_rotating(board, tiles, target, actor, players=DEFAULT_PLAYERS,
                   guard=16, advance=0):
    """Fire a battled rotating block's ray. Returns the tiles it converted.

    `target` is the block that was fought. Chains through any rotating block
    the ray lands on (0xD2B97 marks it 4 and 0x21 loops back to 0x1E), with a
    guard because a pair of blocks facing each other would otherwise ring for
    ever -- the client's own mark test stops it, and so does `seen` here.

    The block itself is deliberately left alone: no capture, no consumption.
    """
    hit, seen, queue = [], set(), [int(target)]
    while queue and len(seen) < guard:
        t = queue.pop(0)
        if t in seen:
            continue
        seen.add(t)
        blk = board.get(t)
        if not is_rotating(blk):
            continue
        phase = rotating_phase(blk, advance)
        if phase is None:
            continue
        u = ray_target(tiles, t, phase)
        if u is None:
            continue
        occ = board.get(u)
        # 0xD2A3B / 0xD2A47 -- the two skips. An empty tile, a block, a special
        # or a card already the actor's is left alone.
        skipped = (occ is None or occ.owner is None
                   or int(occ.owner) >= players
                   or int(occ.owner) == int(actor))
        if skipped:
            # KEY: AND THE CHAIN IS ON THE SKIP PATH, not the capture one. Both
            # skips `jmp 0xD2B7C`, which is where the id is tested and 0xD2B97
            # writes mark 4. So a ray that lands on a NEUTRAL rotating block
            # sets that one firing too -- an untouched block relays the beam,
            # a captured card ends it.
            if is_rotating(occ):
                queue.append(u)
            continue
        occ.owner = int(actor)             # 0xD2B22
        hit.append(u)
    return hit


# --------------------------------------------------------------------------
# 10. SELFTEST
# --------------------------------------------------------------------------

def selftest(say=print):
    """Every assertion here has been checked to FAIL when its rule is broken."""
    import random
    ok = True

    # 1. THE GEOMETRY, against the client's own shipped table.
    derived = _NEIGHBOURS[16]
    for t in range(16):
        got = tuple(16 if v is None else v for v in derived[t])
        if got != _NEIGHBOURS_16_MEASURED[t]:
            say("FAIL: tile %d neighbours %r do not match TM.dll's own table at "
                "rva 0x233748 %r -- the direction order or the row-major tile "
                "numbering is wrong"
                % (t, got, _NEIGHBOURS_16_MEASURED[t]))
            ok = False
    # ...and the relation the battle rule rests on: if t sees u in direction d,
    # u sees t in direction (d+4)&7. A one-sided table makes every battle a
    # flip, silently.
    for tiles in _NEIGHBOURS:
        for t in range(tiles):
            for d, u in enumerate(_NEIGHBOURS[tiles][t]):
                if u is not None and _NEIGHBOURS[tiles][u][opposite(d)] != t:
                    say("FAIL: %d-tile board: %d -> %d is direction %s but the "
                        "reverse is not %s"
                        % (tiles, t, u, DIR_NAMES[d], DIR_NAMES[opposite(d)]))
                    ok = False

    # 2. THE ARROW DISTRIBUTION -- 100 entries, shape measured off rva
    #    0x2334A0. A table that has drifted deals every card wrong.
    if len(ARROW_COUNT_WEIGHTS) != 100:
        say("FAIL: the arrow-count table is rand()%%100 into 100 entries, got %d"
            % len(ARROW_COUNT_WEIGHTS))
        ok = False
    if ARROW_COUNT_WEIGHTS.count(3) != 31 or ARROW_COUNT_WEIGHTS.count(4) != 27:
        say("FAIL: the arrow-count table must weight 3 arrows at 31%% and 4 at "
            "27%% (rva 0x2334A0)")
        ok = False
    rnd = random.Random(1)
    counts = [0] * 9
    for _ in range(4000):
        m = draw_arrows(rnd)
        if not 0 <= m <= 0xFF:
            say("FAIL: an arrow mask is one byte, got 0x%X" % m)
            ok = False
        counts[bin(m).count("1")] += 1
    if counts[3] < counts[2] or counts[3] < counts[5]:
        say("FAIL: 3 arrows is the modal draw -- got %r" % (counts,))
        ok = False

    # Picking WITHOUT replacement is the point: n draws must set n bits.
    class _Fixed(object):
        def __init__(self, n):
            self.n = n

        def randrange(self, bound):
            return self.n if bound == 100 else 0

    for want in (0, 1, 2, 3, 4, 5, 6, 7, 8):
        idx = ARROW_COUNT_WEIGHTS.index(want)
        got = bin(draw_arrows(_Fixed(idx))).count("1")
        if got != want:
            say("FAIL: draw_arrows must set exactly as many bits as the table "
                "asks for (%d), got %d -- picking WITH replacement loses arrows"
                % (want, got))
            ok = False

    # 3. THE ARROW ENGINE. Two cards side by side on a 4x4 board: tile 5 places
    #    with an EAST arrow (bit 2) against tile 6.
    east, west, north = 1 << 2, 1 << 6, 1 << 0

    def _c(arrows, owner):
        return Card([1, 10, 0, 10, 10, 0, arrows, 0], owner)

    board = {5: _c(east, 0), 6: _c(west, 1)}
    if contest(board, 16, 5, 0) != {6: BATTLE}:
        say("FAIL: a card pointing east at one pointing west is a BATTLE")
        ok = False
    board = {5: _c(east, 0), 6: _c(north, 1)}
    if contest(board, 16, 5, 0) != {6: FLIP}:
        say("FAIL: a card pointing east at one NOT pointing back is a free "
            "FLIP, not a battle")
        ok = False
    board = {5: _c(east, 0), 6: _c(west, 0)}
    if contest(board, 16, 5, 0) != {}:
        say("FAIL: your own card is never contested (0xD3820)")
        ok = False
    board = {5: _c(north, 0), 6: _c(west, 1)}
    if contest(board, 16, 5, 0) != {}:
        say("FAIL: no arrow in a direction means nothing happens that way")
        ok = False
    # The edge: tile 3 is the top-right corner, so north and east are off-board
    # and an all-arrows card there reaches exactly 2, 6 and 7.
    board = {3: _c(0xFF, 0), 7: _c(0xFF, 1), 2: _c(0xFF, 1), 6: _c(0xFF, 1)}
    got = contest(board, 16, 3, 0)
    if got != {7: BATTLE, 2: BATTLE, 6: BATTLE}:
        say("FAIL: a corner tile must contest exactly its on-board neighbours "
            "-- got %r" % (got,))
        ok = False
    if next_defender({2: BATTLE, 7: BATTLE, 6: FLIP}) != 7:
        say("FAIL: -BattleCheck auto-selects the LAST flagged tile by index "
            "(0xD394E)")
        ok = False
    if next_defender({6: FLIP}) is not None:
        say("FAIL: a placement with no battle must report none")
        ok = False

    # 4. THE ROLL. The shape is what is measured, so the assertions are on the
    #    shape: never above the raw stat, never below zero, and the raw stat is
    #    the SELECTED stat.
    p_att = unpack([1, 40, 0, 10, 10, 0, 0xFF, 0])       # type 0 (P)
    m_att = unpack([2, 40, 1, 10, 10, 0, 0xFF, 0])       # type 1 (M)
    x_att = unpack([3, 40, 2, 10, 10, 0, 0xFF, 0])       # type 2 (X)
    a_att = unpack([4, 40, 3, 90, 10, 0, 0xFF, 0])       # type 3 (A)
    dfn = unpack([9, 70, 0, 30, 50, 0, 0xFF, 0])
    if _stats(p_att, dfn)[:2] != (40, 30):
        say("FAIL: type P is attack vs PHYSICAL defence")
        ok = False
    if _stats(m_att, dfn)[:2] != (40, 50):
        say("FAIL: type M is attack vs MAGIC defence")
        ok = False
    if _stats(x_att, dfn) != (40, 30, SEL_ATTACK, SEL_PDEF):
        say("FAIL: type X takes the defender's LOWER defence, and the selector "
            "must say which one it was")
        ok = False
    if _stats(a_att, dfn) != (90, 30, SEL_PDEF, SEL_PDEF):
        say("FAIL: type A is the attacker's best stat against the defender's "
            "worst -- got %r" % (_stats(a_att, dfn),))
        ok = False
    # The tie rule: equal defences take the MAGIC one (0xD326B is `setae`).
    even = unpack([9, 1, 0, 20, 20, 0, 0, 0])
    if _stats(x_att, even)[3] != SEL_MDEF:
        say("FAIL: type X with equal defences must select the magic one "
            "(`setae` at 0xD326B)")
        ok = False
    rnd = random.Random(7)
    for _ in range(2000):
        r = resolve(p_att, dfn, rnd)
        if r["a_mult"] != 1 or r["d_mult"] != 1:
            say("FAIL: a card with no ability has multiplier 1")
            ok = False
            break
        if not 0 <= r["a_roll"] <= r["a_raw"] or not 0 <= r["d_roll"] <= r["d_raw"]:
            say("FAIL: a roll is bounded by its own stat -- got %r" % (r,))
            ok = False
            break
        if r["a_raw"] != 40 or r["d_raw"] != 30:
            say("FAIL: the raw stat is the selected stat, unmodified")
            ok = False
            break
    # A 1-stat card can still roll 0, and 0-0 is a real draw the client re-runs.
    weak = unpack([9, 1, 0, 1, 1, 0, 0, 0])
    seen = set()
    for _ in range(4000):
        seen.add(winner(resolve(weak, weak, rnd)))
    if seen != {ATTACKER, DEFENDER, DRAW}:
        say("FAIL: two 1-stat cards must reach all three outcomes including a "
            "DRAW (0xD3569) -- got %r" % (sorted(seen),))
        ok = False
    # And the ability arithmetic, which is dead in retail but must still be the
    # measured shape the moment a special tile exists.
    got = {resolve(p_att, dfn, rnd, att_ability=1)["a_mult"] for _ in range(400)}
    if got != {2, 3, 4, 5}:
        say("FAIL: ability 1 on the attacker is rand()%%4+2, so 2..5 -- got %r"
            % (sorted(got),))
        ok = False
    if resolve(p_att, dfn, rnd, att_mod=-100)["a_raw"] != 1:
        say("FAIL: rec+0x8E is clamped to >= 1 (0xD3353)")
        ok = False

    # 5. THE WINNER, which is 0xD34D0's rule and not 0xD31D0's.
    if winner({"a_roll": 5, "d_roll": 4}) != ATTACKER:
        say("FAIL: the higher roll takes the tile")
        ok = False
    if winner({"a_roll": 4, "d_roll": 5}) != DEFENDER:
        say("FAIL: the lower roll loses the tile")
        ok = False
    if winner({"a_roll": 4, "d_roll": 4}) != DRAW:
        say("FAIL: EQUAL ROLLS ARE A DRAW (0xD3569), not a defender win -- "
            "0xD31D0's own `setle` return is dead code")
        ok = False

    # 6. SLOT 5 IS DERIVED. A nominal card carries CardPrm byte 4 unchanged,
    #    which is why serving it straight out of the table has been right.
    if power_byte(31, 100, 90, 99, 100, 90, 99) != 31:
        say("FAIL: a card with nominal stats carries its baseline power byte")
        ok = False
    if power_byte(31, 200, 180, 198, 100, 90, 99) != 62:
        say("FAIL: the power byte scales with the instance's stat total "
            "(0xB37FB)")
        ok = False

    # 7. THE BOARD OBJECTS. The weight tables are shipped data, so the test is
    #    that we transcribed them, not that we like the numbers.
    import random as _random
    if sum(1 for v in BLOCK_COUNT_WEIGHTS_16 if v == 6) != 50:
        say("FAIL: rva 0x2335B8 row 0 gives a 4x4 board six blocks half the "
            "time -- 6 blocks + 2 players x 5 cards is exactly 16 tiles, "
            "which is what makes the transcription checkable")
        ok = False
    if sum(1 for v in BLOCK_COUNT_WEIGHTS_25 if v == 10) != 25:
        say("FAIL: rva 0x23361C -- 10 blocks + 3 players x 5 cards fills a "
            "5x5 board, and it is the joint most likely count")
        ok = False
    for _n, _t in ((16, BLOCK_COUNT_WEIGHTS_16), (25, BLOCK_COUNT_WEIGHTS_25),
                   (16, SPECIAL_COUNT_WEIGHTS_16),
                   (25, SPECIAL_COUNT_WEIGHTS_25)):
        if len(_t) != 100:
            say("FAIL: every count table is indexed by rand()%%100 and must be "
                "100 entries -- got %d" % (len(_t),))
            ok = False

    # The three rules, each switched on alone, must produce ONLY its own codes.
    _rnd = _random.Random(20260906)
    for _flags, _allowed, _name in (
            ((False, False, False), set(PLAIN_CODES), "no rules"),
            ((True, False, False), set(PLAIN_CODES) | set(SPECIAL_CODES),
             "Special Tiles alone"),
            ((False, True, False), set(PLAIN_CODES) | set(CHANCE_CODES),
             "Chance Blocks alone"),
            ((False, False, True), set(PLAIN_CODES) | set(ROTATING_CODES),
             "Rotating Blocks alone")):
        _seen = set()
        for _ in range(300):
            _seen |= {c for c in roll_board(16, _rnd, *_flags) if c}
        if not _seen <= _allowed:
            say("FAIL: with %s a rolled board may only contain %r -- got %r"
                % (_name, sorted(_allowed), sorted(_seen)))
            ok = False
    # ...and 18 is never dealt, only reached by the client's own mutation path.
    _seen = set()
    for _ in range(400):
        _seen |= {c for c in roll_board(25, _rnd) if c}
    if 18 in _seen:
        say("FAIL: code 18 is only reachable through 0xC19FA, never the deal")
        ok = False

    # THE COUNTS MUST MATCH THE ARRAY, or the client's reveal loop divides by
    # zero. This is the one assertion that stands between us and a crash.
    for _ in range(500):
        _codes = roll_board(_rnd.choice((16, 25)), _rnd)
        _b, _e = board_counts(_codes)
        if _b + _e != sum(1 for c in _codes if c):
            say("FAIL: /B= + /E= must account for EVERY non-empty tile -- "
                "%r gives (%d, %d)" % (_codes, _b, _e))
            ok = False
            break
        if _b > len(_codes) or _e > len(_codes):
            say("FAIL: a count may never exceed the board")
            ok = False
            break
    # VERIFIED: ROTATING BLOCKS ARE DEALT AGAIN, now that section 9 models the ray. This
    # assertion has flipped twice and both flips were real: it
    # required them, then forbade them when the battle branch turned out to be
    # unmodelled and hung a live match, and now requires them again.
    _rot_dealt = set()
    for _ in range(400):
        _rot_dealt |= {c for c in roll_board(16, _rnd, special=False,
                                             chance=False, rotating=True)
                       if c in ROTATING_CODES}
    if not _rot_dealt:
        say("FAIL: with MODEL_ROTATING on, the quarter carve-out must produce "
            "rotating blocks (0xC1140 adds the quarter back into /B=)")
        ok = False
    if not _rot_dealt <= set(ROTATING_CODES):
        say("FAIL: only 9..16 are rotating codes -- got %r" % (sorted(_rot_dealt),))
        ok = False
    # Every rotating code is the SAME card id; only the phase differs.
    if {int(object_card(c).row["id"]) for c in ROTATING_CODES} != {ROTATING_ID}:
        say("FAIL: 0xC1A2B pushes the literal 0x8009 for every rotating block "
            "-- the code supplies the PHASE, not the id")
        ok = False
    if [rotating_phase(object_card(c)) for c in (9, 11, 16)] != [0, 2, 7]:
        say("FAIL: the phase is recoverable from the one-bit arrow mask")
        ok = False

    # KEY: THE RAY FIRES OPPOSITE THE ARROW BIT. Code 9 is phase 0 (mask bit N),
    # so it fires SOUTH -- the way the sprite points. Tile 5's south is 9.
    if ray_target(16, 5, 0) != 9 or ray_target(16, 5, 2) != 4:
        say("FAIL: the ray is `(phase - 4) & 7`, one step -- phase 0 fires "
            "SOUTH and phase 2 (E) fires WEST; got %r / %r"
            % (ray_target(16, 5, 0), ray_target(16, 5, 2)))
        ok = False
    # Tile 1 is on the top row: phase 4 fires NORTH, off the board; phase 0
    # fires SOUTH into tile 5.
    if ray_target(16, 1, 4) is not None or ray_target(16, 1, 0) != 5:
        say("FAIL: a ray off the board lands nowhere -- got %r / %r"
            % (ray_target(16, 1, 4), ray_target(16, 1, 0)))
        ok = False

    # THE CONVERSION and its two guards (0xD2A3B / 0xD2A47).
    _rb = object_card(9)                       # phase 0 -> fires SOUTH
    _rboard = {5: _rb, 9: _c(0xFF, 1)}         # tile 9 is the OPPONENT's
    if apply_rotating(_rboard, 16, 5, 0, players=2) != [9] \
            or _rboard[9].owner != 0:
        say("FAIL: a battled rotating block converts the OPPONENT'S card on "
            "the tile its arrow points at (0xD2B22)")
        ok = False
    # ...and the block itself is neither captured nor consumed.
    if 5 not in _rboard or _rboard[5].owner != OWNER_BLOCK:
        say("FAIL: nothing in states 0x1E..0x22 writes the block's own owner "
            "or rebuilds its tile -- it stays exactly where it is")
        ok = False
    # The actor's own card, an empty tile, and a neutral special are all skipped.
    for _fill, _why in ((_c(0xFF, 0), "the actor's own card"),
                        (None, "an empty tile"),
                        (Card([9, 1, 0, 1, 1, 0, 0xFF, 0], OWNER_SPECIAL),
                         "a special tile")):
        _b2 = {5: object_card(9)}
        if _fill is not None:
            _b2[9] = _fill
        if apply_rotating(_b2, 16, 5, 0, players=2):
            say("FAIL: the ray must skip %s (0xD2A3B / 0xD2A47)" % _why)
            ok = False
            break
    # KEY: AND IT RELAYS THROUGH AN UNTOUCHED BLOCK. Tile 5 fires south into the
    # neutral block on 9, which is skipped -- and that one fires south into 13.
    _b3 = {5: object_card(9), 9: object_card(9), 13: _c(0xFF, 1)}
    if apply_rotating(_b3, 16, 5, 0, players=2) != [13] \
            or _b3[13].owner != 0:
        say("FAIL: a ray landing on a NEUTRAL rotating block marks it 4 "
            "(0xD2B97) and it fires too -- the beam relays")
        ok = False
    # ...and two blocks facing each other must terminate rather than ring.
    _b4 = {5: object_card(9), 9: object_card(13)}     # 9 fires N, back at 5
    apply_rotating(_b4, 16, 5, 0, players=2)

    # 8. WHAT AN OBJECT IS ON THE BOARD.
    if object_card(3) is not None or object_card(0) is not None:
        say("FAIL: a special tile and an empty tile are NOT occupants -- "
            "0xC7015/0xC703C make a special tile a property of a free tile")
        ok = False
    if [special_ability(c) for c in (3, 4, 5)] != [1, 2, 3]:
        say("FAIL: 0xB303C makes the special tile's ability `id - 2`")
        ok = False
    if special_ability(6) != 0:
        say("FAIL: only 3/4/5 carry a special-tile ability")
        ok = False
    _plain, _chance, _rot9 = object_card(1), object_card(6), object_card(9)
    if _plain.arrows != 0:
        say("FAIL: a plain block falls to 0xB30DF, which zeroes rec+0x0D -- "
            "no arrows at all")
        ok = False
    if _chance.arrows != 0xFF or _chance.ability != 0x10:
        say("FAIL: 0xB304B gives a chance block all eight arrows and an "
            "ability in the HIGH nibble -- got 0x%02X / 0x%02X"
            % (_chance.arrows, _chance.ability))
        ok = False
    if _rot9.arrows != 0x01 or object_card(16).arrows != 0x80:
        say("FAIL: 0xB3065 gives a rotating block exactly one arrow, "
            "`1 << (code - 9)`")
        ok = False
    for _code in BLOCK_CODES:
        _card = object_card(_code)
        if _card.owner != OWNER_BLOCK:
            say("FAIL: every block carries owner 4 (0xC1B12) -- code %d has %r"
                % (_code, _card.owner))
            ok = False
            break
        if any(_card.row[_f] for _f in ("attack", "type", "pdef", "mdef")):
            say("FAIL: 0xB315D zeroes a pseudo card's four stats -- code %d "
                "came out %r" % (_code, _card.row))
            ok = False
            break

    # 9. THE NEUTRAL ARM, 0xD386B -- the thing that makes a block a block.
    #    Tile 5 places with an EAST arrow (bit 2) against tile 6.
    _east, _north = 1 << 2, 1 << 0
    _me = _c(_east, 0)
    if contest({5: _me, 6: object_card(1)}, 16, 5, 0, players=2) != {}:
        say("FAIL: a PLAIN block has no arrows, so 0xD387E skips it -- it is "
            "inert, not a free flip")
        ok = False
    if contest({5: _me, 6: object_card(6)}, 16, 5, 0, players=2) != {6: BATTLE}:
        say("FAIL: a CHANCE block is battled -- the neutral arm never tests "
            "the reverse arrow and never writes FLIP")
        ok = False
    # A rotating block pointing AWAY is still a battle: no reverse-arrow test.
    if contest({5: _me, 6: object_card(11)}, 16, 5, 0,
               players=2) != {6: BATTLE}:
        say("FAIL: 0xD386B has no reverse-arrow test, so a rotating block "
            "fights whichever way it points")
        ok = False
    # ...and a card played ON a special tile never sees one as a neighbour,
    # because owner 8 fails 0xD386D outright.
    _spec = Card([0x8003, 0, 0, 0, 0, 0, 0xFF, 0], OWNER_SPECIAL, ability=1)
    if contest({5: _me, 6: _spec}, 16, 5, 0, players=2) != {}:
        say("FAIL: owner 8 is not 4, so 0xD386D drops it -- a special tile is "
            "never contested")
        ok = False
    # The seat path is unchanged: a real neighbour still flips or fights.
    if contest({5: _me, 6: _c(_north, 1)}, 16, 5, 0, players=2) != {6: FLIP}:
        say("FAIL: adding `players` must not change the seat path")
        ok = False

    # 10. AND A SPECIAL TILE ACTUALLY DOES SOMETHING. Ability 1 is the only
    #     input that changes `resolve`'s arithmetic, and until now nothing on
    #     a board could carry one.
    _dfn = unpack([9, 30, 0, 30, 30, 0, 0xFF, 0])
    _att = unpack([9, 40, 0, 10, 10, 0, 0xFF, 0])
    if {resolve(_att, _dfn, rnd, att_ability=special_ability(3))["a_mult"]
            for _ in range(400)} != {2, 3, 4, 5}:
        say("FAIL: a card played onto special tile 3 rolls with a 2..5 "
            "attacker multiplier")
        ok = False
    if Card([9, 1, 0, 1, 1, 0, 0x01, 0], 0,
            ability=special_ability(5)).arrows != 0xFF:
        say("FAIL: special tile 5 is ability 3, which is ALL EIGHT ARROWS "
            "(0xD378D) whatever the card's own mask")
        ok = False

    # 11. THE FLIP CEILING, 0xD301B -- and it only bites after a capture.
    if not flippable(Card([249, 1, 0, 1, 1, 0, 0xFF, 0], 0)):
        say("FAIL: a real card id is far below 0x8009 and must flip")
        ok = False
    for _code in (1, 2, 6, 7, 8):
        if not flippable(object_card(_code)):
            say("FAIL: id 0x%04X is below 0x8009 and the flip loop takes it"
                % (0x8000 + _code,))
            ok = False
    for _code in (9, 16, 17, 18):
        if flippable(object_card(_code)):
            say("FAIL: id 0x%04X is at or above 0x8009 -- 0xD301B's `jae` "
                "skips it, so it can be taken in BATTLE but never FLIPPED"
                % (0x8000 + _code,))
            ok = False
    # ...and while a block is still neutral the question never arises: the
    # neutral arm writes only 1, never 2.
    _blk = object_card(6)
    if FLIP in contest({5: _c(1 << 2, 0), 6: _blk}, 16, 5, 0, players=2).values():
        say("FAIL: 0xD386B never writes FLIP -- an uncaptured block cannot be "
            "flipped at all")
        ok = False
    # Once captured it is an ordinary seat-owned card to the engine, which is
    # exactly when the ceiling starts doing work.
    _blk.owner = 1
    if contest({5: _c(1 << 2, 0), 6: _blk}, 16, 5, 0, players=2) != {6: BATTLE}:
        say("FAIL: a CAPTURED block goes down the seat path (its arrows point "
            "back, so it fights)")
        ok = False

    # 12. THE CHANCE-BLOCK EFFECT, 0xD07F5 -- the high nibble after all.
    if [chance_effect(c) for c in (6, 7, 8, 17, 18)] != \
            ["power_down", "power_up", "take", "scramble", None]:
        say("FAIL: the ability HIGH nibble selects the effect (0xD07F5) -- "
            "1/2/3/4 are states 0x12/0x14/0x16/0x19 and 5 lands on 0x1B, the "
            "ORDINARY post-battle state, so code 18 does nothing")
        ok = False
    # WARNING: THE DEAL MUST NOT SHIP AN EFFECT THE SERVER CANNOT REPRODUCE. An
    # unmodelled effect is a board divergence, and a board divergence hangs the
    # match -- measured live 2026-09-07T00:27Z.
    _rnd2 = _random.Random(4242)
    _seen2 = set()
    for _ in range(600):
        _seen2 |= {c for c in roll_board(16, _rnd2) if c}
        _seen2 |= {c for c in roll_board(25, _rnd2) if c}
    _bad = _seen2 & set(UNMODELLED_CHANCE_CODES)
    if _bad:
        say("FAIL: roll_board dealt %r, whose effect this server does not "
            "model -- the client would apply it and we would not, and the "
            "match hangs on the next battle" % (sorted(_bad),))
        ok = False
    if not (_seen2 & set(MODELLED_CHANCE_CODES)):
        say("FAIL: 1200 boards produced no modelled chance block at all")
        ok = False
    # ...but the ladder still REACHES them when they are allowed, so widening
    # `chance_codes` is all it takes once an effect lands.
    _seen3 = set()
    for _ in range(600):
        _seen3 |= {c for c in roll_board(16, _rnd2,
                                         chance_codes=(6, 7, 8, 17)) if c}
    if not {8, 17} <= _seen3:
        say("FAIL: the client's own 25/25/25/25 ladder must still be able to "
            "produce 8 and 17 -- got %r" % (sorted(_seen3),))
        ok = False

    # POWER UP / POWER DOWN: every card the ACTOR holds moves by 10, and
    # nothing else on the board does.
    _pboard = {0: _c(0xFF, 0), 1: _c(0xFF, 0), 2: _c(0xFF, 1),
               3: object_card(7)}
    _eff, _hit = apply_chance(_pboard, 7, 0, players=2)
    if _eff != "power_up" or _hit != [0, 1]:
        say("FAIL: a power up touches exactly the actor's own tiles -- got "
            "%r / %r" % (_eff, _hit))
        ok = False
    if [_pboard[t].modifier for t in (0, 1, 2, 3)] != [10, 10, 0, 0]:
        say("FAIL: 0xD0ED1 adds +10 to rec+0x26 on the actor's cards ONLY -- "
            "not the opponent's, and not the block itself; got %r"
            % ([_pboard[t].modifier for t in (0, 1, 2, 3)],))
        ok = False
    apply_chance(_pboard, 6, 0, players=2)          # 0xD0B88, -10
    if [_pboard[t].modifier for t in (0, 1)] != [0, 0]:
        say("FAIL: a power down is -10 and must undo the +10 exactly")
        ok = False
    apply_chance(_pboard, 6, 0, players=2)
    if _pboard[0].modifier != -10:
        say("FAIL: rec+0x26 is SIGNED -- a power down past zero goes negative")
        ok = False
    # ...and the modifier is the thing `resolve` already consumed. A -10 on a
    # 5-attack card must clamp the roll's raw stat at 1 (0xD334D), not wrap.
    _weak = unpack([9, 5, 0, 5, 5, 0, 0xFF, 0])
    if resolve(_weak, _weak, rnd, att_mod=-10)["a_raw"] != 1:
        say("FAIL: a powered-down card clamps at 1, which is the whole reason "
            "`resolve` has always taken a modifier")
        ok = False
    # A code with no effect, and one we refuse to deal, both change nothing.
    _pboard2 = {0: _c(0xFF, 0)}
    for _code in (18, 8, 17):
        apply_chance(_pboard2, _code, 0, players=2)
        if _pboard2[0].modifier != 0:
            say("FAIL: code %d must not move a modifier here -- 18 has no "
                "effect and 8/17 are not modelled" % _code)
            ok = False
            break

    # 13. CODE 8 = TAKE. The draw (0xD0823) and the neighbour sweep (0x17).
    if take_owner(0xB2, 0, 2) != 0 or take_owner(0x00, 1, 2) != 1:
        say("FAIL: r < 0xB3 sends the neighbours to the CURRENT player")
        ok = False
    if take_owner(0xB3, 0, 2) != 1 or take_owner(0xFF, 1, 2) != 0:
        say("FAIL: r >= 0xB3 sends them to the NEXT player, wrapping")
        ok = False
    # The +2 arm is THREE-PLAYER ONLY (0xD084F `cmp cl, 3` / `jb`).
    if take_owner(0xD9, 0, 3) != 2 or take_owner(0xD8, 0, 3) != 1:
        say("FAIL: at 3 players, r >= 0xD9 skips a seat -- got %r / %r"
            % (take_owner(0xD9, 0, 3), take_owner(0xD8, 0, 3)))
        ok = False
    if take_owner(0xD9, 0, 2) != 1:
        say("FAIL: the +2 arm must not fire in a two-player game -- 0xD084F "
            "requires a player count of at least 3")
        ok = False
    # ...and roughly 70% of the byte range keeps them with the attacker.
    if sum(1 for r in range(256) if take_owner(r, 0, 2) == 0) != 0xB3:
        say("FAIL: exactly 0xB3 of the 256 byte values keep the neighbours")
        ok = False

    # THE SWEEP. Tile 5's eight neighbours on a 4x4 are 1,2,6,10,9,8,4,0.
    _tboard = {5: object_card(8),            # the block itself
               1: _c(0xFF, 0), 2: _c(0xFF, 1), 6: _c(0xFF, 1),
               9: object_card(1),            # a plain block -- owner 4
               4: Card([9, 1, 0, 1, 1, 0, 0xFF, 0], OWNER_SPECIAL)}
    _eff, _hit = apply_chance(_tboard, 8, 0, players=2, tiles=16, target=5,
                              rand=[0, 0x00] + [0] * 6)        # r=0 -> actor 0
    if _eff != "take" or _hit != [2, 6]:
        say("FAIL: a code-8 block converts every ADJACENT SEAT-OWNED card and "
            "nothing else -- tile 1 is already the actor's, 9 is a block and 4 "
            "is a special; got %r / %r" % (_eff, _hit))
        ok = False
    if [_tboard[t].owner for t in (1, 2, 6)] != [0, 0, 0]:
        say("FAIL: the converted neighbours must all belong to the drawn "
            "player -- got %r" % ([_tboard[t].owner for t in (1, 2, 6)],))
        ok = False
    if _tboard[9].owner != OWNER_BLOCK or _tboard[4].owner != OWNER_SPECIAL:
        say("FAIL: 0xD1648 skips a neighbour whose owner is not a seat, so an "
            "adjacent block or special tile is untouched")
        ok = False
    # WARNING: AND IT CAN GO TO THE OPPONENT, including the card that just attacked.
    _tboard2 = {5: object_card(8), 6: _c(0xFF, 0), 2: _c(0xFF, 0)}
    _eff2, _hit2 = apply_chance(_tboard2, 8, 0, players=2, tiles=16, target=5,
                                rand=[0, 0xFF] + [0] * 6)      # r=0xFF -> seat 1
    if sorted(_hit2) != [2, 6] or [_tboard2[t].owner for t in (2, 6)] != [1, 1]:
        say("FAIL: r >= 0xB3 hands the neighbours to the OTHER player -- that "
            "is the 'chance', and it can cost the attacker its own cards")
        ok = False
    # Without the context it must do NOTHING rather than half an effect.
    _tboard3 = {5: object_card(8), 6: _c(0xFF, 1)}
    if apply_chance(_tboard3, 8, 0, players=2)[1] or _tboard3[6].owner != 1:
        say("FAIL: apply_chance without tiles/target/rand must change nothing")
        ok = False
    # ...and 8 is now dealt, and consumed like the others.
    if 8 not in MODELLED_CHANCE_CODES or 8 not in CONSUMED_CHANCE_CODES:
        say("FAIL: code 8 is modelled now, and 0x16 -> 0x17 -> 0x1D consumes it")
        ok = False
    # 14. CODE 17 = SCRAMBLE. Every real card is lifted and redealt; blocks
    #     stay put. This assertion has flipped three times and every flip was a
    #     live match; it is ON again because the CANDIDATE LIST is now the
    #     client's own (`card+0x1C`, 0xD3ABC) rather than "tiles we hold no
    #     card on", and the fix re-derives the 04:20Z divergence.
    if 17 in UNMODELLED_CHANCE_CODES or 17 not in MODELLED_CHANCE_CODES:
        say("FAIL: 17 is modelled -- 0xD19DF passes arg2=1 to 0xD3A60, whose "
            "0xD3ABC arm tests card+0x1C, and `apply_scramble` now takes the "
            "unplayed special tiles out of the candidate list because 0xC1976 "
            "stamps one 4 there while leaving its owner byte 8")
        ok = False
    _sb = {0: _c(0xFF, 0), 3: _c(0x0F, 1), 5: object_card(1), 9: object_card(9)}
    _sb[0].modifier, _sb[0].ability = -10, 2
    _sr = [3, 1, 4, 1, 5, 9, 2, 6]
    _before = {t: (c.owner, c.row["id"]) for t, c in _sb.items()}
    _moved = apply_scramble(_sb, 16, _sr, players=2)
    # The two BLOCKS must not have moved at all (id >= 0x8000, 0xD194E).
    if 5 not in _sb or 9 not in _sb or _sb[5].owner != OWNER_BLOCK:
        say("FAIL: a scramble skips every card whose id is >= 0x8000, so the "
            "blocks stay exactly where they are")
        ok = False
    # Both real cards moved, and nothing was lost or duplicated.
    if sorted(t for t, c in _sb.items() if int(c.row["id"]) < 0x8000) == [0, 3]:
        say("FAIL: a scramble that leaves every card where it was is not one")
        ok = False
    if len([c for c in _sb.values() if int(c.row["id"]) < 0x8000]) != 2:
        say("FAIL: a scramble must not lose or duplicate a card -- two in, "
            "two out; got %r" % (sorted(_sb),))
        ok = False
    # KEY: OWNER, ABILITY AND MODIFIER TRAVEL WITH THE CARD (0xD1A2E / 0xD1A37),
    #    so the SCORE is unchanged -- only the adjacencies move.
    _after = {(c.owner, c.row["id"]) for c in _sb.values()}
    if _after != set(_before.values()):
        say("FAIL: a scramble moves cards, it does not change them -- %r vs %r"
            % (sorted(_after), sorted(set(_before.values()))))
        ok = False
    _kept = next(c for c in _sb.values() if int(c.row["id"]) < 0x8000
                 and c.owner == 0)
    if _kept.modifier != -10 or _kept.ability != 2:
        say("FAIL: the ability and the modifier ride along (0xD1A2E/0xD1A37) "
            "-- got %r / %r" % (_kept.modifier, _kept.ability))
        ok = False
    # ...and it is DETERMINISTIC from the shared table, which is what lets both
    # sides compute the same shuffle with no extra message.
    def _fresh():
        b = {0: _c(0xFF, 0), 3: _c(0x0F, 1), 5: object_card(1)}
        return b
    _b1, _b2 = _fresh(), _fresh()
    if apply_scramble(_b1, 16, _sr, players=2) !=             apply_scramble(_b2, 16, _sr, players=2):
        say("FAIL: the same /R= must produce the same shuffle on both sides")
        ok = False
    if apply_scramble(_fresh(), 16, _sr, players=2) ==             apply_scramble(_fresh(), 16, [7, 7, 7, 7, 7, 7, 7, 7], players=2):
        say("FAIL: a different /R= must produce a different shuffle")
        ok = False
    # 14a. WARNING: AN UNPLAYED SPECIAL TILE IS NOT A CANDIDATE (0xD3ABC vs
    #      0xD3A84). It is the ONE tile this module holds no card on that the
    #      client's collector still refuses, and it is what hung 04:20Z.
    _spb = {0: _c(0xFF, 0)}
    _got = apply_scramble(_spb, 16, [1] * 8, players=2, specials=[1])
    if 1 in _spb or _got != [(0, 2)]:
        say("FAIL: with tile 1 an unplayed special, the candidates are "
            "[0,2,3,...] and R=1 must land on tile 2, not tile 1 -- got %r"
            % (_got,))
        ok = False
    # ...and without the argument this module counts the client's way only when
    # there is nothing to exclude, which is why the old code passed every test.
    _spb2 = {0: _c(0xFF, 0)}
    if apply_scramble(_spb2, 16, [1] * 8, players=2) != [(0, 1)]:
        say("FAIL: with no specials the list is every empty tile and R=1 "
            "lands on tile 1")
        ok = False
    # 14c. WARNING: THE DRAW IS INDEXED BY THE SOURCE TILE, NOT BY POSITION IN
    #      THE LIFTED LIST. Phase 1 stages BY TILE (a block's slot gets id
    #      0xFFFF rather than being compacted out) and phase 2's `inc ebx` at
    #      0xD1ACD is on the loop tail the skip jumps to, so the counter runs
    #      over every slot. One card lifted off tile 7 draws R[7].
    _ib = {7: _c(0xFF, 0)}
    _ir = [0, 0, 0, 0, 0, 0, 0, 5]          # R[7] = 5, every other index 0
    _igot = apply_scramble(_ib, 16, _ir, players=2)
    # 16 tiles, one lifted -> every tile is free; R[7] % 16 = 5.
    if _igot != [(7, 5)]:
        say("FAIL: a card lifted off tile 7 draws R[7 & 7], not R[0] "
            "(0xD1ACD counts the skipped slots) -- got %r" % (_igot,))
        ok = False
    # 14b. THE LIVE SAMPLE, 2026-09-07T04:20Z, replayed off prod's REAL forced
    #      board `0|6|0|17|7|4|9|0|0|8|0|0|0|0|14|0` -- blocks on 1/3/4/6/9/14
    #      and ONE special, tile 5. Exactly one real card was on it (33, tile
    #      7), so the OLD model's free list was ten long and `183 % 10 = 3`
    #      picked tile 7 -- the `(7, 7)` the log printed. That reproduction is
    #      what makes the reconstruction trustworthy; it is NOT a check of the
    #      new rule, because `R[7]` was never logged (see `apply_scramble`).
    _live_codes = [0, 6, 0, 17, 7, 4, 9, 0, 0, 8, 0, 0, 0, 0, 14, 0]
    _lb0 = {t: object_card(c) for t, c in enumerate(_live_codes)
            if object_card(c) is not None}
    _lb0[7] = _c(0xFF, 0)
    _lb0[7].row["id"] = 33
    _old_free = [t for t in range(16) if t not in _lb0 or t == 7]
    if _old_free != [0, 2, 5, 7, 8, 10, 11, 12, 13, 15]             or _old_free[183 % len(_old_free)] != 7:
        say("FAIL: the 04:20Z board must reproduce the WRONG answer the log "
            "printed (183 %% 10 = 3 -> tile 7) -- got %r" % (_old_free,))
        ok = False
    # ...and under the new rule tile 5 is struck out, so the client's list was
    # nine long and its landing on tile 2 means R[7] = 1 (mod 9).
    _new_cands = [t for t in _old_free if t != 5]
    if len(_new_cands) != 9 or _new_cands[1] != 2:
        say("FAIL: with the unplayed special on tile 5 removed the list is "
            "nine long with tile 2 at index 1 -- got %r" % (_new_cands,))
        ok = False
    _lmoved = apply_scramble(_lb0, 16, [0] * 7 + [1], players=2, specials=[5])
    if _lmoved != [(7, 2)]:
        say("FAIL: R[7] = 1 over that nine-tile list is the client's own "
            "answer, tile 2 -- got %r" % (_lmoved,))
        ok = False
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
