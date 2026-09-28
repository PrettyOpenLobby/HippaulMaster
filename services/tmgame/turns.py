"""Turn flow: rosters, bots taking a departed seat, and announcing the next turn."""
import os
import random
from . import (
    boardrules, careerstats, collection, common, dispatch, matchend, matchmaking, matchstart,
    placement, pots, protocol, purse, pushqueue, scoring, tournament, vscom, watchers, webwatch,
)


def _roster_at(chan, index):
    """The announced roster of the match at this table, or []."""
    for v in matchmaking._MATCH_ROSTER.values():
        if v[0] == chan and v[1] == index:
            return list(v[2])
    return []


def _is_bot(chan, index, member_id):
    """Is this seat played by the server because its player left?"""
    return pushqueue._push_key(member_id) in (matchmaking._MATCH_BOTS.get((chan, index)) or set())


def _humans(chan, index, roster):
    return [(m, v) for m, v in roster if not _is_bot(chan, index, m)]


def _seat_departed(member_id, why):
    """A seated player is leaving; if their match is running, take the seat.

    Idempotent: `@Break=` and `@GameExit=` both arrive for one leaver."""
    if not common._env_int("POL_TM_BOT_SEATS", 1) or member_id is None:
        return
    chan, index, seats = matchmaking._match_of(member_id)
    if not seats or len(seats) < 2 or vscom._in_com_game(member_id):
        return
    key = (chan, index)
    seat = next((i for i, (m, _v) in enumerate(seats)
                 if pushqueue._push_key(m) == pushqueue._push_key(member_id)), None)
    if seat is None:
        return
    st = boardrules._MATCH_TURN.get(key) or {}
    dealt = st.get("turn") is not None
    if st.get("result_sent"):
        return                              # the game is over; nothing to play
    if not dealt and key not in matchstart._CARD_READY and key not in matchmaking._MATCH_STARTED:
        return                              # never started; the room path owns it
    bots = matchmaking._MATCH_BOTS.setdefault(key, set())
    if pushqueue._push_key(member_id) in bots:
        return
    if len(_humans(chan, index, seats)) <= 1:
        # The last human is leaving too: nothing is left to play for.
        return
    bots.add(pushqueue._push_key(member_id))
    n = len(seats)
    common._say("tm: 🤖 member %s (seat %d) LEFT %s table %d mid-match (%s) -- the "
         "server plays that seat from here on so %d other player(s) are not "
         "stranded on (0x43, 10)"
         % (member_id, seat, chan, index, why, len(seats) - len(bots)))
    webwatch._live_matches_write()
    if not dealt:
        # WARNING: A LEAVE DURING CARD SELECT WITH ONE HUMAN LEFT: TELL THEM.
        # Measured 2026-09-07T15:28Z: the Deck quit at card select, the
        # server dealt its seat a synthetic hand and the tester "received
        # no notification at all" -- a game against a nameless bot with the
        # take flow off. Card select is the one match scene that polls
        # (0x43, 40) @DataError, and on it the client shows "the match is
        # gone", sends @Quit= and leaves cleanly. Three or more humans keep
        # the bot path below. `POL_TM_CARDSELECT_LEAVE=bot` restores it.
        # REPORTED LIVE 2026-09-07 20:00Z: `dataerror` is NOT a clean exit -- its
        # dialog is "Invalid card data detected. Exiting Tetra Master." and it
        # throws the survivor OUT of the title (fired live 18:22Z, right after
        # a clean game's rematch vote). Default is `bot` again until the
        # client's own leave path (@GetAway announced after the pick)
        # is measured; `dataerror` stays selectable.
        _mode = (os.environ.get("POL_TM_CARDSELECT_LEAVE") or "bot") \
            .strip().lower()
        if _mode == "dataerror" and len(_humans(chan, index, seats)) <= 1:
            for _om, _ov in seats:
                if _is_bot(chan, index, _om):
                    continue
                pushqueue._queue_push(_om, protocol._turn_code(protocol.DATAERROR_CMD, 0)
                            + b"@DataError=/No=%d" % common._env_int("POL_TM_DATAERROR_NO", 0),
                            "the match is gone (a seat left during card select)",
                            after=0, peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(_om)))
                common._say("tm:   ...seat %d left during CARD SELECT and member %s is "
                     "the last human: pushing (0x43, %d) @DataError so its "
                     "card-select scene says the match is gone and leaves "
                     "(no synthetic deal; POL_TM_CARDSELECT_LEAVE=bot restores)"
                     % (seat, _om, protocol.DATAERROR_CMD))
            return
        if _mode == "getaway" and len(_humans(chan, index, seats)) <= 1:
            # THE CLIENT'S OWN LEAVE PATH (static, 0xC0AB0 read 2026-09-07):
            # a @GetAway received during card select marks the seat (arm
            # 0x104FF6, +0x1A7 = 1); the announcer runs only while the board
            # scene is null, from the state 8 -> 9 transition right after the
            # pick is accepted: "%s" + notice 0x4000041 ("<name> has left"),
            # the seat's hand zeroed, and [obj+0x16D] = 1 when one seat is
            # left -- whose ONLY two readers (0xC3FC0, 0xDA359) skip the turn
            # timer. So the match continues, timer off, and the empty seat is
            # played by the bot path below. /P= is ABSOLUTE (the arm rotates).
            # UNMEASURED LIVE; `bot` (silent) is the default until it is.
            for _om, _ov in seats:
                if _is_bot(chan, index, _om):
                    continue
                pushqueue._queue_push(_om, protocol._turn_code(protocol.GETAWAY_CMD, 0)
                            + b"@GetAway=/P=%d" % seat,
                            "seat %d left during card select (@GetAway)" % seat,
                            after=0, peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(_om)))
                common._say("tm:   ...seat %d left during CARD SELECT: pushing (0x43, "
                     "%d) @GetAway=/P=%d to member %s -- the client announces "
                     "'<name> has left' after its pick and turns the turn "
                     "timer off; the seat is played from here "
                     "(POL_TM_CARDSELECT_LEAVE=getaway)"
                     % (seat, protocol.GETAWAY_CMD, seat, _om))
        # Card select: give the seat a hand and let the deal fire through the
        # SAME arm the client's own pick takes (state, pushes, first turn).
        hands = boardrules._MATCH_HANDS.setdefault(key, {})
        if not hands.get(pushqueue._push_key(member_id)):
            rows = vscom._com_deck_rows(seat)
            hands[pushqueue._push_key(member_id)] = list(rows)
            hands[("deck", seat)] = list(rows)
            matchmaking._BOT_SYNTH.add(key)
            common._say("tm:   ...seat %d had not chosen yet: dealt it a synthetic "
                 "hand (%s); the take flow is OFF for this match"
                 % (seat, ", ".join(r.split(b"|")[0].decode() for r in rows)))
        if pushqueue._push_key(member_id) not in (matchstart._CARD_READY.get(key) or set()):
            try:
                dispatch._handle_line(protocol.encode_code(protocol.IN_MATCH_CODE | (protocol.CARDSELECT_SLOT << 16))
                             + b"@CardSelect=/C=0", peer="bot",
                             member_id=member_id)
            except Exception as exc:
                common._say("tm:   ...synthetic @CardSelect= for seat %d failed (%r)"
                     % (seat, exc))
        st = boardrules._MATCH_TURN.get(key) or {}
        if (st.get("turn") is not None and st.get("active") == seat
                and st.get("await_ack") is None):
            # The deal announces turn 0 inline (not via `_queue_next_turn`),
            # so when the departed seat was drawn to start, arm the gate here:
            # the human's turn-0 ack releases the server's put.
            st["await_ack"] = int(st.get("turn") or 0)
            st["bot_seat"] = seat
            common._say("tm:   ...the departed seat starts: holding its card for a "
                 "human @TurnData= ack")
        return
    pend = placement._PENDING_BATTLE.get(key)
    if pend and pend.get("actor") == seat:
        pend["bot"] = True
        common._say("tm:   ...seat %d was parked on its own @BattleSelect -- "
             "auto-selecting" % seat)
        placement._advance_placement(chan, index)
        return
    if st.get("active") == seat and st.get("await_ack") is None:
        _bot_move(chan, index, int(st.get("turn") or 0), seat)


def _bot_move(chan, index, turn, active, direct_to=None):
    """Play the departed seat `active` on `turn`.

    Returns the `@PutCard=` body framed for `direct_to` (the acker, whose
    reply carries it -- `_com_play_acked`'s shape) or None. Everyone else
    gets the put on their next reply, battles one reply later, and the next
    turn behind those -- the human `@PutData=` path, seat for seat."""
    key = (chan, index)
    seats = _roster_at(chan, index)
    n = len(seats)
    st = boardrules._MATCH_TURN.setdefault(key, {})
    if not seats or not 0 <= active < n:
        return None
    if st.get("bot_played") == turn:
        return None                          # a second acker; already played
    st["bot_played"] = turn
    mid_bot = seats[active][0]
    hands = boardrules._MATCH_HANDS.setdefault(key, {})
    hand = hands.get(pushqueue._push_key(mid_bot)) or []
    board = boardrules._MATCH_BOARD.get(key) or {}
    tiles = boardrules._board_tiles(n)
    empty = [t for t in range(tiles) if t not in board]
    limit = common._env_int("POL_TM_TURN_LIMIT", 5 * n)
    if not hand or not empty:
        common._say("tm: 🤖 seat %d has %d card(s) and %d empty tile(s) on turn %d "
             "-- passing the turn" % (active, len(hand), len(empty), turn))
        _queue_next_turn(chan, index, seats, min(turn + 1, limit),
                         (active + 1) % n, roster=seats)
        return None
    rnd_moves = random.Random()
    hand_idx = rnd_moves.randrange(len(hand))
    row = hand.pop(hand_idx)
    tile = rnd_moves.choice(empty)
    st["turn"], st["active"], st["n"] = turn, active, n
    common._say("tm: 🤖 departed seat %d (turn %d) plays hand slot %d -- card %s -- "
         "onto tile %d%s. Watch for '---->Recv=PUTCARD' on the other client(s)."
         % (active, turn, hand_idx, row.split(b"|")[0].decode(), tile,
            "" if direct_to is None else " (answering member %s's ack directly)"
            % direct_to))
    direct = None
    for mid, _v in seats:
        if _is_bot(chan, index, mid):
            continue
        seat = next((i for i, (m, _x) in enumerate(seats)
                     if pushqueue._push_key(m) == pushqueue._push_key(mid)), 0)
        body = (protocol._turn_code(protocol.PUTCARD_CMD, turn)
                + boardrules._putcard_body(seats, seat, active, hand_idx, tile, row))
        if direct_to is not None and pushqueue._push_key(mid) == pushqueue._push_key(direct_to):
            direct = body
            continue
        pushqueue._queue_push(mid, body, "the departed seat's card on tile %d" % tile,
                    after=common._env_int("POL_TM_PUTCARD_GAP", 0),
                    peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
    watchers._watch_push(chan, index,
                protocol._turn_code(protocol.PUTCARD_CMD, turn)
                + boardrules._putcard_body(seats, 0, active, hand_idx, tile, row),
                "the departed seat's card on tile %d" % tile)
    if common._env_int("POL_TM_BATTLE", 1):
        if placement._begin_placement(chan, index, n, active, tile, row, turn, seats,
                            None, mid_bot, seats):
            placement._PENDING_BATTLE[key]["bot"] = True
            placement._advance_placement(chan, index)
    elif common._env_int("POL_TM_TURNDATA", 1):
        _queue_next_turn(chan, index, seats, turn + 1, (active + 1) % n,
                         roster=seats)
    return direct


def _queue_next_turn(chan, index, seats, turn, active, roster=None, n_solo=None,
                     after=None):
    """Announce turn `turn` to everyone in the match, per recipient.

    WARNING: THE TURN NUMBER IS NOT OURS TO PICK. The client counts it itself at the
    end of every turn (0xC76A2 `inc al` into [obj+0x18D]) and then re-polls
    command 9; `@TurnData` OVERWRITES that counter with our third envelope byte,
    and the next `@PutCard` is compared against it. Send the value the client
    has already reached -- that is `previous + 1` -- or the placement after it
    is refused with `Errot!!Turn%d!=%d`.

    The match ends after `5 * N` turns (0xC7701), so there is nothing to
    announce past that: the client prints `End` and moves to the result scene
    instead of polling 9 again.

    `roster`/`n_solo` are the VS. COM frame: `seats` is EMPTY in a COM game
    (the sixth and seventh seat gates lived in this function -- turn 2+ and
    the result were never announced to a COM game at all), so the caller
    passes who to actually push to (the one human) and the real player count.
    The math stays in `n`; the loops walk `roster`. `after` overrides the
    queue delay -- the COM turn chain needs strictly increasing values so its
    messages arrive in order, one per reply.
    """
    n = len(seats) or int(n_solo or 2)
    roster = list(seats) or list(roster or [])
    limit = common._env_int("POL_TM_TURN_LIMIT", 5 * n)
    if turn >= limit:
        # THE MATCH IS OVER, AND THE CLIENT ASKS FOR ONE MORE THING. 0xC7701
        # prints `End` instead of `Next` here and the post-game scene polls
        # (0x43, 13); measured live, both clients sat on that poll for ever.
        common._say("tm:   ...that was the last turn (%d of %d = 5 x %d players): the "
             "client prints 'End' and polls (0x43, %d) for @ResultData"
             % (turn, limit, n, protocol.RESULTDATA_CMD))
        # ONCE PER MATCH. This branch is reached off the final @PutData, and a
        # re-sent placement must not double-count a game, re-tally a lucky
        # card, or re-push the result. `_remember_match` pops this key, so a
        # new match at the same table re-arms it.
        st = boardrules._MATCH_TURN.setdefault((chan, index), {})
        if st.get("result_sent"):
            return
        st["result_sent"] = True
        # THE MATCH IS FINAL HERE, AND THE BOARD IS STILL IN MEMORY. Tally
        # before the result goes out: `_MATCH_BOARD` is popped when the table
        # breaks up, and a lucky card counted after that is counted never.
        scoring._tally_lucky(chan, index, roster, n=n)
        if not common._env_int("POL_TM_RESULTDATA", 1):
            return
        rnd = matchstart._turn_rand((chan, index))
        scores = scoring._board_scores(chan, index, n)
        tournament._event_score_game(roster, scores, chan, index)
        stats = {}
        # WHERE EACH SEAT FINISHED, which is the only input `Average Rank` has.
        # Computed over the FULL score vector, not the roster, so a COM game
        # ranks the human against the machine rather than against nobody.
        places = matchend._placements(scores) if scores else []
        # ...AND THE ELO VS. RATING (tmrank.elo_match), from everyone's
        # PRE-match rating, so it is worked out before any seat is written.
        elos = careerstats._match_elos(roster, scores, n, n_solo)
        for i, (mid, _v) in enumerate(roster):
            stats[pushqueue._push_key(mid)] = careerstats._bump_result_stats(
                mid, scores[i] if i < len(scores) else 0,
                place=places[i] if i < len(places) else None,
                elo=elos.get(i),
                # `n`, not len(roster): a COM game has one human on the roster
                # and the client still counts the machine as an opponent
                # (0xCC79D reads the PLAYER COUNT, which is what /N= carried).
                opponents=max(0, n - 1),
                # This seat's biggest chain this match, keyed by the ABSOLUTE
                # seat -- `_note_combo` records against the actor the placement
                # names, which is the same numbering `roster` is indexed by.
                combo=(scoring._MATCH_COMBO.get((chan, index)) or {}).get(i, 0))
            # ...AND HAND THEM TO THE CLIENT. The stats above land in the
            # collection JSON; the save is what Player Data -> Status reads,
            # and until 2026-08-24 nothing carried them across.
            careerstats._save_sync_stats(mid)
            # ...AND GROW THE CARDS THIS SEAT USED (guidebook: "the cards you use
            # will grow too"). The ("deck", seat) copy is the full five that
            # survives play (the per-member hand is popped as cards land); seat i
            # is this roster index. A COM seat owns no collection, so it no-ops.
            careerstats._grow_used_cards(
                mid, (boardrules._MATCH_HANDS.get((chan, index)) or {}).get(("deck", i)))
        # THE WAGER SETTLES HERE (the wager money -- see the @ComGame= handler
        # for the client-side map), BEFORE the result bodies are built: the
        # settled net gain rides `/D=` occ 1 (`_resultdata_body`'s prize
        # slot), and until 2026-09-02 the settlement ran AFTER the pushes,
        # so the result screen's wager row read the 0 we served -- the
        # wallet moved, the screen did not (the stats bug one message
        # over). A COM win pays the retail house prize (`_house_prize`; the
        # older "the pot doubles and pays out" was a misread of the Double Up
        # rematch rule); a loss keeps the stake; a draw returns it. Settled
        # once: `staked` is cleared, and @GameExit refunds only what is still
        # staked.
        prizes = {}
        if n_solo and roster and common._env_int("POL_TM_HOUSE_PRIZE", 1):
            # THE HOUSE PRIZE (`_house_prize`): every decisive win pays,
            # whatever the stake -- a 0-gil player's stake-0 win over an
            # opponent of average rank 2.93 is 153. The stake itself was taken at @ComGame= and is not
            # handed back on a win (the client does not hand it back either).
            _wmid = roster[0][0]
            _wentry = vscom._COM_GAME.get(pushqueue._push_key(_wmid)) or {}
            _wstk = int(_wentry.get("staked") or 0)                 if pots._com_wager_enabled() else 0
            _wcoms = list(_wentry.get("coms") or [])
            _wranks = [vscom._com_avg_rank(_wcoms[s - 1] if s - 1 < len(_wcoms)
                                     else None)
                       for s in range(1, len(scores))]
            _pay = vscom._house_prize(_wstk, _wranks, scores, 0)
            if scores and max(scores) == min(scores):
                if _pay:
                    purse._set_money(_wmid, purse.money_of(_wmid) + _pay,
                               "VS. COM draw -- stake returned")
            elif _pay > 0:
                purse._set_money(_wmid, purse.money_of(_wmid) + _pay,
                           "VS. COM WIN -- house prize %d (stake %d, COM "
                           "average rank %s, scores %s)"
                           % (_pay, _wstk, "/".join(str(r) for r in _wranks),
                              "/".join(str(v) for v in scores)))
                matchend._bump_prize(_wmid, _pay, "VS. COM win")
            else:
                common._say("tm:   ...VS. COM LOSS -- no prize; the %d stake is "
                     "forfeit" % _wstk)
            # /D= occ 1 is what the client's count-up adds to its wallet, so
            # it carries exactly what the server credited: prize or refund.
            prizes[pushqueue._push_key(_wmid)] = _pay
            if _wentry.get("staked") is not None:
                _wentry["staked"] = None
                pots._stake_clear(_wmid)
        elif n_solo and pots._com_wager_enabled():
            _wentry = vscom._COM_GAME.get(pushqueue._push_key(roster[0][0])) if roster else None
            _wstk = (_wentry or {}).get("staked")
            if _wstk:
                _wwin = max(range(len(scores)), key=lambda i: scores[i]) \
                    if scores else 0
                _wlose = min(range(len(scores)), key=lambda i: scores[i]) \
                    if scores else 0
                _wmid = roster[0][0]
                _wcap = common._env_int("POL_TM_WAGER_CAP", 2000)
                if scores and scores[_wwin] == scores[_wlose]:
                    purse._set_money(_wmid, purse.money_of(_wmid) + _wstk,
                               "VS. COM draw -- stake returned")
                elif _wwin == 0:
                    _pay = min(2 * _wstk, _wcap)
                    purse._set_money(_wmid, purse.money_of(_wmid) + _pay,
                               "VS. COM WIN -- pot doubled to %d (cap %d) "
                               "and paid out; net +%d"
                               % (_pay, _wcap, _pay - _wstk))
                    matchend._bump_prize(_wmid, _pay - _wstk, "VS. COM win")
                    prizes[pushqueue._push_key(_wmid)] = _pay - _wstk
                else:
                    common._say("tm:   ...VS. COM LOSS -- the %d stake is forfeit "
                         "(the client's own pot is not repaid on a loss)"
                         % _wstk)
                _wentry["staked"] = None
                pots._stake_clear(_wmid)
        # ...AND THE PvP POT. Escrow filled at the accept quorum
        # (`_queue_vsgameinit`): a UNIQUE top score takes every stake; a tie
        # at the top refunds each player their own. Settled once (the escrow
        # is popped); the winner's NET gain feeds the prize tally the
        # ranking money lists sort on.
        if not n_solo and common._env_int("POL_TM_WAGER", 1) \
                and common._env_int("POL_TM_WAGER_PVP", 1):
            _pstakes = pots._PVP_STAKES.pop((chan, index), None)
            if _pstakes:
                # A departed seat's stake stays in the pot and cannot win it:
                # the top score is taken over the seats still playing.
                _hs = [i for i in range(len(scores))
                       if not (i < len(roster)
                               and _is_bot(chan, index, roster[i][0]))] \
                    or list(range(len(scores)))
                _ptop = max(scores[i] for i in _hs) if scores else 0
                _pwinners = [i for i in _hs if scores[i] == _ptop]
                _pot = sum(a for _m, a in _pstakes.values())
                if len(_pwinners) == 1 and _pot > 0:
                    _pw = _pwinners[0]
                    _pwmid = roster[_pw][0] if _pw < len(roster) else None
                    _pwkey = pushqueue._push_key(_pwmid)
                    _own = (_pstakes.get(_pwkey) or (None, 0))[1]
                    if _pwmid is not None:
                        purse._set_money(_pwmid, purse.money_of(_pwmid) + _pot,
                                   "PvP WIN -- takes the %d pot (own stake "
                                   "%d back + %d winnings)"
                                   % (_pot, _own, _pot - _own))
                        matchend._bump_prize(_pwmid, _pot - _own, "PvP win")
                        prizes[_pwkey] = _pot - _own
                    for _k2, (_m2, _a2) in _pstakes.items():
                        pots._stake_clear(_m2)
                        if _k2 != _pwkey:
                            common._say("tm:   ...PvP stake of %d forfeited by "
                                 "member %s" % (_a2, _m2))
                else:
                    for _m2, _a2 in _pstakes.values():
                        if _is_bot(chan, index, _m2):
                            pots._stake_clear(_m2)
                            common._say("tm:   ...PvP stake of %d forfeited by member "
                                 "%s (left the match)" % (_a2, _m2))
                            continue
                        purse._set_money(_m2, purse.money_of(_m2) + _a2,
                                   "PvP draw/tie -- stake returned")
                        pots._stake_clear(_m2)
        # WARNING: `roster`, never `seats`: `_resultdata_body` answers `/P=0` for an
        # empty seat list, and /P=0 is the MEASURED divide-by-zero result-screen
        # crash (0x10F6A5). A COM game gets /P=1 -- only the human's own @No0
        # section, which is the one that carries the divisor.
        _a0 = after if after is not None else common._env_int("POL_TM_RESULT_GAP", 1)
        watchers._watch_push(chan, index,
                    protocol._turn_code(protocol.RESULTDATA_CMD, turn)
                    + matchend._resultdata_body(rnd, roster, 0, scores, stats, prizes),
                    "the result", after=_a0)
        for me, (mid, _v) in enumerate(roster):
            if _is_bot(chan, index, mid):
                continue                # a departed seat reads nothing
            pushqueue._queue_push(mid,
                        protocol._turn_code(protocol.RESULTDATA_CMD, turn)
                        + matchend._resultdata_body(rnd, roster, me, scores, stats,
                                           prizes),
                        "the result",
                        after=_a0,
                        peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
            common._say("tm:   ...result: (0x43, %d) @ResultData= /P=%d to member %s "
                 "(games divisor %d -- 0x10F6A5 divides by it, never 0). Watch "
                 "for '---->Recv=RESULTINFO' and a result screen that LIVES."
                 % (protocol.RESULTDATA_CMD, len(roster),
                    mid, stats.get(pushqueue._push_key(mid), (1, 0))[0]))
        # A MATCH THAT ENDED WITH WATCHERS PRESENT: the players' post-game
        # screen polls (0x43, 36) @Change -- the player-replacement
        # negotiation an observer's channel JOIN arms (measured live
        # 2026-08-22T21:15Z: both players parked on 36+26 after a draw, the
        # first game ever observed). The arm (0x1050A2) sets its success
        # flag on EVERY path, own-seat /P= included, so a no-op
        # @Change=/P=0 (own seat in each recipient's frame) releases the
        # wait without swapping anyone. POL_TM_CHANGE_RELEASE=0 disables.
        if watchers._watchers_of(chan, index) and common._env_int("POL_TM_CHANGE_RELEASE", 1):
            for mid, _v in roster:
                pushqueue._queue_push(mid,
                            protocol._turn_code(protocol.CHANGE_CMD, turn) + b"@Change=/P=0/C=0",
                            "post-game @Change release (watchers present)",
                            after=_a0 + 4,
                            peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
            common._say("tm:   ...watchers present: pushing the no-op @Change "
                 "release to %d player(s)" % len(roster))
        # THE TAKE FLOW, decisive games only. Measured 16:49Z: a WINNER's
        # post-@Ready screen polls (0x43,26) @TCList and (0x43,35) @GetAway
        # and hangs without them; a DRAW polls neither and flows to @Continue
        # by itself. /P= is refused when it names the recipient's own seat
        # (0x105060), so it carries the seat being taken FROM -- the loser.
        # The pools are the ("deck", seat) copies kept at @CardSelect time.
        # PvP now included (POL_TM_TAKE_PVP=0 restores the COM-only flow --
        # a decisive PvP game used to park on exactly these polls): the same
        # messages go to every roster member, and the winner's @GetSelect
        # transfers between two REAL collections. POL_TM_TCLIST=0 disables.
        _take_pvp = (not n_solo and roster and len(roster) > 1
                     and common._env_int("POL_TM_TAKE_PVP", 1)
                     and (chan, index) not in matchmaking._BOT_SYNTH)
        # A TOURNAMENT GAME MOVES NO CARDS:
        # no take list, no perfect-win transfer, no take for a @GetSelect= to
        # commit against. It used to run the PvP take here, and a PERFECT
        # win moved the loser's five cards server-side (2026-09-26).
        if _take_pvp and tournament._is_event_match([m for m, _v in roster]):
            _take_pvp = False
            common._say("tm:   ...no take flow: a tournament game moves no cards")
        if not n_solo and (chan, index) in matchmaking._BOT_SYNTH:
            common._say("tm:   ...no take flow: a departed seat played a SYNTHETIC "
                 "hand, so no card it never owned may change hands")
        if common._env_int("POL_TM_TCLIST", 1) and (n_solo or _take_pvp):
            win = max(range(len(scores)), key=lambda i: scores[i]) \
                if scores else 0
            lose = min(range(len(scores)), key=lambda i: scores[i]) \
                if scores else 0
            _bot_win = (not n_solo and win < len(roster)
                        and _is_bot(chan, index, roster[win][0]))
            if _bot_win:
                common._say("tm:   ...no take flow: the winning seat %d LEFT the "
                     "match, and a departed seat takes nothing" % win)
            if scores and scores[win] != scores[lose] and not _bot_win:
                hands = boardrules._MATCH_HANDS.get((chan, index)) or {}
                pools = [hands.get(("deck", s2)) or [] for s2 in range(n)]
                if all(pools):
                    # The pick needs this after the board is gone: who won,
                    # who lost, every seat's cards by slot, and (for PvP)
                    # WHICH MEMBER sits in each seat -- the loser's card
                    # leaves a real collection. `@GetSelect=` and the COM's
                    # own pick (below) both commit against it, once per
                    # losing seat -- a real transfer, no longer display
                    # only.
                    st["take"] = {"win": win, "lose": lose,
                                  "pools": [list(p) for p in pools],
                                  "roster": [m for m, _v in roster]}
                    # A PERFECT WIN (the winner owns every owned card on the
                    # board -- the client's own test, see `_is_perfect_win`)
                    # has no take-select scene: both clients move EVERY loser
                    # pool card to the winner by themselves, and no
                    # `@GetSelect=` is sent or polled. POL_TM_PERFECT_TAKE=0
                    # restores the one-pick-per-loser flow.
                    _perfect = bool(common._env_int("POL_TM_PERFECT_TAKE", 1)
                                    and scoring._is_perfect_win(scores, win))
                    ga = (protocol._turn_code(protocol.GETAWAY_CMD, turn)
                          + b"@GetAway=/P=%d" % lose)
                    # WHO GETS @GetAway. The arm (0x104FF6, read 2026-09-07)
                    # rotates /P= itself, then MARKS that seat's byte +0x1A7
                    # -- the byte the "Play again?" ctor (0x10B7AB) presets to
                    # DECLINED -- and refuses to mark the recipient's OWN seat
                    # unless its +0x32 == 4. So every recipient that accepts
                    # it sees the loser as "not playing again"; the loser's
                    # own copy is the auto-decline of 2026-09-03 (41d/41e).
                    # `POL_TM_GETAWAY_TO=winner` sends it to the winning seat
                    # only (an A/B: does the loser's take scene need it at
                    # all?); default `all` is the measured-working take.
                    # DEFAULT `none` since 2026-09-07T15:13Z: the winner's take
                    # screen opens and picks without it (measured), and to a
                    # recipient it means "seat P LEFT".
                    _ga_to = (os.environ.get("POL_TM_GETAWAY_TO") or "none").strip().lower()
                    for _si, (mid, _v) in enumerate(roster):
                        if _is_bot(chan, index, mid):
                            continue
                        pk = matchmaking._MATCH_PEER.get(pushqueue._push_key(mid))
                        # THE TAKE LIST IN THE RECIPIENT'S OWN FRAME (41c).
                        _fp = matchend._take_frame(pools, _si)
                        tcl = (protocol._turn_code(protocol.TCLIST_CMD, turn)
                               + matchend._tclist_body(_fp, matchend._take_occ0(_fp, _si, win)))
                        pushqueue._queue_push(mid, tcl, "the take list (@TCList)",
                                    after=_a0 + 1, peer=pk)
                        if _ga_to == "none" or (_ga_to == "winner"
                                                 and _si != win):
                            # WARNING: @GetAway IS THE "PLAYER LEFT" NOTICE, not a take
                            # message (2026-09-07 14:59Z): sent to the winner it
                            # showed "Deck left the game", emptied the take
                            # ("NEXT ENEMY", no @GetSelect) and auto-DECLINED
                            # the panel; the loser's scene ran without it.
                            common._say("tm:   ...@GetAway withheld from seat %d "
                                 "(POL_TM_GETAWAY_TO=%s)" % (_si, _ga_to))
                            continue
                        pushqueue._queue_push(mid, ga, "the take seat (@GetAway)",
                                    after=_a0 + 2, peer=pk)
                    common._say("tm:   ...take flow: @TCList (%s, rotated per "
                         "recipient) + @GetAway=/P=%d to %s. Watch for "
                         "'---->Recv=TRADELIST'."
                         % ("|".join(str(len(p)) for p in pools), lose, _ga_to))
                    if _perfect:
                        # COMMITTED HERE, AT RESULT TIME: this block runs once
                        # per match (`result_sent`), the pools are in hand, and
                        # nothing depends on a client message that a perfect
                        # win does not send -- the winner's @Ready= never
                        # reaches the take logic in a COM game, and a winner
                        # that drops after the result still has the cards its
                        # client already moved. A COM winner's pick is NOT
                        # pushed: the loser's client does not poll (0x43, 15)
                        # on a perfect, so it would only sit in the queue.
                        common._say("tm:   ...PERFECT WIN by seat %d (%s of %d owned "
                             "tiles): no take-select scene, every loser pool "
                             "card goes to the winner"
                             % (win, scores[win], sum(scores)))
                        scoring._perfect_take(st["take"])
                    elif n_solo and win != 0 and common._env_int("POL_TM_TAKE_COM", 1):
                        # THE COM WON: the picks are the server's to make --
                        # the loser's screen polls (0x43, 15) (0xCA481) and
                        # parks without them. ONE PICK PER LOSING SEAT
                        # (mirror of the human-win rule, measured off the
                        # first 3-player win), random slot each, in seat
                        # order; sh is the picker's SEAT (0x107C6F). The
                        # human's card (seat 0 lost) leaves the collection
                        # here -- the push is the commitment.
                        _lsrs = [s2 for s2 in range(n)
                                 if s2 != win and pools[s2]]
                        _gap = _a0 + 3
                        for _sL in _lsrs:
                            slot = random.randrange(len(pools[_sL]))
                            gsel = (protocol._turn_code(protocol.GETSELECT_CMD, win)
                                    + b"@GetSelect=/E=%d/H=%d"
                                    % (common._env_int("POL_TM_GETSEL_E", 0), slot))
                            for mid, _v in roster:
                                pushqueue._queue_push(mid, gsel,
                                            "the COM's pick from seat %d"
                                            % _sL,
                                            after=_gap,
                                            peer=matchmaking._MATCH_PEER.get(
                                                pushqueue._push_key(mid)))
                            _gap += 1
                            common._say("tm:   ...THE COM (seat %d) WON -- picking "
                                 "seat %d's slot %d: card %s"
                                 % (win, _sL, slot,
                                    pools[_sL][slot].split(b"|")[0].decode()))
                            if _sL == 0 and common._env_int("POL_TM_TAKE_LOSS", 1):
                                row = pools[_sL][slot]
                                for mid, _v in roster:
                                    collection._collection_remove_card(
                                        mid, int(row.split(b"|")[0]),
                                        row=row)
                        st["take"]["taken"] = True
                else:
                    common._say("tm:   ...no take flow: deck copies missing for %s"
                         % [s2 for s2 in range(n) if not pools[s2]])
        return
    st = boardrules._MATCH_TURN.setdefault((chan, index), {})
    st["turn"], st["active"], st["n"] = turn, active, n
    rnd = matchstart._turn_rand((chan, index))          # SAME table all match -- see _TURN_RAND
    # /S= is the SCORE (board tile count, recipient-rotated) -- see
    # `_turndata_body` for the measurement. Same knob as the COM chain.
    _scores = (scoring._board_scores(chan, index, n)
               if common._env_int("POL_TM_TURN_SCORES", 1) == 1 else None)
    watchers._watch_push(chan, index,
                protocol._turn_code(protocol.TURNDATA_CMD, turn)
                + matchstart._turndata_body(seats, 0, active, rnd, scores=_scores,
                                 n_solo=n_solo),
                "turn %d" % turn,
                after=(after if after is not None
                       else common._env_int("POL_TM_TURNDATA_GAP", 1)))
    for mid, _v in roster:
        if _is_bot(chan, index, mid):
            continue                    # a departed seat reads nothing
        seat = next((i for i, (m, _x) in enumerate(roster)
                     if pushqueue._push_key(m) == pushqueue._push_key(mid)), 0)
        common._say("tm:   ...turn %d: (0x43, %d) @TurnData= to member %s (seat %d, "
             "/A=%d). Watch for '---->Recv=TURNINFO' then "
             "'GamePlayLink:Turn=%d,Active=%d'."
             % (turn, protocol.TURNDATA_CMD, mid, seat, (active - seat) % n,
                turn, (active - seat) % n))
        _rot = (_scores[seat:] + _scores[:seat]) if _scores else None
        pushqueue._queue_push(mid,
                    protocol._turn_code(protocol.TURNDATA_CMD, turn)
                    + matchstart._turndata_body(seats, seat, active, rnd, scores=_rot,
                                     n_solo=n_solo),
                    "turn %d" % turn,
                    after=(after if after is not None
                           else common._env_int("POL_TM_TURNDATA_GAP", 1)),
                    peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
    # A DEPARTED SEAT'S TURN: the server plays it. Ack-gated like the COM
    # chain (the GamePlayEnd purge race, see `_queue_com_turns`): the first
    # human `@TurnData=` ack for this turn releases the put as its direct
    # reply (`_bot_move` via the ack arm). `POL_TM_COM_ACK_GATE=0` plays it
    # straight away instead.
    if (not n_solo and roster and active < len(roster)
            and _is_bot(chan, index, roster[active][0])):
        if common._env_int("POL_TM_COM_ACK_GATE", 1):
            st["await_ack"] = turn
            st["bot_seat"] = active
            common._say("tm:   ...turn %d belongs to departed seat %d -- holding its "
                 "card for a human @TurnData= ack" % (turn, active))
        else:
            _bot_move(chan, index, turn, active)
