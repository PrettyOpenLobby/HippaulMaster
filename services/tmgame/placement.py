"""Playing a card: apply a placement, park it at a multi-target choice, and resume it."""
import random
import tmbattle
from . import (
    boardrules, common, matchmaking, matchstart, protocol, pushqueue, scoring, turns, vscom,
    watchers, webwatch,
)


def _apply_placement(chan, index, n, actor, tile, row, rnd=None):
    """Play one card onto the board and resolve everything it touches.

    Returns `(battles, marks)` where `battles` is the ordered list of
    `(attacker tile, defender tile, resolve() result, verdict)` this placement
    owes the clients as `@BattleData`, and `marks` is what the placement
    touched, for the log.

    The order is the client's own, not a convenient one:

      1. 0xCE2B8 runs the arrow engine on the placed tile, flagging each
         reachable enemy 1 (battle) or 2 (free flip).
      2. 0xCE470 (`-BattleCheck`) takes the battles first, one at a time,
         auto-selecting when there is exactly one.
      3. On a win the defender's tile changes hands and 0xD24AA re-runs the
         arrow engine FROM THE PLACED TILE -- so a combo chains off the card
         you played, never off the card you just took.
      4. On a loss the placed card itself changes hands (0xCFAB4) -- **and the
         WINNER then re-contests from the placed tile too**: the arrow engine
         runs again with the tile's NEW owner, capturing the former placer's
         pointed-at cards. That is the game's COMBO, seen from the loss side.
      5. Only when no battles are left do the flips apply (0xCE53D -> state
         0x23), each setting the tile's owner to the current player (0xD2B22).

    VERIFIED: THE LOSS ARM WAS MEASURED LIVE 2026-08-22T21:14Z AND THE OLD "the
    placement is over" MODEL WAS WRONG -- this was THE board divergence
    (every wrong wager/take verdict rode it). Two-sided board dump, 2P game:
    the human placed tile 5 (card 10, arrows 0x7E) into three back-pointing
    enemies, the server fought tile 10 and the placer LOST. The client then
    flipped tile 4 -- the human's OWN other card, reached by the beaten
    card's W arrow -- to the defender (`board3p.log` 17:14:13, tile 4
    client-owner 0 -> 1) while this server left it, and the endgame scored
    5|5 draw here vs 4-6 loss on screen. The static read "0xD25B3 refuses to
    re-scan" was the open-the-function-not-just-the-address trap
    again: the re-scan happens, with the new owner. The unified rule -- ONE
    re-contest from the placed tile with its post-battle owner, win or lose --
    also reproduces every previously-matched game (3P turn 8 win-combo; 3P
    turn 11 loss where the re-contest touched only the winner's own cards, so
    nothing visibly moved and the boards agreed by luck).

    WARNING: Still unmeasured on the loss side: a re-contest mark that POINTS BACK
    (a would-be battle). The client's post-battle wave is state 0x23, "arrow
    flips only", so it is applied as a CAPTURE here like every other combo
    mark -- but it is logged loudly, and the first live game that shows one
    should be read against this choice. `POL_TM_LOSS_COMBO=0` restores the
    old stop-dead arm.

    WARNING: The pending flips of the ORIGINAL marks still die with the loss --
    measured (3P turn 11: abs2's pending flip on tile 6 never applied). Only
    the re-contest from the NEW owner runs.
    """
    board = boardrules._MATCH_BOARD.setdefault((chan, index), {})
    tiles = boardrules._board_tiles(n)
    rnd = rnd or random
    if not 0 <= int(tile) < tiles:
        # 0xC6FF1 treats 0xFF as "no placement"; anything else off the board is
        # ours to refuse, loudly, rather than index a dict with it.
        common._say("tm: WARNING: tile %s is not on a %d-tile board -- nothing placed"
             % (tile, tiles))
        return [], {}
    # WARNING: ...AND THE TILE MUST BE FREE. Until 2026-09-06 nothing on a board
    # could occupy a tile the placer had not chosen, so an occupied tile was
    # a client bug and overwriting it was harmless. BLOCKS CHANGE THAT: they
    # hold tiles from the deal, the client's own cursor will not offer one,
    # and overwriting one would DELETE a block the other client can still see.
    # Refuse, loudly -- a `@PutData` naming an occupied tile is a desync, and
    # the log line is how it gets found.
    if int(tile) in board:
        _occ = board[int(tile)]
        common._say("tm: WARNING: tile %s already holds %r (owner %s) -- nothing placed"
             % (tile, _occ, _occ.owner))
        return [], {}
    # Owners BEFORE the placement resolves -- the only way to count what this
    # one card took without threading a counter through every arm below.
    _before = {t: c.owner for t, c in board.items()}
    # ...INHERITING THE TILE'S ABILITY, which is only ever non-zero on a
    # SPECIAL TILE (0xC7015/0xC703C -- see `_tile_ability`). Every `resolve`
    # below already threads `att_ability`/`dfn_ability`; this is the first
    # thing that has ever made one of them non-zero.
    board[int(tile)] = tmbattle.Card(
        row, actor, ability=boardrules._consume_tile_special(chan, index, tile))
    battles, guard = [], 0
    marks = tmbattle.contest(board, tiles, int(tile), actor, players=n)
    ties = common._env_int("POL_TM_BATTLE_TIES", 0)
    # Blocks already fought in THIS placement -- the client's mark 2 (see
    # `_block_has_no_verdict`). `contest` is stateless; this is not.
    _fought_blk = set()

    def _recontest():
        m = tmbattle.contest(board, tiles, int(tile), actor, players=n)
        if common._env_int("POL_TM_BLOCK_NO_VERDICT", 1):
            for _t in _fought_blk:
                m.pop(_t, None)
        return m

    while guard < 64:
        guard += 1
        target = tmbattle.next_defender(marks)
        if target is None:
            break
        att, dfn = board[int(tile)], board[target]
        _vs_block = dfn.owner == tmbattle.OWNER_BLOCK
        res = tmbattle.resolve(att.row, dfn.row, rnd,
                               att_ability=att.ability, dfn_ability=dfn.ability,
                               att_mod=att.modifier, dfn_mod=dfn.modifier)
        verdict = tmbattle.winner(res)
        if verdict == tmbattle.DRAW and not ties:
            # WARNING: A DRAW IS LEGAL AND THE CLIENT HANDLES IT -- by fighting the
            # SAME battle again (0xCFD63 -> state 0xC -> state 7) and asking
            # for a SECOND `@BattleData` on the same (0x43, 12). We re-roll
            # here instead, and the reason is delivery, not squeamishness:
            # `_queue_push` de-duplicates identical bodies and a second message
            # in one slot has never been watched being drained, so shipping a
            # draw is shipping a hang we cannot see. `POL_TM_BATTLE_TIES=1`
            # sends it anyway, which is how that gets measured.
            continue
        battles.append((int(tile), target, res, verdict))
        webwatch._watch_battle(chan, index, int(tile), target, res, verdict)
        # The ray fires on the battle RESOLVING, like the chance effects, and
        # a true return means the block is NOT captured below.
        _rot = boardrules._rotating_after_battle(chan, index, n, actor, target, board,
                                      res=res)
        if boardrules._chance_after_battle(chan, index, n, actor, target, board):
            # WARNING: A SCRAMBLE ENDS THE PLACEMENT. Every card has just been lifted
            # and redealt, so `tile` and every entry in `marks` are stale
            # indices -- continuing to resolve from them read a tile that no
            # longer held the placed card and raised KeyError. The client's own
            # flow agrees: the effect runs at the END of the battle chain and
            # returns to the idle state, not to more resolution.
            if battles:
                boardrules._burn_attack_boost(board, tile)
            scoring._board_map_log(board, tiles, tile, row, battles)
            scoring._note_combo(chan, index, actor, board, _before, tile)
            return battles, {}
        if boardrules._colorshift_took_placed(board, int(tile), actor):
            if battles:
                boardrules._burn_attack_boost(board, tile)
            scoring._board_map_log(board, tiles, tile, row, battles)
            scoring._note_combo(chan, index, actor, board, _before, tile)
            return battles, {}
        if _rot:
            # WARNING: NEITHER ARM APPLIES TO A ROTATING BLOCK. It is not captured
            # and the placed card is not lost -- states 0x1E..0x22 write no
            # owner at all. Written as `verdict == ATTACKER and not _rot` this
            # fell into the `else`, which is the LOSS path, and turned the
            # player's own WINNING card into a block (owner 4) live on
            # 2026-09-07T03:19Z: "board map after tile 10 ... 10:4", score 0-0.
            #
            # WARNING: AND THE TARGET MUST BE DROPPED. `contest` is stateless, and a
            # rotating block is the one object that survives its battle still
            # NEUTRAL and still carrying an arrow -- so it would be re-selected
            # every pass until the 64-step guard. The client stops this with
            # its per-tile mark; we do it by taking the target off the list.
            marks.pop(target, None)
            _fought_blk.add(target)
        elif _vs_block and boardrules._block_has_no_verdict(tile, target, actor, board):
            # No winner: the chance block is consumed (or, never dealt, a
            # code 18 stands and is not fought again) and the placement goes on.
            _fought_blk.add(target)
            marks = _recontest()
        elif verdict == tmbattle.ATTACKER:
            # WARNING: A CONSUMED CHANCE BLOCK LEAVES NOTHING TO TAKE. Its tile was
            # emptied above, so there is no card to change hands and none for
            # the win-side combo to chain off. The re-contest below still runs.
            if target in board:
                _loser = board[target].owner
                board[target].owner = actor
                # THE WIN-SIDE COMBO: off the card just TAKEN (`POL_TM_COMBO`).
                for _ct in boardrules._combo_capture(board, tiles, n, target, _loser,
                                          actor):
                    common._say("tm:   ...combo: tile %d (seat %s's, reached by the "
                         "beaten card on tile %d) taken by seat %s"
                         % (_ct, _loser, target, actor))
            marks = _recontest()
        else:
            # The placed card is taken. 0xCFAB4 writes the DEFENDER'S owner
            # onto the attacker's tile -- and the WINNER combos through it:
            # the same re-contest the win arm runs, from the placed tile,
            # with its new owner (measured 2026-08-22T21:14Z, tile 4; see the
            # docstring). Marks that point back are captured too, flips-only
            # (state 0x23), logged loudly because that half is unmeasured.
            board[int(tile)].owner = dfn.owner
            if dfn.owner >= n:
                # WARNING: A BLOCK TOOK THE CARD, AND A BLOCK DOES NOT PLAY. 0xCFAB4
                # still writes the winner's owner onto the placed tile, so the
                # card really is lost to owner 4 -- but the re-contest is the
                # WINNER'S move, and inventing a block-driven combo would be
                # inventing a rule. Skipping it is the conservative reading.
                common._say("tm:   ...tile %s was taken by a BLOCK (owner %s) -- no "
                     "loss combo; a block has no turn" % (tile, dfn.owner))
            elif common._env_int("POL_TM_LOSS_COMBO", 1):
                if boardrules._combo_mode() == "placed":
                    for t, kind in sorted(tmbattle.contest(
                            board, tiles, int(tile), dfn.owner,
                            players=n).items()):
                        if kind == tmbattle.BATTLE:
                            common._say("tm: WARNING: loss-combo capture of a BACK-POINTING "
                                 "card on tile %d -- applied as a flip; if the "
                                 "client fought a battle here instead, this is "
                                 "the divergence" % t)
                        board[t].owner = dfn.owner
                        common._say("tm:   ...loss combo: tile %d captured by seat %s "
                             "through the beaten card" % (t, dfn.owner))
                else:
                    # Off the beaten card, the LOSER's cards only.
                    for _ct in boardrules._combo_capture(board, tiles, n, int(tile),
                                              actor, dfn.owner):
                        common._say("tm:   ...loss combo: tile %d captured by seat %s "
                             "through the beaten card" % (_ct, dfn.owner))
            if battles:
                boardrules._burn_attack_boost(board, tile)
            scoring._board_map_log(board, tiles, tile, row, battles)
            scoring._note_combo(chan, index, actor, board, _before, tile)
            return battles, marks
    for t, kind in sorted(marks.items()):
        # 0xD301B: the flip loop SKIPS a tile whose card id is >= 0x8009, so a
        # captured rotating block or high chance block can be taken in battle
        # but never flipped. See `tmbattle.FLIP_ID_CEILING`.
        if kind == tmbattle.FLIP and tmbattle.flippable(board[t]):
            board[t].owner = actor
    if battles:
        boardrules._burn_attack_boost(board, tile)
    scoring._board_map_log(board, tiles, tile, row, battles)
    scoring._note_combo(chan, index, actor, board, _before, tile)
    return battles, marks


#: (chan, index) -> a placement mid-resolution, PARKED waiting for the player's
#: `@BattleSelect=/B=<tile>`. The board (`_MATCH_BOARD`) already holds the placed
#: card; this holds what the resolver needs to resume and to finish the turn.
#:
#: WARNING: THE MULTI-DEFENDER PATH, measured live 2026-09-03 (the first time it ever
#: fired -- and it HUNG, because the server auto-resolved every battle at
#: `@PutData` time and never handled `@BattleSelect`). The client scene 0xCE270
#: is a state machine: -BattleCheck (state 2, 0xCE453) counts battle-eligible
#: defenders and branches -- 0 -> flips only, 1 -> auto-select (NO
#: `@BattleSelect`), >1 -> the interactive picker (state 3) which sends ONE
#: `@BattleSelect=/B=<tile>` (0x43, cmd 11) then parks in state 8 polling (0x43,
#: 12) @BattleData. Combos are CLIENT-DRIVEN: after each @BattleData the client
#: re-contests from the placed tile and loops back to -BattleCheck, so a fresh
#: >1 sends another `@BattleSelect`. `/B=` is the ABSOLUTE tile index, unrotated.
_PENDING_BATTLE = {}


def _battle_push(chan, index, turn, att_tile, def_tile, res, verdict, roster):
    """Push one resolved battle's `@BattleData` to every player and watcher.

    Extracted from the `@PutData` path so the resumable resolver pushes each
    battle the same way -- held back one reply and framed from each
    recipient's own match peer."""
    webwatch._watch_battle(chan, index, att_tile, def_tile, res, verdict)
    _rnd8 = matchstart._turn_rand((chan, index))
    _body = boardrules._battledata_body(res, att_tile, def_tile, _rnd8)
    watchers._watch_push(chan, index, protocol._turn_code(protocol.BATTLEDATA_CMD, turn) + _body,
                "the battle on tile %d" % def_tile,
                after=common._env_int("POL_TM_BATTLE_GAP", 1))
    for mid, _v in roster:
        if turns._is_bot(chan, index, mid):
            continue
        pushqueue._queue_push(mid, protocol._turn_code(protocol.BATTLEDATA_CMD, turn) + _body,
                    "the battle on tile %d" % def_tile,
                    after=common._env_int("POL_TM_BATTLE_GAP", 1),
                    peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
    common._say("tm:   ...battle: tile %d (%d/%d) vs tile %d (%d/%d) -- %s takes it. "
         "(0x43, %d) @BattleData=, turn %d. Watch for '---->Recv=BATTLESET' then "
         "'-BattleSet:OK!'."
         % (att_tile, res["a_roll"], res["a_raw"], def_tile,
            res["d_roll"], res["d_raw"],
            {tmbattle.ATTACKER: "the ATTACKER", tmbattle.DEFENDER: "the DEFENDER",
             tmbattle.DRAW: "NOBODY (a draw)"}[verdict], protocol.BATTLEDATA_CMD, turn))


def _begin_placement(chan, index, n, actor, tile, row, turn, seats, com_n,
                     member_id, roster, rnd=None):
    """Put the card on the board and arm the resumable battle resolver.

    Returns True if a placement is now pending (call `_advance_placement`), or
    False if the tile was off the board (nothing placed)."""
    board = boardrules._MATCH_BOARD.setdefault((chan, index), {})
    tiles = boardrules._board_tiles(n)
    if not 0 <= int(tile) < tiles:
        common._say("tm: WARNING: tile %s is not on a %d-tile board -- nothing placed"
             % (tile, n))
        return False
    if int(tile) in board:
        # The same free-tile guard as `_apply_placement` -- see the banner
        # there. A block holds its tile from the deal.
        _occ = board[int(tile)]
        common._say("tm: WARNING: tile %s already holds %r (owner %s) -- nothing placed"
             % (tile, _occ, _occ.owner))
        return False
    _before = {t: c.owner for t, c in board.items()}
    # The same special-tile inheritance as `_apply_placement`; the resumable
    # resolver must not be the path where a special tile quietly does nothing.
    board[int(tile)] = tmbattle.Card(
        row, actor, ability=boardrules._consume_tile_special(chan, index, tile))
    _PENDING_BATTLE[(chan, index)] = {
        "n": n, "actor": actor, "tile": int(tile), "turn": turn,
        "seats": seats, "com_n": com_n, "member_id": member_id,
        "roster": roster, "before": _before, "row": row, "rnd": rnd or random,
    }
    return True


def _advance_placement(chan, index, chosen=None):
    """Resolve the pending placement, pausing at each multi-target choice.

    Mirrors the client scene 0xCE270 exactly: -BattleCheck counts battle-
    eligible defenders of the placed tile; 0 -> apply flips and finish the turn,
    1 -> auto-resolve, >1 -> park for the player's `@BattleSelect` (return
    without finishing). `chosen` is the `/B=` tile from an arriving
    `@BattleSelect`; it resolves that one battle and continues the loop.

    Returns True if the placement fully resolved (turn advanced), False if it
    parked awaiting a `@BattleSelect`."""
    st = _PENDING_BATTLE.get((chan, index))
    if st is None:
        return True
    n, actor, tile, turn = st["n"], st["actor"], st["tile"], st["turn"]
    seats, com_n, member_id = st["seats"], st["com_n"], st["member_id"]
    roster, before, row, rnd = (st["roster"], st["before"], st["row"], st["rnd"])
    board = boardrules._MATCH_BOARD.setdefault((chan, index), {})
    tiles = boardrules._board_tiles(n)
    ties = common._env_int("POL_TM_BATTLE_TIES", 0)
    # Rotating blocks already fought in THIS placement. They survive their
    # battle still neutral and still carrying an arrow, so `contest` -- which
    # is stateless -- would offer the same one every pass.
    _fought_rot = st.setdefault("fought_rot", set())

    def _finish():
        # 0xD30EE, the same -BattleEnd burn `_apply_placement` does. This
        # resolver is only ever entered with a battle already pending, so every
        # path through here has fought one.
        boardrules._burn_attack_boost(board, tile)
        scoring._board_map_log(board, tiles, tile, row, [])
        scoring._note_combo(chan, index, actor, board, before, tile)
        _PENDING_BATTLE.pop((chan, index), None)
        # THE WATCHER'S LIVE BOARD. The played card is the one new face this
        # placement puts down; push it as @WFCard (the only channel the watch
        # scene reads). Flips change OWNER, which @WFCard cannot carry, so they
        # do not get a message here -- a known limitation until the owner
        # channel is found. See `_watch_wfcard`.
        watchers._watch_wfcard(chan, index, turn, [tile], board)
        _sc = scoring._board_scores(chan, index, n)
        common._say("tm:   ...board after turn %d: %s"
             % (turn, " / ".join("seat %d: %d tile(s)" % (i, v)
                                 for i, v in enumerate(_sc))))
        if common._env_int("POL_TM_TURNDATA", 1):
            if com_n:
                vscom._queue_com_turns(chan, index, member_id, n, turn + 1,
                                 (actor + 1) % n)
            else:
                turns._queue_next_turn(chan, index, seats, turn + 1, (actor + 1) % n,
                                 roster=roster, n_solo=com_n)

    guard = 0
    while guard < 64:
        guard += 1
        owner = board[tile].owner
        marks = tmbattle.contest(board, tiles, tile, owner, players=n)
        battles = sorted(t for t, k in marks.items()
                         if k == tmbattle.BATTLE and t not in _fought_rot)
        if chosen is not None:
            if int(chosen) in battles:
                target = int(chosen)
            else:
                common._say("tm: WARNING: @BattleSelect=/B=%s is not a battle-eligible tile "
                     "(options %r) -- auto-selecting the client's own default"
                     % (chosen, battles))
                target = battles[-1] if battles else None
            chosen = None
        elif not battles:
            target = None
        elif len(battles) == 1:
            target = battles[0]
        elif st.get("bot"):
            # A departed seat has no picker to park for: take the client's
            # own default (the last eligible tile, as the fallback above).
            target = battles[-1]
            common._say("tm:   ...tile %d can attack %d defenders %r -- departed seat, "
                 "auto-selecting tile %d" % (tile, len(battles), battles, target))
        else:
            # >1: the client is in its picker (state 3); park until its
            # @BattleSelect names a target. The board and this record stand.
            common._say("tm:   ...tile %d can attack %d defenders %r -- holding for the "
                 "player's @BattleSelect (0x43, 11, /B=). The single-target and "
                 "0-target paths never send it; only this one does."
                 % (tile, len(battles), battles))
            return False
        if target is None:
            # No battles remain: apply the free flips and end the placement.
            for t, kind in sorted(marks.items()):
                # The same 0xD301B ceiling as `_apply_placement`.
                if kind == tmbattle.FLIP and tmbattle.flippable(board[t]):
                    board[t].owner = owner
            _finish()
            return True
        # Resolve `target`, re-rolling a draw (the client fights it again and
        # asks for a second @BattleData; we re-roll rather than ship a hang).
        _vs_block = board[target].owner == tmbattle.OWNER_BLOCK
        while True:
            att, dfn = board[tile], board[target]
            res = tmbattle.resolve(att.row, dfn.row, rnd,
                                   att_ability=att.ability, dfn_ability=dfn.ability,
                                   att_mod=att.modifier, dfn_mod=dfn.modifier)
            verdict = tmbattle.winner(res)
            if verdict == tmbattle.DRAW and not ties:
                continue
            break
        _battle_push(chan, index, turn, tile, target, res, verdict, roster)
        _rot = boardrules._rotating_after_battle(chan, index, n, owner, target, board,
                                      res=res)
        if boardrules._chance_after_battle(chan, index, n, owner, target, board):
            # The same rule as `_apply_placement`: a scramble ends it.
            _finish()
            return True
        if boardrules._colorshift_took_placed(board, tile, owner):
            # The same rule as `_apply_placement`: a code 8 that hands the
            # placed card away ends it.
            _finish()
            return True
        if _rot:
            # The same two rules as `_apply_placement`: neither arm applies to
            # a rotating block, and it must not be fought again. Here `marks`
            # is recomputed from the board every pass, so dropping it from a
            # dict would not stick -- the set on the pending record does.
            _fought_rot.add(target)
            continue
        if _vs_block and boardrules._block_has_no_verdict(tile, target, owner, board):
            _fought_rot.add(target)       # consumed already; a code 18 stands
            continue                      # re-contest (0xD25B8), no verdict
        if verdict == tmbattle.ATTACKER:
            # WARNING: The same consumed-block guard as `_apply_placement`.
            if target in board:
                _loser = board[target].owner
                board[target].owner = owner
                # THE WIN-SIDE COMBO: off the card just TAKEN (`POL_TM_COMBO`).
                for _ct in boardrules._combo_capture(board, tiles, n, target, _loser,
                                          owner):
                    common._say("tm:   ...combo: tile %d (seat %s's, reached by the "
                         "beaten card on tile %d) taken by seat %s"
                         % (_ct, _loser, target, owner))
            continue                      # re-contest from the placed tile
        # A loss: the placed card is taken, and the winner combos through it
        # (the same loss-side rule `_apply_placement` measured 2026-08-22T21:14Z
        # -- flips only, back-pointers logged loudly). The original marks' flips
        # die with the loss. This ends the placement.
        board[tile].owner = dfn.owner
        if dfn.owner >= n:
            # A block won it -- see the same guard in `_apply_placement`.
            common._say("tm:   ...tile %s was taken by a BLOCK (owner %s) -- no loss "
                 "combo; a block has no turn" % (tile, dfn.owner))
        elif common._env_int("POL_TM_LOSS_COMBO", 1):
            if boardrules._combo_mode() == "placed":
                for t, kind in sorted(tmbattle.contest(
                        board, tiles, tile, dfn.owner, players=n).items()):
                    if kind == tmbattle.BATTLE:
                        common._say("tm: WARNING: loss-combo capture of a BACK-POINTING card "
                             "on tile %d -- applied as a flip; if the client "
                             "fought a battle here instead, this is the "
                             "divergence" % t)
                    board[t].owner = dfn.owner
                    common._say("tm:   ...loss combo: tile %d captured by seat %s "
                         "through the beaten card" % (t, dfn.owner))
            else:
                for _ct in boardrules._combo_capture(board, tiles, n, tile, owner,
                                          dfn.owner):
                    common._say("tm:   ...loss combo: tile %d captured by seat %s "
                         "through the beaten card" % (_ct, dfn.owner))
        _finish()
        return True
    common._say("tm: WARNING: battle resolver hit its 64-step guard at %s table %s -- "
         "finishing the placement to avoid a hang" % (chan, index))
    _finish()
    return True
