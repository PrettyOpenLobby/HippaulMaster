"""From seats to a match: match codes, table info pushes, @MuchMake, the roster, the accept quorum
and the seat heartbeat.
"""
import json
import os
import time
import tmstore
from . import (
    boardrules, common, matchstart, peers, pots, protocol, pushqueue, rematch, scoring, seating,
    tableaudit, tablesettings, turns, vscom, webwatch,
)


#: VERIFIED: MATCHMAKING, MEASURED 2026-08-20 AND SERVER-INITIATED.
#:
#: Live: player 1 reserves and sticks on "Setting Up"; player 2 sees the
#: table, can only "Reserve" or "View table members", reserves, and sticks too.
#: Both got what they should have: `@GameEN=` -> `/EN=1` then `/EN=2`, and the
#: `/EN=` consumer at `0x90114` takes the != 1 branch into a COUNTDOWN at
#: `0x90145` (`now - [0x52461E0]`, formatted). So the wait was correct and
#: nothing ever ended it.
#:
#: What ends it is the one command in the family whose direction is inverted
#: (the command table: "`@MuchMakeAns=` is an ANSWER, so matchmaking is server-initiated").
#: Read off the receiver at `0x85521`:
#:
#:     0x825A0(buf, 0xD2, 0)          waits for code **0xD2**, msgid 0
#:     0xAA920(msg, "@MuchMake")      the name, no '=' in the literal
#:     0xAB080("/TblNo=", ..)  int    -> [0x52223A8]
#:     0xAB470("/TblId=", ..)  u64    -> [0x521BB58]/[0x521BB5C]
#:
#: and the client then answers `@MuchMakeAns=/Ans=` - builder `0x8563F`,
#: `0xA97C0(buf, "@MuchMakeAns=", 0xD3, 0, ..)`, so **code 0xD3**.
#:
#: WARNING: `/TblId=` IS A CHOICE AND IT IS FLAGGED. The client stores it at a
#: standalone global, not into a record, so nothing in the parser says whether it
#: wants the table RECORD's `+0x00` id or the table PEER's guid. We send the peer
#: guid because that is the value the client itself demonstrably uses to address
#: that table (it is the nick every `@GameEN=` for it went to), and
#: `POL_TM_MATCH_TBLID=record` switches it. If matchmaking fires and the client
#: goes nowhere, this is the first thing to flip.
#: WARNING: AND IT IS **TWO** PUSHES, NOT ONE -- which is why the first attempt was
#: answered with silence. The push went out eight times (authserv.log) and
#: `@MuchMakeAns=` never came back, because there are TWO receivers and the
#: answer belongs to the other one:
#:
#:     0x85521   0x825A0(buf, 0xD2, 0)   msgid 0 -- reads /TblNo= and /TblId=,
#:                                       stores them, transitions
#:     0x85803   0x825A0(buf, 0xD2, 1)   msgid 1 -- reads **/Start=**, and
#:     0x85626   0x828E0(0xD2, 1)        is the slot the ANSWER drains before
#:                                       building `@MuchMakeAns=`
#:
#: So msgid 0 is "you are matched, here is the table" and msgid 1 is "start",
#: and only the second is ever answered. We were sending the first alone.
MATCH_PUSH_CODE = 0x000000D2    # msgid 0: `@MuchMake=/TblNo=<n>/TblId=<hex>`
MATCH_START_CODE = 0x000100D2   # msgid 1: `@MuchMake=/Start=<n>`  <- answered
MATCH_ANS_CODE = 0xD3           # `@MuchMakeAns=/Ans=<n>`, the client's reply

#: VERIFIED:VERIFIED: THE SIGNAL THE RESERVATION SCENE IS ACTUALLY WAITING ON, and the
#: shipped SHIM LOGS are what found it. The client logs its own message-store
#: lookups, and across ~26,000 of them it polls exactly two groups:
#:
#:     15927x  0x2C 0x2A 0x25 0x23 0x21      (the trade family)
#:     10521x  0x41 0x1A                     (the game / reservation family)
#:
#: **It never looks up 0x13 or 0xD2 -- not once.** So `@MuchMake` and the table-
#: info push were being delivered into slots nothing reads, which is why both
#: were met with silence no matter how they were framed. Every lookup in the log
#: is "not found" while `store_count=3` sits unread.
#:
#: Within 0x41 the polled msgids are 2 (`@GameEA`), 4 (`@GameML`), 6 (`@GameQA`)
#: -- all of which we answer -- plus **7** and **0x10**, which we have never sent.
#: And msgid 7's handler at 0x8477F parses NOTHING:
#:
#:     0x8477F  0x825A0(buf, 0x41, 7)     poll
#:     0x84788  0x828E0(0x41, 9)          drain
#:     0x84793  0x828E0(0x41, 0x10)       drain
#:     0x8479E  flag = 1
#:
#: no `0xAA920` name match, no field reads -- **ARRIVAL ALONE IS THE EVENT**, and
#: it clears the two pending slots behind it. That is the shape of a "stop
#: waiting, it is happening" notification.
#:
#: WARNING: The body is therefore free. A name is sent anyway so the wire stays
#: readable; POL_TM_MATCH41_BODY overrides it.
MATCH_41_MSGID = 0x00070041     # code 0x41, msgid 7 -- the reservation wake-up

#: VERIFIED: IT WORKED, AND HERE IS WHAT CAME BACK. Pushing (0x41, 7) on a match
#: moved the HOST straight through a sequence never seen before on this server
#: (authserv.log 2026-08-20T04:02):
#:
#:     m3 <- @CheckJoinTable=   (code 0x2D)   <- the FIRST EVER, and we answered
#:     m3 <- @GameReady=        (0x41, 0x0A)
#:     m3 <- @Req=              (code 0x01)
#:     m6 <- @GameNG=           (0x41, 0x0F)  <- the GUEST DECLINED
#:
#: and the tester saw "setting up game" on the host only. So msgid 7 is right,
#: and the two players need DIFFERENT treatment: the host proceeds, the guest is
#: being asked something and answered NO.
#:
#: `@GameNG=` is msgid 0x0F, and the `request+1` rule this family keeps makes its
#: answer **msgid 0x10** -- which is one of the two polled-but-never-filled slots,
#: and its waiter at 0x84B22 names it: **`@GameWa`** with an **`/Error=`** field.
#:
#: WARNING: `/Error=0` is the least-committal value and is a CHOICE. The field is
#: named, its slot is measured, and the client polls it constantly; what a
#: non-zero value does is not measured. POL_TM_GAMEWA_ERROR moves it,
#: POL_TM_GAMEWA=0 stops sending it.
#: VERIFIED: `@GameWa=/Error=<n>` ACKNOWLEDGES BOTH ANSWERS, ON DIFFERENT MSGIDS.
#: The complete polled map for code 0x41, read off every `0x825A0` call site:
#:
#:     msgid 2    0x835C1   `@GameEA`     answered
#:     msgid 4    0x83870   `@GameML`     answered
#:     msgid 6    0x84221   `@GameQA`     answered
#:     msgid 7    0x8477F   the wake-up   answered (bare, no fields)
#:     msgid 9    0x84B01   **`@GameWa` + `/Error=`**   <- the `@GameOK=` reply
#:     msgid 0x10 0x84B22   **`@GameWa` + `/Error=`**   <- the `@GameNG=` reply
#:     msgid 0xC  0x843AA   not sent
#:     msgid 0xE  0x84601   not sent
#:     msgid 0x13 0x85361   not sent
#:
#: `@GameOK=` is msgid 8 and `@GameNG=` is 0xF, so request+1 puts their answers
#: at 9 and 0x10 -- the same message, two slots. The msgid-9 handler reads
#: `/Error=` (ABSENT defaults it to -1, 0x84B78) and then DRAINS (0x41, 7),
#: retiring the wake-up it is acknowledging.
#:
#: WARNING: AND `@GameReady=` HAS NO REPLY SLOT. It is msgid 0x0A, request+1 would
#: be 0x0B, and 0x0B IS NOT POLLED ANYWHERE. It was being answered with 0x10 --
#: `@GameNG=`'s slot -- which was the error: a decline acknowledgement delivered
#: for a readiness announcement. It is fire-and-forget; we now say nothing.
GAMEWA_OK_MSGID = 0x00090041    # reply to `@GameOK=`  (msgid 8)
GAMEWA_NG_MSGID = 0x00100041    # reply to `@GameNG=`  (msgid 0xF)

#: VERIFIED: `@GameOwner` -- AND THIS ONE THE RESERVATION SCENE POLLS ITSELF.
#:
#: Found 2026-08-20 by enumerating every code the PC client waits on (all 26
#: `0x825A0`/`0x827F0` sites). The reservation family lives at 0x83xxx-0x84xxx
#: (0x841E0 is the reservation call, 0x83580 the `@GameEA` waiter), and inside it
#: sits `0x84D69: 0x825A0(buf, 0x1A, -1)` -- **code 0x1A, any msgid** -- reading
#:
#:     0xAA920(msg, "@GameOwner")     the name, no '=' in the literal
#:     0xAB470("/NN=", ..)     u64    a player guid
#:     0xAAD60("/CN=", ..)     str    -> [ebp+8]
#:     0xAAD60("/QN=", ..)     str    -> [ebp+0xC]
#:
#: That matters more than `@MuchMake` does: this is polled BY THE SCENE THE
#: PLAYERS ARE STUCK IN, whereas `@MuchMake`'s two receivers are reached from
#: functions we have not identified and one of them sits behind a state-machine
#: jump table.
#:
#: WARNING: AND IT IS AN OWNERSHIP **TRANSFER**, NOT AN ANNOUNCEMENT -- MEASURED
#: ON SCREEN 2026-08-20, BY BREAKING IT. Pushed on a match, the client rendered
#:
#:     "Since <blank> has cancelled their game you are now the owner"
#:
#: So the client DOES consume this message and the field map above is right --
#: but its meaning is the opposite of what it was used for. `@GameOwner` says
#: THE OWNER LEFT AND YOU INHERIT THE TABLE, which told player 2 that player 1
#: had cancelled, and left the table stuck. The blank is our empty `/CN=`, so
#: that field is the DISPLAY NAME of the player who left.
#:
#: **DEFAULT OFF.** It is kept, wired and documented because it is now a measured
#: message we will need the day a table owner actually leaves -- but pushing it
#: to announce a match is wrong, and it made things worse than sending nothing.
#: `POL_TM_OWNER_PUSH=1` re-enables it.
#:
#: WARNING: `/QN=` is still unmeasured; `/CN=` is now known to be a display name. `@Init=` uses `/CN=` for a content id
#: (hex ASCII) and `@GameEN=` sends `/CN=` EMPTY, so empty is what the client
#: itself does with it here. They are sent empty unless POL_TM_OWNER_CN /
#: POL_TM_OWNER_QN say otherwise, rather than filled with a plausible guess.
OWNER_PUSH_CODE = 0x1A          # `@GameOwner=/NN=<hex>/CN=<s>/QN=<s>`

#: VERIFIED: `@Req=` -> `@PLAYACK`, AND THIS IS WHERE BOTH PLAYERS NOW HANG.
#:
#: With the wake-up delivered correctly, both clients walk
#: `@GameEN=` -> `@CheckJoinTable=` -> `@GameReady=` -> **`@Req=`** and stop dead,
#: because we answer nothing. The trace says why it is answerable: after `@Req=`
#: they poll only the ambient group, so the reply had to be a slot they read --
#: and it is, one the earlier scan MISSED because the code is pushed as decimal
#: `2` rather than `0x2`:
#:
#:     0x86730  the `@Req=` sender: `0x828E0(msgid=-1, code=2)` FIRST -- the
#:              drain-before-you-wait pattern this family uses everywhere --
#:              then `0xA97C0(buf, "@Req=", 1, 0)`.  So `@Req=` is code 1 and
#:              its answer is code **2**, the request+1 rule again.
#:     0x86A25  `0x825A0(buf, 2, -1)` -- the waiter
#:     0x86A4D  `0xAA920(msg, "@PLAYACK")`  the name, no '=' in the literal
#:     0x86A6E  `0xAB080("/EN=", ..)`       the ONLY field it reads
#:
#: THE GATE, off 0x86A7F, and it is the same shape as `@GameEA` and `@InitAns`:
#:
#:     /EN= == 0   ->  esi = 0xFFFF7FFD and the scene FAILS
#:     /EN=  < 0   ->  passed through as that error
#:     /EN=  > 0   ->  0x86AC9, which reads the table count at [0x524290C] and
#:                     walks the table array -- i.e. it proceeds
#:
#: so `/EN=` must be > 0. 1 is the least-committal positive, as everywhere else
#: in this file; `POL_TM_PLAYACK_EN` sweeps it and `POL_TM_PLAYACK=0` restores
#: the silence for an A/B.
MSG_PLAYACK = 0x02              # reply `@PLAYACK=/EN=<n>` to `@Req=` (code 1)

#: `@GameExit=` is (0x41, 0x0D) and its answer is request+1 -- see the handler.
GAMEEXIT_ANS_MSGID = 0x000E0041


def table_info_body(fields):
    """`@Tet=`/`@Tab=` for one table, in the order 0x87090 parses them.

    Absent keys are sent as 0 rather than omitted: the parser reads all
    seventeen by name, and a table that answers "no value" for a rule is not a
    thing the client has any way to draw.
    """
    def _one(k):
        # TEXT KEYS GO BACK VERBATIM. `pw` is the password's own characters
        # (see `TABLE_SETTING_TEXT_KEYS`); `%d` on it either raises or, worse,
        # silently ships a different password.
        v = fields.get(k.decode(), None)
        if k in tablesettings.TABLE_SETTING_TEXT_KEYS:
            text = "0" if v in (None, "") else str(v)
            return b"/%s=%s" % (k, text.encode("ascii", "replace"))
        try:
            return b"/%s=%d" % (k, int(v or 0))
        except (TypeError, ValueError):
            return b"/%s=0" % (k,)

    tet = b"".join(_one(k) for k in protocol.TABLE_INFO_TET)
    tab = b"".join(_one(k) for k in protocol.TABLE_INFO_TAB)
    return b"@Tet=" + tet + b"@Tab=" + tab


def table_peer_nick(tmroom, chan, index):
    """The NICK the client addresses table `index` by, or None.

    VERIFIED: THE SERVER CAN COMPUTE THIS NOW, and could not before 2026-08-22. A
    table's peer is not something the client tells us and not something we
    allocate -- it is our own published table id run through the client's id
    key: `wire = (id ^ K) & 0xFF_FFFF_FFFF`, rendered as 8 base-36 digits and
    scrambled into a login nick. See `tmroom.CLIENT_KEY_DEFAULT`.

    VERIFIED AGAINST THE LIVE WIRE: table 1 of `#TM0R001` synthesises to
    `UGRAWG3GT`, which is exactly the nick the client addressed its `@Tet=`
    confirm to at 05:15:20Z. Same value, arrived at from the other end.
    """
    try:
        import polnick
        room_no = tmroom._room_no(chan)
        if not room_no:
            return None
        guid = tmroom.wire_guid(tmroom.canonical_table_id(int(index) - 1, room_no))
        nick = polnick.nick_for_polid(polnick._digits_out(guid))
        return nick.encode("ascii") if nick else None
    except Exception:
        return None


def _push_table_info(tmroom, chan, index, why="", skip=None):
    """Tell the room what table `index` is configured as (code 0x13).

    Sent to members of the room, not just the table's occupants: the point is
    that somebody LOOKING at the table can see what it is, and 0x86ED0 keys the
    info record off the message rather than off who asked.

    VERIFIED: BACK ON BY DEFAULT 2026-08-22, AND THE REASON IT WAS OFF IS RETRACTED.
    The note this replaces said the pushes "CLOGGED the client's small receive
    store and STARVED the reservation list", measured as a seated owner holding
    5 unconsumed `@Tet=` pushes while `+0x108` stayed 0. The starvation was
    real; the CAUSE was not. `+0x108` stayed 0 because the reservation
    `@GameML` rode msgid **0xC**, which the `@GameKick` poll (`0x84360` at
    `0xA92D5`) POPs before the reservation arm runs whenever the selected-table
    gate is set -- always, at your own table. Fixed in `2246e005` by moving it
    to msgid 4. So this push was disabled for something it did not do.
    WARNING: THE ONE TRUE OBSERVATION IN THAT NOTE IS KEPT: the pushes that piled up
    were the ones ECHOED BACK TO THE OWNER WHO SET THEM, with the settings scene
    already closed so nothing ever consumed them. `skip` drops that echo, which
    removes the queue pressure without removing the feature. The owner does not
    need to be told what they just typed.
    WARNING: AND WITHOUT THIS THE SECOND PLAYER NEVER SEES A CHANGE. `0x873A0` gates
    on `record+0x30 == 2` and returns 0 otherwise; once a viewer's record is at
    2 it holds the values it was given, so a table reconfigured after they
    looked stays stale on their screen until something pushes the new set. Only
    `lu`/`ll` have a home in the `b/g/PTL` row (`_settings_block`), so the
    `TD` delta cannot carry the other twelve fields.
    `POL_TM_TABLE_INFO_PUSH=0` disables it again -- its OWN knob, deliberately:
    it used to share `POL_TM_TABLE_INFO` with the `@Rule=` answer arm while the
    two carried DIFFERENT compiled defaults (1 there, 0 here). Setting that one
    variable to reach this behaviour would silently have switched off the
    `@Rule=` reply that "Change Table Settings" depends on -- the
    two-defaults-per-setting trap, one subsystem over.
    """
    if not common._env_int("POL_TM_TABLE_INFO_PUSH", 1):
        return
    fields = tablesettings._table_rules_get(chan, index)
    if not fields:
        return                          # nothing measured to echo yet
    body = protocol.encode_code(protocol.TABLE_INFO_CODE) + table_info_body(fields)
    try:
        who = [mid for mid, _v in tmroom.members(chan)]
    except Exception:
        return
    sent = [mid for mid in who if skip is None or str(mid) != str(skip)]
    # WARNING: THE PUSH MUST RIDE THE TABLE'S OWN PEER -- the wrong-peer law, and skipping it
    # is why the first cut of this changed nothing on the other player's screen
    # (measured live 2026-08-22, 05:15Z). An unpinned push is framed FROM THE
    # RECIPIENT'S OWN NICK (`_game_notice_line(body, _peer or nick, ...)`), so
    # the client reconstructs `own_guid ^ K` -- its own id -- and `0x86ED0`
    # compares that against every table record, misses, and drops the message.
    # Byte-identical payload, delivered, consumed, and invisible. Exactly the
    # id-space failure the `@Rule=` reply had, one path over: there the peer
    # came for free because we answered the message it arrived on, and here
    # there is no arriving message to answer.
    peer = table_peer_nick(tmroom, chan, index)
    if peer is None:
        common._say("tm:   table %d info NOT pushed -- cannot synthesise the table's "
             "peer nick for %s, and an unpinned 0x13 is framed from the "
             "recipient's own nick, which the client discards" % (index, chan))
        return
    common._say("tm:   table %d info -> %d of %d member(s) as code 0x13 on %s%s%s"
         % (index, len(sent), len(who), peer.decode("latin1"),
            (" (%s)" % why) if why else "",
            "" if skip is None else
            " [not echoed to member %s, who set them]" % skip))
    for mid in sent:
        # SOURCE, NOT A SOCKET PIN. The envelope has to be the table's peer or
        # the client resolves the message to the wrong object and drops it; the
        # SOCKET must stay unpinned, because the whole point is to reach someone
        # who has NOT been addressing this table and therefore has no connection
        # carrying its peer. Measured live: pinning both queued the push and
        # never sent it ("[only on a reply to UGRAWGL2Q]", no delivery line).
        pushqueue._queue_push(mid, body, "table %d info" % index, source=peer)


def _announce_match(tmroom, chan, index, who):
    """Push `@MuchMake` to everyone seated once a table has enough of them.

    WARNING: "TETRA MASTER IS A TWO-PLAYER GAME" WAS WRONG, AND IT IS WHY THREE CAN
    NEVER SIT DOWN. That sentence stood here as the justification for the
    default, and live testing corrected it 2026-08-20: **the game also supports
    three players.** The number was never measured -- it was inferred from "a
    card game has two sides" and then reasoned from.

    The consequence is not merely a wrong default. Announcing the moment the
    SECOND player sits is what FORECLOSES the third: the match fires, both
    players leave the room for the board (`_MATCH_ROSTER`'s banner: entering a
    match means leaving the room), and the seat a third person was walking
    toward is gone. So a headcount trigger cannot express a three-player game at
    all, whatever number is in it.

    WARNING: WHAT THE RIGHT TRIGGER IS, IS NOT KNOWN, and this is where to start
    looking rather than where to guess. It is almost certainly an OWNER ACTION --
    the person who reserved the table saying "go" once the people they want are
    seated -- because that is the only thing that can distinguish "two is all I
    wanted" from "still waiting for a third". Candidates already decoded but not
    connected to this: `@GameQA=/QT=` (seen live in the class-A ring), `@GameOK=`
    / `@GameNG=` (the ACCEPT half, which runs after the announce, not before),
    and the settings pair `@Tet=`/`@Tab=`. None of the seven `/R=` rule values
    carries a seat count (`VSGAME_RULE_KEYS`: bm du st cb ca gs tl -- `tl` is a
    time limit), so the count is NOT part of the table's declared rules and has
    to arrive some other way.

    Until that is measured, this stays a count WE choose and the dial is
    `POL_TM_MATCH_AT`: 2 to keep today's behaviour, 3 to hold the table open for
    a third (a pair will then never start, so it is a test setting, not a fix),
    0 to disable the push entirely -- the control for "did this change
    anything". The client's own floor is the only hard number here:
    `0x1024D5` refuses `/N=` <= 1 with `StartAloneERROR`.
    """
    need = common._env_int("POL_TM_MATCH_AT", 2)
    if need <= 1:
        # 0 disables; 1 would be a table of one, which the client refuses at
        # 0x1024D5 -- raised here rather than paid for as a StartAloneERROR.
        return
    if len(who) < need:
        return
    # THE OWNER MUST HAVE FINISHED. See `_TABLE_CONFIRMED`: announcing into an
    # open rules dialog gets `@GameNG=` from the host and strands the guest.
    # THE CONFIRM SURVIVES A DEPLOY VIA tmroom.table_confirmed -- the shared
    # flag, written where the process-local set is written. Measured 23:52:31:
    # the hold fired for a confirm sent at 23:48:13, across one restart, and
    # the "Setting up" freeze locks every control on every seated client, so a
    # wiped flag bricks the table with the players inside it.
    # WARNING: NOT inferred from stored rules -- @Save= (0x24) also stores rules, and
    # the selftest's "no match before the owner confirms" caught that shortcut
    # before it shipped. The flag is its own fact.
    if (common._env_int("POL_TM_MATCH_NEED_CONFIRM", 1)
            and (chan, index) not in seating._TABLE_CONFIRMED
            and not tmroom.table_confirmed(chan, index)):
        common._say("tm:   %s table %d has %d seated but its owner has NOT confirmed "
             "the rules yet -- holding the match. A wake-up delivered into an "
             "open settings dialog is answered @GameNG=." % (chan, index, len(who)))
        return
    which = (os.environ.get("POL_TM_MATCH_TBLID", "peer") or "peer").lower()
    room_peer = tmroom.room_peer(chan)
    if which == "record":
        row = dict(tmroom.tables(chan)).get("#TM0T%03d" % index)             or tmroom.fixture_table(index) or []
        tblid = int((row[2] if len(row) > 2 else "0") or "0", 16)
    else:
        # The table peer's guid -- room peer with the table class nibble and the
        # table's own index, i.e. exactly what `table_index_for_peer` inverts.
        tblid = ((peers.TABLE_PEER_CLASS << 32)
                 | (((room_peer or 0) & 0xFFFFFFFF) - 1 + index)) if room_peer else 0
    start = common._env_int("POL_TM_MATCH_START", 1)
    bodies = []
    # THE ONE SLOT THE SCENE POLLS. First, because it is the only push here with
    # live evidence that the client is listening for it at all.
    if common._env_int("POL_TM_MATCH41", 1):
        # WARNING: NEVER IN THE SAME BATCH AS THE REPLY THAT TRIGGERED IT. Measured
        # 2026-08-20 with the trace on both clients, and the two differ by exactly
        # this:
        #
        #   m6 (guest)  @GameEA=/EN=2 + 0x13 + @GameStart= in ONE reply
        #               -> store_count=3, polls (0x41,7), **not found**, and 8 s
        #                  later tears down with @GameNG=
        #   m3 (host)   @GameStart= alone on a LATER exchange
        #               -> store_count=2, polls (0x41,7), **FOUND**
        #
        # The second player's wake-up arrives before the scene that polls for it
        # exists -- the reply it rides is the one that CREATES that scene. So it
        # waits a reply, which on this client is the ~2 s `@Pong=` heartbeat.
        # POL_TM_MATCH41_GAP=0 restores the same-batch behaviour for an A/B.
        bodies.append(protocol.encode_code(MATCH_41_MSGID)
                      + os.environ.get("POL_TM_MATCH41_BODY",
                                       "@GameStart=").encode("latin1", "replace"))
    # WARNING: DEFAULT OFF: the shim logs prove the client never polls 0xD2, so these
    # only fill a store it never drains. Kept, documented and switchable because
    # the receivers are real -- some scene we have not identified does read them.
    if common._env_int("POL_TM_MUCHMAKE_PUSH", 0):
        bodies.append(protocol.encode_code(MATCH_PUSH_CODE)
                      + b"@MuchMake=/TblNo=%d/TblId=%016X" % (index, tblid))
    if common._env_int("POL_TM_MUCHMAKE_PUSH", 0) and common._env_int("POL_TM_MATCH_START_PUSH", 1):
        # WARNING: THE ORDER MATTERS AND IT IS THE READ, NOT A GUESS: msgid 0 is what
        # gives the client the table, msgid 1 is what it answers. Sending the
        # start first would announce a match for a table it has not been told
        # about yet.
        bodies.append(protocol.encode_code(MATCH_START_CODE)
                      + b"@MuchMake=/Start=%d" % start)
    # THE ROSTER, CAPTURED WHILE IT STILL EXISTS. See `_MATCH_ROSTER`: the
    # players PART the room on their way into the match, so this is the last
    # moment anything knows who is playing whom.
    _remember_match(chan, index, who)
    common._say("tm: MATCH READY at %s table %d (%d seated) -- pushing %s to %s"
         % (chan, index, len(who),
            ", ".join(b[:8].decode("ascii", "replace") for b in bodies) or "NOTHING",
            ", ".join(str(m) for m, _ in who)))
    if common._env_int("POL_TM_OWNER_PUSH", 0):
        # THE OWNER IS THE FIRST PLAYER SEATED -- the one who reserved the table
        # and whose id is already in the row's occupancy block.
        owner = next((i for _m, i in who if i), 0)
        bodies.insert(0, protocol.encode_code(OWNER_PUSH_CODE)
                      + b"@GameOwner=/NN=%016X/CN=%s/QN=%s"
                      % (owner,
                         os.environ.get("POL_TM_OWNER_CN", "").encode("latin1", "replace"),
                         os.environ.get("POL_TM_OWNER_QN", "").encode("latin1", "replace")))
    # WARNING: ONE PUSH PER REPLY, IN ORDER. Not "the first now, the rest later" -- an
    # earlier version did that and put both `@MuchMake` halves back in the same
    # batch the moment a third message joined the list, which is the exact fault
    # the stagger exists to prevent. Each body waits for its own reply.
    gap = common._env_int("POL_TM_MATCH_START_GAP", 1)
    lead = common._env_int("POL_TM_MATCH41_GAP", 1)      # see the banner above
    for mid, _ident in who:
        for n, b in enumerate(bodies):
            pushqueue._queue_push(mid, b, "match at table %d" % index,
                        after=lead + n * gap)


#: member_id -> (chan, table index, seats) as they were when the match was
#: ANNOUNCED.
#:
#: WARNING: BECAUSE `_SEATED` IS ALREADY EMPTY BY THE TIME `@GameOK=` ARRIVES, AND
#: THAT IS CORRECT BEHAVIOUR, NOT A BUG TO FIX THERE. Measured live
#: 2026-08-20T10:43Z, both players:
#:
#:     tm:   #TM0R001 table 1 released -> 1 seated
#:     tm: member 6 left #TM0R001 -- released table(s) 1
#:     tm: member 6 sent @GameOK= -- answering (0x41, 0x9) @GameWa=/Error=0
#:     tm:   no @VsGameInit -- member 6 is seated at no table
#:
#: **Entering the match means LEAVING the room**, so the PART path runs
#: `release_seats` in the middle of the accept chain -- between
#: `@CheckJoinTable=` and `@GameOK=`. Deriving the roster at `@GameOK=` time
#: therefore always finds nobody. Snapshot it at the one moment it is complete
#: and true: when the table reached its seat count and the match was announced.
#:
#: WARNING: Keyed per MEMBER, not per table, so each player keeps their own view --
#: `_vsgame_ids` needs to know which seat the RECIPIENT holds.
_MATCH_ROSTER = {}


#: (chan, table index) -> the set of members who have sent `@GameOK=`.
#:
#: WARNING: THE GUEST'S BOARD NEVER OPENED, AND THE REASON IS THIS FILE'S OLDEST LAW
#: ONE LEVEL DOWN. Measured live 2026-08-20T10:55Z, the two players differing in
#: exactly one thing -- how long after accepting the message arrived:
#:
#:     m6 guest  accepted 10:55:01, @VsGameInit at 10:55:03  (2 s)  -> nothing,
#:                                            then @GameExit= at 10:55:41
#:     m3 host   accepted 10:55:24, @VsGameInit at 10:55:36  (12 s) -> FOUND,
#:                                            ---->Recv=PLGAMEINIT, ID=0, and it
#:                                            went on to send @CardSelect=
#:
#: The guest accepted FIRST, so its board was pushed while the host had not
#: accepted yet -- into a scene that did not exist, in a store the transition
#: then drained. Same fault as `POL_TM_MATCH41_GAP`, same fault as `@MuchMake`'s
#: two halves: **when a message arrives is as load-bearing as what it says.**
#:
#: A reply-count hold-back cannot fix it, because the thing to wait for is not a
#: number of replies -- it is the OTHER PLAYER. A game starts when everyone is
#: in, so the push waits for the last accept and then goes to all of them.
#: `POL_TM_VSGAME_QUORUM=0` restores the push-on-own-accept behaviour for an A/B.
_MATCH_ACCEPTS = {}

#: (chan, table index) -> set of push keys whose player LEFT a running match
#: (`@Break=` / `@GameExit=` mid-game). The server plays those seats itself --
#: see `_seat_departed` and `_bot_move`. Cleared with the match.
_MATCH_BOTS = {}
#: (chan, table index) of matches where a departed seat got a SYNTHETIC hand
#: (left during card select) -- the take flow is off for those.
_BOT_SYNTH = set()

#: WARNING: THE ACCEPT QUORUM MUST OUTLIVE THE PROCESS. Measured 2026-09-03T22:57Z on
#: prod: member 11 `@GameOK=` at 22:57:13 ("1 of 2"), a hand `docker compose`
#: recreated authsess at 22:57:07-18 EDT (18 s, NOT the git-sync timer -- its
#: 18:57:32 run touched only `jan`), member 6 `@GameOK=` at 22:57:24 -> "1 of
#: 2, waiting for 11". The first accept lived only in `_MATCH_ACCEPTS`, so the
#: fresh process could never reach quorum; both players backed out (`@Break=`
#: + `@GameExit=`) and re-did Start Game. The deploy gate could not help
#: either: `tm-matches-live.json` was first written at `@CardSelect=`, so the
#: whole Start Game -> deal window reported count 0 (`_live_matches_write` now
#: runs at `@GameReady=` / `@GameOK=` too, and counts pending accepts).
#:
#: So each accept is ALSO kept outside the process, keyed by table, with the
#: roster it was given against and a stamp; `_note_accept` adopts a fresh entry
#: when this process holds nothing for that table and the stored roster still
#: matches. A stale entry (older than POL_TM_ACCEPT_TTL_S, default 600 s) is
#: ignored -- an accept from yesterday's game must not start today's. Cleared on
#: quorum, on a fresh announcement (`_remember_match`) and on a rematch reset.
#: `POL_TM_ACCEPT_PERSIST=0` disables both the write and the adopt.
#:
#: Where: the Valkey hash `tm:match-accepts` (it was the file
#: `<POL_DATA_DIR>/tm-match-accepts.json`), one field per table. It is live
#: state; the hash expires POL_TM_ACCEPT_TTL_S after its last write, since no
#: entry is worth adopting after that anyway.
_ACCEPTS_FILE = "tm:match-accepts"


def _accepts_path():
    """The live key the accept sets are kept under."""
    return os.environ.get("POL_TM_ACCEPTS_KEY", _ACCEPTS_FILE)


def _accepts_key(chan, index):
    return "%s|%s" % (chan, index)


def _accepts_ttl():
    return max(1, common._env_int("POL_TM_ACCEPT_TTL_S", 600))


def _accepts_load():
    try:
        got = tmstore.kv.hgetall(_accepts_path())
    except Exception as exc:                     # noqa: BLE001 -- Valkey away
        common._say("tm:   accept quorum store unreadable (%r)" % (exc,))
        return {}
    out = {}
    for field, raw in (got or {}).items():
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            out[field] = entry
    return out


def _accepts_store(data):
    """Replace every table's entry with `data` ({table key: entry})."""
    key = _accepts_path()
    try:
        tmstore.kv.delete(key)
        if data:
            tmstore.kv.hset(key, mapping={k: json.dumps(v) for k, v in data.items()})
            tmstore.kv.expire(key, _accepts_ttl())
    except Exception as exc:                     # noqa: BLE001 -- Valkey away
        common._say("tm:   accept quorum NOT persisted (%r) -- a restart before the "
             "deal loses it" % (exc,))


def _accepts_persist(chan, index, got, need):
    """Keep one table's accept set beside the roster it was taken against."""
    if not common._env_int("POL_TM_ACCEPT_PERSIST", 1):
        return
    key = _accepts_path()
    entry = {
        "stamp": time.time(),
        "got": sorted(str(k) for k in got),
        "need": sorted(str(k) for k in need),
    }
    try:
        tmstore.kv.hset(key, _accepts_key(chan, index), json.dumps(entry))
        tmstore.kv.expire(key, _accepts_ttl())
    except Exception as exc:                     # noqa: BLE001 -- Valkey away
        common._say("tm:   accept quorum NOT persisted (%r) -- a restart before the "
             "deal loses it" % (exc,))


def _accepts_forget(chan, index):
    if not common._env_int("POL_TM_ACCEPT_PERSIST", 1):
        return
    try:
        tmstore.kv.hdel(_accepts_path(), _accepts_key(chan, index))
    except Exception as exc:                     # noqa: BLE001 -- Valkey away
        common._say("tm:   accept quorum entry not cleared (%r)" % (exc,))


def _accepts_adopt(chan, index, need):
    """The persisted accept set for this table, if fresh and for THIS roster."""
    if not common._env_int("POL_TM_ACCEPT_PERSIST", 1):
        return set()
    entry = _accepts_load().get(_accepts_key(chan, index))
    if not isinstance(entry, dict):
        return set()
    age = time.time() - float(entry.get("stamp") or 0)
    ttl = common._env_int("POL_TM_ACCEPT_TTL_S", 600)
    if not 0 <= age <= ttl:
        common._say("tm:   persisted accept set for %s table %s is %ds old (> %ds) "
             "-- ignored" % (chan, index, int(age), ttl))
        return set()
    if sorted(str(k) for k in need) != list(entry.get("need") or []):
        common._say("tm:   persisted accept set for %s table %s was taken against a "
             "different roster (%s vs %s) -- ignored"
             % (chan, index, entry.get("need"), sorted(str(k) for k in need)))
        return set()
    by_str = {str(k): k for k in need}
    got = {by_str[s] for s in (entry.get("got") or []) if s in by_str}
    if got:
        common._say("tm: VERIFIED: adopted %d persisted accept(s) for %s table %s from a "
             "previous process (%s) -- the quorum survives the restart"
             % (len(got), chan, index, ", ".join(str(k) for k in sorted(got, key=str))))
    return got


def _remember_match(chan, index, who):
    """Snapshot the roster for everyone in it. See `_MATCH_ROSTER`."""
    seats = list(who)
    cur = turns._roster_at(chan, index)
    if (_MATCH_BOTS.get((chan, index)) and cur
            and {pushqueue._push_key(m) for m, _v in seats}
            <= {pushqueue._push_key(m) for m, _v in cur}):
        # A leaver being unseated from a match the server is now playing a
        # seat of: the announced roster stands (seat numbers, hands, board),
        # and resetting here would wipe the game the others are still in.
        common._say("tm:   ...%s table %d roster shrank to %d while a departed seat "
             "is being played -- keeping the running match"
             % (chan, index, len(seats)))
        return
    for mid, _ident in seats:
        # SEATED HERE MEANS OUT OF THERE. A member still on another
        # table's roster (a crash left it) is retired from it first, and a
        # live VS. COM binding at another table -- a crashed COM game keeps
        # its table Playing for POL_TM_MATCH_HOLD_S -- is released, since
        # `_in_com_game` would otherwise read this PvP seat as a COM game.
        # See `_match_abandoned`; the same knob gates both.
        _k = pushqueue._push_key(mid)
        _old = _MATCH_ROSTER.get(_k)
        if _old and (_old[0], _old[1]) != (chan, index):
            tableaudit._match_abandoned(mid, "seated at %s table %s now" % (chan, index))
        _cc, _ci = vscom._com_table_of(mid)
        if (_ci is not None and (_cc, _ci) != (chan, index)
                and common._env_int("POL_TM_ABANDON_CLEANUP", 1)):
            common._say("tm: 🧹 member %s is seated at %s table %s but still bound to "
                 "a VS. COM game at %s table %s -- releasing the stale binding"
                 % (mid, chan, index, _cc, _ci))
            vscom._release_com_table(_cc, _ci, why="member %s is seated in a PvP "
                               "match" % mid)
            rematch._reset_for_rematch(None, _k)
    for mid, _ident in seats:
        _MATCH_ROSTER[pushqueue._push_key(mid)] = (chan, index, seats)
    # A fresh announcement is a fresh game: nobody has accepted it yet.
    _MATCH_ACCEPTS.pop((chan, index), None)
    _MATCH_BOTS.pop((chan, index), None)
    _BOT_SYNTH.discard((chan, index))
    _accepts_forget(chan, index)
    matchstart._CARD_READY.pop((chan, index), None)
    # ...and a fresh hand and a fresh turn counter. Leaving the previous match's
    # hands behind would relay the WRONG CARD -- `/H=` is an index into this
    # list and nothing on the wire would contradict it.
    boardrules._MATCH_HANDS.pop((chan, index), None)
    boardrules._MATCH_TURN.pop((chan, index), None)
    boardrules._MATCH_BOARD.pop((chan, index), None)
    boardrules._MATCH_OBJECTS.pop((chan, index), None)
    scoring._MATCH_COMBO.pop((chan, index), None)
    matchstart._TURN_RAND.pop((chan, index), None)
    rematch._MATCH_CONTINUE.pop((chan, index), None)
    # ...and the Double Up escalation. A fresh announcement is a fresh table
    # session; the escalator only compounds ACROSS REMATCHES, which is what
    # makes it a rematch stake and not a wager multiplier.
    pots._TABLE_POT.pop((chan, index), None)


#: (chan, table index) -> set of members who have sent `@GameReady=` this game.
#: Guards the guest wake-up against a ping-pong: only players who have NOT started
#: are woken, so the guest's own @GameReady= (sent after it is woken) does not push
#: @GameStart= back at the already-playing host. Reset on the rules CONFIRM (0x14),
#: which is the once-per-game signal that survives the start sequence's PART churn.
_MATCH_STARTED = {}

#: *** THE RESERVATION IS A HEARTBEAT, AND ITS SILENCE IS THE CANCEL. ***
#:
#: Measured live 2026-08-22 (live testing):
#: the client opens a dedicated `@Pong=/Cnt=N/GM=0` conversation with the TABLE's
#: OWN PEER the moment `@GameEN=` is confirmed -- counter from 0, one beat every
#: ~15 s -- and that heartbeat is owned by the RESERVATION, not the room scene:
#:
#:   * rules-Confirm + parking in the wait scene: beats continue (Cnt 0..7+);
#:   * PART, the rooms list, standing in ANOTHER room: beats continue
#:     (Cnt 16..19 measured after the PART) -- reservation persistence is
#:     literally "keep ponging your table from wherever you are";
#:   * the tile-menu Cancel Reservation: **zero bytes on the wire; the beats
#:     just stop.** The cancel has no message (the bare `@GameQT=` earlier
#:     measurements attributed to it is the settings dialog-confirm).
#:
#: So SE's server can only have freed a cancelled seat by TIMEOUT, and that one
#: rule also covers every ghost-seat producer on record: dead sockets,
#: crashed clients, and the post-match leak (a finished match's players never
#: resume the table heartbeat). `_SEAT_ALIVE` stamps each (chan, member) on
#: every table-peer @Pong; `expire_silent_seats` frees a seat whose stamp is
#: older than POL_TM_SEAT_TTL_S (default 45 s = three missed beats + slack).
#:
#: WARNING: THE MATCH GATE: players in a live match CONSUME the reservation (the
#: client-side clear at 0xA9520) and stop ponging the table -- expiring their
#: seats mid-game would tear the table down under a running match. A table
#: whose match began less than POL_TM_MATCH_HOLD_S ago (default 2700 = 45 min,
#: a generous full-game bound) is exempt; after that the TTL reclaims it, which
#: is deliberate -- nothing else ever ends a match server-side (the result
#: screen is a client-local park; see tm-vs-com-gate).
#:
#: WARNING: RESTART SAFETY: stamps are process-local, so a fresh process sees seats
#: with no stamp. The sweep's first sight of such a seat STARTS its clock
#: rather than freeing it -- a live holder re-stamps within ~15 s, and only a
#: genuinely silent one ages out. (The d1bd5100 lesson: a bounce must never
#: free a live player's seat.)
_SEAT_ALIVE = {}     #: (chan, member int) -> last table-peer @Pong epoch seconds

_MATCH_BEGAN = {}    #: (chan, table index) -> epoch of the game's first @GameReady=
_SEAT_SWEEP = [0.0]  #: last expiry sweep, rate limit


def _match_running(chan, index):
    """Is a game actually RUNNING at this table -- somebody sent `@GameReady=`
    (the host's Start Game) and has not yet sent the room-return `@GameExit=`.

    THE ROOM'S "Playing" TRUTH. While this is True the row publishes at state 2
    (arm 0x67344 -- the observable-in-play tile: "Playing" label, Observe on
    the menu, VAR4 = no join for foreign players). Before this the row sat at
    state 1 (recruiting) for the whole match, so the room kept offering Join
    on a table mid-game (live testing, 2026-08-22).

    WARNING: THIS STATE MUST NOT OUTLIVE THE MATCH. `_MATCH_STARTED` empties on
    the last `@GameExit=` (republished there), is popped when the table empties
    (`_republish_table`), and `_occupancy_state` returns the authored state for
    an empty seat list BEFORE it ever looks at inplay -- so a crashed match
    degrades to the TTL sweep freeing the seats, never to a stuck "Playing".
    Process-local, deliberately: a restart forgets the flag and the row falls
    back to recruiting, which is recoverable; persisting it would be a new
    producer of exactly the phantom-seat problem.

    `POL_TM_INPLAY_STATE=0` disables (seat count >= POL_TM_INPLAY_AT stays the
    only way to state 2)."""
    if not common._env_int("POL_TM_INPLAY_STATE", 1):
        return False
    # ...AND A VS. COM GAME COUNTS. It is a real game at a real table (see
    # `_COM_AT`, and the peer decode that proves which one), so the room shows
    # Playing and offers Observe for it exactly as it does for a PvP match.
    # Before this, a COM game left the tile free: the reported bug.
    if vscom._com_key_at(chan, index) is not None:
        return True
    try:
        return bool(_MATCH_STARTED.get((chan, int(index))))
    except (TypeError, ValueError):
        return False


def _alive_key(chan, member, index=None):
    """The heartbeat key for one member's hold on one TABLE.

    WARNING: `index` IS NOT OPTIONAL DECORATION -- keying by room alone let one
    table's beat keep ANOTHER table's seat alive. The reservation heartbeat is
    addressed to the TABLE's own peer (see the `_SEAT_ALIVE` banner), so a
    player reserved at table 1 who walks into a VS. COM game at table 2 pongs
    table 2 for the whole game -- and under a `(chan, member)` key every one of
    those beats re-stamped the table-1 reservation, so the TTL could never
    reap it. That is a phantom-tile producer with an engine behind it:
    the stale seat is refreshed forever, by design, by a different table.

    Measured 2026-08-25 (member 6, reserved table 1 at 04:04:05Z, COM game at
    table 2 from 04:04:25Z, ponging table 2's peer every ~15 s).

    `index=None` is still accepted so a caller that genuinely means "this
    member's hold on this room" (there is one: the pre-table-resolution path)
    does not have to invent a number, and it keys distinctly from any real
    table so the two can never alias.
    """
    try:
        member = int(member)
    except (TypeError, ValueError):
        pass
    try:
        index = int(index) if index is not None else None
    except (TypeError, ValueError):
        index = None
    return (chan, index, member)


def _peer_table(peer_nick):
    """(chan, index) when this peer nick IS a table's id, else (None, None).

    A published table id is `(K_hi ^ room) << 32 | (n << 12 | n)` (see
    `tmroom.canonical_table_id`), and a nick carries `(id ^ K) & 40 bits`, so
    `peer_guid ^ K` restores the id exactly. The low half's shape (the table
    number twice) is the discriminator -- member guids do not fold to it.
    """
    if not peer_nick:
        return None, None
    try:
        import tmroom
        tid = peers.peer_guid(peer_nick) ^ tmroom.client_key()
    except Exception:
        return None, None
    low = tid & 0xFFFFFFFF
    n = low & 0xFFF
    if not (1 <= n <= 16) or low != ((n << 12) | n):
        return None, None
    room = ((tid >> 32) ^ ((tmroom.client_key() >> 32) & 0xFFFFFFFF)) & 0xFFFFFFFF
    if not (1 <= room <= 999):
        return None, None
    return "#TM0R%03d" % room, n


def expire_silent_seats(now=None):
    """Free every seat whose holder's table heartbeat has gone silent.

    See the `_SEAT_ALIVE` banner: the client's cancel (and every crash/ghost
    variant) is expressed ONLY as the table-peer @Pong stopping. Runs from the
    @Pong arm (rate-limited) and from `reconcile_room_tables`, sweeps the
    SHARED seat map so a seat this process has not touched is still covered.
    Returns the number of seats freed. `POL_TM_SEAT_TTL_S=0` disables.
    """
    ttl = common._env_int("POL_TM_SEAT_TTL_S", 45)
    if ttl <= 0:
        return 0
    now = now if now is not None else time.time()
    if now - _SEAT_SWEEP[0] < min(ttl / 3.0, 15.0):
        return 0
    _SEAT_SWEEP[0] = now
    freed = 0
    try:
        import tmroom
        hold = common._env_int("POL_TM_MATCH_HOLD_S", 2700)
        for room_hex, tbls in (tmroom._live_seats() or {}).items():
            try:
                rid = int(room_hex, 16)
            except (TypeError, ValueError):
                continue
            chan = "#TM0R%03d" % (rid & 0xFFFFFFFF)
            for idx_s, rows in (tbls or {}).items():
                try:
                    idx = int(idx_s)
                except (TypeError, ValueError):
                    continue
                began = _MATCH_BEGAN.get((chan, idx))
                if (_MATCH_STARTED.get((chan, idx))
                        and (began is None or now - began < hold)):
                    continue        # a match is (or may be) running here
                for m, _i in list(rows or []):
                    key = _alive_key(chan, m, idx)
                    stamp = _SEAT_ALIVE.get(key)
                    if stamp is None:
                        # First sight (deploy, restart, adoption): start the
                        # clock instead of freeing -- a live holder re-stamps
                        # within one beat (~15 s).
                        _SEAT_ALIVE[key] = now
                    elif now - stamp > ttl:
                        common._say("tm: member %s's seat at %s table %d: table "
                             "heartbeat silent %ds (> TTL %ds) -- the client "
                             "cancelled or is gone; releasing"
                             % (m, chan, idx, int(now - stamp), ttl))
                        tableaudit.release_seats(m, chan)
                        _SEAT_ALIVE.pop(key, None)
                        freed += 1
    except Exception as exc:
        common._say("tm:   heartbeat expiry sweep failed (%r) -- seats stand" % (exc,))
    return freed


#: member_id -> the peer NICK that member's `@GameOK=` arrived on.
#: That is the band its match scene reads; see `_pending_pushes`.
#: WARNING: A VS. COM game never sends `@GameOK=`, so the COM path fills this at
#: `@GameENC=` time instead -- the game peer that request arrives on is the
#: peer whose guid becomes the match object's SESSION PAIR ([obj+0x28]/[0x2C],
#: stored by the `@ComGameInit` arm at 0x102D3C from the delivering record's
#: sender u64). Every in-match push must be FRAMED from that nick or the
#: equality gates (0x1034FD for @StartData, 0x103054 for @Quit, ...) discard
#: it silently -- which is what "the deal was pushed and nothing happened" was.
_MATCH_PEER = {}


def _note_accept(chan, index, member_id, seats):
    """Record one `@GameOK=`; return the members to hand the board to.

    Empty until the last player accepts, then every seat at once. See
    `_MATCH_ACCEPTS`.
    """
    if not common._env_int("POL_TM_VSGAME_QUORUM", 1):
        return [member_id]
    need = {pushqueue._push_key(m) for m, _i in seats}
    if (chan, index) not in _MATCH_ACCEPTS:
        # Nothing in THIS process for the table: a restart may have eaten an
        # earlier accept (measured 2026-09-03T22:57Z, see `_ACCEPTS_FILE`).
        _MATCH_ACCEPTS[(chan, index)] = _accepts_adopt(chan, index, need)
    got = _MATCH_ACCEPTS[(chan, index)]
    got.add(pushqueue._push_key(member_id))
    waiting = need - got
    if waiting:
        _accepts_persist(chan, index, got & need, need)
        webwatch._live_matches_write()
        common._say("tm:   %s table %d: %d of %d accepted -- holding the board until "
             "%s does too (a board pushed before the last accept lands in a "
             "scene that does not exist yet)"
             % (chan, index, len(got & need), len(need),
                ", ".join(str(w) for w in sorted(waiting, key=str))))
        return []
    _accepts_forget(chan, index)
    common._say("tm:   %s table %d: ALL %d accepted -- the game starts now"
         % (chan, index, len(need)))
    return [m for m, _i in seats]


def _match_of(member_id):
    """(chan, index, seats) for the match this member is in, or (None, None, []).

    In-match messages arrive AFTER the player has left the room, so `_SEATED`
    and `room_of()` are both empty by then -- the announced-match snapshot is
    the only thing that still knows. See `_MATCH_ROSTER`.
    """
    got = _MATCH_ROSTER.get(pushqueue._push_key(member_id))
    return got if got else (None, None, [])


def _table_of_member(chan, member_id):
    """(table index, seats) for wherever this member is sitting, or (None, []).

    The live seat list first -- it is authoritative while they are still in the
    room -- then the announced-match snapshot, which is the only source that
    survives the PART that entering a match performs.
    """
    for idx, seats in sorted(seating._seats_of(chan).items()):
        if any(m == member_id for m, _i in seats):
            return idx, list(seats)
    remembered = _MATCH_ROSTER.get(pushqueue._push_key(member_id))
    if remembered:
        r_chan, idx, seats = remembered
        if chan is None or r_chan == chan:
            return idx, list(seats)
    return None, []
