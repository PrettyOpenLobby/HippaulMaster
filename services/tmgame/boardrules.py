"""The board: per-match hands, turn and board state, @PutCard/@BattleData bodies, blocks, chance and
special tiles, combos.
"""
import os
import random
import tmbattle
import re
from . import common, matchstart, ruleset, scoring, tablesettings, webwatch


#: (chan, index) -> {push_key(member): [b"161|100|2|90|99|31|21|255", ...]}
#:
#: THE HANDS, CAPTURED FROM `@CardSelect=`. The client sends its whole hand as
#: `@CardSelect=/C=5@N0=/D=<row>@N1=/D=<row>...`, one eight-value row per card,
#: and that is the ONLY place the server ever learns what a player is holding.
#: `@PutData=` then names a card by its HAND INDEX only (`/H=`), so without this
#: there is nothing to put in `@PutCard`'s `/D=`.
_MATCH_HANDS = {}

#: (chan, index) -> {"turn": int, "active": absolute seat, "n": players}
_MATCH_TURN = {}

#: (chan, index) -> {tile: tmbattle.Card}. THE BOARD, and it is the thing this
#: server did not have.
#:
#: WARNING: EVERY INPUT ALREADY ARRIVED; NOTHING WAS BEING KEPT. `@PutData=` names
#: the tile (`/F=`) and the hand slot (`/H=`), `_MATCH_HANDS` turns the slot
#: into a row, and the actor is the connection -- so which card sits on which
#: tile and whose it is has been derivable since the first relayed placement.
#: Without it the server can answer `@PutCard` and nothing else: it cannot say
#: whether a placement STARTS a battle, cannot resolve one, and cannot count
#: the tiles at the end to know who won.
#:
#: Reset in `_remember_match` with the hands and the turn, for the same reason
#: they are: a stale board relays a battle that is not happening.
_MATCH_BOARD = {}

#: (chan, index) -> one OBJECT CODE PER TILE, exactly as `@StartData`'s `/F=`
#: sent it. See `tmbattle.roll_board` for what a code is.
#:
#: Kept past the deal because two different things need it afterwards: a
#: SPECIAL TILE has to hand its ability to whatever is played on it (the
#: client does this at 0xC7015/0xC703C and so must we -- `_tile_ability`), and
#: an observer arriving mid-match needs the same array in `@WatchInfo`.
#: Cleared wherever `_MATCH_BOARD` is.
_MATCH_OBJECTS = {}

#: The eight `|`-separated fields of a card row, named from the two independent
#: measurements that fixed them: `@PutCard`'s parser (0x103951..0x103AAA) and
#: `CardPrm.BIN`. occ0 is a WORD (`[obj+0x33A]`, and the client refuses
#: an id >= [0x2FF4FC], the card-count limit); the rest are bytes. occ2 is the
#: TYPE and the client rejects >= 4 with `Error:CardNum=%d` -- which is exactly
#: CardPrm's "no type 4 exists".
#:
#: WARNING: SLOTS 5 AND 6 WERE BOTH NAMED WRONG HERE UNTIL 2026-08-20, AND SLOT 6 IS
#: THE ONE THAT MATTERED: it is the **ARROW MASK**, not the level, and the
#: whole of the battle mechanic hangs off it. The reasoning that fixed it is in
#: `tmbattle.ARROW_COUNT_WEIGHTS`; the short version is that the client
#: GENERATES the byte with a random arrow count (0xB2DF4), bit-tests it per
#: direction in both the renderer (0xB4D0D) and the adjacency engine (0xD3760),
#: and that no column of a static card table can be any of those things.
#:
#: WARNING: AND THE CAPTURE THAT SEEMED TO SAY OTHERWISE WAS OUR OWN DATA COMING BACK.
#: The one measured hand, `161|100|2|90|99|31|21|255`, has 21 in slot 6, and
#: 21 is card 161's level -- because `_draw_pack` PUT it there. A round-trip
#: through the client is not a measurement of the client. `tmbattle` is the
#: authority for these names now; this tuple is kept in its order.
CARD_ROW_FIELDS = tmbattle.ROW_FIELDS


def _hands_from_cardselect(cmd):
    """The card rows out of one `@CardSelect=` body, in hand order.

    WARNING: Keyed by the `@N<i>=` number, not by order of appearance: `/H=` in
    `@PutData=` is an index into THIS list and an off-by-one here puts the
    wrong card on the board with no error anywhere.
    """
    rows = {}
    for m in re.finditer(rb"@N(\d+)=/D=([0-9|]+)", cmd or b""):
        rows[int(m.group(1))] = m.group(2)
    return [rows[i] for i in sorted(rows)]


def _putcard_body(seats, me, actor, hand_idx, tile, row, n_solo=None):
    """`@PutCard=` -- one card lands on the board. Arm 0x103820, `Recv=PUTCARD`.

    Field for field, off the parser:

        /A=   the ACTING player, rotated into the recipient's frame like every
              other seat number here. 0x103951 tests it against **255** and
              treats that as "nothing was placed", so a real seat must be sent.
        /H=   -> byte [0x2B63C4]+0x1B2, the hand slot the card came from
        /F=   -> byte [0x2B63C4]+0x1B1, the BOARD TILE. 0xC6FD7 prints it as
              `GamePlayPutCard=%d` and 0xC6FF1 treats 0xFF as "no placement".
        /D= x8  the card, one `|` list:
              occ0 word [obj+0x33A] (the id; `cad%05d` names its texture),
              occ1..6 bytes [obj+0x334..0x339], occ7 byte [obj+0x342].

    The recipient is always player 0 in its own copy, so `me` is 0 for
    everyone and `/A=` is the actor's DISTANCE from the recipient.
    """
    n = len(seats) or int(n_solo or 2)
    a_val = (int(actor) - int(me)) % n
    return (b"@PutCard=/A=%d/H=%d/F=%d/D=%s"
            % (a_val, int(hand_idx), int(tile), row))


def _battledata_body(res, att_tile, def_tile, rand):
    """`@BattleData=` -- one card contests another. Arm 0x103BB7,
    `Recv=BATTLESET`.

    VERIFIED: THIS IS THE MESSAGE A MATCH STOPPED ON. Measured live
    2026-08-20T17:34Z: a placement landed next to an enemy card, both clients
    printed `-BattleInit / -BattleCheck / -Battle:Select=1 / -BattleDecide:7 /
    -BattleStart:8` and then parked in `0x101A10(0xC)` at 0xCF107 for ever.

    Ten values, five per side -- `/A=` the ATTACKER (the card just placed),
    `/D=` the DEFENDER -- each a pipe list read by occurrence:

        occ0  byte  the board TILE      [obj+0x1B1] / [obj+0x1B2]
        occ1  word  the raw STAT        record +0x8E
        occ2  word  the ROLLED value    record +0x8C
        occ3  byte  the stat SELECTOR   record +0x29
        occ4  byte  the ability MULT    record +0x2A

    then `/R=` x8, the match's own random table again (0x103E38 writes it into
    the SAME [obj+0x1A8+i] slots `@TurnData` fills, and the client re-prints it
    as `RandTbl=`).

    WARNING: `/CBC=` IS DELIBERATELY OMITTED. It is optional -- 0x103E7B is
    `test eax,eax / je` straight to the arm's normal end -- and it carries a
    whole card row in `@PutCard`'s eight-field shape (0x103E85 applies the same
    card-id ceiling). INFERRED, from the name, that it is the "Combo Battle
    Card". Sending a card row for a mechanic nobody has watched happen is
    exactly the kind of invention this file exists to stop.

    WARNING: `occ0` of `/A=` is confirmatory only. 0xCF1D6 takes the ATTACKER tile
    from [obj+0x179], which the client set itself when it placed the card
    (0xC6FF9); only the DEFENDER tile is read out of the message, and only when
    the network flag is set (0xCF1A5, gated on [0x2B64CA]). Both are sent
    because both are parsed.
    """
    return (b"@BattleData=/A=%d|%d|%d|%d|%d/D=%d|%d|%d|%d|%d"
            % (int(att_tile) & 0xFF, res["a_raw"] & 0xFFFF,
               res["a_roll"] & 0xFFFF, res["a_sel"], res["a_mult"],
               int(def_tile) & 0xFF, res["d_raw"] & 0xFFFF,
               res["d_roll"] & 0xFFFF, res["d_sel"], res["d_mult"])
            + b"/R=" + b"|".join(b"%d" % v for v in rand[:8]))


def _board_tiles(n):
    """How many tiles an n-player board has. 0x23359E, and `TILES_BY_PLAYERS`."""
    idx = min(max(int(n) - 2, 0), len(matchstart.TILES_BY_PLAYERS) - 1)
    return common._env_int("POL_TM_START_TILES", matchstart.TILES_BY_PLAYERS[idx])


def _dealt_chance_codes():
    """The chance codes this server may DEAL, after the server owner's knobs.

    `tmbattle.MODELLED_CHANCE_CODES` is the truth about what is MODELLED; this
    is the shorter list a server owner can ask for without a deploy. Only 17 has
    a switch, and it has one because it is the only effect that MOVES cards:
    get its candidate list wrong and the two boards disagree about which tile
    holds which card, which parks the next placement for ever (04:20Z). Every
    other code changes a card in place, so a divergence there plays wrong
    rather than hanging.

    `POL_TM_SCRAMBLE=0` -> no scramble is dealt and a forced board carrying one
    is blanked, the same way an unmodelled code always was.
    """
    codes = tuple(tmbattle.MODELLED_CHANCE_CODES)
    if not common._env_int("POL_TM_SCRAMBLE", 1):
        codes = tuple(c for c in codes if c != 17)
    return codes


def _unplayed_specials(chan, index):
    """The tiles still carrying a SPECIAL nobody has played on -- `card+0x1C`.

    WARNING: THE SCRAMBLE'S CANDIDATE LIST NEEDS THIS AND NOTHING ELSE DOES. The
    client collects its redeal candidates with 0xD3A60's arg2=1 arm, which
    tests `card+0x1C`; an unplayed special tile is stamped **4** there
    (0xC1976) while its owner byte stays 8, so it is the one square this
    server holds no card on that the client will never redeal onto. Leaving it
    in made our list longer than theirs and the modulus picked a different
    tile -- the 2026-09-07T04:20Z hang. See `tmbattle.apply_scramble`.

    A special that has been PLAYED on is not in here: `_consume_tile_special`
    clears the deal entry when the card lands, which is the same thing the
    client does to the tile record (0xC7098 overwrites the 4 with the owner).
    """
    codes = _MATCH_OBJECTS.get((chan, index)) or []
    return [t for t, c in enumerate(codes)
            if int(c) in tmbattle.SPECIAL_CODES]


def _match_objects(chan, index, n, fresh=False):
    """This match's board objects, rolled ONCE and remembered.

    WARNING: ONCE PER MATCH, NOT ONCE PER RECIPIENT. `@StartData` is built per
    player because `/S=` is in the recipient's own seat frame -- but the BOARD
    is absolute, tiles are not rotated, so rolling inside `_startdata_body`
    would deal each player a different board and the two clients would
    disagree about every tile. Same law as `_TURN_RAND`.

    Rolling also SEEDS `_MATCH_BOARD` with the blocks, and that one line is
    what makes every consumer downstream correct without touching it: the
    COM's `empty` list is already `t not in board`, `_board_scores` already
    skips an owner outside the seat range, and `tmbattle.contest` now has the
    neutral arm the blocks need.

    WARNING: SPECIAL TILES ARE DELIBERATELY NOT PUT ON THE BOARD. A special tile is a
    property of a FREE tile, not an occupant (`tmbattle.special_ability`);
    putting one in the board dict would make it unplayable and cost the player
    a square. That asymmetry is the client's, not a shortcut.

    `POL_TM_BOARD_OBJECTS=0` deals the old empty board for an A/B.
    """
    key = (chan, index)
    if not fresh and key in _MATCH_OBJECTS:
        return _MATCH_OBJECTS[key]
    tiles = _board_tiles(n)
    if (not common._env_int("POL_TM_BOARD_OBJECTS", 1)
            or tiles not in tmbattle.BLOCK_COUNT_WEIGHTS):
        if tiles not in tmbattle.BLOCK_COUNT_WEIGHTS:
            common._say("tm: WARNING: no shipped block weights for a %d-tile board -- "
                 "dealing it empty" % tiles)
        _MATCH_OBJECTS[key] = [0] * tiles
        return _MATCH_OBJECTS[key]
    # WARNING: THE OVERRIDES LIVE HERE, NOT IN THE DEAL BUILDER, AND THAT COST A LIVE
    # GAME (2026-09-07T00:16Z, the first run with objects on). `POL_TM_START_F`
    # used to be read only by `_startdata_body`, so `/F=` went out as the
    # explicitly authored board while THIS function rolled a different one and
    # seeded `_MATCH_BOARD` from it: the client saw blocks on tiles 1/4/6/9/11/14
    # and the server believed 4/8/9/11/13. Every downstream consumer -- the free
    # list, `_tile_ability`, the arrow engine -- was then answering about a board
    # nobody was looking at. One source, or none.
    _explicit = os.environ.get("POL_TM_START_F")
    if _explicit:
        codes = []
        for _p in _explicit.replace(",", "|").split("|"):
            try:
                codes.append(int(_p.strip(), 0))
            except ValueError:
                codes.append(0)
        codes = (codes + [0] * tiles)[:tiles]
    elif os.environ.get("POL_TM_START_TILE") is not None:
        codes = [common._env_int("POL_TM_START_TILE", 0)] * tiles
    else:
        codes = None
    if codes is not None:
        # WARNING: AN OVERRIDE IS NOT A LICENCE TO DEAL AN UNMODELLED EFFECT. The
        # forced board is a debugging convenience; the rule it must not escape
        # is `tmbattle.MODELLED_CHANCE_CODES`, because a chance code the client
        # applies and this server does not is a board divergence, and a board
        # divergence hangs the match. That is how 2026-09-07T04:20Z ended: the
        # env still pinned a 17 on tile 3 after `roll_board` had stopped dealing
        # one, so withdrawing the effect protected nobody. Blanked, loudly --
        # the deal is the one place this can still be caught.
        # WARNING: NOT just `UNMODELLED_CHANCE_CODES` (which is empty again now):
        # anything the DEAL is not allowed to produce must not arrive this way
        # either, or `POL_TM_SCRAMBLE=0` would withdraw 17 from the roll and
        # leave the override still pinning one -- which is exactly what made
        # the first withdrawal protect nobody.
        _dealable = set(_dealt_chance_codes())
        _unmodelled = set(tmbattle.UNMODELLED_CHANCE_CODES) | {
            c for c, eff in tmbattle.CHANCE_EFFECTS.items()
            if eff is not None and c not in _dealable}
        _dropped = [(t, c) for t, c in enumerate(codes) if c in _unmodelled]
        if _dropped:
            for t, _c in _dropped:
                codes[t] = 0
            common._say("tm:   WARNING: forced board for %s table %s asked for %s -- BLANKED. "
                 "%s not on this server's deal list, and dealing one diverges "
                 "the board from the client's on the first battle against it"
                 % (chan, index,
                    ", ".join("code %d on tile %d" % (c, t) for t, c in _dropped),
                    "They are" if len(set(c for _t, c in _dropped)) > 1
                    else "It is"))
        _MATCH_OBJECTS[key] = codes
        board = _MATCH_BOARD.setdefault(key, {})
        for t, c in enumerate(codes):
            card = tmbattle.object_card(c)
            if card is not None:
                board[t] = card
        b, e = tmbattle.board_counts(codes)
        common._say("tm:   ...board objects for %s table %s: FORCED by the environment "
             "-- /B=%d /E=%d (%s)"
             % (chan, index, b, e,
                ", ".join("tile %d %s" % (t, tmbattle.object_kind(c))
                          for t, c in enumerate(codes) if c) or "none"))
        return codes
    fields = tablesettings._table_rules_get(chan, index) or tablesettings._com_rules_of(chan, index) or {}

    def _on(k):
        """One rule byte, table first then the client's own defaults."""
        return int(fields.get(k, ruleset.TET_DEFAULTS.get(k, 0)) or 0) != 0

    # `POL_TM_BOARD_SEED` deals the SAME board every match, which is the knob
    # the live proof wants: two runs that differ only in the thing under test.
    _seed = os.environ.get("POL_TM_BOARD_SEED")
    _rnd = random
    if _seed:
        try:
            _rnd = random.Random(int(_seed, 0))
        except ValueError:
            common._say("tm: WARNING: POL_TM_BOARD_SEED=%r is not a number -- ignoring"
                 % (_seed,))
    codes = tmbattle.roll_board(tiles, _rnd,
                                special=_on("st"), chance=_on("cb"),
                                rotating=_on("ca"),
                                chance_codes=_dealt_chance_codes())
    _MATCH_OBJECTS[key] = codes
    board = _MATCH_BOARD.setdefault(key, {})
    for t, c in enumerate(codes):
        card = tmbattle.object_card(c)
        if card is not None:
            board[t] = card
    b, e = tmbattle.board_counts(codes)
    common._say("tm:   ...board objects for %s table %s: /B=%d /E=%d (%s) -- rules "
         "st=%d cb=%d ca=%d. %s"
         % (chan, index, b, e,
            ", ".join("tile %d %s" % (t, tmbattle.object_kind(c))
                      for t, c in enumerate(codes) if c) or "none",
            _on("st"), _on("cb"), _on("ca"),
            "%d tile(s) left to play on" % (tiles - b)))
    return codes


def _tile_code(chan, index, tile):
    """The object code this match dealt onto `tile`, or 0. See `_match_objects`."""
    codes = _MATCH_OBJECTS.get((chan, index)) or []
    tile = int(tile)
    return int(codes[tile]) if 0 <= tile < len(codes) else 0


def _chance_after_battle(chan, index, n, actor, target, board):
    """Run a CHANCE BLOCK's effect after a battle resolves against `target`.

    The client does this at scene state 0x11 (`0xD07EA`): it reads the ability
    byte off `[obj+0x17A]` -- the battle TARGET -- and shifts it right by four.
    Nothing between the battle and that shift tests who won, so the effect
    belongs to the battle RESOLVING, not to the attacker winning it.

    WARNING: A code whose effect this server cannot reproduce must never have been
    dealt (`tmbattle.MODELLED_CHANCE_CODES`). If one turns up here anyway it is
    logged loudly and NOT applied, because a half-applied effect is the same
    divergence as none: the client changes its board, we do not, and the next
    placement parks on a battle we will never fight.
    """
    code = _tile_code(chan, index, target)
    if not code or code not in tmbattle.CHANCE_EFFECTS:
        return False
    # WARNING: THE DEAL ARRAY IS NOT THE BOARD. `_MATCH_OBJECTS` records what was
    # DEALT onto each tile and never changes; the board does. A consumed block
    # frees its square, a card gets played there, and keying the effect off the
    # deal array alone re-fires it against that CARD -- and the consumption
    # below then DELETES it. Caught by the whole-match selftest 2026-09-07:
    # "BEFORE 4:('0x9', 1) ... AFTER tile 4 is gone", a card played on a former
    # chance-block tile vanishing on the next battle.
    #
    # So the tile must still be holding the object itself. Pseudo ids are all
    # >= 0x8000 and no real card can reach that (`@PutCard` refuses >= 250).
    _occ = board.get(int(target))
    if _occ is None or int(_occ.row["id"]) < 0x8000:
        return False
    if code in tmbattle.UNMODELLED_CHANCE_CODES:
        common._say("tm: WARNING: tile %s is chance block %d (%s) and this server does NOT "
             "model its effect -- the client is about to change its board and "
             "we are not. This code should not have been dealt; expect the "
             "match to park on the next battle."
             % (target, code, tmbattle.chance_effect(code)))
        return False
    # `rand` is the match's own `/R=`; `take_owner` reads index 1 of it, which
    # is what makes a code-8 draw reproducible on both sides with no message.
    # WARNING: READ BEFORE THE EFFECT. A scramble refills the board as it runs, so a
    # candidate count taken afterwards is the LEFTOVER empties, not the list
    # the redeal indexed -- and a log line that reads as a measurement and is
    # not one is how the last two of these went wrong.
    _specials = _unplayed_specials(chan, index)
    # ...and the list a SCRAMBLE indexes is taken after phase 1 has lifted every
    # real card (0xD1974..0xD197C write all three bytes back to 8), so an
    # occupied tile IS a candidate and only the blocks and the unplayed
    # specials are not.
    _cands = [t for t in range(_board_tiles(n))
              if t not in set(_specials)
              and int((board.get(t).row["id"] if board.get(t) else 0)) < 0x8000]
    eff, hit = tmbattle.apply_chance(
        board, code, actor, players=n, tiles=_board_tiles(n), target=target,
        rand=matchstart._turn_rand((chan, index)),
        # WARNING: THE SCRAMBLE'S CANDIDATES ARE `card+0x1C`, NOT "no card here".
        # An unplayed special tile is stamped 4 there and is not a candidate;
        # leaving it in is what put card 33 on tile 7 while the client put it
        # on tile 2 (04:20Z). The block being fought is still ON the board at
        # this point -- it is consumed below -- so it is excluded already.
        specials=_specials)
    webwatch._watch_effect(chan, index, eff, target, hit)
    if eff == "scramble":
        common._say("tm:   ...CHANCE BLOCK on tile %s (code 17): SCRAMBLE -- every "
             "card lifted and redealt from /R=. The FIRST candidate list "
             "(card+0x1C == 8, rebuilt per card at 0xD19DF) was %r; the "
             "unplayed special tile(s) %r are NOT in it, which is the whole "
             "of the 04:20Z fix. %d moved: %r"
             % (target, _cands, _specials, len(hit), hit))
    if eff in ("power_up", "power_down") and hit:
        common._say("tm:   ...CHANCE BLOCK on tile %s (code %d): %s -- seat %s's "
             "card(s) on tile(s) %r move by %+d (rec+0x26)"
             % (target, code, eff.upper().replace("_", " "), actor, hit,
                tmbattle.POWER_STEP if eff == "power_up"
                else -tmbattle.POWER_STEP))
    elif eff == "take":
        _who = tmbattle.take_owner(matchstart._turn_rand((chan, index))[1], actor, n)
        if _who == actor and hit:
            scoring._event_count(chan, index, actor, "chance", len(hit))
        common._say("tm:   ...CHANCE BLOCK on tile %s (code 8): TAKE -- /R= occ 1 = %d "
             "sends every adjacent card to seat %s%s; converted tile(s) %r"
             % (target, matchstart._turn_rand((chan, index))[1], _who,
                " (the attacker)" if _who == actor else " (NOT the attacker)",
                hit))
    _scrambled = (eff == "scramble")
    if code in tmbattle.CONSUMED_CHANCE_CODES and int(target) in board:
        # WARNING: THE BLOCK IS CONSUMED, NOT CAPTURED. State 0x1D rebuilds the target
        # as card id 0xFFFF -- the empty-tile constructor -- so the square goes
        # back to being playable and belongs to nobody. Holding it as a captured
        # card is what put THREE PHANTOM TILES in our score on 2026-09-07T01:07Z:
        # we served /S=2|7 while the tester's screen held six cards, and the
        # client dutifully displayed our wrong number.
        del board[int(target)]
        # ...and the deal array forgets it too, the way the client clears the
        # tile's own code (0xC1BCF) and ability (0xD2526). Without this the
        # square keeps claiming to be a block for the rest of the match.
        _codes = _MATCH_OBJECTS.get((chan, index))
        if _codes and 0 <= int(target) < len(_codes):
            _codes[int(target)] = 0
        common._say("tm:   ...and tile %s is CONSUMED -- the block is spent, the square "
             "is empty and playable again (0xD2543, card id 0xFFFF)" % (target,))
    elif eff is None:
        common._say("tm:   ...chance block on tile %s (code %d) has NO effect -- "
             "nibble 5 lands on state 0x1B, the ordinary post-battle state"
             % (target, code))
    return _scrambled


def _colorshift_took_placed(board, tile, actor):
    """Did a chance block's effect just hand the PLACED card to another seat?

    WARNING: IF SO THE PLACEMENT IS OVER -- measured live 2026-09-24T22:53Z (2P VS.
    COM). Card 90 (all eight arrows) on tile 7 had two targets, chance
    blocks 6 and 11, both code 8. The player chose 11; `/R=` occ 1 = 254 sent
    the neighbours to seat 1, NOT the attacker, and tile 7 was one of them.
    This server then fought tile 6 anyway, with the card it had just lost,
    and consumed it. The client did not: on turn 1 the COM's card 46 on tile
    1 pointed SE at tile 6, the client still had a "?" there and parked on a
    battle this server never fought. The turn never ended.

    The client's -BattleCheck is the PLACER's; once the placed card is no
    longer theirs it has nothing left to fight with. So this ends the
    placement the way a loss does: no more battles and no pending flips
    (unmeasured -- in the live sample tile 7 had no flippable neighbours).
    A code 8 that keeps the card with the attacker (occ 1 < 0xB3) is
    untouched: the 22:44Z 3P game fought on to a second block and did not hang.

    `POL_TM_COLORSHIFT_ENDS=0` restores the old keep-fighting behaviour.
    """
    if not common._env_int("POL_TM_COLORSHIFT_ENDS", 1):
        return False
    placed = board.get(int(tile))
    if placed is None or placed.owner == actor:
        return False
    common._say("tm:   ...the chance block handed the PLACED card on tile %s to seat "
         "%s -- the placement ends here (no more battles, no flips), as the "
         "client's does" % (tile, placed.owner))
    return True


def _block_has_no_verdict(tile, target, actor, board):
    """A battle against a BLOCK (owner 4) has NO WINNER. True -> no capture,
    no loss: the caller re-contests from the placed tile and never fights
    `target` again this placement.

    WARNING: THE 2026-09-26 VS. COM FREEZE (2P, vs Flower Girl Natasha). State
    9 reads the DEFENDER'S owner before any count-down:

        0xCF687  al = [target.card + 0x1D]      the owner
        0xCF68E  cmp al, 4 / je 0xCF6C7
        0xCF6C7  id == 0x8009 ?  -> state 0x1E (0xCF6F5), or 0x0A (0xCF703)
                                    whose tail 0xCF9BD sends 0x8009 to 0x1E
        0xCF711  anything else   -> state 0x11, the chance effect

    The count-down that decides a battle, 0xD34D0, is called from state 0xB
    only (0xCFA2E), and the two writes that hand a tile over live in its tail
    (loss 0xCFAB4, win 0xCFC45). A block battle never enters 0xB, so the
    client never takes the block and never loses the placed card to it: our
    `/A=`/`/D=` rolls are shown and ignored. After the effect, state 0x1D
    (0xD24BD) empties the tile and re-runs the arrow engine from the placed
    tile when it is still the current player's (0xD25B3 / 0xD25B8); the
    rotating path ends at 0x22 with the fired block's mark stepped 3 -> 2
    (0xD2E6F), which the neutral arm then skips for the rest of the placement
    (0xD386F).

    This server used to run its LOSS arm on a lost chance-block roll: T2 the
    COM's card on tile 5 became owner 4, T3 and T4 then fought "block" cards
    the client holds as seat 1's, and T5 parked for a 3-target
    @BattleSelect ([5, 6, 12]) while the client, holding one target (12),
    auto-selected and waited for a @BattleData that never came.

    `POL_TM_BLOCK_NO_VERDICT=0` restores the old verdict-driven arms.
    """
    if not common._env_int("POL_TM_BLOCK_NO_VERDICT", 1):
        return False
    common._say("tm:   ...tile %s fought the BLOCK on tile %s: NO verdict (0xCF68E -> "
         "state 0x11/0x1E, never the count-down 0xB) -- the placed card stays "
         "seat %s's and the placement re-contests from it"
         % (tile, target, board[int(tile)].owner if int(tile) in board
            else actor))
    return True


def _rotating_after_battle(chan, index, n, actor, target, board, res=None):
    """Fire a battled ROTATING BLOCK's ray. Returns True if `target` was one.

    WARNING: A TRUE RETURN MEANS THE CALLER MUST NOT CAPTURE THE TILE. The client's
    rotating branch (0xCF6C7 -> state 0x1E) replaces the ordinary post-battle
    path outright, and nothing in states 0x1E..0x22 writes the block's own
    owner. Treating it as an ordinary defender is what desynced the boards and
    hung the match on 2026-09-07T02:37Z.

    What fires instead is one tile's worth of ray -- see
    `tmbattle.apply_rotating`.
    """
    blk = board.get(int(target))
    if not tmbattle.is_rotating(blk):
        return False
    # THE BLOCK HAS TURNED SINCE THE DEAL: one step per turn (measured
    # 2026-09-07T14:23Z). `POL_TM_ROTATING_STEP=0` = the static block
    # that froze that game.
    _turn = int((_MATCH_TURN.get((chan, index)) or {}).get("turn") or 0)
    _adv = _turn * common._env_int("POL_TM_ROTATING_STEP", 1)
    # WARNING: THE RAY WRAPS AND REACHES (2026-09-26 VS. COM freeze). The client
    # walks the ray through its WRAPPING table (rva 0x2338D8, not the arrow
    # engine's 0x233748) and walks it `min(a_raw * a_mult // 30 + 1, players)`
    # tiles out (0xD2BE7). POL_TM_ROTATING_WRAP=0 / POL_TM_ROTATING_REACH=0
    # restore the old one-tile ray that stops at the edge.
    _wrap = bool(common._env_int("POL_TM_ROTATING_WRAP", 1))
    _reach = 1
    if common._env_int("POL_TM_ROTATING_REACH", 1) and res is not None:
        _reach = tmbattle.ray_reach(res.get("a_raw", 1), res.get("a_mult", 1), n)
    hit = tmbattle.apply_rotating(board, _board_tiles(n), int(target), actor,
                                  players=n, advance=_adv, wrap=_wrap,
                                  reach=_reach)
    _phase = tmbattle.rotating_phase(blk, _adv)
    _aim = (tmbattle.ray_target(_board_tiles(n), int(target), _phase,
                                wrap=_wrap)
            if _phase is not None else None)
    webwatch._watch_effect(chan, index, "ray", target, hit, aim=_aim)
    if hit:
        scoring._event_count(chan, index, actor, "rot", len(hit))
    common._say("tm:   ...ROTATING BLOCK on tile %s: it is NOT captured -- it fires "
         "%d tile(s) %s (deal phase %s + turn %d -> phase %s, opposite its "
         "arrow bit; wrap=%d) first at tile %s, and converted %r for seat %s"
         % (target, _reach, tmbattle.DIR_NAMES[(( _phase or 0) - 4) & 7],
            tmbattle.rotating_phase(blk, 0), _turn, _phase, int(_wrap), _aim,
            hit, actor))
    return True


def _burn_attack_boost(board, tile):
    """An ATTACK-UP special tile is spent on the battle it powered. 0xD30EE.

    KEY: THE THREE SPECIAL TILES ARE NOT SYMMETRIC. At `-BattleEnd` (0xD3104
    prints the string) the client reads the ability of the tile it just played
    on -- `[obj+0x179]`, written from the chosen tile at 0xC6FF9 and the same
    index 0xC703C hands the inherited ability to -- and zeroes it **only when
    it is exactly 1**:

        0xD30EE  mov al, [ebp + tile*156 + 0x368]
        0xD30F5  cmp al, 1
        0xD30FE  jne  0xD3104          ; 2 and 3 survive untouched
        0xD3100  mov byte [ebp+...], 0

    So ability 1 (the ATTACKER multiplier) fires ONCE and is gone, while
    ability 2 (the defender multiplier) and ability 3 (all eight arrows) stay
    on the card for the rest of the match. That asymmetry is the rule, not an
    accident: 1 only ever applies to the card's own attack, so a card that has
    attacked has used it.

    WARNING: The compare is against the WHOLE byte, so a chance block's high-nibble
    ability (0x10..0x50) never matches -- those are consumed by their own
    states (0xD249B / 0xD2526) instead.

    Called on every path out of the battle chain, and only when a battle
    actually happened: a placement that only flips never reaches -BattleEnd.
    """
    card = board.get(int(tile))
    if card is not None and int(card.ability) == 1:
        card.ability = 0
        return True
    return False


def _consume_tile_special(chan, index, tile):
    """`_tile_ability`, and the tile stops being a special the moment it pays.

    WARNING: THE DEAL ARRAY IS NOT THE BOARD -- the same law the chance blocks
    earned, applied to the other object that outlives its own reveal. The
    client keeps NOTHING in its object-code array after the opening animation
    (0xC1BCF / 0xC2E33 zero each entry as it is materialised); the truth lives
    in the tile record, and a placement overwrites it: 0xC7015 lifts the
    ability out, 0xB3200 builds the played card over the top, 0xC703C writes
    the ability onto the CARD and 0xC7098 replaces the special's `+0x1C` = 4
    with the owner. From then on the square is an ordinary occupied tile.

    Holding the code forever made this server answer "unplayed special" about
    a square with a card on it, which the scramble's candidate list reads
    (`_unplayed_specials`) -- and it would have handed the same ability out
    twice if a scramble ever lifted the card off again.
    """
    ability = _tile_ability(chan, index, tile)
    codes = _MATCH_OBJECTS.get((chan, index))
    if codes and 0 <= int(tile) < len(codes)             and int(codes[int(tile)]) in tmbattle.SPECIAL_CODES:
        codes[int(tile)] = 0
    return ability


def _tile_ability(chan, index, tile):
    """The ability a SPECIAL TILE hands the card played on it.

    The client's own placement does this in three instructions: 0xC7015 saves
    `[tile.card+0x24]`, 0xB3200 builds the played card over the top (zeroing
    it like every card constructor), 0xC703C writes it straight back. So the
    tile's ability outlives the placement and belongs to the new card, which
    is what makes a special tile do anything at all.
    """
    codes = _MATCH_OBJECTS.get((chan, index)) or []
    tile = int(tile)
    return tmbattle.special_ability(codes[tile]) if 0 <= tile < len(codes) else 0


#: THE COMBO RULE -- `POL_TM_COMBO`, and the 3-player freeze that pinned it.
#:
#: WARNING: MEASURED 2026-09-06T23:29Z (replayed turn by
#: turn in `_selftest_replay_3p`): seats 18/3/11 froze on turn 13 because the
#: CLIENTS' boards and this server's had diverged at turn 7 -- every client
#: ran `-BattleCheck` -> `-Battle:Select=1` -> `-BattleStart:8` on tile 5 and
#: parked for a `@BattleData` this server never sent, because on THEIR board
#: tile 11 was seat 1's (taken by the turn-8 combo) and on ours it was still
#: seat 0's. The rule the clients play, and the one this knob defaults to:
#:
#:   defeated  (default)  the chain runs off the DEFEATED card. Every card its
#:                        arrows point at that belongs to the LOSER changes
#:                        hands with it -- no battle, one level, whether or not
#:                        it points back (the client's post-battle wave is
#:                        state 0x23, flips only). On a WIN the defeated card
#:                        is the one on the TARGET tile; on a LOSS it is the
#:                        card you played. A third seat's card is never taken.
#:   placed               the pre-2026-09-07 model: re-contest from the PLACED
#:                        tile with its post-battle owner, taking anything not
#:                        the winner's. IDENTICAL to `defeated` in every
#:                        2-player LOSS (the placed card IS the defeated card,
#:                        and "not the winner's" IS the loser's), which is why
#:                        the 2026-08-22 loss measurement could not tell them
#:                        apart -- and WRONG on every WIN (the chain must run
#:                        off the card you TOOK, not the one you played) and in
#:                        every 3-player loss (a third seat's card is not the
#:                        loser's). Kept for an A/B; it reproduces the frozen
#:                        game's own server log to the tile.
#:
#: `POL_TM_LOSS_COMBO=0` still restores the stop-dead loss arm under either.
def _combo_mode():
    return (os.environ.get("POL_TM_COMBO") or "defeated").strip().lower()


def _combo_capture(board, tiles, n, defeated, loser, winner):
    """Chain one battle's outcome off the DEFEATED card. Returns the tiles taken.

    For each arrow of the card on `defeated`, the neighbour it points at is
    captured for `winner` when it is the LOSER's; anyone else's is left alone,
    and a card that points back is taken all the same (a combo is a flip, not
    a fight). One level: a card taken here does not chain further. A loser
    that is not a seat (a BLOCK, owner >= n) has no cards and chains nothing.
    `placed` mode returns [] and leaves the old arms to do their re-contest.
    """
    if _combo_mode() == "placed":
        return []
    took = []
    card = board.get(defeated)
    if card is None or loser is None or winner is None or loser == winner \
            or not 0 <= loser < n:
        return took
    for d in range(8):
        if not (card.arrows >> d) & 1:
            continue
        u = tmbattle.neighbours(tiles, defeated)[d]
        if u is None:
            continue
        occ = board.get(u)
        if occ is None or occ.owner != loser:
            continue
        if not tmbattle.flippable(occ):
            continue                 # the 0xD301B ceiling: a flip wave skips it
        occ.owner = winner
        took.append(u)
    return took
