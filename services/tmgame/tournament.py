"""The event (tournament): the countdown, the matchmaker, the event hand, scoring, prizes and the
live ticker.
"""
import os
import tm_cardprm
import tmbattle
import time
from tmcup import event_info, event_missions, event_phase, event_prizes, event_window
from . import (
    boardrules, cardshop, cardtables, collection, common, dispatch, matchend, matchmaking,
    protocol, purse, pushqueue, scoring,
)


#: THE EVENT COUNTDOWN. `@EventTimeReq=` is sent by the event loader (0x96A22)
#: and by the member-list pump (0x975CF), and BOTH block on the answer -- so an
#: unanswered one is the same 75-second dead wait `@Quit=` and `@CvReq=` were.
#:
#: The three fields are stored to 0x2B5F9C/0x2B5FA0/0x2B5FA4 by the parser at
#: 0x8C2E9 and read back by the event main scene at 0x6F7A6, which tests exactly
#: one thing:
#:
#:     if Start == -1 and End == -1:  no event -- clear the clock, go to step 2
#:     else:                          [scene+0x1D8] = Now / 3600   (hours)
#:                                    [scene+0x1DC] = Now % 3600 / 60 (minutes)
#:
#: So **Start = End = -1 is the honest "no event is running" answer**, and it is
#: also the one that lets the scene move on rather than sit on a clock counting
#: down to something we do not serve. That is the default here on purpose: the
#: point of answering at all is to stop the hang, not to invent an event.
#: `Now` is a countdown in SECONDS, not a wall clock.
def _eventtime_enabled():
    return os.environ.get("POL_TM_EVENTTIME", "1") == "1"


def _eventtime_fields():
    def get(name, default):
        try:
            return int(os.environ.get("POL_TM_EVENT_" + name, str(default)), 0)
        except ValueError:
            return default
    return get("START", -1), get("END", -1), get("NOW", 0)


#: THE EVENT SHOP'S CARD DOOR. `@EInitReq=` is state 3 of the card machine
#: (0x9A1C2 -> 0x8B9B0); state 2 is the `@CardReq=` we already answer. The reply
#: parser is 0x8BE34 and it reads two fields:
#:
#:     /Card=  -> stored straight into the caller's out-param (an int, NOT a
#:                card list -- the `@Card=` family is a different message)
#:     /Ans=   -> if > 0, take OUR sender id as the endpoint (0x2B2958/0x2B295C),
#:                the same global `@ShEnter` and `@CvEnter` write
#:
#: WARNING: This can only ever fire once the Event Shop scene is OPEN, and the event-loader reading says
#: that needs us to place the player in the top three of an event ranking we do
#: not yet serve. It is here so the door is not the thing that blocks; do not
#: read silence on it as evidence about `/Card=`.
#: THE TOURNAMENT TEST MEMBERS -- the same list the lobby serves the event zone
#: to (tmtitle._zl_event_zone). Empty = nobody, the default.
_EVENT_JOIN_SHOP = 1


def _event_test_member(member_id):
    spec = os.environ.get("POL_TM_EVENT_ZONE_MEMBERS", "").strip()
    if not spec or member_id is None:
        return False
    allowed = {x.strip() for x in spec.split(",") if x.strip()}
    return "*" in allowed or str(member_id) in allowed


#: THE TOURNAMENT MATCHMAKER (static reading of the client).
#:
#: A player's event status is character 13 of the 16-letter status block their
#: `<DE>`/`<PD>` carries in value 10 (PC 0x2B5F98): 'A' in the room with the
#: Reservation box ON (matchable), 'C' box off, 'E' entering, 'B' in an event
#: game, 'D' watching. Nothing is SENT on the Reservation click; the match is a
#: server push the tournament screen polls every frame (0x854B0):
#:
#:   1. (0xD2, 0) @MuchMake=/TblNo=<n>/TblId=<16 hex> to each matched player.
#:      /TblId= is the +0x00 id of an 'A' table row in the room's PTL as the
#:      client holds it (the fixture's #TM0T<n>), NOT the room or the host.
#:   2. The client answers (0xD3, 0) @MuchMakeAns=/Ans=1 (0 = declined), after
#:      DISCARDING any pending (0xD2, 1) -- so the start must come after it.
#:   3. (0xD2, 1) @MuchMake=/Start=1 once everyone has accepted; /Start=-1 to
#:      the others if anyone declines, which makes their client give up cleanly.
#:
#: Then CEventGameStart does the ordinary table handshake (@GameEN, JOIN, @Req,
#: @GameOK, (0x41,9)), which this module already answers, and the board comes
#: as @EventGameInit (same fields as @VsGameInit, no wager) -- see
#: `_queue_vsgameinit`. UNMEASURED past the push: the first live pair is the test.
_EVENT_STATUS = {}          # member -> (status letter, room id, time)
_EVENT_MATCH = {}           # member -> match dict (shared by both players)
_EVENT_MATCH_TTL = 60.0     # an unanswered match expires after this
_EVENT_STATUS_IDX = 13


def _event_room_key(values):
    try:
        return int(str(values[9]).strip("()"), 16)
    except (ValueError, IndexError, TypeError):
        return None


def _event_letter(values):
    try:
        return str(values[10])[-16:][_EVENT_STATUS_IDX]
    except (IndexError, TypeError):
        return None


def _event_grace():
    """Seconds a player's Reservation box must stay ON before they are paired.
    The client ticks it by itself on entry (PC 0x6D6B1), so without this a
    player walking in is matched before they can untick it."""
    return max(0, common._env_int("POL_TM_EVENT_MATCH_GRACE", 20))


def returning_from_game(member_id):
    """True when this member's tournament game has been played and they
    have not been released back to the room yet."""
    m = _EVENT_MATCH.get(member_id)
    return bool(m and m.get("playing"))


def _in_event_match(member_id):
    m = _EVENT_MATCH.get(member_id)
    return bool(m and m.get("started"))


def event_tick(member_id):
    """Called on every roster <DR> poll (~2 s) so a pair forms the moment the
    grace period runs out, not only on the next status change."""
    if member_id is None or not _event_test_member(member_id):
        return
    _event_phase_push(member_id)
    _ticker_countdown_tick()
    if event_phase()[0] == "over":
        _publish_games_live(force=False)
    st = _EVENT_STATUS.get(member_id)
    if st is None:
        _event_seed(member_id)
        st = _EVENT_STATUS.get(member_id)
    if st is not None:
        _event_try_pair(st[1])


#: member -> the phase their client last heard (from the time answer or a push).
#: PERSISTED (Valkey, _SEEN_KEY): 2026-10-03 test cup, authsess restarted at
#: 04:12 wiped this dict, the clients on the tournament screen never re-sent
#: @EventTimeReq, so at 04:15 nobody was told the event ended and every client
#: gave up with "could not exit the tournament correctly".
_EVENT_SEEN = {}
_SEEN_KEY = "tm:event-seen"
_SEEN_LOADED = [False]


def _seen_restore():
    if _SEEN_LOADED[0]:
        return
    _SEEN_LOADED[0] = True
    try:
        import tmstore
        for k, v in (tmstore.kv.get_json(_SEEN_KEY) or {}).items():
            _EVENT_SEEN.setdefault(int(k), str(v))
    except Exception as exc:                                    # noqa: BLE001
        common._say("tm: tournament screen list not restored (%r)" % (exc,))


def _seen_save():
    try:
        import tmstore
        tmstore.kv.set_json(_SEEN_KEY, {str(k): v for k, v in _EVENT_SEEN.items()},
                            ttl=12 * 3600)
    except Exception:                                           # noqa: BLE001
        pass


def note_seen(member_id, phase):
    """A member's client has heard `phase` (the time answer)."""
    _seen_restore()
    _EVENT_SEEN[member_id] = phase
    _seen_save()


def _event_time_push(kind):
    """The (0xD1, msgid) @EventTime pushes (no sender or
    shop gate): start = games begin (msgid 1, /Start= stored, never read) plus
    a msgid-3 resync so Time Left is exact; end = time is up (msgid 2); close =
    results final (msgid 8, /ECM= = index of the 'F' prize door row, 0)."""
    if kind == "start":
        _ph, ws, we = event_phase()
        return [protocol.encode_code(0x000100D1) + b"@EventTime=/Start=0",
                protocol.encode_code(0x000300D1) + (b"@EventTime=/Start=-1/End=%d/Now=0"
                                           % max(0, int(we - time.time())))]
    if kind == "end":
        return protocol.encode_code(0x000200D1) + b"@EventTime=/End=0"
    return protocol.encode_code(0x000800D1) + b"@EventTime=/Close=1/ECM=0"


def _event_phase_push(member_id):
    """Tell a member on the tournament screen when the phase moves on."""
    _seen_restore()
    seen = _EVENT_SEEN.get(member_id)
    if seen is None:
        return
    ph = event_phase()[0]
    if ph == seen:
        return
    order = ["pending", "running", "over", "closed"]
    bodies = []
    if seen == "pending" and ph == "running":
        bodies += _event_time_push("start")
    if order.index(seen) <= 1 and order.index(ph) >= 2:
        bodies.append(_event_time_push("end"))
    if ph == "closed" and seen != "closed":
        bodies.append(_event_time_push("close"))
    if order.index(ph) < order.index(seen):
        # A new window began (closed -> pending/running): their screen is the
        # old event; they re-enter to see the new one. Nothing to push.
        _EVENT_SEEN[member_id] = ph
        _seen_save()
        return
    _EVENT_SEEN[member_id] = ph
    _seen_save()
    for i, b in enumerate(bodies):
        pushqueue._queue_push(member_id, b, "tournament %s -> %s" % (seen, ph), after=i)
    common._say("tm: tournament phase %s -> %s for member %s (%d push(es))"
         % (seen, ph, member_id, len(bodies)))


def _event_seed(member_id):
    """Pick up a member's status from tmroom's PERSISTED records when this
    process has not heard their <DE>/<PD> -- a deploy restart wiped the dict
    and the first live pair (2026-09-26) never formed because of it."""
    try:
        import tmroom
        chan = tmroom.room_of(member_id)
        if not chan:
            return
        for mid, vals in tmroom.members(chan):
            if str(mid) == str(member_id):
                letter = _event_letter(vals)
                if letter:
                    _EVENT_STATUS[member_id] = (letter, _event_room_key(vals),
                                                time.time())
                return
    except Exception as exc:
        common._say("tm: event status for member %s not seeded (%r)" % (member_id, exc))


def note_event_status(member_id, values):
    """Called for every roster <DE>/<PD>; pairs two reserved test members."""
    if member_id is None or not _event_test_member(member_id):
        return
    letter = _event_letter(values)
    if letter is None:
        return
    room = _event_room_key(values)
    now = time.time()
    prev = _EVENT_STATUS.get(member_id)
    # The timestamp is when the letter was ENTERED, so the grace period counts
    # from the moment the box went on, not from the latest repeat of it.
    since = prev[2] if prev is not None and prev[0] == letter else now
    _EVENT_STATUS[member_id] = (letter, room, since)
    if prev is None or prev[0] != letter:
        common._say("tm: event status member %s -> %r (room %s)"
             % (member_id, letter, "?" if room is None else "%#x" % room))
    m = _EVENT_MATCH.get(member_id)
    if m is not None:
        if letter == "B":
            m["playing"] = True
        elif letter == "A" and m.get("playing"):
            _event_match_release(m, member_id)
        elif letter == "E" and m.get("started"):
            m.setdefault("entered", set()).add(member_id)
        elif (letter == "A" and m.get("started")
              and member_id in m.get("entered", ())):
            # THE START BOUNCED: accepted, went to the table ('E'), back in the
            # room without ever playing ('B'). The 2026-10-03 cup's "Could not
            # start game" (twice, 02:22 and 02:38): a started match is never
            # declined or expired, so both players were unpairable until the
            # next restart.
            _event_start_failed(m, member_id)
        elif letter == "C":
            _event_match_decline(m, member_id, "reservation turned off")
    _event_try_pair(room)


#: frozenset(pair) -> when its start bounced; that pair is not re-paired for
#: _EVENT_FAILED_HOLD while anyone else could be matched instead.
_EVENT_FAILED = {}
_EVENT_FAILED_HOLD = 300.0
#: a started match that never reaches 'B' is cleared after this
_EVENT_START_TTL = 120.0


def _event_start_failed(m, member_id):
    _EVENT_FAILED[frozenset(m["who"])] = time.time()
    _EVENT_TABLE_USED[m["index"]] = time.time()
    if _EVENT_MATCH.get(member_id) is m:
        _EVENT_MATCH.pop(member_id, None)
    left = [mid for mid in m["who"] if _EVENT_MATCH.get(mid) is m]
    common._say("tm: event match at table %d: member %s came back without "
                "playing -- the start FAILED, released%s"
                % (m["index"], member_id,
                   (" (waiting on %s)" % left) if left else ""))
    _publish_games_live()


def _event_present(room):
    """Member ids tmroom says are IN this room now, or None if it cannot say.
    Live 2026-09-26: a player back on the Event List still had a stale 'A'
    and the other player was sent a match against nobody."""
    try:
        import tmroom
        return {str(mid) for mid, _v in tmroom.members("#TM0R%03d" % (room & 0xFFF))}
    except Exception:
        return None


def event_left_room(member_id):
    """A member's <PC> (leaving) or a closed room band: forget their status."""
    if _EVENT_STATUS.pop(member_id, None) is not None:
        common._say("tm: event status member %s forgotten (left the room)" % (member_id,))
    m = _EVENT_MATCH.get(member_id)
    if m is not None:
        _event_match_decline(m, member_id, "left the room")


#: table index -> when a match last used it (paired, ended or failed); pairing
#: takes the least recently used free table.
_EVENT_TABLE_USED = {}


_GAMES_PUBLISHED = [0.0]


def _publish_games_live(force=True):
    """Tell both processes how many tournament games are live (tmcup
    games_live): the results close as soon as this reaches 0 after time-up."""
    now = time.time()
    if not force and now - _GAMES_PUBLISHED[0] < 2.0:
        return
    _GAMES_PUBLISHED[0] = now
    try:
        import tmcup
        live = {id(m) for m in _EVENT_MATCH.values() if m.get("started")}
        tmcup.note_games_live(event_window()[1], len(live), now)
    except Exception:                                        # noqa: BLE001
        pass


def _event_match_clear(m, why):
    _EVENT_TABLE_USED[m["index"]] = time.time()
    for mid in m["who"]:
        if _EVENT_MATCH.get(mid) is m:
            _EVENT_MATCH.pop(mid, None)
    common._say("tm: event match at table %d cleared (%s)" % (m["index"], why))
    _publish_games_live()


def _event_match_release(m, member_id):
    """One player of a finished event game is back in the room: let THEM go.

    It used to clear the match for BOTH players, so the one still on the
    result screen lost `_in_event_match` -- their @Ready= then took the
    ordinary PvP path and never got the event @Continue= the board waits on
    with no timeout (the PC's 80% bar). Measured on a test host 2026-09-26: the
    second player's @Ready= came 4 s after the first had gone back.
    """
    _EVENT_TABLE_USED[m["index"]] = time.time()
    if _EVENT_MATCH.get(member_id) is m:
        _EVENT_MATCH.pop(member_id, None)
    left = [mid for mid in m["who"] if _EVENT_MATCH.get(mid) is m]
    common._say("tm: event match at table %d: member %s back in the room%s"
         % (m["index"], member_id, (" -- waiting on %s" % left) if left else " -- match over"))
    _publish_games_live()


#: member -> table index of a match they were told was called off, so a late
#: answer to that same offer is not called off a second time (a spare queued
#: /Start=-1 would cancel their NEXT match).
_EVENT_CALLED_OFF = {}


def _event_call_off_body():
    return protocol.encode_code(matchmaking.MATCH_START_CODE) + b"@MuchMake=/Start=-1"


def _event_match_decline(m, member_id, why):
    if m.get("started"):
        return
    # EVERY other player who was offered it, not only those who already
    # answered: 2026-10-03 05:00 the one who had not answered yet accepted the
    # dead offer a second later, never heard it was off, and walked to table 1
    # while the re-pair sent the other to table 2 (black screen, both alone).
    for mid in m["who"]:
        if mid != member_id:
            _EVENT_CALLED_OFF[mid] = m["index"]
            pushqueue._queue_push(mid, _event_call_off_body(), "event match called off")
    _event_match_clear(m, "member %s: %s" % (member_id, why))


def event_stale_answer(member_id, ans, tblno):
    """@MuchMakeAns= for a match this member is no longer in (or another
    table): the body to send back, or None. An accept gets a call-off unless
    that offer's call-off was already pushed."""
    if ans != 1:
        return None
    if tblno is not None and _EVENT_CALLED_OFF.get(member_id) == tblno:
        _EVENT_CALLED_OFF.pop(member_id, None)
        return None
    common._say("tm: member %s accepted a match that is gone (table %s) -- calling it off"
                % (member_id, tblno))
    return _event_call_off_body()


def _event_try_pair(room):
    ph, ws, _we = event_phase()
    if room is None or ph != "running":
        return
    now = time.time()
    # Not in the first moments of the event: 05:00:00 the first offer went out
    # in the same instant as the "games begin" push and a client declined it
    # on its own (/Ans=0) while it was still handling the start.
    if now - ws < _event_grace():
        return
    for mid, m in list(_EVENT_MATCH.items()):
        if not m.get("started") and now - m["t"] > _EVENT_MATCH_TTL:
            _event_match_decline(m, None, "nobody answered in %ds"
                                 % _EVENT_MATCH_TTL)
        elif (m.get("started") and not m.get("playing")
              and now - m.get("t_start", m["t"]) > _EVENT_START_TTL
              and _EVENT_MATCH.get(mid) is m):
            _EVENT_FAILED[frozenset(m["who"])] = now
            _event_match_clear(m, "started %ds ago and never played"
                               % _EVENT_START_TTL)
    grace = _event_grace()
    present = _event_present(room)
    ready = sorted(mid for mid, (letter, r, t) in _EVENT_STATUS.items()
                   if letter == "A" and r == room and mid not in _EVENT_MATCH
                   and (present is None or str(mid) in present)
                   and grace <= now - t < 3600)
    if len(ready) < 2:
        return
    for k, t in list(_EVENT_FAILED.items()):
        if now - t > _EVENT_FAILED_HOLD:
            _EVENT_FAILED.pop(k, None)
    pairs = [[a, b] for i, a in enumerate(ready) for b in ready[i + 1:]]
    who = next((p for p in pairs if frozenset(p) not in _EVENT_FAILED), pairs[0])
    busy = {m["index"] for m in _EVENT_MATCH.values() if m["room"] == room}
    # LEAST RECENTLY USED TABLE, not the lowest free one. 2026-10-03 cup: every
    # match put on table 1 within ~20 s of a game there ending (02:05, 02:22,
    # 02:53, and 02:38 over seat state that outlived a restart) died with
    # "Could not start game" on both clients right after @GameEA -- they read
    # the table's leftover state from the game before; matches on a rested
    # table started every time.
    free = sorted((i for i in range(1, 17) if i not in busy),
                  key=lambda i: (_EVENT_TABLE_USED.get(i, 0.0), i))
    index, tblid = None, 0
    for i in free:
        row = None
        try:
            import tmroom
            row = tmroom.fixture_table(i)
        except Exception:
            row = None
        try:
            tblid = int((row[2] if row and len(row) > 2 else "0") or "0", 16)
        except ValueError:
            tblid = 0
        if tblid:
            index = i
            break
    if index is None:
        common._say("tm: event pair %s NOT matched -- no free table with a fixture row"
             % (who,))
        return
    _EVENT_TABLE_USED[index] = now
    m = {"who": who, "room": room, "index": index, "tblid": tblid,
         "ans": set(), "t": now, "started": False, "playing": False}
    for mid in who:
        _EVENT_MATCH[mid] = m
        # NO TICKER PAGES DURING SEATING. Pages queued after the last game
        # result trickle out one per reply, and one rode the table's @GameEA
        # reply (2026-10-03 15:10:24, narration log): the client queued Pong +
        # @CheckJoinTable, wrote only the Pong, and said "Could not start
        # game". That is why a FRESH client always started and every start
        # after a finished game failed. The ticker is re-sent on return.
        pushqueue._drop_pushes_with_code(mid, 0xD7, "matched -- no ticker during seating")
    body = (protocol.encode_code(matchmaking.MATCH_PUSH_CODE)
            + b"@MuchMake=/TblNo=%d/TblId=%016X" % (index, tblid))
    common._say("tm: EVENT MATCH -- members %s at table %d (TblId %016X); pushing "
         "(0xD2,0) @MuchMake to both" % (", ".join(map(str, who)), index, tblid))
    for mid in who:
        pushqueue._queue_push(mid, body, "event match at table %d" % index)


def _event_match_answer(member_id, ans, tblno=None):
    """@MuchMakeAns= from a matched player -> the (0xD2,1) start or the call-off.
    Returns the body to send straight back to this member, or None."""
    m = _EVENT_MATCH.get(member_id)
    if m is None:
        return event_stale_answer(member_id, ans, tblno)
    if tblno is not None and tblno != m["index"]:
        # An answer to an OLDER offer (another table) must not count for this
        # match: 05:00 the server took a table-1 accept as the table-2 one.
        return event_stale_answer(member_id, ans, tblno)
    if ans != 1:
        _event_match_decline(m, member_id, "declined (/Ans=%d)" % ans)
        return None
    m["ans"].add(member_id)
    if set(m["who"]) - m["ans"]:
        common._say("tm: event match table %d: member %s accepted, waiting for %s"
             % (m["index"], member_id,
                ", ".join(str(x) for x in set(m["who"]) - m["ans"])))
        return None
    m["started"] = True
    m["t_start"] = time.time()
    _publish_games_live()
    start = protocol.encode_code(matchmaking.MATCH_START_CODE) + b"@MuchMake=/Start=1"
    common._say("tm: event match table %d: everyone accepted -- (0xD2,1) "
         "@MuchMake=/Start=1" % m["index"])
    for mid in m["who"]:
        if mid != member_id:
            pushqueue._queue_push(mid, start, "event match start at table %d" % m["index"])
    return start


def _is_event_match(members):
    """True when every member given is in the same started event match."""
    ms = {id(_EVENT_MATCH.get(mid)) for mid in members}
    return (len(ms) == 1 and _EVENT_MATCH.get(members[0]) is not None
            and _EVENT_MATCH[members[0]].get("started"))



#: THE EVENT HAND (static reading of the client). An event board
#: (mode 2) clears the hands and waits in state 0x17 for the server to deal:
#: (0x43, 7) `@EventCard=/Ok=1/D0=..D4=` from the table peer, rows of six
#: values `id|attack|type|pdef|mdef|arrows` (the client derives power), keyed
#: from the recipient's seat, i.e. D0..D4 for itself; /Ok=0 fills the cards but
#: never leaves "Selecting". No @CardSelect= ever comes back, so after it the
#: server starts the ordinary PvP deal (@StartData, @TurnData) itself, by
#: feeding each player's five cards through the @CardSelect= handler.
def _event_hand_rows(member_id):
    """Five 8-value hand rows for `member_id`: their deck 1 when it is whole,
    else the first five cards they own. [] when they own fewer than five."""
    try:
        import tmeventstate
        picked = tmeventstate.deck(event_window()[0], member_id)
        if len(picked) == 5:
            return picked
    except Exception:
        pass
    data = collection._collection_load(member_id)
    rows = []
    slots = data.get("deck_slots") or {}
    try:
        deck = [slots.get(str(i)) or slots.get(i) for i in range(5)]
    except AttributeError:
        deck = []
    if deck and all(isinstance(v, (list, tuple)) and len(v) >= 6 for v in deck):
        for v in deck:
            cid, atk, typ, pdf, mdf, arr = (int(x) for x in v[:6])
            base = tm_cardprm.row(cid) or []
            pwr = (tmbattle.power_byte(base[4], atk, pdf, mdf,
                                       base[0], base[2], base[3])
                   if len(base) > 4 else 0)
            rows.append([cid, atk, typ, pdf, mdf, pwr, arr])
    else:
        for v in (data.get("cards") or [])[:5]:
            if isinstance(v, (list, tuple)) and len(v) >= 7:
                rows.append([int(x) for x in v[:7]])
    if len(rows) < 5:
        return []
    return [("%d|%d|%d|%d|%d|%d|%d|255" % tuple(r)).encode("ascii")
            for r in rows[:5]]


def _event_deal(seats, band_of, chan=None, index=None):
    """Push each seated player's @EventCard, then start the deal as if each had
    sent @CardSelect= with those cards. `band_of(mid)` is the table peer nick."""
    # The deal reads the roster from _MATCH_ROSTER (_match_of), which a PvP
    # match gets from its announce; an event match never had one, so the deal
    # refused with "the match is gone" and pushed @DataError (live 2026-09-26).
    if chan is not None and index is not None:
        matchmaking._remember_match(chan, index, list(seats))
        scoring._EVENT_COUNTS.pop((chan, index), None)
    hands = {}
    for mid, _ident in seats:
        rows = _event_hand_rows(mid)
        if not rows:
            common._say("tm: WARNING: event deal: member %s owns fewer than five cards -- "
                 "no @EventCard; their board stays on Selecting" % (mid,))
            return
        hands[mid] = rows
    for mid, _ident in seats:
        rows = hands[mid]
        six = [b"|".join(r.split(b"|")[:5] + [r.split(b"|")[6]]) for r in rows]
        body = (protocol.encode_code(EVENTCARD_CODE) + b"@EventCard=/Ok=1"
                + b"".join(b"/D%d=%s" % (i, row) for i, row in enumerate(six)))
        common._say("tm: event deal: (0x43,7) @EventCard to member %s -- %s"
             % (mid, ", ".join(r.split(b"|")[0].decode() for r in rows)))
        pushqueue._queue_push(mid, body, "the event hand",
                    after=common._env_int("POL_TM_EVENTCARD_GAP", 1), peer=band_of(mid))
    for mid, _ident in seats:
        rows = hands[mid]
        pick = (protocol.encode_code(EVENTCARD_CODE) + b"@CardSelect=/C=5"
                + b"".join(b"@N%d=/D=%s" % (i, r) for i, r in enumerate(rows)))
        try:
            dispatch._handle_line(pick, peer_nick=band_of(mid), member_id=mid)
        except Exception as exc:
            common._say("tm: WARNING: event deal: synthetic @CardSelect for member %s "
                 "failed (%r)" % (mid, exc))


EVENTCARD_CODE = 0x00070043     # code 0x43, msgid 7: @EventCard / @CardSelect


def _event_score_game(roster, scores, chan=None, index=None):
    """A finished TOURNAMENT game moves the chocobos: record it in the shared
    standings (tmeventstate) the ranking file is built from. No-op for any
    other game. Steps and mission rules: see tmeventstate's banner."""
    try:
        mids = [m for m, _v in (roster or [])]
        if len(mids) < 2 or not scores or not _is_event_match(mids):
            return
        import tmeventstate
        top = max(scores[:len(mids)])
        winners = [i for i in range(len(mids)) if scores[i] == top]
        results = []
        for i, mid in enumerate(mids):
            if len(winners) > 1 and i in winners:
                outcome = "tie"
            elif i in winners:
                outcome = "perfect" if scoring._is_perfect_win(scores, i) else "win"
            else:
                outcome = "lose"
            results.append((mid, outcome))
        counts = scoring._EVENT_COUNTS.pop((chan, index), {}) if chan is not None else {}
        starter = (boardrules._MATCH_TURN.get((chan, index)) or {}).get("starter")
        extras = {}
        for i, (mid, outcome) in enumerate(results):
            c = counts.get(i) or {}
            extras[mid] = {"combos": int(c.get("combos") or 0),
                           "rot_flips": int(c.get("rot") or 0),
                           "chance_flips": int(c.get("chance") or 0),
                           "first_wins": int(outcome in ("win", "perfect")
                                             and starter is not None
                                             and int(starter) == i)}
        ws, _we = event_window()
        rows = tmeventstate.record_game(ws, results, event_missions(), extras)
        try:
            import tmroom
            def _nm(m):
                return tmroom.name_of(m) or "A player"
            w = [m for m, o in results if o in ("win", "perfect")]
            moment = None
            if w and dict(results)[w[0]] == "perfect":
                moment = "%s won a PERFECT game!" % _nm(w[0])
            elif w and rows.get(str(w[0]), {}).get("streak", 0) >= 3:
                moment = "%s has won %d in a row!" % (
                    _nm(w[0]), rows[str(w[0])]["streak"])
            elif w:
                moment = "%s beat %s!" % (_nm(w[0]), ", ".join(
                    _nm(m) for m, o in results if m != w[0]))
            else:
                moment = "%s tied!" % " and ".join(_nm(m) for m, _o in results)
            event_ticker_broadcast(moment, "game result")
        except Exception as exc:
            common._say("tm: ticker after result failed (%r)" % (exc,))
        common._say("tm: EVENT RESULT %s -> %s" % (
            ", ".join("member %s %s" % (m, o) for m, o in results),
            ", ".join("%s: %d step(s), missions %#x" % (m, r["steps"], r["missions"])
                      for m, r in rows.items())))
    except Exception as exc:
        common._say("tm: WARNING: event game NOT scored (%r)" % (exc,))


#: THE PRIZES (static reading of the client). The rankings screen opens
#: the Event Shop by itself for a player in the top 3 with steps, or with a
#: cleared mission (0x783A3). Its card machine sends (0x90) @CardReq= to the
#: 'F' row; the answer is @CardEnt=/Ans=1 and (0xB2,0x13) @EcmInit= carrying the
#: prize money in /PM= and the prize cards inline (/C= is MANDATORY). The
#: client adds /PM= to the money it shows and animates the cards, then sends
#: @Get= and @Quit=, which the existing shop handler answers with @EQuit. It
#: never says what it won, so the server credits it here, once per player per
#: event. Amounts are OUR policy: POL_TM_EVENT_PRIZE_MONEY (1st,2nd,3rd),
#: POL_TM_EVENT_MISSION_MONEY per cleared mission, POL_TM_EVENT_PRIZE_CARDS
#: (cards for 1st,2nd,3rd) drawn from pack POL_TM_EVENT_PRIZE_PACK.
def _event_prize_lines(member_id):
    import tmeventstate
    ws = event_phase()[1]
    board = tmeventstate.standings(ws)
    ranked = sorted(((int(r.get("steps", 0)), mid) for mid, r in board.items()),
                    reverse=True)
    rank = None
    for i, (steps, mid) in enumerate(ranked):
        if mid == str(member_id):
            # competition ranking, as the client's own sort does (0x6C5C0)
            rank = 1 + sum(1 for s2, _m in ranked if s2 > steps)
            my_steps = steps
            break
    row = board.get(str(member_id)) or {}
    # THE SHOP BYTE MUST NOT BE 1. The Event Shop waits on 0x101A10(0x13) until
    # it returns exactly 1, and the 0x13 (@Init/@EcmInit) arm returns -1 for
    # shop byte 1 (see the event deck pick in dispatch.py). 2026-10-03 cup: the
    # winner's prize was credited but their client sat on a loading screen
    # forever, never sending @Get=/@Quit=, after an @EcmInit with sh=1.
    n = common._env_int("POL_TM_EVENT_PRIZE_SHOP", 2)
    if n == 1:
        n = 2
    money_before = purse.money_of(member_id)

    def ints(name, default):
        try:
            return [int(x) for x in os.environ.get(name, default).split(",")]
        except ValueError:
            return [int(x) for x in default.split(",")]
    pm, cards = 0, []
    if row.get("paid"):
        why = "already claimed"
    else:
        top = rank is not None and rank <= 3 and my_steps > 0
        if top:
            pz = event_prizes()
            pm += pz["money"][rank - 1]
            k = pz["cards"][rank - 1]
            cards = list(cardshop._shopbuy_cards(pz["pack"]))[:k]
        cleared = bin(int(row.get("missions", 0))).count("1")
        pm += cleared * event_prizes()["mission_money"]
        why = ("rank %s, %d mission(s)" % (rank, cleared))
        if pm or cards:
            purse._set_money(member_id, money_before + pm, "tournament prize: " + why)
            if pm:
                matchend._bump_prize(member_id, pm, "tournament prize")
            if cards:
                collection._collection_add_cards(member_id, cards)
            tmeventstate.mark_paid(ws, member_id)
    body = (b"@EcmInit=/N=%d/RA=0/M=%d/CP=%d/S=1/RT=1/PM=%d/C=%d"
            % (n, money_before, cardtables.card_level_of(member_id), pm, len(cards)))
    for i, v in enumerate(cards):
        body += b"@N%d=/D=" % i + b"|".join(b"%d" % x for x in v)
    common._say("tm: TOURNAMENT PRIZE for member %s (%s): %d money, %d card(s)"
         % (member_id, why, pm, len(cards)))
    return [protocol.encode_code(protocol.MSG_CARDENT) + b"@CardEnt=/Ans=1",
            protocol.encode_code(protocol.MSG_SHOPINIT | ((n & 0xFF) << 24)) + body]


#: THE LIVE TICKER. @ETelop (0xD7, reader 0x8C790): up to 10 pages of 200 bytes
#: (hex, so ~99 characters), one message per page sharing an /ID=, /Pg=
#: index|total; a GREATER /ID= replaces the whole set, a smaller one is
#: ignored. So the id is seconds since the event started: it only ever grows,
#: across restarts too. Pages go to every player on the tournament screen after
#: each game, at the 30- and 5-minute marks, and on entry.
_TICKER_MOMENT = {}     # event id -> (time, text): the latest thing that happened
_TICKER_MARKS = {}      # event id -> countdown marks already announced


def _ticker_pages(moment=None):
    """tmcup.ticker_pages with this server's names (tmroom)."""
    import tmcup
    try:
        import tmroom
        name_of = tmroom.name_of
    except Exception:
        name_of = None
    return tmcup.ticker_pages(moment, name_of)


def _ticker_bodies(pages):
    ws = event_window()[0]
    tid = max(1, int(time.time() - ws))
    out = []
    for i, text in enumerate(pages):
        st = text.encode("cp932", "replace")[:99].hex().upper().encode("ascii")
        out.append(protocol.encode_code(0xD7) + b"@ETelop=/ID=%d/Lp=%d/Pg=%d|%d/St=%s"
                   % (tid, common._env_int("POL_TM_EVENT_TELOP_LOOPS", 3), i, len(pages), st))
    return out


def _ticker_audience(room=None):
    """Members on the tournament screen: heard the time answer, still present."""
    out = []
    _seen_restore()
    for mid, ph in list(_EVENT_SEEN.items()):
        st = _EVENT_STATUS.get(mid)
        if room is not None and st and st[1] != room:
            continue
        if st and st[0] == "B":            # in a game: they get it on return
            continue
        if mid in _EVENT_MATCH:            # matched / seating: see _event_try_pair
            continue
        present = _event_present(st[1]) if st else None
        if present is not None and str(mid) not in present:
            continue
        out.append(mid)
    return out


def event_ticker_broadcast(moment=None, why="update"):
    """Refresh every tournament screen's ticker now."""
    if not common._env_int("POL_TM_EVENT_LIVE_TICKER", 1):
        return
    if moment:
        import tmcup
        tmcup.note_moment(event_info().get("id"), moment)
    bodies = _ticker_bodies(_ticker_pages(moment))
    who = _ticker_audience()
    for mid in who:
        for i, b in enumerate(bodies):
            pushqueue._queue_push(mid, b, "tournament ticker (%s)" % why, after=i)
    if who:
        common._say("tm: tournament ticker (%s) -> %d player(s), %d page(s)"
             % (why, len(who), len(bodies)))


def _ticker_countdown_tick():
    ph, ws, we = event_phase()
    if ph != "running":
        return
    left = we - time.time()
    eid = event_info().get("id")
    done = _TICKER_MARKS.setdefault(eid, set())
    for mark in (1800, 300):
        if left <= mark and mark not in done:
            done.add(mark)
            event_ticker_broadcast(why="%d min left" % (mark // 60))


def _event_deck_pick_on(member_id):
    """The entry card picker: everyone with POL_TM_EVENT_DECK_PICK=1, else only
    the members listed in POL_TM_EVENT_DECK_PICK_MEMBERS (to debug it while
    tournaments are live for everyone else)."""
    if common._env_int("POL_TM_EVENT_DECK_PICK", 0):
        return True
    spec = os.environ.get("POL_TM_EVENT_DECK_PICK_MEMBERS", "").strip()
    return bool(spec) and member_id is not None and (
        "*" in spec.split(",") or str(member_id) in {x.strip() for x in spec.split(",")})


def _event_test_secs():
    try:
        return max(1, int(os.environ.get("POL_TM_EVENT_TEST_SECS", "7200"), 0))
    except ValueError:
        return 7200


def _einitent_enabled():
    return os.environ.get("POL_TM_EINITENT", "1") == "1"


def _einitent_fields():
    def get(name, default):
        try:
            return int(os.environ.get("POL_TM_EINITENT_" + name, str(default)), 0)
        except ValueError:
            return default
    return get("CARD", 0), get("ANS", 1)
