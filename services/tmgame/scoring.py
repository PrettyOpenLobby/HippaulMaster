"""Scoring a finished board: combo and mission counters, tile counts, perfect wins, takes and lucky
cards.
"""
import tm_cardprm
import tmbattle
from .deps import tmprize
from . import boardrules, collection, common, matchend, pushqueue, webwatch


#: (chan, index) -> {actor: the largest chain that seat has taken this match}.
#: Cleared with the rest of the match state; see `_remember_match`.
_MATCH_COMBO = {}


#: TOURNAMENT MISSION COUNTERS for the match in progress: (chan, index) ->
#: {seat: {"combos", "rot", "chance"}}. Filled by the battle code below,
#: read once by _event_score_game, cleared when an event deal starts.
_EVENT_COUNTS = {}


def _event_count(chan, index, seat, key, n=1):
    try:
        d = _EVENT_COUNTS.setdefault((chan, index), {}).setdefault(int(seat), {})
        d[key] = int(d.get(key) or 0) + int(n)
    except Exception:
        pass


def _note_combo(chan, index, actor, board, before, placed):
    """Record the size of the chain one placement just took.

    WARNING: **THIS IS AN INFERENCE, AND IT IS WHY THE WRITE IS OFF BY DEFAULT.**
    An earlier reading identified the save field (`+0x7C` -> struct `+0xAC`, a career MAX kept
    at `0xD029B` over a per-seat byte at `48*seat + struct+0x19C`), and
    `0xD003E` shows what that byte MEANS in outline -- the client saves it,
    calls the battle routine `0xD3590`, re-reads it, and fires only when it
    GREW, which is the shape of "did this action take anything". What was never
    read is the exact unit: whether the client counts battles won, cards
    flipped, or both, and whether the placed card itself is in the total.

    So this counts **cards whose owner became `actor`, excluding the placed
    card** -- the most defensible reading -- and `POL_TM_MOST_COMBOS` gates the
    save write, defaulting to **0**. The tally accumulates either way, so the
    day the unit is measured the history is already there and only the gate
    moves. Putting a plausible wrong number on the Status screen is the failure
    this project keeps paying for; keeping an unused correct-shaped tally costs
    nothing.
    """
    # every placement path ends here, with the owners from before it: the web
    # watch file's one step per move
    webwatch._watch_move(chan, index, actor, board, before, placed)
    try:
        taken = sum(1 for t, c in board.items()
                    if t != int(placed) and t in before
                    and before[t] != actor and c.owner == actor)
        if taken <= 0:
            return
        if taken >= 2:
            # A COMBO for the tournament mission "Exceed N combos!": one
            # placement that takes two or more cards is a chain. The client's
            # own unit is unmeasured (see above); this is the reading.
            _event_count(chan, index, actor, "combos")
        seat = _MATCH_COMBO.setdefault((chan, index), {})
        if taken > int(seat.get(actor) or 0):
            seat[actor] = taken
    except Exception:
        pass                     # a stat must never cost a placement


def _board_map_log(board, tiles, placed, row, battles):
    """One line per placement: the full owner map, for diffing against the
    client's board when verdicts disagree.

    2026-08-22 18:36Z: the server called a 6|4-looking endgame 5|5 (draw,
    stake returned, no take flow -> the client parked on (0x43,35)) while the
    client showed a LOSS -- its `-BattleCheck` found ZERO battles on the
    final placement where we found one, so the boards had already diverged
    on some earlier flip. A per-placement owner map on both sides is the
    instrument that names the first divergent placement and therefore the
    rule that is wrong. POL_TM_BOARD_LOG=0 silences it.
    """
    if not common._env_int("POL_TM_BOARD_LOG", 1):
        return
    common._say("tm:   ...board map after tile %s (card %s, arrows %s%s): %s"
         % (placed, row.split(b"|")[0].decode(),
            row.split(b"|")[6].decode() if row.count(b"|") >= 6 else "?",
            (", %d battle(s)" % len(battles)) if battles else "",
            " ".join("%d:%s" % (t, board[t].owner)
                     for t in sorted(board))))


def _board_scores(chan, index, n):
    """Tiles held per absolute seat. 0xD23E5's own count, and THE SCORE.

    The client tallies exactly this at the end of a match: walk every tile,
    read its owner from rec+0x1D, and increment `[0x2B6580 + 48*owner]`. With
    a board model the server can finally answer "who won" without a new
    message -- which is what the battle plan asked for.

    WARNING: It does NOT yet decide what a win PAYS. `@ResultData`'s per-player
    sections now carry this count as the per-game score (`_bump_result_stats`
    accumulates it -- the sections became MANDATORY when `/P=0` turned out to
    be the result-screen divide-by-zero, see `_resultdata_body`), but the
    transfer-or-faucet question the take flow raised is still open; a tile count is
    not a prize.
    """
    board = boardrules._MATCH_BOARD.get((chan, index)) or {}
    out = [0] * (n or 2)
    for card in board.values():
        if card.owner is not None and 0 <= card.owner < len(out):
            out[card.owner] += 1
    return out


def _is_perfect_win(scores, win):
    """True when seat `win` owns EVERY owned card on the finished board.

    The client's result pass (0xCC58D) compares each seat's score with the
    board total from 0xC73DC and, on equality, sets the seat flag +0x1A2 = 3
    and [obj+0x189] = 3 -- a PERFECT. The total is the count of cards a seat
    owns (unowned blocks/specials carry owner 4/8 and are in no seat's count,
    exactly as `_board_scores` skips them), so the test is "the winner's count
    is the sum of every seat's count, and is not 0". In two players that means
    the loser holds no tile at all.
    """
    if not scores or not 0 <= win < len(scores):
        return False
    return scores[win] > 0 and scores[win] == sum(scores)


def _perfect_take(take):
    """Commit a PERFECT win's take: the winner gets EVERY loser pool card.

    On a perfect the clients skip the take-select scene: the winner's client
    moves all of each loser's pool cards to itself (slot order 0..4) and sends
    no `@GetSelect=`, and the loser's client animates the same without polling
    (0x43, 15). So the server commits the whole transfer here, once, and marks
    the take complete (`picked` covers every loser, `taken` set) so the
    @Ready= / @GetSelect= arms see nothing left to wait for.

    Same side effects the per-pick paths use: `_collection_add_cards` for a
    member winner (a COM winner owns no collection), `_collection_remove_card`
    for a member loser under `POL_TM_TAKE_LOSS` (a COM pool leaves nothing).
    `POL_TM_TAKE_WIN=0` / `POL_TM_TAKE_COM=0` keep their per-pick meaning:
    no transfer for a human / COM winner respectively.
    """
    if take.get("taken"):
        return
    win = take.get("win")
    pools = take.get("pools") or []
    ros = take.get("roster") or []
    wmid = ros[win] if win is not None and win < len(ros) else None
    take["perfect"] = True
    take["taken"] = True
    picked = take.setdefault("picked", [])
    losers = matchend._losers_from(win, pools)
    if wmid is not None:
        doit = common._env_int("POL_TM_TAKE_WIN", 1)
    else:
        doit = common._env_int("POL_TM_TAKE_COM", 1)
    for seat in losers:
        pool = pools[seat]
        cards = [[int(v) for v in r.split(b"|")] for r in pool]
        picked.extend(c[0] for c in cards)
        if not doit:
            continue
        lmid = ros[seat] if seat < len(ros) else None
        if wmid is not None:
            collection._collection_add_cards(wmid, cards)
        if lmid is not None and (wmid is None
                                 or pushqueue._push_key(lmid) != pushqueue._push_key(wmid)) \
                and common._env_int("POL_TM_TAKE_LOSS", 1):
            for r, c in zip(pool, cards):
                collection._collection_remove_card(lmid, c[0], row=r)
        common._say("tm: VERIFIED: PERFECT: %s takes ALL %d card(s) of seat %d's pool%s: %s"
             % (("member %s" % wmid) if wmid is not None
                else ("the COM (seat %d)" % win),
                len(cards), seat,
                (" = member %s" % lmid) if lmid is not None else " (COM)",
                " ".join(str(c[0]) for c in cards)))
    if not doit:
        common._say("tm:   ...PERFECT take not transferred (POL_TM_TAKE_%s=0)"
             % ("WIN" if wmid is not None else "COM"))


def _board_takes(chan, index, n):
    """Per seat, the card ids that seat TOOK -- owns now, did not play.

    `Card.placer` is set once when the card is put down and never moves;
    `Card.owner` moves every time a battle is lost. So `owner != placer` is
    precisely the client's own idea of a card changing hands, and reading it
    off the finished board needs no new message.

    WARNING: This is NOT a transfer. Nothing here puts a card in anybody's collection
    -- what a win PAYS is still the open question the take flow raised, and a tile is
    not a card until that is answered. It is a TALLY, and the only thing that
    consumes it is the lucky-card counter, which counts takes by the client's
    own statistic ("Lucky Cards Won This Week", Playdat.BIN 83).
    """
    board = boardrules._MATCH_BOARD.get((chan, index)) or {}
    out = [[] for _ in range(n or 2)]
    for card in board.values():
        if card.owner is None or not 0 <= card.owner < len(out):
            continue
        if card.placer == tmbattle.OWNER_BLOCK:
            # A CAPTURED BLOCK IS A TILE, NOT A CARD. Its `placer` is 4 and
            # its `owner` is now a seat, so the `owner != placer` test below
            # would read it as a take and feed a pseudo id (0x8000+) into the
            # lucky-card counter.
            continue
        if card.owner != card.placer:
            out[card.owner].append(card.row["id"])
    return out


def _tally_lucky(chan, index, seats, n=None):
    """Credit every seat's takes against this week's lucky cards.

    Called once, at the end of the match, from the same place `@ResultData` is
    queued -- which is the only moment the board is final and still in memory
    (`_MATCH_BOARD` is popped when the table breaks up). `n` is the real
    player count when `seats` is the VS. COM roster (one human on an N-seat
    board).
    """
    if tmprize is None or not common._env_int("POL_TM_LUCKY", 1):
        return
    takes = _board_takes(chan, index, n or len(seats) or 2)
    lucky = tmprize.pick_lucky()
    common._say("tm:   ...this week's lucky cards: %s"
         % ", ".join("#%d %s" % (c, tm_cardprm.name(c)) for c in lucky))
    for seat, (mid, _v) in enumerate(seats):
        if seat < len(takes) and takes[seat]:
            tmprize.record_takes(mid, takes[seat], say=common._say)
