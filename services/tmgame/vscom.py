"""Playing the computer: COM tables and decks (PlPrm), COM turns, @ComList/@ComGameInit, @GameECA
and the COM match.
"""
import os
import random
import tmbattle
import struct
import time
from . import (
    boardrules, cardshop, cardtables, common, deps, matchmaking, matchstart, placement, pots,
    protocol, purse, pushqueue, ruleset, scoring, seating, tableaudit, tablerow, tablesettings,
    tournament, turns, watchers, webwatch,
)


#: push_key -> {"n": total players, "coms": [character indexes], "peer": nick}.
#:
#: THE VS. COM ROSTER, AND THE CLIENT TELLS US IT EXACTLY ONCE. The COM
#: character-select screen sends (live 2026-08-22T13:57:26Z)
#:
#:     @ComGame=/Rule=100|0|1|1|1|0|0|0/Com=1|2       (0x43, msgid 6)
#:
#: where `/Com=` is a PIPE LIST of the chosen COM character indexes -- built by
#: the client at 0x107239 from [obj+0xE5] (count) over the per-entry table at
#: [obj+0x1CA]. One entry per opponent, so the PLAYER COUNT of the match is
#: `1 + len(/Com=)`: `/Com=2` = 2-player, `/Com=1|2` = 3-player. Discarding it
#: hardcoded every COM game to N=2 -- a 3-player game got `/Ans=1|1` and
#: `/L=0|0` ("1 of 2 have chosen" on a three-seat board, log 13:57:58Z).
_COM_GAME = {}


def _com_n(member_id):
    """Player count of this member's VS. COM game, or None outside one."""
    got = _COM_GAME.get(pushqueue._push_key(member_id))
    return got.get("n") if got else None


#: (chan, table index) -> push_key of the member playing the COM there.
#:
#: VERIFIED: A VS. COM GAME IS AT A REAL TABLE, AND THE CLIENT NAMES IT -- measured
#: live 2026-08-25 on prod. `@GameENC=` carries no table field, so the COM
#: path never looked for one; but it ARRIVES ON A TABLE PEER, and
#: `_peer_table` decodes that exactly (peer `UGRAWGL2Q` -> `#TM0R001` table 2,
#: `UGRAWGI78` -> table 3; three sessions, perfect correlation). The client
#: then PARTs the room and `JOIN`s that table's own channel `#TM0T002` --
#: byte for byte the observer's own flow.
#:
#: So the room's tile CAN say "Playing" for a COM game and the Observe menu
#: CAN reach it; nothing was missing but this binding. Two consumers:
#:
#:   * `_match_running` -- the tile's state-2 ("Playing"/Observe) truth, with
#:     `_com_who` supplying the occupancy the row needs to render it;
#:   * the `@Data=/Watch=` arm -- a watcher of a COM table is registered under
#:     the COM MATCH KEY `(None, push_key)`, which is what every COM
#:     `_watch_push` call already uses, so the relays flow unchanged.
#:
#: WARNING: THIS MUST NOT OUTLIVE THE GAME. Cleared on `@GameExit=`, on the
#: next `@GameENC=` from the same member (a rematch rebinds), and by
#: `expire_com_tables` when the COM player's table-peer heartbeat goes silent
#: -- the same signal, TTL and first-sight grace as a reservation.
_COM_AT = {}


def _in_com_game(member_id):
    """Is this member INSIDE a VS. COM game right now?

    WARNING: THE QUESTION `seats` CANNOT ANSWER, and getting it from `seats` is what
    made a COM game unplayable. `_match_of` reads `_MATCH_ROSTER`, which
    SURVIVES the PART into a match on purpose (it is the only thing that still
    knows the roster once the room is gone) -- so a player who RESERVES a table
    and then walks into VS. COM instead arrives at the COM handlers with a
    one-entry `seats` list, and every `None if seats else _com_n(...)` in this
    file reads their COM game as a one-player PvP match.

    Measured live 2026-08-25 (member 6): `@GameEN=` 04:04:05Z reserved table 1,
    `@GameENC=` 04:04:25Z started the COM game at table 2. Consequences, all
    from that one stale seat list:
      * `n_eff = len(seats) = 1` -- "1 of 1 have chosen", and the `if com_n:`
        arm that DEALS THE COM ITS CARDS never ran;
      * `_queue_next_turn` instead of `_queue_com_turns`, with n = 1, so
        `(actor + 1) % 1` is 0 for ever: the COM was announced the human's turn
        every time and never played a card (reported live: "it just skips");
      * the match state keyed by the stale PvP `(chan, index)` while the
        watchers sat under the COM key, so the deal's sh=1 @WatchInfo refresh
        went to a key with no watchers and the observer stayed parked on
        "players are selecting their cards".

    `_COM_AT` is the honest answer: bound at `@GameENC=`, released on the
    room-return `@GameExit=` and by the heartbeat TTL, so it is true for
    exactly the life of the game. `_COM_GAME` is NOT usable here -- nothing
    pops it, so it would go on claiming a later PvP match is a COM game.
    """
    return _com_table_of(member_id)[1] is not None


def _com_table_of(member_id):
    """(chan, index) of this member's VS. COM game, or (None, None)."""
    got = _COM_GAME.get(pushqueue._push_key(member_id)) or {}
    tbl = got.get("table")
    return tbl if tbl else (None, None)


def _com_key_at(chan, index):
    """The COM match key `(None, push_key)` for a game at this table, else None.

    The key, not the member: it is what `_MATCH_BOARD`/`_MATCH_TURN`/
    `_MATCH_HANDS` and every COM `_watch_push` are already keyed by.
    """
    try:
        key = _COM_AT.get((chan, int(index)))
    except (TypeError, ValueError):
        return None
    return (None, key) if key is not None else None


def _com_who(chan, index):
    """`[(member_id, ident)]` for the COM player at this table, else [].

    The tile's occupancy list, in the shape `_occupancy_block` wants. `ident`
    is the `/NN=` off that member's own `@GameENC=` -- the client compares
    block[14..29] against its OWN guid, so a fabricated one is worse than none
    (see `_occupancy_block`).
    """
    try:
        key = _COM_AT.get((chan, int(index)))
    except (TypeError, ValueError):
        return []
    got = _COM_GAME.get(key) or {}
    mid = got.get("member")
    if mid is None:
        return []
    return [(mid, got.get("ident") or 0)]


def _bind_com_table(member_id, peer_nick, ident=None):
    """Bind this member's VS. COM game to the table its peer names, and publish
    that row as Playing so the room can SEE it -- and observe it.

    Returns (chan, index), or (None, None) when the peer names no table.
    `POL_TM_COM_TABLE=0` restores the pre-2026-08-25 behaviour, where a COM
    game was invisible to the room.
    """
    if not common._env_int("POL_TM_COM_TABLE", 1):
        return None, None
    chan, index = matchmaking._peer_table(peer_nick)
    if index is None:
        common._say("tm: 🔭 member %s VS. COM -- peer %r decodes to no table, so the "
             "room cannot be told and nobody can observe this game. (Every "
             "measured COM request arrived on the picked table's own peer; if "
             "this line appears, that changed.)" % (member_id, peer_nick))
        return None, None
    key = pushqueue._push_key(member_id)
    # A rematch, or a second game at another table: drop the old binding first,
    # so one member can never hold two tables Playing.
    for k, v in list(_COM_AT.items()):
        if v == key and k != (chan, index):
            # NOT popped here: `_release_com_table` pops it AND republishes the
            # row. Popping first makes it a no-op and strands the old tile at
            # Playing -- exactly the phantom-tile shape this feature must not add.
            _release_com_table(k[0], k[1],
                               why="the same member started another COM game")
    # WARNING: AND THE LOBBY SEAT GOES. One seat per player is already this file's
    # rule -- `_seat_at_table` enforces it when a player reserves somewhere
    # else, because the client has ONE my-table slot -- and a COM game is
    # "somewhere else". Without this, reserving table 1 and then playing the
    # computer at table 2 leaves table 1 reserved by somebody who is demonstrably
    # not there, which is the phantom tile arriving by the front door.
    # `POL_TM_COM_RELEASES_SEAT=0` keeps the reservation.
    if common._env_int("POL_TM_COM_RELEASES_SEAT", 1) and member_id is not None:
        try:
            _held = [i for i, ss in (seating._seats_of(chan) or {}).items()
                     if any(m == member_id for m, _v in ss) and i != index]
            if _held:
                common._say("tm: 🔭 member %s starts VS. COM at %s table %d but still "
                     "holds table(s) %s in that room -- releasing. One seat per "
                     "player; the client has one my-table slot."
                     % (member_id, chan, index,
                        ", ".join(str(i) for i in _held)))
                tableaudit.release_seats(member_id, chan)
        except Exception as exc:
            common._say("tm:   ...could not release member %s's lobby seat(s) for the "
                 "COM game (%r) -- a phantom reservation may remain"
                 % (member_id, exc))
    _COM_AT[(chan, index)] = key
    ent = _COM_GAME.setdefault(key, {})
    ent["table"] = (chan, index)
    ent["member"] = member_id
    if ident:
        ent["ident"] = ident
    # Start the heartbeat clock HERE, exactly as `@GameEN=` does for a
    # reservation: the first table-peer @Pong lands ~4 s later (measured), and
    # an unstamped binding would ride the sweep's first-sight grace instead of
    # the real TTL.
    if member_id is not None:
        matchmaking._SEAT_ALIVE[matchmaking._alive_key(chan, member_id, index)] = time.time()
    try:
        import tmroom
        tablerow._republish_table(tmroom, chan, index, _com_who(chan, index),
                         why="VS. COM started, now Playing")
    except Exception as exc:
        common._say("tm:   %s table %s not flipped to Playing for the COM game (%r) "
             "-- the room keeps seeing a free tile" % (chan, index, exc))
    # ...AND OPEN THE DEPLOY WINDOW. `_live_matches_write` counts `_COM_AT`
    # now, but the gate reads the STAMP's freshness too -- so write one here
    # rather than inherit whatever the last match-flow handler left.
    webwatch._live_matches_write()
    common._say("tm: 🔭 member %s VS. COM is at %s table %d (decoded from peer %r) -- "
         "row published Playing; the room can observe it"
         % (member_id, chan, index, peer_nick))
    return chan, index


def _release_com_table(chan, index, why="VS. COM over"):
    """Unbind a COM table and put its row back to whatever the seats justify."""
    if index is None:
        return
    key = _COM_AT.pop((chan, index), None)
    if key is None:
        return
    ent = _COM_GAME.get(key)
    if ent:
        ent.pop("table", None)
    # A departing COM game takes its watchers with it -- they are registered
    # under the COM match key, and the next game must not inherit them.
    watchers._MATCH_WATCHERS.pop((None, key), None)
    try:
        import tmroom
        tablerow._republish_table(tmroom, chan, index,
                         seating._seats_of(chan).get(index) or [], why=why)
    except Exception as exc:
        common._say("tm:   %s table %s not returned from Playing (%r)"
             % (chan, index, exc))
    webwatch._live_matches_write()
    common._say("tm: 🔭 %s table %s: %s -- the COM binding is released"
         % (chan, index, why))


def expire_com_tables(now=None):
    """Free a COM table whose player's table heartbeat has gone silent.

    The no-phantom-tile rule, applied to the producer this feature adds: a crash, a kill or a
    relaunched client leaves no `@GameExit=`, and without this the row would
    say "Playing" for ever on a table nobody is at. Same signal and TTL as a
    reservation (`_SEAT_ALIVE`, stamped by the @Pong arm off the table peer --
    a COM client pongs its table peer every ~15 s, measured 2026-08-25), and
    the same first-sight grace so a restart starts the clock instead of
    reaping. `POL_TM_COM_TTL_S=0` disables.
    """
    ttl = common._env_int("POL_TM_COM_TTL_S", common._env_int("POL_TM_SEAT_TTL_S", 45))
    if ttl <= 0 or not _COM_AT:
        return 0
    now = now if now is not None else time.time()
    freed = 0
    for (chan, index), key in list(_COM_AT.items()):
        got = _COM_GAME.get(key) or {}
        mid = got.get("member")
        if mid is None:
            continue
        # WARNING: THE MATCH GATE, and it is the same one `expire_silent_seats`
        # needs: a client in the match scene may stop ponging its table (the
        # reservation is consumed client-side -- see the `_SEAT_ALIVE`
        # banner), and reaping mid-game would clear the tile under a running
        # COM game. A game with a live turn gets the generous match hold
        # instead of the beat TTL.
        _st = boardrules._MATCH_TURN.get((None, key)) or {}
        _running = (_st.get("turn") is not None
                    and not _st.get("result_sent"))
        eff = common._env_int("POL_TM_MATCH_HOLD_S", 2700) if _running else ttl
        akey = matchmaking._alive_key(chan, mid, index)
        stamp = matchmaking._SEAT_ALIVE.get(akey)
        if stamp is None:
            matchmaking._SEAT_ALIVE[akey] = now
        elif now - stamp > eff:
            common._say("tm: member %s's VS. COM game at %s table %d: table heartbeat "
                 "silent %ds (> %ds) -- the client is gone; releasing the "
                 "tile" % (mid, chan, index, int(now - stamp), eff))
            _release_com_table(chan, index, why="VS. COM heartbeat expired")
            matchmaking._SEAT_ALIVE.pop(akey, None)
            freed += 1
    return freed


def _is_com_deck(rec):
    """True for a PackPrm record that is a COM opponent's DECK.

    A deck is kind 0, never sold, and carries a real draw distribution (at
    least one firing row -- a `(level, group, cat, cum)` tuple with cum > 0).
    That is what separates it from a Prize Center record (kind 5, price > 0,
    one named card, all-zero cum) and from a buyable booster (kind 1, priced).
    The distinction matters because `_draw_pack` on a single-card prize record
    deals that ONE card five times.
    """
    return bool(rec) and rec.get("kind") == 0 and not rec.get("price") \
        and any(d[3] > 0 for d in (rec.get("draw") or []))


def _com_deck_ladder():
    """The contiguous run of COM-deck records starting at the base rung.

    25..44 in the shipped file -- twenty decks; record 45 on is the Prize
    Center (kind 5). Computed, not hardcoded, so a different PackPrm.BIN can
    only shorten or lengthen it, never smuggle a prize record in.
    """
    base = common._env_int("POL_TM_COM_DECK_BASE", 25)
    ladder = []
    no = base
    while _is_com_deck(cardtables._pack_rec(no)):
        ladder.append(no)
        no += 1
    return ladder or [base]


#: THE VS. COM OPPONENTS' OWN ROWS: `PlPrm.BIN` (built into services/tmdata/
#: by tools/tmdata_build.py from your client), 20-byte records after the
#: 16-byte header, indexed by the client's `/Com=` value. Record 0 is the
#: player's own placeholder row, never an opponent.
#:
#:   +0x04  the opponent's AVERAGE RANK (x100), which 0x1109C6 copies into
#:          the COM seat's rank slot when the match is built (0xEC169 ->
#:          0x110970). Lower is stronger.
#:   +0x0B  the PackPrm record of the opponent's own DECK. Several opponents
#:          deal from a record off the 25..44 ladder.
#:
#: This replaces the guess `25 + index - 1` wrapped in the ladder, which dealt
#: every opponent from 21 on a beginner deck. POL_TM_COM_DECKS=ladder
#: restores it. Without the table, decks fall back to the ladder and every
#: COM's average rank to the placeholder's 200.
_PLPRM = None
_PLPRM_REC = 20
_COM_RANK_DEFAULT = 200


def _plprm():
    """[(average rank x100, deck record)] per `PlPrm.BIN` record; [] if the
    table is missing or unreadable."""
    global _PLPRM
    if _PLPRM is None:
        _PLPRM = []
        try:
            here = os.path.dirname(os.path.abspath(deps.FACADE_FILE))
            with open(os.path.join(here, "tmdata", "PlPrm.BIN"), "rb") as f:
                blob = f.read()
            n = struct.unpack_from("<I", blob, 4)[0]
            if 0 < n <= 256 and len(blob) >= 16 + _PLPRM_REC * n:
                for i in range(n):
                    o = 16 + _PLPRM_REC * i
                    _PLPRM.append((struct.unpack_from("<H", blob, o + 4)[0],
                                   blob[o + 0x0B]))
        except Exception as exc:
            common._say("tm: PlPrm.BIN unreadable (%r) -- COM decks use the ladder and "
                 "every COM's average rank is %d" % (exc, _COM_RANK_DEFAULT))
    return _PLPRM


def _com_deck_of(char_index):
    """The PackPrm deck record `PlPrm.BIN` names for opponent `char_index`,
    or None."""
    t = _plprm()
    try:
        i = int(char_index)
    except (TypeError, ValueError):
        return None
    return t[i][1] if 0 < i < len(t) else None


def _com_avg_rank(char_index):
    """One COM opponent's average rank x100 (`PlPrm.BIN` +0x04); an index
    outside the table gets the placeholder row's value."""
    t = _plprm()
    base = t[0][0] if t else _COM_RANK_DEFAULT
    try:
        i = int(char_index)
    except (TypeError, ValueError):
        return base
    return t[i][0] if 0 < i < len(t) else base


#: WARNING: EVERY WIN PAYS A HOUSE PRIZE, AND UNTIL 2026-09-26 THIS SERVER PAID NONE.
#: A player with 0 gil beat Lumberjack Mick six times and earned nothing,
#: because the settlement only doubled the stake and a broke player's stake is
#: always 0 (0xEC7EF clamps the COM's wager to the wallet).
#:
#: SE's guidebook (`pml/game/tetra/guidebook/tactics/src/srpm082.pml`):
#:
#:     2 players: prize = {(4 - opponent's average rank) x 0.5 + 1} x (stake + 100)
#:     3 players: the same over the MEAN of the opponents' average ranks,
#:                x the number of players who finished below you
#:     perfect win: x3
#:
#: and `guidebook/manual/in_mnd.pml` tells a new player to start with VS. COM
#: against Natasha, "and your money grows as you play" -- the house prize is
#: the bootstrap for a 0-gil account (a new save starts at 0).
#:
#: The client carries the same arithmetic, TM.dll 0xCC719..0xCC7A6 (in
#: 0xCBD80), writing struct +0xCC:
#:
#:     ((0x190 - avgOpp) / 2 + 100) * (pot + 100) * perfect * beaten / 100
#:
#: perfect = 3 when the seat holds every card, beaten = seats strictly below,
#: a loss 0, a draw = the pot. The result count-up (0xC7DBE..) then CREDITS
#: +0xCC to the wallet. Online the client skips its own formula ([0x52464CA]
#: is set) and takes +0xCC from `@ResultData /D=` occ 1 (0x1042B4) -- so SE's
#: server computed it, and so must we. An earlier reading called this value
#: "Technical Rating" from a screenshot; it is the prize.
#:
#: PARTIAL: The stake is NOT returned on top: the client's pot-return arms need
#: [0x52464C9] == 1, which the online init zeroes, so its wallet reads
#: `wallet - stake + prize` and the server mirrors that. The guidebook's
#: manual page (srpm030) says a 2-player perfect is x2; srpm082 and the code
#: say x3 -- `POL_TM_PRIZE_PERFECT` holds the code's 3.
#:
#: `POL_TM_HOUSE_PRIZE=0` restores the old "win pays 2 x stake" settlement.
def _house_prize(pot, opp_ranks, scores, seat=0):
    """The retail prize for `seat` (see the banner above): 0 on a loss, the
    pot on an all-level draw, otherwise the guidebook formula."""
    pot = max(0, int(pot or 0))
    scores = list(scores or [])
    if len(scores) < 2 or not (0 <= seat < len(scores)):
        return 0
    if max(scores) == min(scores):
        return pot
    mine = scores[seat]
    beaten = sum(1 for i, v in enumerate(scores) if i != seat and v < mine)
    if beaten <= 0:
        return 0
    ranks = [int(r) for r in (opp_ranks or [])] or [_com_avg_rank(0)]
    avg = sum(ranks) // len(ranks)
    perfect = common._env_int("POL_TM_PRIZE_PERFECT", 3)         if all(v == 0 for i, v in enumerate(scores) if i != seat) else 1
    return max(0, ((400 - avg) // 2 + 100) * (pot + 100) * perfect * beaten
               // 100)


def _com_deck_record(char_index):
    """The PackPrm record number for one COM character's deck.

    The client's own table (`PlPrm.BIN` +0x0B, `_com_deck_of`).
    Anything outside it, or a record that is not a COM deck, falls back to the
    old ladder guess - wrapped, so it can never land on a single-card Prize
    Center record (a plain offset once dealt Trade King Gohn Maechen x5).
    """
    i = int(char_index)
    no = _com_deck_of(i)
    if os.environ.get("POL_TM_COM_DECKS", "") != "ladder" and no is not None:
        if _is_com_deck(cardtables._pack_rec(no)):
            return no
    ladder = _com_deck_ladder()
    return ladder[max(0, i - 1) % len(ladder)]


def _com_deck_rows(char_index):
    """Five card rows (wire format) for one COM opponent.

    `PackPrm.BIN` carries the COM decks -- kind 0, price 0, never sold:
    record 12 is Cid's (category 4, airships), 27 Quina's (group 3 category 5,
    mascots), and 25..44 read as a difficulty ladder (level bands rising).
    See `_com_deck_record` for the (unmeasured) index -> record mapping.
    """
    rows = cardshop._draw_pack(_com_deck_record(char_index), n=5)
    return [b"|".join(b"%d" % v for v in r) for r in rows]


def _queue_com_turns(chan, index, member_id, n, turn, active, after=None):
    """Play every consecutive COM turn from (turn, active), then hand back.

    VERIFIED: THE CLIENT DOES NOT PLAY THE COM -- MEASURED LIVE 2026-08-22T14:31Z.
    With the deal and turn 0 finally landing (`bc3f4d8c`), the human placed a
    card, we announced turn 1 `/A=1`, the client ACKED the announcement and
    then sat polling (0x43, 10) -- it never sends `@PutData=` for a COM seat.
    In network mode the server is the authority for every seat, COM included
    (0xCEE51 skips the client's own resolver outright). So this function is
    the COM player: announce the COM's turn, pick a move from the COM hand
    dealt at @CardSelect time, push `@PutCard=` into the slot the client is
    parked on, battles behind it, and repeat until the turn is the human's
    again -- or the match is over, where `_queue_next_turn` takes the result.

    Every message gets a strictly increasing `after` so it rides its own
    reply, in order -- the drain law, chained. Move choice is uniform random
    over the empty tiles and the COM's remaining hand; `POL_TM_COM_PLAY=0`
    disables the whole player (restoring the parked-scene behaviour for an
    A/B).

    WARNING: THE CHAIN IS ACK-GATED NOW, AND THE 3-PLAYER CRASH IS WHY (measured
    2026-08-22T17:03Z, shim-CASPC.1.log:201272). The client PURGES the store
    at every GamePlayEnd -- 0xC7642 hands (0x43, 0x28/8/0xA/0xC) to 0x101830
    before it advances the turn -- so a `@PutCard` that arrives while the
    PREVIOUS turn's battles are still animating is deleted as a stale record:
    the trace shows the turn-13 put popped with no `Recv=PUTCARD` at
    `GamePlayEnd:Turn=12`, the client then polling (0x43,10) for a message
    that no longer exists, and the turn-14 put failing `0x103876`'s compare
    (`Errot!!Turn13!=14` -> return -1 -> 0xC72A6's "invalid card data
    detected" dialog -> CardGameOutClose). It is a RACE, not a 3-player
    fault -- two chained COM turns just double the puts in flight.

    The gate is the client's own per-turn `@TurnData=` ack (measured: every
    turn, COM turns included, 17:01-17:02Z session): announce the COM's turn
    here, then hold its put/battles until the ack proves the client has
    ENTERED that turn -- past the purge, parked on (0x43,10). @TurnData
    itself (cmd 9) is not in the purge list, so the announcement may arrive
    any time. `POL_TM_COM_ACK_GATE=0` restores the old one-shot chain.
    """
    if not common._env_int("POL_TM_COM_PLAY", 1):
        return
    key = (chan, index)
    limit = common._env_int("POL_TM_TURN_LIMIT", 5 * n)
    rnd_moves = random.Random()
    a = common._env_int("POL_TM_TURNDATA_GAP", 1) if after is None else int(after)
    rnd = matchstart._turn_rand(key)
    peer = matchmaking._MATCH_PEER.get(pushqueue._push_key(member_id))
    guard = 0
    while guard < 32:
        guard += 1
        if turn >= limit:
            turns._queue_next_turn(chan, index, [], turn, active,
                             roster=[(member_id, 0)], n_solo=n, after=a)
            return
        st = boardrules._MATCH_TURN.setdefault(key, {})
        st["turn"], st["active"], st["n"] = turn, active, n
        # /S= IS THE SCORE -- the per-seat BOARD TILE COUNT (measured static
        # 2026-08-22: 0x103799 stores it into 0x5246580+48k, the field the
        # client's own turn-end tally rebuilds from tile owners; see
        # `_turndata_body`). The hand-remaining counts served before were the
        # live report "score fluctuated, 1 point with 6 cards on the field";
        # the constant 5 before that was the "COM hand refilled" report.
        # POL_TM_TURN_SCORES=0 falls back to the hand counts, =-1 to the
        # constant, both kept as A/B apparatus.
        counts = None
        _sc_mode = common._env_int("POL_TM_TURN_SCORES", 1)
        if _sc_mode == 1:
            counts = scoring._board_scores(chan, index, n)
        elif _sc_mode == 0 and common._env_int("POL_TM_COM_TURN_COUNTS", 1):
            _h = boardrules._MATCH_HANDS.get(key) or {}
            counts = [len(_h.get(pushqueue._push_key(member_id) if s2 == 0
                                 else ("com", s2)) or [])
                      for s2 in range(n)]
        td = protocol._turn_code(protocol.TURNDATA_CMD, turn) + matchstart._turndata_body(
            [], 0, active, rnd, scores=counts, n_solo=n)
        common._say("tm:   ...turn %d: (0x43, %d) @TurnData= /A=%d /S=%s%s"
             % (turn, protocol.TURNDATA_CMD, active % n,
                "|".join(str(v) for v in counts) if counts else "(const)",
                "" if active % n else " -- the human's turn, COM chain done"))
        pushqueue._queue_push(member_id, td, "turn %d" % turn, after=a, peer=peer)
        # ...AND TO THE WATCHERS. A COM game's turns/puts/battles are authored
        # HERE, not relayed from a client, so without these three pushes an
        # observer of a COM table would see the human's own placements
        # (relayed by the `@PutData=` arm) and nothing the machine does.
        watchers._watch_push(chan, index, td, "turn %d" % turn, after=a)
        a += 1
        if active % n == 0:
            return
        if common._env_int("POL_TM_COM_ACK_GATE", 1):
            # Hold the COM's put until the client acks THIS turn -- see the
            # docstring. `_com_play_acked` (called from the @TurnData= ack
            # handler) plays it and re-enters this function for the next turn.
            st["await_ack"] = turn
            common._say("tm:   ...COM turn %d announced -- holding its @PutCard for "
                 "the client's own @TurnData= ack (the GamePlayEnd purge race)"
                 % turn)
            return
        hands = boardrules._MATCH_HANDS.setdefault(key, {})
        hand = hands.get(("com", active)) or []
        if not hand:
            # The client skips cardless players itself (0xC76A2's advance),
            # so announce-and-move-on mirrors it. With 5 cards each over 5*N
            # turns this should never fire; say so if it does.
            common._say("tm: WARNING: COM seat %d has no cards left on turn %d -- "
                 "announced the turn and moved on" % (active, turn))
            turn, active = turn + 1, (active + 1) % n
            continue
        board = boardrules._MATCH_BOARD.get(key) or {}
        tiles = boardrules._board_tiles(n)
        empty = [t for t in range(tiles) if t not in board]
        if not empty:
            common._say("tm: WARNING: no empty tile for COM seat %d on turn %d -- the "
                 "match should already be over; announcing the result"
                 % (active, turn))
            turns._queue_next_turn(chan, index, [], limit, active,
                             roster=[(member_id, 0)], n_solo=n, after=a)
            return
        hand_idx = rnd_moves.randrange(len(hand))
        row = hand.pop(hand_idx)
        tile = rnd_moves.choice(empty)
        common._say("tm: 🤖 COM seat %d (turn %d) plays hand slot %d -- card %s -- "
             "onto tile %d. Watch for '---->Recv=PUTCARD'."
             % (active, turn, hand_idx, row.split(b"|")[0].decode(), tile))
        pc = protocol._turn_code(protocol.PUTCARD_CMD, turn) + boardrules._putcard_body(
            [], 0, active, hand_idx, tile, row, n_solo=n)
        pushqueue._queue_push(member_id, pc, "COM seat %d's card" % active,
                    after=a, peer=peer)
        watchers._watch_push(chan, index, pc, "the COM's card on tile %d" % tile,
                    after=a)
        a += 1
        if common._env_int("POL_TM_BATTLE", 1):
            fights, _marks = placement._apply_placement(chan, index, n, active, tile, row)
            rnd8 = matchstart._turn_rand(key)
            for att, dfn, res, verdict in fights:
                common._say("tm:   ...COM battle: tile %d vs tile %d -- %s takes it"
                     % (att, dfn,
                        {tmbattle.ATTACKER: "the ATTACKER",
                         tmbattle.DEFENDER: "the DEFENDER",
                         tmbattle.DRAW: "NOBODY (a draw)"}[verdict]))
                _bd = (protocol._turn_code(protocol.BATTLEDATA_CMD, turn)
                       + boardrules._battledata_body(res, att, dfn, rnd8))
                pushqueue._queue_push(member_id, _bd,
                            "the COM's battle on tile %d" % dfn,
                            after=a, peer=peer)
                watchers._watch_push(chan, index, _bd,
                            "the COM's battle on tile %d" % dfn, after=a)
                a += 1
        turn, active = turn + 1, (active + 1) % n
    common._say("tm: WARNING: COM turn chain guard tripped (32 iterations) -- stopped")


def _com_play_acked(chan, index, member_id, n, turn, active):
    """Play ONE ack-released COM turn; return its `@PutCard=` as the DIRECT
    REPLY to the ack that released it.

    The other half of `_queue_com_turns`'s ack gate. The client has just
    acked `turn`, so it is past its GamePlayEnd purge and parked on
    (0x43, 10) -- the put can answer the ack itself (the cmd-9 reader never
    touches a (0x43, 10) body, so the "answer nothing on command 9" rule is
    about the SHAPE of the reply, not its existence). Battles ride the next
    replies (`POL_TM_BATTLE_GAP` -- same law as the human's placement: the
    put MOVES the scene, its own reply must not carry more), and the next
    turn's announcement queues behind them, which re-arms the gate or emits
    the result.
    """
    key = (chan, index)
    peer = matchmaking._MATCH_PEER.get(pushqueue._push_key(member_id))
    hands = boardrules._MATCH_HANDS.setdefault(key, {})
    hand = hands.get(("com", active)) or []
    limit = common._env_int("POL_TM_TURN_LIMIT", 5 * n)
    if not hand:
        common._say("tm: WARNING: COM seat %d has no cards left on acked turn %d -- "
             "announcing the next turn instead" % (active, turn))
        _queue_com_turns(chan, index, member_id, n, turn + 1, (active + 1) % n)
        return None
    board = boardrules._MATCH_BOARD.get(key) or {}
    tiles = boardrules._board_tiles(n)
    empty = [t for t in range(tiles) if t not in board]
    if not empty:
        common._say("tm: WARNING: no empty tile for COM seat %d on acked turn %d -- the "
             "match should already be over; announcing the result"
             % (active, turn))
        turns._queue_next_turn(chan, index, [], limit, active,
                         roster=[(member_id, 0)], n_solo=n)
        return None
    rnd_moves = random.Random()
    hand_idx = rnd_moves.randrange(len(hand))
    row = hand.pop(hand_idx)
    tile = rnd_moves.choice(empty)
    common._say("tm: 🤖 COM seat %d (turn %d, ack-released) plays hand slot %d -- "
         "card %s -- onto tile %d, answering the ack directly. Watch for "
         "'---->Recv=PUTCARD'."
         % (active, turn, hand_idx, row.split(b"|")[0].decode(), tile))
    a = common._env_int("POL_TM_BATTLE_GAP", 1)
    if common._env_int("POL_TM_BATTLE", 1):
        fights, _marks = placement._apply_placement(chan, index, n, active, tile, row)
        rnd8 = matchstart._turn_rand(key)
        for att, dfn, res, verdict in fights:
            common._say("tm:   ...COM battle: tile %d vs tile %d -- %s takes it"
                 % (att, dfn,
                    {tmbattle.ATTACKER: "the ATTACKER",
                     tmbattle.DEFENDER: "the DEFENDER",
                     tmbattle.DRAW: "NOBODY (a draw)"}[verdict]))
            _bd = (protocol._turn_code(protocol.BATTLEDATA_CMD, turn)
                   + boardrules._battledata_body(res, att, dfn, rnd8))
            pushqueue._queue_push(member_id, _bd,
                        "the COM's battle on tile %d" % dfn,
                        after=a, peer=peer)
            watchers._watch_push(chan, index, _bd,
                        "the COM's battle on tile %d" % dfn, after=a)
            a += 1
    _pc = protocol._turn_code(protocol.PUTCARD_CMD, turn) + boardrules._putcard_body(
        [], 0, active, hand_idx, tile, row, n_solo=n)
    # The put ANSWERS the ack for the player, so a watcher -- who sent no ack
    # -- can only get it as a push. Ahead of the battles it rides with
    # (after=0): same order the player sees, the put first.
    watchers._watch_push(chan, index, _pc, "the COM's card on tile %d" % tile)
    _queue_com_turns(chan, index, member_id, n, turn + 1, (active + 1) % n,
                     after=a)
    return _pc


def _comlist_body(values):
    """`@ComList=` -- the COM opponent list. Arm 0x102F74, `Recv=COMLIST`.

    WARNING: THE MESSAGE THE "CHANGE SETTINGS" BUTTON WAITS FOR, and until
    2026-09-07 this server had never sent one. `@Continue=`'s `/Conf=1` is that
    button (the arm's own comment called it "...but change the settings first"
    and marked the `@ComList` wait INFERRED); a tester took that path three
    times in one night and the client froze every time, sending nothing at all
    afterwards -- because a slot poll is a LOCAL lookup and makes no traffic.

    The format is measured off the parser, not invented:

        0x102FC3  /L=  parsed first  -> [obj+0x18C], the ROW COUNT
        0x103003  /D=  one occurrence per row, `|`-separated like /ID= and /CN=
        0x10300D  each parsed as a DWORD into the table at [0x524B630],
        0x10301C  row i at + i*0x14 + 0x18

    WARNING: WHAT A ROW'S DWORD MEANS IS NOT MEASURED. It is served as the COM
    character index -- the same index space `@ComGame=`'s `/Com=` already
    sends back to us -- because that is the only COM identifier on this wire.
    `POL_TM_COMLIST_VALUES` sweeps it (a comma list) and `POL_TM_COMLIST=0`
    stops sending it at all. The client says whether it landed:
    `---->Recv=COMLIST` (0x51C4AA0).
    """
    vals = list(values)
    return (b"@ComList=/L=%d" % len(vals)
            + b"/D=" + b"|".join(b"%d" % int(v) for v in vals))


def _comlist_values():
    """The opponent indices to advertise. Defaults to our own COM roster."""
    raw = os.environ.get("POL_TM_COMLIST_VALUES")
    if raw:
        out = []
        for part in raw.replace("|", ",").split(","):
            try:
                out.append(int(part.strip(), 0))
            except ValueError:
                pass
        return out or [0]
    return list(range(len(_com_deck_ladder())))


def _comgame_body(member_id, rules):
    """`@ComGameInit=` -- the vs-COM opener, arm 0x102D21.

    One player, no seats and no `/ID=` search: the fields are this player's own
    numbers plus the same seven-value ruleset.

        /RA= -> dword [obj+0x04]      /M=  -> dword [obj+0xC8] (clamped >= 0)
        /CP= -> word  [obj+0x00]      /S=  -> word  [obj+0xEC]

    WARNING: `/CP=` IS THE CARD LEVEL SLOT, AND SENDING 0 LOCKED THE OPPONENT LIST
    (2026-09-08). `0x102DCA mov word [esi], ax` puts this field into struct
    +0x00 -- the SAME word the save parser derives the card level into
    (0x10147F) and the same word the opponent-unlock routine reads
    (`0x101E47`/`0x105160` load it and pass it to `0x110A70`, which marks an
    entry playable only when it is >= that opponent's `PlPrm.BIN` +0x02
    threshold, and otherwise substitutes record 0's placeholder). We sent a
    hardcoded 0, so every VS. COM open told the client its card level was zero
    and every opponent past the first two locked until something recomputed it.
    The live symptom was exactly that: no new opponent appears while you
    play, and one or two are suddenly available after a relaunch (the save
    parse re-derives the real number).

    WARNING: The field is NAMED Card Points and that name is what hid this -- the
    value it lands in is the card level. `POL_TM_VSGAME_CP` still forces a
    number for an A/B; unset, it is now `card_level_of(member)`.
        /R= x7 -> [obj+0xDC] dword, [obj+0xE0..0xE4] bytes, [obj+0xD9] byte
    """
    # WARNING: `/CP=` is CARD POINTS (struct +0x00 <- file +0x38), not money. `/M=` is
    # the wallet (struct +0xC8 <- file +0x34). They are adjacent in the save and
    # were conflated for a while; do not let the balance back into /CP=.
    _cp = os.environ.get("POL_TM_VSGAME_CP")
    if _cp is None or _cp == "":
        _cp_val = cardtables.card_level_of(member_id)
    else:
        _cp_val = common._env_int("POL_TM_VSGAME_CP", 0)
    common._say("tm:   ...VS. COM card level = %d (0xB9040 ported; it gates which "
         "opponents unlock against PlPrm.BIN +0x02, and this server sent 0 "
         "until 2026-09-08)" % (_cp_val,))
    return (b"@ComGameInit=/RA=%d/M=%d/CP=%d/S=%d"
            % (common._env_int("POL_TM_VSGAME_RA", 0), purse.money_of(member_id),
               _cp_val, common._env_int("POL_TM_COMGAME_S", 1))
            + b"/R=" + b"|".join(b"%d" % v for v in rules))


def _queue_vsgameinit(member_id):
    """Hand this player the board, once they have accepted the match.

    WARNING: IT MUST NOT RIDE THE `@GameWa=` THAT TRIGGERED IT. That reply is what
    lets the client leave the reservation scene and BUILD the match scene, and
    the match scene is what polls `(0x43, 1)`; a message delivered before its
    poller exists sits unread in a store that the scene change then drains. Same
    fault and same fix as `POL_TM_MATCH41_GAP` -- see the banner above
    `MATCH_41_MSGID`. `POL_TM_VSGAME_GAP=0` restores the same-batch behaviour
    for an A/B.
    """
    if not common._env_int("POL_TM_VSGAME", 1):
        return
    try:
        import tmroom
        chan = tmroom.room_of(member_id) if member_id is not None else None
        # WARNING: `chan` is allowed to be None here and must NOT be an early return:
        # the player has just LEFT the room to enter the match, so `room_of`
        # legitimately answers nothing. `_table_of_member` falls back to the
        # announced-match snapshot, which carries the room with it.
        index, seats = matchmaking._table_of_member(chan, member_id)
        if index is None or not seats:
            common._say("tm:   no @VsGameInit -- member %s is at no table and no "
                 "announced match remembers them" % (member_id,))
            return
        chan = chan or matchmaking._MATCH_ROSTER.get(pushqueue._push_key(member_id), (None,))[0]
        if len(seats) < 2:
            # 0x1024D5's own refusal, raised here rather than on the client where
            # it costs a `StartAloneERROR` and a scene that never starts.
            common._say("tm:   no @VsGameInit -- table %d has %d seat(s) and /N= must be"
                 " > 1 (0x1024D5 -> StartAloneERROR)" % (index, len(seats)))
            return
        # SHARED STATE, not the process cache: a match started after a restart
        # would otherwise be played under default rules instead of the table's.
        # `_vsgame_rule_seven`, NOT `_rule_seven`: @VsGameInit's parser reads
        # `/R=` in a different slot order and `_rule_seven`'s @Tet order put the
        # timer in the wrong slot -> every PvP game ran at 0:30 (see
        # VSGAME_RULE_KEYS_PARSED).
        rules = ruleset._vsgame_rule_seven(tablesettings._table_rules_get(chan, index))
        # WARNING: NOT just this member. The board goes to everyone, once everyone has
        # accepted -- see `_MATCH_ACCEPTS` for the measurement that forced it.
        _pvp_staked = False
        # A TOURNAMENT GAME: same (0x43,1) slot and fields, named @EventGameInit
        # (board arm 0x10296D stores no wager and no @No<i> sections), no stake.
        _event_game = tournament._is_event_match([m for m, _v in seats])
        for mid in matchmaking._note_accept(chan, index, member_id, seats):
            seat = next((i for i, (m, _v) in enumerate(seats)
                         if pushqueue._push_key(m) == pushqueue._push_key(mid)), None)
            if seat is None:
                continue
            body = protocol.encode_code(protocol.VSGAME_MSGID) + matchstart._vsgame_body(seats, seat, rules)
            if _event_game:
                body = body.replace(b"@VsGameInit=", b"@EventGameInit=", 1)
            common._say("tm: pushing (0x43, 1) @VsGameInit to member %s -- %s table %d, "
                 "seat %d of %d, /R=%s. The client's OWN trace is the "
                 "measurement: '---->Recv=PLGAMEINIT' then 'ID=%d'."
                 % (mid, chan, index, seat, len(seats),
                    ",".join(str(v) for v in rules), seat))
            # WARNING: NO HOLD-BACK, AND THE QUORUM IS WHY. `POL_TM_MATCH41_GAP`'s
            # rule is "do not arrive before the scene exists"; waiting for the
            # LAST accept already guarantees every player is in that scene, so
            # a further reply of delay buys nothing and costs a lot. Measured
            # 2026-08-20T11:04Z: with a hold-back of 1 the two boards went out
            # **19 s apart** (11:04:09 and 11:04:28) because each rides its own
            # member's next reply and this band only speaks on `@Pong=`. The
            # guest sat on a black screen for those 19 s and then could not
            # start. `POL_TM_VSGAME_GAP=1` restores the old delay.
            band = matchmaking._MATCH_PEER.get(pushqueue._push_key(mid))                 if common._env_int("POL_TM_VSGAME_PIN_PEER", 1) else None
            pushqueue._queue_push(mid, body, "the board at table %d" % index,
                        after=common._env_int("POL_TM_VSGAME_GAP", 0), peer=band)
            _pvp_staked = True
        # THE PvP WAGER STAKES IN HERE -- once, on the accept quorum (the
        # loop above runs only when the LAST player accepts). `/R=` occ 0 is
        # the table wager (block[30..33], the table-settings panel) and the
        # clients already receive it -- the pot at [obj+0xDC] -- but the
        # server never moved the money, so a PvP win displayed winnings the
        # next save sync confiscated (the last money gap). ESCROW: each
        # member stakes min(wager, balance); the result block hands the whole
        # pot to a UNIQUE winner (net + the losers' stakes), refunds everyone
        # on a tie at the top; @GameExit before a result refunds via the
        # durable staked_wager, and a restart's orphan is refunded by
        # `_stake_recover` -- the same machinery the COM wager proved.
        # /M= in the bodies above is PRE-stake (built before this runs), the
        # same order the COM path uses. POL_TM_WAGER_PVP=0 disables; rematches
        # are UNSTAKED (no new @VsGameInit -- logged when one begins).
        if _pvp_staked and _event_game and common._env_int("POL_TM_EVENT_DEAL", 1):
            tournament._event_deal(seats, lambda _m: (
                matchmaking._MATCH_PEER.get(pushqueue._push_key(_m))
                if common._env_int("POL_TM_VSGAME_PIN_PEER", 1) else None),
                chan, index)
        if _pvp_staked and not _event_game and common._env_int("POL_TM_WAGER", 1) \
                and common._env_int("POL_TM_WAGER_PVP", 1):
            # KEY: THE POT IS NOW A TABLE-LIVED FIGURE, not `rules[0]` read
            # afresh: Double Up moves it between games (`_double_up_pot`) and
            # `@Continue=` carries no field that reports the new one back, so
            # this server has to keep its own copy or the client's panel and
            # our escrow drift apart on the first rematch. A fresh
            # @VsGameInit is a fresh MATCH, so the escalation resets here.
            _pot = pots._table_pot(chan, index, {"bm": rules[0]}, reset=True)
            pots._stake_pvp_pot(chan, index, seats, _pot, "wager")
    except Exception as exc:
        # `_say` must never raise from a reply path, and neither must this.
        common._say("tm:   @VsGameInit not queued (%r)" % (exc,))


def _gameeca_enabled():
    """`POL_TM_GAMEECA=0` restores the silent control for an A/B.

    Kept for the same reason `POL_TM_GAMEEA` has one: this reply is built from a
    string-pool NEIGHBOURHOOD and an INFERRED msgid, so the ability to put the
    old behaviour back without a redeploy is part of shipping it.
    """
    return os.environ.get("POL_TM_GAMEECA", "1") == "1"


def _gameeca_msgid():
    """The `@GameECA` message code. `POL_TM_GAMEECA_MSGID` overrides the msgid.

    WARNING: INFERRED, NOT MEASURED -- request msgid + 1, which is the rule every
    documented pair in this protocol follows. On this band a wrong msgid is
    silently ignored, exactly like no reply at all, so this knob is the FIRST
    sweep if "Preparing game" does not move: try 0x13, 0x02, 0x12.
    """
    try:
        mid = int(os.environ.get("POL_TM_GAMEECA_MSGID", "0x13"), 0) & 0xFF
    except (TypeError, ValueError):
        mid = 0x13
    return 0x00000041 | (mid << 16)


def _gameeca_en():
    """`/EN=` for `@GameECA`, and it MUST be > 0.

    VERIFIED: READ OFF THE PARSER, after two wrong guesses cost two live rounds
    (TM.dll `0x8538D`):

        push "@GameECA" ; call 0xAA920      ; match the command
        push "/EN="     ; call 0xAB080      ; parse the integer
        cmp  eax, 0
        je   -> [esp+0xc] = 0xFFFFFF9D      ; -99, the FAILURE arm
        jle  -> skip
        jg   -> [0x5228200] = 0xF           ; success, the session advances

    So `@GameECA` is the exact twin of `@GameEA`: same `/EN=` field, same
    "must be positive" rule. `MSG_GAMEEA`'s note that 1 is the value TM.dll
    `0x90114` singles out applies here too, so 1 is the default.

    WARNING: WHAT WAS READ WRONG TWICE, because it is the instructive part: `/TblId=`
    and `/TblNo=` are NOT `@GameECA`'s fields. They belong to **`@MuchMake`**,
    a LATER message in the same session -- the parser at `0x85511` matches
    `"@MuchMake"` (VA 0x51B27DC) and only then reads them, storing TblNo ->
    `[0x52223A8]` and TblId -> `[0x521BB58/5C]`. The first cut read the string POOL's
    neighbourhood (`@GameECA` `/TblId=` `/TblNo=` `@MuchMake` ...) and assumed
    adjacency meant ownership. It does not: the pool is just a pool. Both live
    failures followed from putting the right fields on the wrong message, which
    left `/EN=` absent -> parsed as 0 -> the -99 arm -> Lobby.BIN 448.
    **Adjacency in a string table is a hint about a SUBSYSTEM, never about a
    message's fields. Open the parser.**
    """
    return common._env_int("POL_TM_GAMEECA_EN", 1)


def _queue_com_match(member_id, peer_nick=None):
    """Hand a VS. COM player the table, then the start -- the `@MuchMake` pair.

    VERIFIED: WHY THIS EXISTS SEPARATELY FROM `_push_muchmake`. That one is the PvP
    path and fires on SEAT COUNT at a lobby table; a COM game has one human and
    no seated peers, so it can never fire. Live 2026-08-22: with `@GameECA=/EN=1`
    accepted the client runs the rest of the handshake by itself
    (`@CheckJoinTable=` -> `_Ans=/Join=1/Mem=1`, `@Req=` -> `@PLAYACK=/EN=1`)
    and then sits in the COM scene on a bare UI background, drawing nothing.
    Nothing was left to send it.

    WARNING: AND `POL_TM_MUCHMAKE_PUSH` IS DEFAULT-OFF ON PURPOSE -- do not "fix" that.
    It was measured: in the MATCH scene the client never polls 0xD2, so pushing
    there only fills a store nothing drains. That measurement is about a
    DIFFERENT SCENE. `@MuchMake`'s receivers were always known to be real but
    unplaced -- msgid 0 at `0x854B0` (called from `0x6D780` and `0x7B770`) and
    msgid 1 at `0x85790`, reached through the state-machine jump table at
    `0x985F3` -- and the COM scene is the strongest candidate yet for being one
    of them, because it is a scene that demonstrably waits for something. So
    this path gets its own switch (`POL_TM_COM_MUCHMAKE`) and leaves the PvP
    default exactly where its measurement put it.

    WARNING: THE STAGGER IS THE SAME LAW AS EVERYWHERE ELSE HERE: msgid 0 gives the
    client the table, msgid 1 is the start it answers, and msgid 1's poller only
    exists after the client has ACTED on the table. Same batch = the start lands
    in a store before anything looks for it, and a scene change drains it. See
    `_queue_push`'s banner and `POL_TM_MATCH41_GAP`.
    """
    # *** THE BOARD FIRST -- `@ComGameInit` INTO THE SLOT THE CLIENT POLLS. ***
    #
    # VERIFIED: MEASURED, and it is the first thing on this feature that was not a
    # guess. `probe_lookup` on the live client (shim `[tmlog] probe_lookup=1`,
    # the 0x825A0 store-lookup probe) shows the client polling
    # **`code=0x43 msgid=2`** 645 times, "not found", every time -- and
    # `COMGAME_MSGID` IS (0x43, 2). It is asking for `@ComGameInit` and we have
    # never put one there.
    #
    # We already BUILD it (`_comgame_body`, arm 0x102D21) and already answer it
    # when the client sends `@ComGame=` -- but in a VS. COM session the client
    # never sends `@ComGame=`, so the builder was only ever reachable down a
    # path this flow does not take. Hence a push, into the slot it polls.
    #
    # WARNING: AND `@ComGameInit` IS THE RIGHT SHAPE FOR THIS: "one player, no seats
    # and no `/ID=` search", per its own banner -- no second player, no table,
    # no room. That is exactly a COM game, and it is why `_queue_vsgameinit`
    # (the PvP board, `/N=` must be > 1 or `0x1024D5` raises StartAloneERROR)
    # is NOT what a COM game needs.
    if common._env_int("POL_TM_COM_GAMEINIT", 1):
        # WARNING: COM ORDER, NOT @Tet ORDER. This board seeds the VS. COM rules screen,
        # and the client parses its /R= as bm|st|cb|ca|tl|du (0x102D21), so the
        # @Tet-order `_rule_seven` served Special Tile OFF / Time Limit 0:30
        # instead of the factory On / 3:00 (confirmed live 2026-09-02). See
        # `_com_rule_seven`.
        rules = ruleset._com_rule_seven(None)
        # `peer=` matters: whichever @ComGameInit the client reads LAST is the
        # one whose delivering sender becomes the session pair (0x102D3C). A
        # copy framed from the recipient's own nick would set the pair to the
        # OWN guid and every later in-match push would fail the equality gates.
        pushqueue._queue_push(member_id,
                    protocol.encode_code(protocol.COMGAME_MSGID)
                    + _comgame_body(member_id, rules),
                    why="VS. COM: the board (@ComGameInit)",
                    after=common._env_int("POL_TM_COM_GAMEINIT_GAP", 1),
                    peer=peer_nick)
        common._say("tm: member %s VS. COM -- queued @ComGameInit into (0x43, 2), the "
             "slot probe_lookup measured the client polling 645x and never "
             "finding. /R=%s (POL_TM_COM_GAMEINIT=0 disables)"
             % (member_id, ",".join(str(v) for v in rules)))

    # \U0001f534 AND PRE-LOAD THE LEAVE ACK INTO (0x43, msgid 5), BECAUSE THE PS2
    # NEVER ASKS FOR IT.
    #
    # MEASURED 2026-09-08 off the leave-hang savestate. `CQMComConfig` (the VS.
    # COM settings screen, tick `0x002f3cb0`) leaves like this: at `0x002f4514`
    # it puts a progress dialog on the parent `CQMCardGame` (`+296`), sets
    # percent 0 / ceiling 80, enters its state 4, and then polls
    # `0x003edbe0(0x005D6550, 5)` = **(code 67, msgid 5) = `@Quit`**. On timeout
    # it raises selector 339 -> **`-1-37339`** and parks on that dialog, which is
    # exactly the 80/80 read off the savestate.
    #
    # \U0001f534 THE CLIENT NEVER SENDS `@Quit=` ON THIS PATH. The string
    # `@Quit` (`0x00489CB8`) has exactly ONE reference in `TMaster.pex` and it is
    # the RECEIVE arm `0x003eedd8`; the console only ever parses one. So the
    # `@Quit=` handler in `_handle_line` -- which answers correctly, in the slot
    # the message arrived on -- can never fire here. It has to be a PUSH, the
    # same shape as `@ComGameInit` above.
    #
    # WARNING: msgid 5 is polled by THREE screens (`0x002f3cb0` here,
    # `0x002faa10`, `0x00365280`) and all three raise 339 on timeout, so this is
    # the console's general "leave acknowledged" and the same gap very likely
    # hangs the exit of those screens too.
    #
    # WARNING: Staggered BEHIND the board on purpose: same-batch delivery lands it
    # in the store before anything looks for it, and the client only reads msgid
    # 5 while it is in a leave-wait. `POL_TM_COM_QUIT_PRELOAD=0` disables.
    if common._env_int("POL_TM_COM_QUIT_PRELOAD", 1):
        _no = common._env_int("POL_TM_MATCHQUIT_NO", 0)
        _b = common._env_int("POL_TM_MATCHQUIT_B", 0)
        pushqueue._queue_push(member_id,
                    protocol.encode_code(protocol.COMQUIT_MSGID)
                    + (b"@Quit=/No=%d/B=%d" % (_no, _b)),
                    why="VS. COM: the leave ack (@Quit)",
                    after=common._env_int("POL_TM_COM_QUIT_GAP", 2),
                    peer=peer_nick)
        common._say("tm: member %s VS. COM -- pre-loaded @Quit=/No=%d/B=%d into "
             "(0x43, 5), the slot CQMComConfig state 4 polls when you leave. "
             "The console never SENDS @Quit (one xref in TMaster.pex, the "
             "receive arm), so without this the leave times out with -1-37339. "
             "(POL_TM_COM_QUIT_PRELOAD=0 disables)" % (member_id, _no, _b))

    # \U0001f534 AND THE STEP AFTER THAT: `@GameEA=/Exit=1` INTO (0x41, 0x0E).
    #
    # MEASURED off the second leave-hang savestate, once the `@Quit` above had
    # moved `CQMComConfig` from state 4 to state **6** (proved live: `[+301]`
    # read 5 before the fix and 6 after). State 6 only raises the parent's
    # progress bar 80 -> 100 when event **`0x03001f57`** arrives, and that event
    # is posted by **`CComPart`** (`0x00424600`, the leave counterpart of
    # `CComJoin`) from its state 7.
    #
    # `CComPart` was parked in state **100** with selector **182** in its dialog
    # (`[obj+320]`), raised from its state 1: `0x003b0c70` fetches
    # **(code 0x41, msgid 0x0E)**, matches **`@GameEA`** (`0x00486BD0`) and reads
    # **`/Exit=`** (`0x00486C80`) -- and returns 0 when the message is absent.
    #
    # We already BUILD exactly that (`GAMEEXIT_ANS_MSGID` + `@GameEA=/Exit=1`),
    # but only as a REPLY to `@GameExit=` -- and the console never sent one
    # (authserv.log 00:17: nothing at all after `@PLAYACK`). Its `CComPart`
    # state 0 does reference `@GameExit=` (`0x003b09a4`) and then advances
    # unconditionally, so state 1 polls whether or not anything reached us.
    # Third instance of the same console pattern: it polls a slot instead of
    # asking. `POL_TM_COM_EXIT_PRELOAD=0` disables.
    if common._env_int("POL_TM_COM_EXIT_PRELOAD", 1):
        _ex = common._env_int("POL_TM_GAMEEXIT_EN", 1)
        pushqueue._queue_push(member_id,
                    protocol.encode_code(matchmaking.GAMEEXIT_ANS_MSGID)
                    + (b"@GameEA=/Exit=%d" % _ex),
                    why="VS. COM: the part ack (@GameEA)",
                    after=common._env_int("POL_TM_COM_EXIT_GAP", 3),
                    peer=peer_nick)
        common._say("tm: member %s VS. COM -- pre-loaded @GameEA=/Exit=%d into "
             "(0x41, 0x0E), the slot CComPart state 1 polls while leaving. "
             "Without it CComPart parks at selector 182 and never posts "
             "0x03001f57, so the progress bar stays clamped at 80. "
             "(POL_TM_COM_EXIT_PRELOAD=0 disables)" % (member_id, _ex))

    # WARNING: DEFAULT OFF AS OF THE LIVE READ. The `@MuchMake` pair below was
    # delivered cleanly (log 06:32:38/06:32:40, correctly folded TblId) and the
    # scene did not move -- and `probe_lookup` then showed why: the COM scene
    # polls NOTHING but the ambient set, 0xD2 among the codes it never asks for.
    # Kept switchable rather than deleted because the receivers at 0x854B0 /
    # 0x85790 are real and still unplaced, but it is a no-op here and must not
    # read as load-bearing.
    if not common._env_int("POL_TM_COM_MUCHMAKE", 0):
        return
    tid_s, no = _gameeca_table(member_id)
    try:
        tblid = int(tid_s, 16)
    except (TypeError, ValueError):
        common._say("tm: no COM @MuchMake -- table id %r is not hex" % (tid_s,))
        return
    gap = common._env_int("POL_TM_COM_MUCHMAKE_GAP", 2)
    pushqueue._queue_push(member_id,
                protocol.encode_code(matchmaking.MATCH_PUSH_CODE)
                + b"@MuchMake=/TblNo=%d/TblId=%016X" % (no, tblid),
                # after=1, NOT 0. "0 = the very next one" INCLUDES the reply
                # being built right now -- measured on the bench: after=0 put
                # @MuchMake in the same batch as @GameECA, which is precisely
                # the scene-change drain this stagger exists to avoid.
                why="VS. COM: the table", after=1)
    if common._env_int("POL_TM_COM_MATCH_START", 1):
        pushqueue._queue_push(member_id,
                    protocol.encode_code(matchmaking.MATCH_START_CODE)
                    + b"@MuchMake=/Start=%d"
                      % common._env_int("POL_TM_COM_MATCH_START_N", 1),
                    why="VS. COM: the start", after=1 + gap)
    common._say("tm: member %s VS. COM -- queued @MuchMake /TblNo=%d /TblId=%016X "
         "then /Start= %d reply(ies) later (POL_TM_COM_MUCHMAKE=0 disables)"
         % (member_id, no, tblid, gap))


def _gameeca_table(member_id):
    """`(TblId, TblNo)` for **`@MuchMake`** -- NOT for `@GameECA`, see
    `_gameeca_en`. Unused until `@MuchMake` is implemented; kept because the
    room-fold reasoning below is the part that was hard to get right.

    A REAL table in the player's own room, named exactly as that room's
    `b/g/PTL` names it.

    WARNING: THE FIRST VERSION OF THIS WAS WRONG TWICE, and the client said so:
    `@GameECA` was accepted (the `@GameENC=` retry loop stopped) and then the
    session died with Lobby.BIN 448 "Could not play against the computer."
    Both defects point the same way -- we named a table that does not exist:

      1. **`TblNo` was 17**, deliberately "past the 16 playable rows so it
         cannot collide". But the client only ever hears about tables 1..16;
         17 is not a tile it holds. Sibling string 447, "Could not play against
         computer as this table is not yet ready", says this family of error is
         ABOUT THE TABLE.
      2. **`TblId` was the PRE-FOLD id.** `tmroom.canonical_table_id(n, 0)` is
         not what any room serves -- ids are ROOM-FOLDED
         (`canonical_table_id(n, room_no)`) precisely so one room's table 1
         cannot match another's. So even the id for a valid number named a
         table in no room at all. This is the same fold that
         `_normalise_table_ids` exists to apply, and the same class of bug as
         the my-reservation collision it fixed.

    So resolve the member's ACTUAL room and hand back that room's table, with
    the fold applied. `POL_TM_GAMEECA_TBLNO` picks the table number (1-based,
    default 1) and `POL_TM_GAMEECA_TBLID` still force-overrides the id for a
    sweep.

    WARNING: Still unmeasured: whether a COM game wants a lobby table at all, rather
    than a slot on the `'E'` server (whose capacity check at `0x76338` is
    `>= 8`, so a slot index would be 0..7). If a real, correctly-folded table
    still fails, THAT is the next hypothesis -- and it is why the override
    survives.
    """
    no = common._env_int("POL_TM_GAMEECA_TBLNO", 1)
    tid = os.environ.get("POL_TM_GAMEECA_TBLID")
    if tid:
        return tid, no
    room_no = 0
    try:
        import tmroom
        chan = tmroom.room_of(member_id) if member_id is not None else None
        room_no = tmroom._room_no(chan) if chan else 0
        if not room_no:
            # SAY SO. room_no 0 falls back to the PRE-FOLD id, which is exactly
            # the value that just failed live -- an id no room serves. If this
            # line appears, the reply is wrong and the member placement is the
            # bug, not the table number.
            common._say("tm: \U0001f534 member %s @GameENC= but no room record places "
                 "them -- /TblId= falls back to the PRE-FOLD id, which names a "
                 "table in NO room and is what Lobby.BIN 448 rejected before. "
                 "Fix placement, not the table number." % (member_id,))
        return "0x%016X" % tmroom.canonical_table_id(no - 1, room_no), no
    except Exception:
        # Never let a lookup failure turn into silence -- an unanswered
        # `@GameENC=` is the hang this whole branch exists to end.
        return "0x%016X" % (0x0000002100000000 | (no << 12) | no), no
