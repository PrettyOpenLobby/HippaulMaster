"""The push queue: server-initiated E-bodies waiting to ride a later reply, idle pushes and
requeues.
"""
import os
import threading
import time
from . import common, protocol, webwatch


#: member_id -> [[after, E-body], ...] waiting to ride a later reply.
_PUSHES = {}

#: WARNING: ONE LOCK FOR EVERY `_PUSHES` TOUCH. A member holds at least TWO live
#: connections (room band + game band, plus a zombie after a relaunch), each in
#: its own thread, and both the reply path (`_pending_pushes`) and the idle
#: tick (`idle_pushes`) do read-modify-write on the same list -- `q` -> `keep`
#: -> reassign. Unlocked, one thread's reassignment overwrites the other's pop:
#: a push delivered twice at best, LOST at worst -- and this band never
#: resends, so a lost @StartData/@TurnData is a player parked mid-match until
#: they kill the client (the 09-02 review's H4).
_PUSH_LOCK = threading.RLock()


def _push_key(member_id):
    """A stable queue key. WARNING: NOT `int()` -- `member_id` is whatever the caller
    has, and the selftest passes the string 'selftest'. A push queue must never
    be the thing that raises inside a reply path."""
    if member_id is None:
        return None
    try:
        return int(member_id)
    except (TypeError, ValueError):
        return str(member_id)


#: In-match commands whose queued copies belong to ONE game: the deal, turns,
#: cards, battles, the result, the take list.
_STALE_MATCH_CMDS = (8, 9, 10, 12, 13, 26)


def _drop_stale_match_pushes(member_id, why):
    """Forget in-match pushes still queued for this member from an EARLIER game.

    WARNING: MEASURED 2026-09-25: a player whose client died
    mid-turn left `@BattleData` x2 + `@TurnData` (turn 6) queued; they rode the
    first reply of that member's NEXT VS. COM game on a new connection, the new
    game opened on a stale turn and the COM's move never came. A new game
    cannot own anything queued before it started. The E-body header text is
    `CC 00 MM SS` (code, 0, command, turn). `POL_TM_DROP_STALE_PUSHES=0`
    keeps them.
    """
    if member_id is None or not common._env_int("POL_TM_DROP_STALE_PUSHES", 1):
        return 0
    key = _push_key(member_id)
    dropped = 0
    with _PUSH_LOCK:
        keep = []
        for entry in _PUSHES.get(key) or []:
            try:
                hdr = bytes.fromhex(bytes(entry[1][:8]).decode("ascii"))
                stale = hdr[0] == protocol.IN_MATCH_CODE and hdr[2] in _STALE_MATCH_CMDS
            except (ValueError, TypeError, IndexError, UnicodeDecodeError):
                stale = False
            if stale:
                dropped += 1
            else:
                keep.append(entry)
        if keep:
            _PUSHES[key] = keep
        else:
            _PUSHES.pop(key, None)
    if dropped:
        common._say("tm: member %s -- dropped %d in-match push(es) left over from an "
             "earlier game (%s)" % (member_id, dropped, why))
    return dropped


def _drop_pushes_with_code(member_id, code, why):
    """Forget every push still queued for this member whose E-body code is
    `code` (header byte 0). Returns how many were dropped."""
    key = _push_key(member_id)
    if key is None:
        return 0
    dropped = 0
    with _PUSH_LOCK:
        keep = []
        for entry in _PUSHES.get(key) or []:
            try:
                same = bytes.fromhex(bytes(entry[1][:2]).decode("ascii"))[0] == code
            except (ValueError, TypeError, IndexError, UnicodeDecodeError):
                same = False
            if same:
                dropped += 1
            else:
                keep.append(entry)
        if keep:
            _PUSHES[key] = keep
        else:
            _PUSHES.pop(key, None)
    if dropped:
        common._say("tm: member %s -- dropped %d queued code-0x%02X push(es) (%s)"
                    % (member_id, dropped, code, why))
    return dropped


def _queue_push(member_id, body, why="", after=0, peer=None, source=None):
    """Hand one E-body to a later reply this member draws.

    `after` is how many replies to let pass first. 0 = the very next one.

    WARNING: `peer` AND `source` ARE TWO DIFFERENT QUESTIONS AND CONFLATING THEM COST
    A LIVE ROUND (2026-08-22, table-settings propagation). `peer` is a SOCKET
    pin: which of the member's connections may carry this, per the band law
    below. `source` is the ENVELOPE: which nick the NOTICE is framed FROM, and
    therefore -- through the id key -- which object the client thinks the
    message is ABOUT.
    They coincide for a push that answers a conversation already happening on
    that peer, which is every caller that predates this. They do NOT coincide
    for a push about an object the recipient has never addressed: pinning
    `_push_table_info` to the table's peer made the envelope right and then held
    the message forever, because a player who has never touched that table has
    no connection carrying its peer. `source` alone gives the right envelope on
    any game-carrying connection -- which is sound here because TM's receive
    store is ONE global ring (`0x5228250`, fed by `0x81A00` from the single
    `0x52af848` queue), not per-socket. `source` defaults to `peer`, so no
    existing caller changes behaviour.

    WARNING: THE STAGGER IS NOT COSMETIC. `@MuchMake` msgid 0 and msgid 1 are read
    by DIFFERENT scenes -- msgid 0 from 0x854B0 (called at 0x6D780 and 0x7B770)
    and msgid 1 from 0x85790, which is reached through a STATE-MACHINE JUMP TABLE
    at `0x985F3: jmp [eax*4 + 0x5028F54]`. So the state that polls for the start
    only exists AFTER the client has acted on the table it was handed. Sending
    both in one batch puts the start in the store before anything is looking for
    it, and a scene change is exactly where a message store gets drained.
    """
    key = _push_key(member_id)
    if key is None:
        return
    with _PUSH_LOCK:
        q = _PUSHES.setdefault(key, [])
        if any(e[1] == body for e in q):
            return                      # never announce the same match twice
        # The 4th slot is WHEN, for `idle_pushes`: an entry has to be able to
        # say how long it has been held, and keying that on id(entry) was wrong
        # twice over -- ids are reused after GC, and a missing stamp read as
        # 0.0, which made "held back one reply" expire on the very first tick.
        q.append([int(after), body, (peer or None), time.time(),
                  (source or peer or None)])
    common._say("tm:   queued a push for member %s%s%s%s%s"
         % (member_id, (" -- " + why) if why else "",
            "" if not after else " (holding it back %d reply/replies)" % after,
            "" if not peer else " [only on a reply to %s]"
            % peer.decode("latin1", "replace"),
            "" if not source or source == peer else " [framed from %s]"
            % source.decode("latin1", "replace")))


def _pending_pushes(member_id, peer_nick=None):
    """The bodies due now; anything still waiting has its counter decremented.

    WARNING: A PUSH MUST RIDE A REPLY ON THE BAND ITS READER IS LISTENING TO, and that
    is not automatic -- "the member's next reply" can be on a different peer.
    Measured 2026-08-20, the same message to the same member, twice:

        11:22:40  m6 [UE7QN1N9G]  @VsGameInit  -> the client READ it
                                                  (`---->Recv=PLGAMEINIT`)
        11:30:54  m6 [UKXDBA266]  @VsGameInit  -> never seen; 130,000 further
                                                  trace lines, no Recv at all

    `UE7QN1N9G` is the TABLE peer and `UKXDBA266` is the ROOM peer. The second
    board carried the correct GAME-IDs and was still invisible, because it rode
    a reply on the room band while the match scene reads the table band. So a
    correct message on the wrong peer is exactly as unread as no message --
    the same class of fault as sending it to the wrong (code, msgid) slot.

    An entry with `peer=None` rides anything, as before.
    """
    key = _push_key(member_id)
    if key is None:
        return []
    with _PUSH_LOCK:
        q = _PUSHES.get(key)
        if not q:
            return []
        due, keep = [], []
        for entry in q:
            want = entry[2] if len(entry) > 2 else None
            if want is not None and peer_nick is not None and want != peer_nick:
                keep.append(entry)      # right member, wrong band -- wait
                continue
            if entry[0] <= 0:
                due.append(entry[1])
            else:
                entry[0] -= 1
                keep.append(entry)
        if keep:
            _PUSHES[key] = keep
        else:
            _PUSHES.pop(key, None)
        return due


#: *** A PUSH USED TO NEED THE CLIENT TO SPEAK FIRST, AND IN A MATCH IT BARELY
#: SPEAKS AT ALL. ***
#:
#: Measured 2026-08-20T16:32..16:37Z, the first two-player match ever played on
#: this server. In a match the client's ONLY unprompted traffic is `@Pong=`,
#: **every 15 seconds**:
#:
#:     16:32:36  m6 <- @PutData=/P=0/H=0/F=3     the move
#:     16:32:36  m6 -> @PutCard=...              answered instantly (a REPLY)
#:     16:32:48  m9 -> @PutCard=...              the other player: +12 s
#:     16:33:02  m6 -> @TurnData= turn 1         +26 s
#:
#: Every one of those pushes was correct and sitting in the queue the whole
#: time; `_pending_pushes` can only hand one over when a reply is being built,
#: so each held reply costs a full Pong interval. The live report was
#: "it's happening but it's happening veeeery slowly", and this is all of it --
#: not a turn timer, not the client thinking.
#:
#: The auth session loop already wakes every **2 s** (`POL_GMCHAT_POLL`; the
#: socket timeout was lowered from the 60 s ping interval for exactly this
#: reason on the GM chat track) and can `chat_sess.send()` from that tick. So a
#: push does not have to wait to be asked for. `responders._auth_session_hop`
#: calls this on each idle tick.
#:
#: WARNING: THE HOLD-BACK LAW STILL APPLIES, and it is why this is not just "flush
#: everything". `after=N` exists because a message must not ride -- or closely
#: follow -- the reply that CREATES the scene meant to read it (see
#: `POL_TM_MATCH41_GAP` and `@MuchMake` before it). A tick is a genuinely
#: separate transmission, so it counts as one of the N; but the ticks are 2 s
#: apart and a reply-gap was worth ~15 s, so each step also has to be worth at
#: least `POL_TM_IDLE_MIN_S` seconds of wall clock or the staging that was
#: measured with 15 s gaps stops being staging at all.
def idle_pushes(member_id, peers=None, now=None):
    """(peer_nick, body) pairs due for this member RIGHT NOW, unprompted.

    Returns [] for everything else, including a member with nothing queued, so
    the caller can invoke it on every tick of every session for free.

    WARNING: `peers` IS NOT OPTIONAL IN PRACTICE, AND SKIPPING IT COST A LIVE MATCH.
    A member has MORE THAN ONE auth connection -- the room band and the table
    band are separate sockets -- and the first version of this drained the queue
    on whichever of them ticked first. Measured 2026-08-20T17:10..17:11Z:
    member 9's `@Tet=` and `@VsGameInit` went out on 127.0.0.1:**50478** while
    its `@GameStart=` went out on :**50512**, and the board was never read --
    the live report was "one of the players is still at a black screen
    instead of getting to selecting cards", which is precisely the wrong-peer symptom
    one axis over.

    The wrong-peer bug established that a push must ride the right PEER; this is the same law
    on the socket. `peers` is the set of peer nicks THIS connection has actually
    carried game traffic for, so a pinned push only leaves on the connection
    that peer lives on. An unpinned push needs the connection to have carried
    SOME game traffic -- otherwise it would go out on the login socket.
    """
    webwatch._watch_publish()                 # the web watch file's heartbeat (throttled)
    if common._env_int("POL_TM_IDLE_PUSH", 1) != 1:
        return []
    key = _push_key(member_id)
    q = _PUSHES.get(key)
    if not q:
        return []
    if peers is not None and not peers:
        return []                       # no game traffic here: not our band
    now = time.time() if now is None else now
    # 4 SECONDS IS MEASURED, NOT PICKED. Every match that started cleanly had
    # the wake-up land ~4 s after the second player's own `@GameEN=`
    # (+4.0 / +4.0 / +4.7 s on 2026-08-20 at 14:17, 16:30 and 16:47); the one
    # that answered `@GameNG=` had it land 2 ms after. A reply-gap used to be
    # worth a 15 s Pong interval, so "one step" has to keep meaning something
    # on that scale rather than one 2 s poll.
    min_s = float(os.environ.get("POL_TM_IDLE_MIN_S", "4") or 4)
    with _PUSH_LOCK:
        return _idle_pushes_locked(member_id, key, peers, now, min_s)


def _idle_pushes_locked(member_id, key, peers, now, min_s):
    q = _PUSHES.get(key)
    if not q:
        return []
    due, keep = [], []
    for entry in q:
        peer = entry[2] if len(entry) > 2 else None
        if peers is not None and peer is not None and peer not in peers:
            # WARNING: WAIT, BUT NOT FOREVER. A pinned push only leaves on the
            # connection its peer lives on, so one pinned to a peer this member
            # never carries would sit in the queue for the life of the process.
            # That was harmless while almost nothing pinned; `_push_table_info`
            # pins every table-settings push, and a member who is in the room
            # but has never touched that table is exactly the case. Drop it
            # loudly rather than leak it. POL_TM_PUSH_TTL_S=0 waits forever.
            ttl = float(os.environ.get("POL_TM_PUSH_TTL_S", "120") or 0)
            held = now - (entry[3] if len(entry) > 3 else now)
            if ttl and held > ttl:
                common._say("tm:   dropping a push for member %s held %.0fs for peer "
                     "%s, which this connection never carries: %s"
                     % (member_id, held,
                        peer.decode("latin1", "replace"), protocol.describe_line(entry[1])))
                continue
            keep.append(entry)          # right member, wrong SOCKET -- wait
            continue
        if entry[0] <= 0:
            # FRAME from `source`, which is `peer` unless the caller split them.
            due.append((entry[4] if len(entry) > 4 else peer, entry[1]))
            continue
        # Not due yet. One tick counts as one step, but only once the step has
        # actually been worth some wall clock -- see the banner.
        last = entry[3] if len(entry) > 3 else 0.0
        if now - last >= min_s:
            entry[0] -= 1
            if len(entry) > 3:
                entry[3] = now
        keep.append(entry)
    if keep:
        _PUSHES[key] = keep
    else:
        _PUSHES.pop(key, None)
    for peer, body in due:
        common._say("tm:   ...pushing to member %s unprompted%s: %s"
             % (member_id,
                "" if not peer else " [on %s]" % peer.decode("latin1", "replace"),
                protocol.describe_line(body)))
    return due


def requeue_pushes(member_id, items, why="delivery failed"):
    """Put undelivered (peer, body) pairs BACK at the head of the queue.

    WARNING: A POP IS NOT A DELIVERY. `idle_pushes` hands entries to the caller and
    forgets them, and the caller's send can fail (peer gone mid-write, an
    exception in the encode) -- the old arm logged "push failed" and dropped
    the bodies on the floor. This band never resends, so every dropped
    @StartData/@TurnData was a player parked mid-match forever (09-02 review
    H3). The consumer now hands back what it could not deliver; entries go to
    the FRONT with after=0 so the next tick or reply retries them, and
    `queue_push`'s own dedup keeps a retry from doubling anything that did
    land. POL_TM_PUSH_REQUEUE=0 restores drop-on-failure.
    """
    if common._env_int("POL_TM_PUSH_REQUEUE", 1) != 1:
        return
    key = _push_key(member_id)
    if key is None or not items:
        return
    now = time.time()
    with _PUSH_LOCK:
        q = _PUSHES.setdefault(key, [])
        have = {e[1] for e in q}
        back = [[0, body, (peer or None), now, (peer or None)]
                for peer, body in reversed(list(items))
                if body not in have]
        for entry in back:
            q.insert(0, entry)
    if back:
        common._say("tm:   requeued %d undelivered push(es) for member %s (%s)"
             % (len(back), member_id, why))
