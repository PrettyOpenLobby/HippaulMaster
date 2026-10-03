"""How a VS. COM opponent picks its move: uniform random on the low rungs, a
scored search that gets sharper as the opponent's deck rung rises.

Retail's own COM logic lives in the client (the vs-COM path that 0xCEE51
skips online), and nobody has read it yet, so this is ours. The one promise
it keeps from the old picker is that a move is always LEGAL: a card still in
the COM's hand, onto a tile that is empty right now. It never looks at the
human's hand, only at the board both players can see.

THE DIAL is `skill_of(char_index)`, 0.0 .. 1.0, taken from the opponent's
average rank in the client's `PlPrm.BIN` (the deck rung when that table is
missing; see `_strength_frac`). Below `POL_TM_COM_AI_FLOOR` (default 0.25 of
the way from the weakest opponent to the strongest) the skill is 0 and the
move is the old uniform pick, the same draws as before. Above it:

  * every legal move is played out on a COPY of the board (`_outcomes`): the
    placement's battle chain as a probability tree, each battle's odds exact
    from the client's roll (`win_chance`), combos through
    `boardrules._combo_capture`, free flips at the end;
  * each outcome is scored (`_evaluate`): tiles owned, then -- weighted up
    with skill -- the COM's cards left open to a free flip, the human's cards
    left open to the COM, and a small cost for spending a strong card on a
    move a weak one would have made;
  * the pick is the best expected score, blurred by noise and an occasional
    outright random move, both of which fade to nothing at skill 1.

Knobs: `POL_TM_COM_AI=0` restores the uniform pick for every opponent;
`POL_TM_COM_SKILL=<0..1>` forces one skill on every opponent (an A/B, or a
server that wants them all hard).
"""
import functools
import os
import random

import tmbattle
from . import boardrules, common


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _strength_frac(char_index):
    """Where one opponent sits between the weakest (0.0) and the strongest
    (1.0), or None.

    The client's own measure first: `PlPrm.BIN` +0x04, the opponent's average
    rank (`vscom._com_avg_rank`, lower is stronger), scaled between the
    table's weakest and strongest rows. The deck rung is only the fallback,
    because nine of the thirty opponents deal from a special deck OFF the
    25..44 ladder -- the two strongest in the list among them (average rank
    111 and 100) -- and a rung-only dial left them playing at random.
    """
    from . import vscom
    try:
        i = int(char_index)
    except (TypeError, ValueError):
        return None
    table = vscom._plprm()
    if 0 < i < len(table):
        ranks = [r for r, _deck in table[1:]]
        weakest, strongest = max(ranks), min(ranks)
        if weakest > strongest:
            return (weakest - table[i][0]) / float(weakest - strongest)
    ladder = vscom._com_deck_ladder()
    rec = vscom._com_deck_record(i)
    if len(ladder) < 2 or rec not in ladder:
        return None
    return ladder.index(rec) / float(len(ladder) - 1)


def skill_of(char_index):
    """0.0 (uniform random) .. 1.0 (best move every time) for one COM
    opponent. Below `POL_TM_COM_AI_FLOOR` of the way up (default 0.25) it is
    0; from there it climbs from 0.1 to 1.0 at the strongest opponent."""
    if not common._env_int("POL_TM_COM_AI", 1):
        return 0.0
    if os.environ.get("POL_TM_COM_SKILL", ""):
        return min(1.0, max(0.0, _env_float("POL_TM_COM_SKILL", 0.0)))
    frac = _strength_frac(char_index)
    floor = min(0.99, max(0.0, _env_float("POL_TM_COM_AI_FLOOR", 0.25)))
    if frac is None or frac < floor:
        return 0.0
    return min(1.0, 0.1 + 0.9 * (frac - floor) / (1.0 - floor))


# --------------------------------------------------------------------------
# THE ODDS -- the client's roll, as a distribution
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=2048)
def _roll_dist(n_raw):
    """P(roll == k), k = 0..n_raw, for `tmbattle._roll` with raw*mult ==
    n_raw: the floor of the mean of two uniform draws over 0..n_raw. (The
    client takes each draw `% (n_raw + 1)` out of 0..0x7FFF; the bias that
    leaves is under 1% and ignored here.)"""
    n_raw = max(0, int(n_raw))
    counts = [0] * (n_raw + 1)
    for s in range(2 * n_raw + 1):
        counts[s // 2] += min(s, 2 * n_raw - s) + 1
    total = float((n_raw + 1) ** 2)
    return tuple(c / total for c in counts)


def _mix(raw, ability, want):
    """The raw*multiplier values a side can roll against, equally likely:
    `tmbattle._multiplier` gives 2..5 to the matching ability, 1 otherwise."""
    return tuple(raw * m for m in (2, 3, 4, 5)) if (ability & 0xF) == want else (raw,)


@functools.lru_cache(maxsize=8192)
def _p_beats(a_values, d_values):
    """(P(attacker roll > defender roll), P(<)) over the two mixtures."""
    win = lose = 0.0
    w = 1.0 / (len(a_values) * len(d_values))
    for av in a_values:
        pa = _roll_dist(av)
        for dv in d_values:
            pd = _roll_dist(dv)
            cum, below = 0.0, []
            for p in pd:
                below.append(cum)
                cum += p
            # below[k] = P(D < k); P(D > k) = 1 - below[k] - pd[k]
            for k, p in enumerate(pa):
                if k < len(pd):
                    win += w * p * below[k]
                    lose += w * p * (1.0 - below[k] - pd[k])
                else:
                    win += w * p
    return win, lose


def win_chance(att, dfn):
    """The chance the card `att` beats `dfn` (both `tmbattle.Card`s) in one
    battle, draws re-rolled the way `_apply_placement` re-rolls them."""
    a_stat, d_stat, _as, _ds = tmbattle._stats(att.row, dfn.row)
    a_raw = max(1, a_stat + att.modifier)
    d_raw = max(1, d_stat + dfn.modifier)
    win, lose = _p_beats(_mix(a_raw, att.ability, 1), _mix(d_raw, dfn.ability, 2))
    return win / (win + lose) if win + lose > 0 else 0.5


# --------------------------------------------------------------------------
# PLAYING A MOVE OUT, ON A COPY
# --------------------------------------------------------------------------

def _clone(board):
    out = {}
    for t, c in board.items():
        k = tmbattle.Card(c.row, c.owner, ability=c.ability, modifier=c.modifier)
        k.placer = c.placer
        out[t] = k
    return out


def _outcomes(board, tiles, n, tile, actor, p=1.0, fought=frozenset(), depth=0):
    """[(probability, board after)] for the placement already on `board` at
    `tile`, played the way `placement._apply_placement` plays it: battles one
    at a time in `next_defender` order, a win re-contests from the placed tile,
    a loss hands the placed card over and ends it, free flips only once no
    battle is left. `board` is consumed.

    Simplified where the real resolver reaches for the match's state: a won
    battle against a BLOCK takes nothing and the block is not fought again,
    and chance-block effects (power, take, scramble) are not modelled -- the
    search treats them as an ordinary block."""
    marks = tmbattle.contest(board, tiles, tile, actor, players=n)
    for t in fought:
        marks.pop(t, None)
    target = tmbattle.next_defender(marks)
    if target is None or depth > 16:
        for t, kind in marks.items():
            if kind == tmbattle.FLIP and tmbattle.flippable(board[t]):
                board[t].owner = actor
        return [(p, board)]
    att, dfn = board[tile], board[target]
    q = win_chance(att, dfn)
    out = []
    if q > 0:
        won = _clone(board)
        if dfn.owner >= n:
            out += _outcomes(won, tiles, n, tile, actor, p * q,
                             fought | {target}, depth + 1)
        else:
            won[target].owner = actor
            boardrules._combo_capture(won, tiles, n, target, dfn.owner, actor)
            out += _outcomes(won, tiles, n, tile, actor, p * q, fought, depth + 1)
    if q < 1:
        board[tile].owner = dfn.owner
        if dfn.owner < n:
            boardrules._combo_capture(board, tiles, n, tile, actor, dfn.owner)
        out.append((p * (1 - q), board))
    return out


def _strength(row):
    """A card's rough worth for hand management: its best stat."""
    r = row if isinstance(row, dict) else tmbattle.unpack(row)
    return max(r["attack"], r["pdef"], r["mdef"])


#: How much one neighbouring card is worth to whoever drops a card next to it:
#: a free flip needs the one arrow pointing at it, which a random card carries
#: about 45% of the time (3.6 arrows of 8, `tmbattle.ARROW_COUNT_WEIGHTS`); a
#: card that points back has to be beaten in a battle as well.
_OPEN_FREE, _OPEN_GUARDED = 0.45, 0.2


def _evaluate(board, tiles, n, actor, w_exposure, w_reach):
    """One outcome's score for `actor`: tiles held over the opponents'
    average, minus the most the next opponent could take with ONE card on
    one empty tile, plus the most the actor could take back the same way on
    its own next turn. A player only places one card, so it is the best
    single empty tile that matters, not the sum of every open side."""
    mine, theirs = 0, 0
    for c in board.values():
        if c.owner is None or c.owner >= n:
            continue
        if c.owner == actor:
            mine += 1
        else:
            theirs += 1
    threat = reach = 0.0
    if w_exposure or w_reach:
        for u in range(tiles):
            if u in board:
                continue
            t_u = r_u = 0.0
            for d, v in enumerate(tmbattle.neighbours(tiles, u)):
                c = board.get(v) if v is not None else None
                if c is None or c.owner is None or c.owner >= n:
                    continue
                # Does the card on v point back at u (direction d reversed)?
                w = _OPEN_GUARDED if (c.arrows >> tmbattle.opposite(d)) & 1 \
                    else _OPEN_FREE
                if c.owner == actor:
                    t_u += w
                else:
                    r_u += w
            threat, reach = max(threat, t_u), max(reach, r_u)
    others = theirs / float(max(1, n - 1))
    return (mine - others) - w_exposure * threat + w_reach * reach


def score_moves(board, tiles, n, actor, hand, empty, skill, specials=None):
    """[(expected score, hand index, tile)] for every legal move."""
    w_exposure = 0.5 * min(1.0, skill * 1.5)
    w_reach = 0.25 * skill
    w_keep = 0.15 * skill
    strengths = [_strength(r) for r in hand]
    top = float(max(strengths) or 1)
    out, seen = [], {}
    for hi, row in enumerate(hand):
        for tile in empty:
            ability = (specials or {}).get(tile, 0)
            memo = (bytes(row) if isinstance(row, (bytes, bytearray)) else str(row),
                    tile)
            if memo in seen:
                out.append((seen[memo], hi, tile))
                continue
            b = _clone(board)
            b[tile] = tmbattle.Card(row, actor, ability=ability)
            ev = sum(p * _evaluate(after, tiles, n, actor, w_exposure, w_reach)
                     for p, after in _outcomes(b, tiles, n, tile, actor))
            ev -= w_keep * strengths[hi] / top
            seen[memo] = ev
            out.append((ev, hi, tile))
    return out


#: The two things that make a mid-ladder COM beatable, both fading to zero at
#: skill 1: the chance of an outright random move (LOOSE x (1 - skill)^1.5)
#: and the blur on each move's score (NOISE x (1 - skill), in tiles).
LOOSE, NOISE = 0.7, 2.5


def choose_move(chan, index, n, actor, hand, empty, char_index, rnd=None, skill=None):
    """(hand index, tile) for the COM seat `actor`. Always legal: an index
    into `hand`, a tile out of `empty`. `skill` overrides `skill_of`."""
    rnd = rnd or random.Random()
    skill = skill_of(char_index) if skill is None else skill
    if skill <= 0 or len(hand) * len(empty) <= 1:
        return rnd.randrange(len(hand)), rnd.choice(empty)
    if rnd.random() < LOOSE * (1.0 - skill) ** 1.5:
        common._say("tm:   ...COM (skill %.2f) plays loose this turn -- a random move"
                    % skill)
        return rnd.randrange(len(hand)), rnd.choice(empty)
    board = boardrules._MATCH_BOARD.get((chan, index)) or {}
    tiles = boardrules._board_tiles(n)
    specials = {t: boardrules._tile_ability(chan, index, t) for t in empty}
    scored = score_moves(board, tiles, n, actor, hand, empty, skill,
                         specials={t: a for t, a in specials.items() if a})
    sigma = NOISE * (1.0 - skill)
    best = max(scored, key=lambda s: (s[0] + rnd.gauss(0, sigma) if sigma else s[0],
                                      rnd.random()))
    common._say("tm:   ...COM (skill %.2f) weighed %d moves; picked hand slot %d onto "
                "tile %d (score %.2f, best on the board %.2f)"
                % (skill, len(scored), best[1], best[2], best[0],
                   max(s[0] for s in scored)))
    return best[1], best[2]


# --------------------------------------------------------------------------
# SELFTEST
# --------------------------------------------------------------------------

def _row(card_id, attack, pdef, mdef, arrows, kind=0):
    return b"%d|%d|%d|%d|%d|0|%d|0" % (card_id, attack, kind, pdef, mdef, arrows)


def selftest(say=print):
    ok = True
    saved = {k: os.environ.get(k) for k in ("POL_TM_COM_SKILL", "POL_TM_COM_AI")}
    key = ("comai-selftest", 0)
    try:
        # The odds: a card always beats one it out-rolls at every value.
        hi, lo = tmbattle.Card(_row(1, 200, 200, 200, 0), 1), tmbattle.Card(_row(2, 1, 1, 1, 0), 0)
        if win_chance(hi, lo) < 0.95 or win_chance(lo, hi) > 0.05:
            say("FAIL: comai.win_chance %.3f / %.3f" % (win_chance(hi, lo), win_chance(lo, hi)))
            ok = False
        even = tmbattle.Card(_row(3, 50, 50, 50, 0), 0)
        if abs(win_chance(even, tmbattle.Card(_row(4, 50, 50, 50, 0), 1)) - 0.5) > 1e-9:
            say("FAIL: comai.win_chance of two equal cards is not 0.5")
            ok = False

        # A free flip: the human's card on tile 5 has no arrows; a COM card
        # with an E arrow (bit 2) on tile 4 takes it without a fight.
        boardrules._MATCH_BOARD[key] = {5: tmbattle.Card(_row(10, 30, 30, 30, 0), 0)}
        boardrules._MATCH_OBJECTS.pop(key, None)
        hand = [_row(20, 30, 30, 30, 0), _row(21, 30, 30, 30, 1 << 2)]
        empty = [t for t in range(16) if t != 5]
        os.environ["POL_TM_COM_SKILL"] = "1"
        picks = [choose_move(key[0], key[1], 2, 1, hand, empty, 1, random.Random(s))
                 for s in range(20)]
        # Tile 4 is the only square whose E arrow reaches tile 5.
        if not all(p == (1, 4) for p in picks):
            say("FAIL: comai skill 1 passed up a free flip: %r" % picks[:5])
            ok = False
        # ...and its twin: skill 0 must NOT find it every time, or the check
        # above proves nothing about the search.
        os.environ["POL_TM_COM_SKILL"] = "0"
        took = sum(1 for s in range(200)
                   if choose_move(key[0], key[1], 2, 1, hand, empty, 1,
                                  random.Random(s)) == (1, 4))
        if took > 40:
            say("FAIL: comai skill 0 found the flip %d/200 times -- it is not random" % took)
            ok = False

        # A hopeless battle is avoided: the human's card points back
        # everywhere (0xFF) and is huge; attacking it loses the COM's card.
        boardrules._MATCH_BOARD[key] = {5: tmbattle.Card(_row(11, 250, 250, 250, 0xFF), 0)}
        hand = [_row(22, 5, 5, 5, 0xFF)]
        os.environ["POL_TM_COM_SKILL"] = "1"
        ring = {0, 1, 2, 4, 6, 8, 9, 10}
        bad = [s for s in range(20)
               if choose_move(key[0], key[1], 2, 1, hand, empty, 1, random.Random(s))[1]
               in ring]
        if bad:
            say("FAIL: comai skill 1 attacked an unbeatable card (%d/20)" % len(bad))
            ok = False

        # Legal, always: random boards, every skill.
        r = random.Random(7)
        for trial in range(60):
            os.environ["POL_TM_COM_SKILL"] = "%.2f" % (trial % 6 / 5.0)
            board = {}
            for t in r.sample(range(16), r.randrange(0, 12)):
                board[t] = tmbattle.Card(_row(r.randrange(1, 99), r.randrange(1, 200),
                                              r.randrange(1, 200), r.randrange(1, 200),
                                              r.randrange(256), r.randrange(4)),
                                         r.randrange(2))
            boardrules._MATCH_BOARD[key] = board
            empty = [t for t in range(16) if t not in board]
            hand = [_row(r.randrange(1, 99), r.randrange(1, 200), r.randrange(1, 200),
                         r.randrange(1, 200), r.randrange(256), r.randrange(4))
                    for _ in range(r.randrange(1, 6))]
            hi_, t_ = choose_move(key[0], key[1], 2, 1, hand, empty, 1, r)
            if not (0 <= hi_ < len(hand) and t_ in empty):
                say("FAIL: comai picked an illegal move (%d, %d)" % (hi_, t_))
                ok = False
                break

        # The dial: the weakest opponent plays at random, the strongest at
        # full skill, and the skill never falls as the average rank improves
        # (only checkable with the client's PlPrm.BIN built in).
        os.environ.pop("POL_TM_COM_SKILL", None)
        from . import vscom
        table = vscom._plprm()
        if len(table) > 2:
            by_rank = sorted(range(1, len(table)), key=lambda i: -table[i][0])
            skills = [skill_of(i) for i in by_rank]
            if skills[0] != 0.0 or skills[-1] != 1.0 or any(
                    b < a for a, b in zip(skills, skills[1:])):
                say("FAIL: comai skill must climb 0 -> 1 with average rank, got %r"
                    % ["%.2f" % v for v in skills])
                ok = False
        os.environ["POL_TM_COM_AI"] = "0"
        if skill_of(99) != 0.0:
            say("FAIL: POL_TM_COM_AI=0 must make every COM random")
            ok = False
    finally:
        boardrules._MATCH_BOARD.pop(key, None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if ok:
        say("ok: comai -- odds, free flip found (and not by chance), hopeless "
            "battle avoided, every pick legal")
    return ok
