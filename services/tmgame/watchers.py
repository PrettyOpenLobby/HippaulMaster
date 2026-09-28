"""Watching a match in the client: the watcher list, the watch stream and its purges, @WFCard and
@WatchInfo.
"""
import tmbattle
from . import common, protocol, pushqueue


#: (chan, index) -> {push_key: (member_id, table peer nick)} -- THE WATCHERS.
#: An observer JOINs the table's own channel and sends `@Data=/Watch=` on
#: (0x43, 37) (measured live 2026-08-22T20:07Z);
#: after the @WatchInfo snapshot it consumes the
#: same in-match pushes the players get, pinned to the table peer it @Pongs.
_MATCH_WATCHERS = {}


def _watchers_of(chan, index):
    return list((_MATCH_WATCHERS.get((chan, index)) or {}).values())


#: WARNING: RETRACTED 2026-09-03, SAME DAY: the watcher DOES read cmd 9/10/12 -- but
#: only AFTER it has entered the play scene. The CLIENT TRACE (shim-CASPC,
#: `Recv=WATCHCARD` -> `GamePlayInit`, and the player trace `Recv=STARTDATA` ->
#: `GamePlayInit` -> `Recv=TURNINFO` -> `Recv=PUTCARD` each turn) settles what
#: the arm scan got wrong: `@WFCard` (cmd 38) is what MOVES the watcher into the
#: play scene (its `GamePlayInit`), and once there it renders the board from the
#: SAME `@TurnData`/`@PutCard`/`@BattleData` a player reads. Dropping them left
#: the observer in an inited-but-empty play scene with nothing to draw. So the
#: watcher now gets @WFCard (the GamePlayInit trigger) AND the cmd 9/10/12 stream
#: (the render). `POL_TM_WATCH_DROP_STREAM=1` restores the drop for an A/B; a
#: brief pre-GamePlayInit pile is the tradeoff, and @WFCard fires on turn 0 so it
#: is small.
_WATCHER_DEAF_CMDS = frozenset((protocol.TURNDATA_CMD, protocol.PUTCARD_CMD, protocol.BATTLEDATA_CMD))


#: The per-turn stream a `@WatchInfo` SNAPSHOT supersedes. Every one of these
#: carries state the snapshot itself re-states -- the board, the turn, whose
#: move it is, the hands -- so a copy still sitting in the queue when the
#: snapshot goes out is BY CONSTRUCTION older than what the client is about to
#: be told. A queued `@Quit`/`@GameML`/table push is not per-turn state and is
#: NOT in here: the purge must not become "empty the queue".
_WATCH_SNAPSHOT_SUPERSEDES = frozenset((
    protocol.STARTDATA_CMD, protocol.TURNDATA_CMD, protocol.PUTCARD_CMD, protocol.BATTLEDATA_CMD,
    protocol.RESULTDATA_CMD, protocol.WFCARD_CMD))


def _watch_stream_purge(member_id, why=""):
    """Drop a watcher's QUEUED per-turn stream. Returns how many went.

    WARNING: A SNAPSHOT DOES NOT CATCH THE QUEUE UP -- IT ONLY OUTRUNS IT (fixed
    2026-09-09, live report "invalid card data detected" while watching a COM
    game). `@WatchInfo` sh=1 hands the observer the board AS OF turn T and the
    client starts drawing from there. Nothing dropped what was already queued
    for turns BEFORE T -- and after a rematch that is an entire PREVIOUS match,
    because a watcher stays registered in `_MATCH_WATCHERS` across the game
    boundary while `_PUSHES` keeps filling.

    Measured, member 3 watching table 1:

        04:07:27.745..746  TWENTY-ONE bodies in one millisecond -- the previous
                           game's turns 3..10, its @ResultData, and then the NEW
                           game's turn-0 @TurnData
        04:07:33.789       the client handshakes; we answer a snapshot at /T=1
        04:07:35.793       turn 0's @BattleData + @PutCard arrive BEHIND it
        04:07:39.796       turn 1's @TurnData

    The client filed turn 0's `@PutCard` into msgid slot 0x0a and never consumed
    it -- its scene was already past turn 0 -- then read turn 1's `@TurnData`,
    advanced (`GamePlayLink:Turn=1`), polled its put slot and found that stale
    card:

        Errot!!Turn1!=0  ->  (0x43, 40) @DataError, fixed text
                             "Invalid card data detected. Exiting Tetra Master."

    KEY: THE GAP CANNOT FIX THIS AND NEVER COULD. `POL_TM_WATCH_PUT_GAP` orders a
    render against the @TurnData of its OWN turn; it says nothing about a body
    from a turn the client has already been told it is past. And once a watcher
    falls behind, `_idle_pushes_locked` hands over EVERY entry at counter <= 0 in
    one batch (the 21 above), so `after=N` stops separating anything at all.
    The snapshot has to CLEAR what it supersedes rather than race it.

    `POL_TM_WATCH_SNAPSHOT_PURGE=0` restores the racing behaviour for an A/B.
    """
    if common._env_int("POL_TM_WATCH_SNAPSHOT_PURGE", 1) != 1:
        return 0
    key = pushqueue._push_key(member_id)
    if key is None:
        return 0
    dropped = []
    with pushqueue._PUSH_LOCK:
        q = pushqueue._PUSHES.get(key)
        if not q:
            return 0
        keep = []
        for entry in q:
            try:
                cmd = (int(entry[1][:8], 16) >> 8) & 0xFF
            except (ValueError, TypeError, IndexError):
                cmd = -1
            if cmd in _WATCH_SNAPSHOT_SUPERSEDES:
                dropped.append(entry[1])
            else:
                keep.append(entry)
        if keep:
            pushqueue._PUSHES[key] = keep
        else:
            pushqueue._PUSHES.pop(key, None)
    if dropped:
        common._say("tm: 🔭 cleared %d queued per-turn push(es) for member %s -- the "
             "@WatchInfo snapshot supersedes them%s"
             % (len(dropped), member_id, (" (%s)" % why) if why else ""))
        for body in dropped[:24]:
            common._say("tm:   ...superseded: %s" % protocol.describe_line(body))
        if len(dropped) > 24:
            common._say("tm:   ...and %d more" % (len(dropped) - 24))
    return len(dropped)


#: The `after` a watcher's purgeable render messages (@PutCard/@BattleData/
#: @WFCard) ride on. See `_watch_push`. It is a TWO-SIDED constraint, not "as
#: late as possible" -- the render must land AFTER the @TurnData that enters its
#: own turn N (queued a cycle earlier, so further decremented) and BEFORE the
#: @TurnData that enters turn N+1 (queued right after it, same cycle). Both
#: @TurnData ride `after=1`, so the render must ride `after=1` too: it then
#: delivers behind turn N's @TurnData (which is ahead of it in the queue) and
#: ahead of turn N+1's @TurnData (which is behind it, same drain, list order).
#: WARNING: after=0 overtakes turn N's own @TurnData -> Errot!!Turn(N-1)!=N (the
#: original crash); after=2 overshoots turn N+1's @TurnData -> Errot!!Turn(N+1)
#: !=N (a crash this gap CAUSED at 2, live 2026-09-06). 0 disables the floor
#: entirely (restores the racing after=0) for an A/B.
def _watch_render_gap():
    return max(0, common._env_int("POL_TM_WATCH_PUT_GAP", 1))


def _watch_push(chan, index, body, why, after=0):
    """One body to every watcher of this match, pinned to the table peer.

    Relays the in-match stream (@TurnData/@PutCard/@BattleData) the watcher reads
    once its play scene is up -- @WFCard (`_watch_wfcard`) is what starts that
    scene. `POL_TM_WATCH_DROP_STREAM=1` reverts to dropping the stream.

    WARNING: THE OBSERVER PURGE-RACE GAP (2026-09-06). A watcher's play scene has no
    ack, so it cannot tell the server it has entered a turn. GamePlayEnd
    (`0xC7642`) purges the receive store of EVERY per-turn message --
    `@WFCard`/`@Start`/`@PutCard`/`@BattleData`, msgids {0x25,0x26,7,8,0xA,0xC,
    0x28} -- and keeps ONLY `@TurnData` (9). So a render message that lands in
    the same drain as the `@TurnData` that enters its turn is DELETED, the client
    reaches the turn with no card, reads the NEXT turn's put, and prints
    `Errot!!Turn` -> "invalid card data detected" (live, watching a COM game).
    A player never hits this because the ack gate holds each turn's put until the
    player has acked past the purge; a passive watcher can't ack, so its
    purgeable render rides a fixed GAP behind the @TurnData that leads it.
    `@TurnData` itself is exempt -- it survives the purge and MUST lead.
    `POL_TM_WATCH_PUT_GAP=0` restores the racing behaviour."""
    try:
        # The 8-char header is little-endian bytes [0x43, 00, cmd, turn]
        # (`_turn_code`/`encode_code`), so the command is byte 2 -> >>8.
        cmd = (int(body[:8], 16) >> 8) & 0xFF
    except (ValueError, TypeError):
        cmd = -1
    if common._env_int("POL_TM_WATCH_DROP_STREAM", 0) and cmd in _WATCHER_DEAF_CMDS:
        return
    if cmd in (protocol.PUTCARD_CMD, protocol.BATTLEDATA_CMD):
        after = max(int(after), _watch_render_gap())
    for wmid, wpeer in _watchers_of(chan, index):
        pushqueue._queue_push(wmid, body, "watcher: " + why, after=after, peer=wpeer)


def _wfcard_body(tile, row, turn):
    """`@WFCard=` (cmd 38, arm 0x1020F4) for ONE board tile. Decoded 2026-09-03:
    `/T=` is the TURN ([obj+0x18d]); the tile is named by an `@<tile>` SECTION
    marker; `/D=` is the same 6-value row `@PutCard` carries --
    `id|attack|type|pdef|mdef|power` (arrows/flag are DERIVED from the card DB by
    id, not sent). WARNING: The arm writes NO owner -- a flip's colour change is not
    expressible here; owners come only from the initial @WatchInfo `/F=`."""
    d = b"|".join(b"%d" % int(row[f]) for f in tmbattle.ROW_FIELDS[:6])
    return b"@WFCard=/T=%d@%d/D=" % (int(turn), int(tile)) + d


def _watch_wfcard(chan, index, turn, tiles, board):
    """Push one @WFCard per named tile to every watcher -- THE watcher's only
    live board-update channel (`_watch_push` drops the cmd 9/10/12 stream it
    cannot read). WARNING: `@<tile>` is matched as a raw substring by the client
    (0xaa920), so a multi-digit tile (>=10) may collide with a single-digit one.

    VERIFIED: RE-ENABLED 2026-09-03: @WFCard is what TRANSITIONS the watch scene into
    the play scene (`Recv=WATCHCARD` -> `GamePlayInit`, measured shim-CASPC).
    Without it the dealt @StartData/@TurnData/@PutCard just FILE unread (the
    watch scene polls only cmd 38). The turn-desync it caused before was a
    MISSING BASELINE, not @WFCard itself -- the watcher now also gets the deal's
    @StartData + turn-0 @TurnData (see "DEAL THE WATCHERS TOO"), so the play
    scene consumes cmd 8 -> 9 -> 10 in order. `POL_TM_WATCH_WFCARD_LIVE=0`
    disables it for an A/B."""
    if not common._env_int("POL_TM_WATCH_WFCARD_LIVE", 1):
        return
    watchers = _watchers_of(chan, index)
    if not watchers:
        return
    for t in tiles:
        card = board.get(int(t))
        if card is None:
            continue
        body = protocol._turn_code(protocol.WFCARD_CMD, turn) + _wfcard_body(t, card.row, turn)
        for wmid, wpeer in watchers:
            # @WFCard (0x26) is purged at GamePlayEnd too, so it rides the same
            # observer gap as @PutCard/@BattleData -- behind the @TurnData that
            # enters the turn (see `_watch_push`, the purge-race note).
            pushqueue._queue_push(wmid, body, "watcher: @WFCard tile %d (card %d)"
                        % (t, int(card.row["id"])),
                        after=_watch_render_gap(), peer=wpeer)
        common._say("tm:   ...watcher board: @WFCard tile %d = card %d, turn %d"
             % (t, int(card.row["id"]), turn))


def _watchinfo_body(names_hex, rules, n, mid_game=None):
    """`@WatchInfo=` -- the observer's state snapshot (arm 0x101B15).

    `names_hex` is one UPPERCASE-hex name per ABSOLUTE seat (occ order --
    the arm applies no rotation; a watcher lives in the absolute frame).
    `mid_game` is None for the pre-game form (sh=0) or a dict with
    turn/active/board/hand counts for the sh=1 form -- the arm reads
    /H /T /S /A /F /CH only when the envelope sh is 1.

    WARNING: /F= and /CH= per-tile encodings are the arm's WRITES read backwards
    (low 6 bits -> [obj+0x1F5+i], bits 6-7 -> the tile record's owner slot,
    /CH= -> the tile record's first byte); which values render correctly is
    for the first live observe to measure -- serve (owner+1)<<6 and the
    card id, logged as the experiment they are.

    VERIFIED: THE LOW SIX BITS NOW CARRY SOMETHING REAL. `[obj+0x1F5+i]` is the
    OBJECT CODE array (`tmbattle.roll_board`), so an unplayed block or special
    tile is sent as its own code -- the same byte `@StartData` puts there --
    while a tile a player holds keeps the owner-bit form. A CAPTURED block
    renders as its captor's tile and carries no card face, because a pseudo
    card id (0x8000+) is not something `/CH=` can name.
    """
    body = bytearray(b"@WatchInfo=/N=%d" % n)
    body += b"/CN=" + b"|".join(names_hex)
    body += b"/R=" + b"|".join(b"%d" % v for v in list(rules)[:7])
    body += b"/Q=" + b"|".join([b"0"] * n)
    body += b"/C=" + b"|".join([b"0"] * n)
    if mid_game:
        counts = mid_game.get("counts") or [0] * n
        body += b"/H=" + b"|".join(b"%d" % v for v in counts[:n])
        body += b"/T=%d" % int(mid_game.get("turn") or 0)
        body += b"/S=%d" % int(mid_game.get("s") or 0)
        body += b"/A=%d" % int(mid_game.get("active") or 0)
        tiles = int(mid_game.get("tiles") or 16)
        board = mid_game.get("board") or {}
        objects = mid_game.get("objects") or []
        fvals, chvals = [], []
        for t in range(tiles):
            card = board.get(t)
            code = int(objects[t]) if t < len(objects) else 0
            held = (card is not None and card.owner is not None
                    and int(card.owner) < n)
            if held:
                fvals.append(b"%d" % (((int(card.owner) + 1) & 3) << 6))
                # A pseudo card (a captured block) has no face to name.
                cid = int(card.row["id"])
                chvals.append(b"%d" % (cid if cid < 250 else 0))
            elif code:
                fvals.append(b"%d" % (code & 0x3F))
                chvals.append(b"0")
            else:
                fvals.append(b"0")
                chvals.append(b"0")
        body += b"/F=" + b"|".join(fvals)
        body += b"/CH=" + b"|".join(chvals)
    body += b"/L=" + b"|".join([b"0"] * n)
    return bytes(body)
