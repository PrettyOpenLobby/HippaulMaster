"""The shop scene's @Init family: the card shop opener, the auction Check Out door, the event shop
list, the champion's pack.
"""
import os
import time
import tmstore
from .deps import tmauction
from . import cardshop, cardtables, common, protocol, purse


#: *** THE MONEY MESSAGE. WE HAD NEVER SENT IT, WHICH IS WHY MONEY READ 0. ***
#:
#: `AUCMONEY` is msgid 0x20 on code 0xB2, and its arm is TM.dll rva 0x1060B4:
#:
#:     050960BD  push 0x51c4728            "@Data" -- the body's command key
#:     050960C2  call 0x503a920            ...and the body must start with it
#:     050960D8  cmp  [esi+0x28] / +0x2c   the SENDER ID gate (as SHOPINIT)
#:     050960F9  cmp  [esi+0x30]           the SHOP NUMBER gate  <- this is /N=
#:     05096119  push 0x51c4b2c            "/M=" -- the SAME key SHOPINIT parses
#:     0509611E  call 0x503b080
#:     0509612A  mov  [esi+0xcc], ecx      -> scene+0xCC, a DWORD
#:
#: So `/M=` IS the money -- just not on SHOPINIT, where the very same key is
#: parsed and then thrown away (its value is overwritten by the `/CP=`
#: call before anything reads it). Two messages, one key, one live use.
#:
#: MEASURED SYMPTOM: a tester's money read 0 on BOTH the card shop and the
#: auction screen, through a save carrying 4321 at `tmsave.MONEY_OFF` and a
#: SHOPINIT carrying `/M=3333` and `/CP=1234`. None of them is the wallet,
#: because the wallet arrives here and we were silent.
#:
#: WARNING: THE SHOP NUMBER IN THE HEADER MUST EQUAL `/N=`. `[esi+0x30]` is where
#: SHOPINIT stores `/N=`, and this arm compares the incoming header's shop byte
#: against it -- so a mismatch is a silently dropped message, not an error. The
#: header is `code | msgid << 16 | shop << 24`, which is why this is built rather
#: than a constant. The client confirms the pairing from its own side: served
#: `/N=11`, its next `@Quit=` came back as `sh=11`.
MSG_AUCMONEY = 0x002000B2   #: code 0xB2 + msgid 0x20; the shop byte is OR'd in


def _shop_n():
    """The `/N=` we serve, which is the SHOP NUMBER every later message carries."""
    try:
        return int(os.environ.get("POL_TM_SHOP_N", "1"), 0) & 0xFF
    except ValueError:
        return 1


def _aucmoney_enabled():
    return os.environ.get("POL_TM_AUCMONEY", "1") == "1"


#: *** `@ShReq=` IS TWO DOORS, AND THE msgid IS WHICH ONE. ***
#:
#: Measured on prod 2026-08-19, twice, 28 seconds apart in one session:
#:
#:     20:41:54  GTM0GB0000000@ShReq=...   code 176 msgid 0x00   card shop
#:     20:42:22  GTM0GB0000100@ShReq=...   code 176 msgid 0x01   auction Check Out
#:
#: -- the header layout the banner above `MSG_SHOPINIT` documents (chars 0..1
#: code, 4..5 msgid, 6..7 shop), and authserv decoded the second one as
#: `msgid=0x01` on its own. The handler matched on the body text alone and
#: answered both identically, AUCMONEY included.
#:
#: WARNING: WHICH FABRICATED A SALE. `/M=` means DIFFERENT THINGS on the two scenes:
#: on the card shop it is the wallet (the measurement in the `_start_money`
#: banner: money read 0 on both screens until this message existed), but the
#: auction Check Out screen announces it as THE PROCEEDS OF A CARD SALE. Two
#: machines said so on the same evening, each quoting its OWN member's balance:
#:
#:     member 3   no stored balance -> _start_money() -> "/M=10000" -> sold for 10,000
#:     member 1   stored 4321 (the selftest's own value, `data/resources/
#:                1.tm_collection.json`)  -> "/M=4321"  -> sold for 4,321
#:
#: Two servers, two members, two numbers, each matching that member's wallet.
#: That is our message being read back, not a client-side computation.
#:
#: WARNING: AND WITHHOLDING THE MESSAGE IS **NOT** THE ANSWER -- MEASURED, 2026-08-19,
#: and it is the correction to this banner's own first version. Gating the push
#: off for msgid 0x01 made Check Out hang EARLIER: the screen no longer rendered
#: at all, the client went straight to `@Pong=/Cnt=0` and stayed there. So
#: AUCMONEY is a message the Check Out scene REQUIRES, exactly as SHOPINIT is the
#: one that advances the prize centre (see the `@CvReq=` banner) -- the scene
#: blocks until it arrives.
#:
#: It follows that `/M=` here is not "a wallet we should not have sent", it is
#: **the amount awaiting collection**, and the bug was only ever the VALUE. With
#: no completed auction that amount is 0, which is also what the all-zero
#: `u/g/TM0_DI` says (nothing to collect). So both doors get the message and the
#: AMOUNT is what differs:
#:
#:     msgid 0x00  card shop      /M= = money_of(member)   -- the wallet
#:     msgid 0x01  Check Out      /M= = POL_TM_CHECKOUT_M  -- awaiting collection
#:
#: `POL_TM_AUCMONEY_SHOP_ONLY=1` restores the withhold, which is kept ONLY so the
#: measurement above can be reproduced. It is not a fallback: it is the thing
#: that hung the load.
#:
#: WARNING: PARTLY RETRACTED, 2026-08-19 -- "the bug was only ever the VALUE" IS WRONG.
#: Everything above about the two doors and about AUCMONEY being required stands;
#: the conclusion that only `/M=` remained does not. The client's own log shows
#: the Check Out screen running the CARD SHOP's `@Init` arm, because that is the
#: SHOPINIT body we sent it. See the banner above `_INIT_ARM_FIELDS`: the arm was
#: wrong, which is why chasing this field never moved the screen.
SHREQ_MSGID_CHECKOUT = 0x01


def _aucmoney_shop_only():
    return os.environ.get("POL_TM_AUCMONEY_SHOP_ONLY", "0") == "1"


def _checkout_money():
    """`/M=` for the auction Check Out door -- the proceeds awaiting collection.

    0 until a real auction settles, which is the honest answer and the one that
    agrees with the empty `u/g/TM0_DI`. Overridable because the moment a sale
    DOES settle this stops being a constant, and because it is the field to A/B
    if the screen turns out to want something else here.
    """
    env = os.environ.get("POL_TM_CHECKOUT_M")
    if env is not None:
        # The A/B override still wins, so the field stays testable.
        try:
            return max(0, int(env, 0))
        except ValueError:
            return 0
    return 0


def _aucmoney_line(member_id, amount=None):
    """`AUCMONEY` carrying this member's balance, or None if it is switched off.

    `amount` overrides the wallet -- the Check Out door reads the same `/M=` as
    the proceeds awaiting collection, not as money held. See the banner above
    `SHREQ_MSGID_CHECKOUT`.

    Pushed UNSOLICITED right after SHOPINIT, for the same reason SHOPINIT itself
    is: once the client has the endpoint it stops asking and waits. The arm has
    no reply of its own -- it stores the value and sets a flag.
    """
    if not _aucmoney_enabled():
        return None
    code = MSG_AUCMONEY | (_shop_n() << 24)
    amount = purse.money_of(member_id) if amount is None else amount
    return protocol.encode_code(code) + (b"@Data=/M=%d" % amount)


#: `AUCCARDS` -- code 0xB2 msgid 0x21, the arm at TM.dll 0x10613D. Decoded
#: 2026-08-20 by listing the Check Out scene's `0x101A10` waiters: that scene
#: waits on exactly TWO commands, 0x20 (AUCMONEY, the money) and 0x21 (this,
#: the cards). So Check Out's two halves are MESSAGES, which is why the
#: `u/g/TM0_DI` / `_BI` files are loaded and never read on the PC build.
#:
#: The body is the CARD SHOP'S OWN `@Card=` shape -- `0x106200` formats `"@N%d"`
#: in a loop over `/S=` and parses each one's `/D=` -- so nothing new had to be
#: reverse engineered for delivery.
#:
#: WARNING: `/E=` GATES THE WHOLE LIST: `0x1061BC` bails on `<= 0` before reading a
#: single card, so it must be positive or the message is a no-op that looks
#: exactly like one that never arrived.
MSG_AUCCARDS = 0x002100B2


#: EVENT SHOP (via @EInit down the Prize Center door) -- item list, PACED. Members
#: who just opened the event shop and owe an item push on their next @Pong(s).
#: In authsess only (both @CvReq and @Pong are auth-band), so a module dict is
#: safe. member_id -> pushes remaining.
_EVENTSHOP_PENDING = {}
#: Members standing in the PRIZE CENTER (their last door was `@CvReq=`). The
#: shop scene's `@Buy=` means "exchange prize points for prize /No=" there, and
#: "buy pack /No= for gold" behind `@ShReq=`; nothing else on the wire says
#: which.
_PRIZE_DOOR = set()


def _eventshop_item_lines(member_id):
    """The event shop's item list -- delivered via @EcmInit (0xB2 msgid 0x13), the
    EVENT CARD-MACHINE init, NOT @Card/@Data.

    MEASURED 2026-08-22 (tmlog store-lookup trace): after @EInit the scene polls
    code 0xB2 with ONLY msgid 0x13 (SHOPINIT family) and 0x27 (@Dead) -- 5776 polls
    each, ZERO of 0x21(@Card)/0x20(@Data). So the item list arrives on the SAME
    0x13 arm as the opener, as @EcmInit, which carries the cards inline: after
    /N /RA /M /CP /S /RT /PM comes /C=<count> then @N<d>/D=<row> records (arm
    0x105788). @EcmInit MUST include /C= or the arm loops @N%d over
    UNINITIALISED STACK and freezes the client (the missing-/C= bug).
    Same 0x13 framing as @EInit (which the scene accepted), so the sender/shop gate
    that rejected @Card does not apply. POL_TM_EINIT_PM tunes /PM=."""
    if os.environ.get("POL_TM_EINIT_CARDS", "1") != "1":
        return []
    n  = common._env_int("POL_TM_SHOP_N", 1)
    rt = common._env_int("POL_TM_EINIT_RT", 1)
    # WARNING: THE MEMBER'S REAL BALANCE, NOT A CONSTANT. This was a flat 10000, the
    # same invented figure `_start_money` used to carry -- and a shop opener that
    # announces a wallet the member does not have is how the 2026-08-25 grant
    # reached players in the first place. An explicit POL_TM_EINIT_M still wins,
    # because this whole body is a live-bisection experiment.
    m  = common._env_int("POL_TM_EINIT_M", purse.money_of(member_id))
    pm = common._env_int("POL_TM_EINIT_PM", 0)
    cards = cardshop._shopbuy_cards(common._env_int("POL_TM_EINIT_PACK", 1))
    # /CP= IS THE CARD LEVEL SLOT (struct +0x00, stored at 0x105836 in this
    # arm) -- the number the VS. COM opponent unlock reads. A 0 here locks the
    # opponent list; see `_comgame_body` and `card_level`.
    body = (b"@EcmInit=/N=%d/RA=0/M=%d/CP=%d/S=1/RT=%d/PM=%d/C=%d"
            % (n, m, cardtables.card_level_of(member_id), rt, pm, len(cards)))
    for i, v in enumerate(cards):
        body += b"@N%d=/D=" % i + b"|".join(b"%d" % x for x in v)
    return [protocol.encode_code(protocol.MSG_SHOPINIT) + body]


#: `@Dead` -- code 0xB2 msgid 39, arm TM.dll 0x105DB3. WARNING: THE SHOP SCENE'S
#: ERROR TERMINATOR, NOT AN END-OF-STREAM MARKER -- the 08-21 "stream over +
#: closing notice" double-@Dead model is RETRACTED (static decode 08-21, the
#: whole sequencer read end to end).
#:
#: EVERY state of the Check Out sequencer (0x1333D0, dispatch 0x133FDC /
#: 0x134024) polls 0x27 as an ABORT channel while it walks toward the exit,
#: and an answered @Dead in ANY of them pushes dialog id 221 via
#: 0x1952A0(scene, 0x142600A, 0xDD) and parks the scene in state 0x80,
#: which waits for the user to acknowledge a box that says -- descriptor
#: table 0x2373E8, entry 0xDD, its own inline strings -- "Failed to connect
#: to server." (en) / "the shop master does not exist" (jp), TRM string id
#: -37221. The 08-21T04:56Z "spinner forever, then failed-to-connect, on
#: both machines" was this path working exactly as coded: our @Dead was
#: eaten by state 9/0x0A's abort polls BEFORE the client reached state 0x0B,
#: where it SENDS `@Quit=` (0x101530) -- so it never asked to leave, nothing
#: ever answered msgid 23, and the scene died on its own 60 s timeouts
#: (dialog 343 = "Timed out while disconnecting from the server.").
#: WARNING: The earlier "Aucti.BIN 221 'You sold your card!'" label for this dialog
#: was numerology -- dialog-id space is TRM 37000+id, not Aucti rows.
#:
#: The CLEAN exit needs no push at all: after money B the client walks
#: states 9->0x0B on its own sub-scene events ([0x2EF344] is client-set),
#: sends `@Quit=/No=1` on (0xB2, 0x17), and our `@EQuit=/D=0` answer on the
#: echoed header advances 0x0C -> 0x0D -> 0x0E -> clean teardown (the @EQuit
#: arm 0x105BA3: /D= must be 0, /Stat= 0..3 optional, sender-id and /N= must
#: match the door's @Init -- all four /Stat= arms set result 1).
#:
#: Kept only for the legacy knob (POL_TM_CHECKOUT_STREAM=0) bisect shape.
MSG_AUCDEAD = 0x002700B2


def _aucdead_line():
    """`@Dead` -- "that is all the cards", which ends the Check Out delivery."""
    return protocol.encode_code(MSG_AUCDEAD | (_shop_n() << 24)) + b"@Dead="


def _pending_of(member_id):
    """What this member is owed, or an empty purse if the store is unreadable."""
    if tmauction is None:
        return {"money": 0, "cards": []}
    try:
        return tmauction.pending(member_id)
    except Exception:
        return {"money": 0, "cards": []}


def _checkout_money_for(member_id):
    """`/M=` for this member: the env override, else what they are actually owed."""
    env = os.environ.get("POL_TM_CHECKOUT_M")
    if env is not None:
        return _checkout_money()
    return int(_pending_of(member_id).get("money") or 0)


def _auccards_line(member_id, bucket="cards"):
    """`AUCCARDS` for this member -- ALWAYS a message, even with nothing owed.

    `bucket` picks which Check Out card section this fills: "cards" = RETURNED
    (unsold) cards on cards-A ("Returned Card"), "won" = cards WON at auction
    on cards-B ("Successful Bid"). Same wire shape, different pending list.

    WARNING: AN EMPTY LIST STILL HAS TO BE SENT, and getting this wrong hung the
    screen (measured 2026-08-20T21:48Z: Check Out served AUCMONEY alone and sat
    there). The first version returned None when there was nothing to deliver,
    reasoning that `/E=` <= 0 makes the arm bail before reading a field so the
    message is "indistinguishable from silence".

    That confuses two different mechanisms. The ARM bailing is about parsing.
    The WAIT at `0x134F97` is `0x101A10(0x21)` -- it needs command 0x21 to
    ARRIVE at all, and no amount of bailing happens if nothing turns up. The
    scene waits on 0x20 AND 0x21, so answering only the money answers half of
    it and blocks on the other half for ever.

    `/E=0` is therefore the correct way to say "nothing to collect": the
    command is answered, and the arm reads no cards. Answer the command the
    client asks, even when the answer is empty -- the same rule the chat roster
    needed.
    """
    if tmauction is None:
        return None
    pend = _pending_of(member_id)
    buckets = (bucket,) if isinstance(bucket, str) else tuple(bucket)
    cards = []
    for b in buckets:
        cards += pend.get(b) or []
    code = MSG_AUCCARDS | (_shop_n() << 24)
    if not cards:
        return protocol.encode_code(code) + b"@Card=/E=0/S=0/C=0"
    body = b"@Card=/E=1/S=%d/C=%d" % (len(cards), len(cards))
    for i, ii in enumerate(cards):
        row = tmauction.card_row_from_ii(ii)
        body += b"@N%d=/D=" % i + b"|".join(b"%d" % v for v in row)
    return protocol.encode_code(code) + body


#: *** THE CHECK OUT SCENE IS FOUR SECTIONS, AND THE WHOLE MACHINE IS NOW READ
#: OUT OF TM.dll (2026-08-21, static; rvas at image base 0x04F90000, flat dump).
#: The short version:
#:
#:   * The scene sequencer (inner object update, rva 0x1333D0, state at
#:     [obj+0x17C], jump table 0x133FDC) waits SHOPINIT (0x101A10(0x13)) and
#:     then runs FOUR sub-scenes in order, each with its own 0x101A10 wait and
#:     each OR-ing one bit into the global [0x2EF340] when it had nothing:
#:
#:         money A  rva 0x1348F6  waits cmd 0x20  bit 1   (the 08-19 'Payment:
#:                                                         777' notice was THIS)
#:         cards A  rva 0x134F06  waits cmd 0x21  bit 2
#:         cards B  rva 0x135416  waits cmd 0x21  bit 4
#:         money B  rva 0x135926  waits cmd 0x20  bit 8   (built only after the
#:                                                         @Quit answer, 0x133A3F)
#:
#:     [0x2EF340] == 0xF is what the outer scene (0x132A8A) tests to draw the
#:     "nothing to check out" outcome. Aucti.BIN 336..343 name the sections:
#:     Bid Refund / Collecting Payment / Returned Card / Successful Bid --
#:     WHICH section wears WHICH label is still unmeasured except money A
#:     (its dialog drew "Payment: 777" on 2026-08-19, so it is the collect side).
#:
#:   * So the door needs TWO 0x20 records and TWO 0x21 records queued -- the
#:     store is a queue and each section's wait consumes the next matching
#:     record, exactly the mechanism SHOPINIT already relies on. One AUCMONEY
#:     alone (the previous default) parks the client for ever in cards A's
#:     0x101A10(0x21): measured as the drain poll 0x2C/21/25/23/2A/43/B2/A2.
#:     THAT is why the screen "fails to load" -- this scene has NO base UI; it
#:     renders nothing but its per-section dialogs, so an unanswered section IS
#:     a black screen.
#:
#:   * @Dead (cmd 0x27) is NOT this scene's end-marker. The sequencer tests it
#:     at every section boundary (7 sites: 0x13353F, 0x133636, 0x13371B,
#:     0x133800, 0x1338E5, 0x133B07, 0x133BEB) and an ANSWERED 0x27 raises
#:     Aucti.BIN 221 -- "You sold your card! Collected closing bid amount." --
#:     and parks the scene in state 0x80. Sending @Dead in the entry batch
#:     therefore FABRICATES a sale notice, the AUCMONEY /M= trap in message
#:     form. Do not send it here.
#:
#:   * The 08-21T01:5x "falsification" of the @Dead theory is itself retracted:
#:     authserv.log shows those runs served @EcmInit + /M=0 WITH AUCCARDS and
#:     @Dead still on (fa4fbe60 was not yet deployed at 01:34/01:37Z), and the
#:     777 env never reached the container (/M=0 on the wire, wallet 39260
#:     logged beside it). No run has ever reproduced the 08-19 shape.
#:
#: `POL_TM_CHECKOUT_STREAM=0` restores the pre-stream behaviour (one AUCMONEY
#: plus the POL_TM_AUCCARDS gate) so the broken shape stays reproducible.
def _checkout_stream_enabled():
    return os.environ.get("POL_TM_CHECKOUT_STREAM", "1") == "1"


def _auccards_empty_line():
    """A well-formed EMPTY cards section -- answers a 0x21 wait, delivers 0."""
    code = MSG_AUCCARDS | (_shop_n() << 24)
    return protocol.encode_code(code) + b"@Card=/E=0/S=0/C=0"


#: WARNING: MEASURED LIVE 2026-08-21T03:29Z, AND IT RETRACTS THE PRE-QUEUE: a
#: section's wait DRAINS EVERY same-msgid record in the store at once (m3's
#: trace: cards B polling (0xB2,33) against store_count=5, "not found" -- the
#: second @Card had been drained together with the first by cards A; same
#: mechanism the 08-20T22:00Z "the cards HAD been consumed" measured). So the
#: later sections' records must arrive AFTER the earlier section drained, and
#: the client provides the pacing signal itself: **@Get=/No=1 (0xB2 msgid
#: 0x15, no arm -- table 0x106D98 sends 0x15 to the default) is sent when a
#: cards section completes** (sub-scene state 6, 0x106E30(0x15) at 0x13538C).
#: The flow is a PULL: door batch answers sections 1-2, the first @Get
#: releases cards B's record, the second releases money B's -- and that is
#: the whole stream. (The 08-21 note that 0x133A3F "builds money B's
#: sub-scene" was wrong: 0x133A3F is state 0x0C's msgid-23 arm building the
#: EXIT window after our @EQuit answer; money B was measured consumed with
#: no @Quit exchange at 03:42Z.) The exit is client-driven: @Quit= ->
#: @EQuit=/D=0.
_CHECKOUT_PULL = {}

#: WARNING: THE DRAIN RULE SETS THE PACING. Both card sections wait 0x21 and both
#: money sections wait 0x20, and a section's wait DRAINS EVERY same-msgid
#: record in the store at once (03:29Z, 20:28Z: an all-in-door batch left
#: cards B starving after cards A ate both 0x21). So each same-type record
#: must arrive AFTER its predecessor drained -- paced on the @Get a card
#: section sends when it COMPLETES (instantly when empty, after the player's OK
#: when it displayed a card -- confirmed 20:28Z, a card on cards-A displayed
#: and the OK sent the @Get). The synthesis of every measurement:
#:     door        -> money A + cards A (the card)
#:     @Get #1     -> cards B (empty)         [cards A drained, fresh 0x21]
#:     @Get #2     -> money B + @EQuit        [money A drained, fresh 0x20]
#: money B and cards B are pre-built at door time so they survive the clear.
#:
#: WARNING: RETURNED cards -> cards-A ("Returned Card"), WON cards -> cards-B
#: ("Successful Bid"). The earlier "all cards on cards-A" was because cards-B
#: hung -- but that was the SAME missing-terminator bug later found for cards-A
#: (19:37Z cards-B had a card with NO /E=0 terminator, so its receive loop
#: never closed). With the terminator, cards-B should display too. Each card
#: section is [<@Card list> , <@Card /E=0 terminator>] when non-empty; an empty
#: section is just its own /E=0.
_CHECKOUT_PULL = {}
_CHECKOUT_MONEYB = {}
_CHECKOUT_CARDSB = {}


def _auccards_section(member_id, bucket):
    """A card section's records: the @Card list plus a /E=0 TERMINATOR when it
    is non-empty (the receive loop needs it to close and display the card); an
    empty section is a single /E=0, which is its own terminator."""
    line = _auccards_line(member_id, bucket)
    if line and b"/E=1" in line:
        return [line, _auccards_empty_line()]
    return [line or _auccards_empty_line()]


def _checkout_stream_lines(member_id, amount):
    """The door batch: money A (proceeds) + cards A (RETURNED cards + its
    terminator). cards B (WON cards) and money B pace onto the follow-up @Gets
    -- see the banner above (the drain rule). Pre-builds cards B and money B
    before the caller's clear.
    """
    _CHECKOUT_PULL[member_id] = 1
    refund = int(_pending_of(member_id).get("refund") or 0)
    _CHECKOUT_MONEYB[member_id] = _aucmoney_line(member_id, refund)
    _CHECKOUT_CARDSB[member_id] = _auccards_section(member_id, "won")
    lines = []
    money_a = _aucmoney_line(member_id, amount)
    if money_a:
        lines.append(money_a)
    lines += _auccards_section(member_id, "cards")       # cards A = returned
    return lines


def _checkout_pull_lines(member_id, peer_nick=None):
    """@Get #1 (cards A done) -> cards B (WON cards + terminator, fresh 0x21).
    @Get #2 (cards B done) -> money B + @EQuit (fresh 0x20). Each same-type
    record lands after its predecessor drained (the drain rule)."""
    stage = _CHECKOUT_PULL.get(member_id)
    if stage == 1:
        _CHECKOUT_PULL[member_id] = 2
        return _CHECKOUT_CARDSB.pop(member_id, None) or [_auccards_empty_line()]
    if stage == 2:
        _CHECKOUT_PULL[member_id] = 3
        money_b = _CHECKOUT_MONEYB.pop(member_id, None) or _aucmoney_line(
            member_id, 0)
        equit = (protocol.encode_code(0xB2 | (0x17 << 16) | (_shop_n() << 24))
                 + (b"@EQuit=/D=0/N=%d" % _shop_n()))
        return [l for l in (money_b, equit) if l]
    return []


def _shopinit_body(member_id=None):
    """`@Init=` plus the six fields arm 0x105201 reads, in its own parse order.

    `/M=` is the member's REAL money now -- see the banner above `_start_money`.
    An explicit `POL_TM_SHOP_M` still wins, because that override is how this
    field's meaning was being A/B'd in the first place.
    """
    out = bytearray(b"@Init=")
    for name, default in cardshop._SHOPINIT_FIELDS:
        if name == "CP" and "POL_TM_SHOP_CP" not in os.environ:
            # /CP= IS THE CARD LEVEL SLOT (struct +0x00, this arm's store is
            # 0x1052B1), the number the VS. COM opponent unlock compares
            # against each PlPrm.BIN +0x02 threshold. The `_SHOPINIT_FIELDS`
            # default sent a flat 0, so opening the CARD SHOP zeroed it too.
            # See `card_level` and `_init_body`'s CP branch.
            out += b"/CP=%d" % cardtables.card_level_of(member_id)
            continue
        if name == "M" and "POL_TM_SHOP_M" not in os.environ:
            out += b"/M=%d" % purse.money_of(member_id)
            continue
        # WARNING: `/CP=` IS CARD POINTS, NOT MONEY -- and this line briefly carried
        # the balance because of that mistake. `/CP=` stores to struct +0x00
        # (0x1052B1 here, 0x102DCA in @ComGameInit), which the save parser fills
        # from file **+0x38**; MONEY is file +0x34 -> struct +0xC8, where `/M=`
        # lands. Putting the wallet in `/CP=` is the same off-by-four the save
        # writer had, in the other direction. See tmsave.MONEY_OFF.
        try:
            v = int(os.environ.get("POL_TM_SHOP_" + name, str(default)), 0)
        except ValueError:
            v = default
        out += b"/%s=%d" % (name.encode("ascii"), v)
    # WARNING: `/PM=` IS THE MONEY, BUT IT IS **NOT A FIELD OF `@Init`** -- CORRECTED.
    #
    # An earlier version of this comment claimed SHOPINIT zeroes the wallet when
    # `/PM=` is absent, and that omitting it was the bug. That was WRONG and the
    # live client said so: served `/PM=4321`, money still read 0.
    #
    # msgid 0x13 handles FOUR DIFFERENT BODY COMMANDS, not one message with
    # variants, and each has its own field set:
    #
    #     @Init     0x105201   /N= /RA= /M= /CP= /S= /SP= /SN= /CMP= /HD=
    #     @ExcInit  0x1054EE   /N= /PP= /LC=
    #     @EInit    0x10563A   /N= /RA= /M= /CP= /S= /RT= /Stat=
    #     @EcmInit  0x105788   /N= /RA= /M= /CP= /S= /RT= **/PM=**
    #
    # `/PM=` and the zeroing at 0x10589A both live in the `@EcmInit` block. We
    # send `@Init`, so neither runs -- the key below is simply IGNORED. It is
    # kept because it costs nothing and reads correctly in a log, NOT because it
    # is known to do anything.
    #
    # WHAT IS STILL TRUE: `[esi+0xcc]` is the money field, written by `@EcmInit`
    # and by `AUCMONEY`, and `/M=` on `@Init` is parsed and discarded.
    # WHAT IS NOT KNOWN: what puts money on the CARD SHOP screen, which is the
    # one the player is reading. Do not add another invented key here.
    #
    # SHOPINIT parses FOURTEEN distinct keys, not the six this file has been
    # serving since the message was first answered:
    #
    #   /N= /RA= /M= /CP= /S= /SP= /SN= /CMP= /HD= /PP= /LC= /RT= /Stat= /PM=
    #
    # The arm (rva 0x105201..0x105a97) is four alternative blocks, each
    # re-reading /N= /RA= /M= /CP= /S=, and the LAST one ends:
    #
    #   0509588E  push 0x51c4770         "/PM="
    #   05095893  mov byte [esi+0xea], 1
    #   0509589A  mov dword [esi+0xcc], 0        <- money ZEROED first
    #   050958A0  call 0x503b080                 <- then parsed
    #   050958AC  mov dword [esi+0xcc], eax      <- money := /PM=
    #
    # `[esi+0xcc]` is the same field AUCMONEY writes, so the two agree on where
    # money lives; `/M=` in this message is parsed and discarded, which is
    # the trap that cost several rounds. A DWORD here, unlike the
    # save's u16 at `tmsave.MONEY_OFF`.
    #
    # THE CHAMPION'S NAME. Read off arm 0x105201 on 2026-09-04: `/SN=` is
    # copied (0xAAD60, fixed 0x11 bytes) into the shop object at +0x148, then
    # formatted as "%s's Pack" (0x105368) and logged as `Now1stPackName=` --
    # it is the label of PackPrm record 5, `rank_1 Pack` (kind 3, 100,000
    # gold, three cards drawn from levels 9..11), Cardsh.BIN's "celebrate
    # <X>'s #1 rank" feature. This is the wiki-documented Tetra Master
    # "event": the top-ranked player's pack in the ordinary Card Shop. The
    # weekly publish names the champion (`tools/tmrank.py`, table `tm_champion`);
    # with no champion the key is omitted and the shop shows "'s Pack", which
    # is what it has always shown.
    _sn = _champion_name()
    if _sn:
        out += b"/SN=" + _sn
    if os.environ.get("POL_TM_SHOP_PM", "1") != "0":
        out += b"/PM=%d" % purse.money_of(member_id)
    return bytes(out)


#: THE CHAMPION -- last week's #1 by VS. Rating, named by the Sunday publish.
#: The newest row of the `tm_champion` table (PostgreSQL, through tmstore; it
#: was the file `<POL_DATA_DIR>/tm-champion.json`) = {"member_id", "name",
#: "week"}. The card shop labels the rank_1 Pack "<name>'s Pack" (`/SN=`,
#: above). `POL_TM_CHAMPION_NAME` overrides the table for a test. Read at most
#: once every _CHAMPION_TTL_S, since the shop opens far more often than the
#: champion changes (once a week).
#:
#: WARNING: RETRACTED 2026-09-07: the save header's +0x104 string is NOT the shop's
#: pack label. The shop init at 0x1014EE does copy save +0x104 into the shop
#: object's +0x148 -- but that slot is the FIRST DECK SET NAME, and the first
#: Sunday publish (week 2957) put the champion's name on every
#: player's first deck tab (seen on a tester's card screen). The save stamp is now
#: behind `POL_TM_CHAMPION_SAVE_SLOT` (default 0); the slots are authored from
#: the player's own `@Decks=` name report -- see `SAVE_OFF_DECK_NAMES`.
_CHAMPION_FILE = "tm_champion"
_CHAMPION_CACHE = {"mtime": None, "data": None}
_CHAMPION_TTL_S = 60.0
SAVE_OFF_CHAMPION = 0x104           #: = SAVE_OFF_DECK_NAMES[0]; stamp OFF by default
SAVE_CHAMPION_WIDTH = 17            #: 16 chars + NUL, the shape of every name


def _champion():
    """{"member_id", "name", "week"} for the current champion, or {}."""
    forced = os.environ.get("POL_TM_CHAMPION_NAME")
    if forced is not None:
        return {"name": forced} if forced.strip() else {}
    now = time.monotonic()
    if _CHAMPION_CACHE["mtime"] is None or now - _CHAMPION_CACHE["mtime"] >= _CHAMPION_TTL_S:
        try:
            tmstore.ensure_schema()
            row = tmstore.db.query_one(
                "SELECT member_id, name, week FROM tm_champion"
                " ORDER BY named_at DESC LIMIT 1")
            _CHAMPION_CACHE["data"] = dict(row) if row else {}
        except tmstore.errors() as exc:
            common._say("tm: champion not read (%r) -- the rank_1 Pack keeps its "
                        "plain label" % (exc,))
            _CHAMPION_CACHE["data"] = {}
        _CHAMPION_CACHE["mtime"] = now
    return _CHAMPION_CACHE["data"] or {}


def _champion_name():
    """The champion's name as the client's own 16-byte cp932 field, or b""."""
    name = str((_champion() or {}).get("name") or "").strip()
    if not name:
        return b""
    raw = name.encode("cp932", "replace")[:SAVE_CHAMPION_WIDTH - 1]
    # never split a double-byte character at the cut
    return raw.decode("cp932", "ignore").encode("cp932", "ignore")


#: *** THE CHECK OUT DOOR WAS BEING SET UP AS A CARD SHOP, AND THE CLIENT SAID
#: SO IN ITS OWN LOG. *** Read 2026-08-19; the offsets are TM.dll RVAs and every
#: one of them lands in the table in `_shopinit_body` above:
#:
#:     [tm] +10520B  ---->Recv=SHOPINIT       0x105201 = the `@Init` arm entry
#:     [tm] +105390  Now1stPackName='s Pack   INSIDE `@Init`: the CARD SHOP's
#:                                            pack-name setup, printing a
#:                                            possessive with nothing in front
#:                                            of it -- there are no packs here
#:     [tm] +10610E  ---->Recv=AUCMONEY       ...and then `@Pong=` for ever
#:
#: So AUCMONEY is delivered AND read -- it just lands in a scene that has been
#: initialised as the card shop. That is why no value of `/M=` ever helped (the
#: banner above `SHREQ_MSGID_CHECKOUT` chased the value for a whole evening) and
#: why withholding the message hung it EARLIER rather than fixing it: the amount
#: was never the problem, the ARM was. msgid 0x13 dispatches on the BODY COMMAND,
#: and we sent the card shop's to both doors.
#:
#: WHICH arm Check Out actually wants is NOT measured. The field sets are the
#: ones RE'd in `_shopinit_body`, and the client's log is the oracle -- it names
#: the arm it ran, so one launch per candidate settles it. That is what the knob
#: below is for; it is A/B apparatus, not a fallback.
#: *** `@ExcInit`'s `/PP=` AND `/LC=` ARE MEASURED NOW -- IT IS THE PRIZE
#: CENTER'S OPENER, AND IT CARRIES THE PRIZE-POINT AWARDS. *** Read 2026-08-20
#: at rva 0x105504..0x1055EF. `esi` is the shop object (0x52463E0),
#: and each result is read out of the buffer pushed LAST, one call behind:
#:
#:     /PP= idx 0  -> [esi+0xB4]  u32  the prize-point BALANCE
#:     /PP= idx 1  -> [esi+0xBA]  u16  Top 30 award, PENDING
#:     /PP= idx 2  -> [esi+0xBE]  u16  lucky-card award, PENDING
#:     /PP= idx 3  -> [esi+0xBC]  u16  Best Rookie award, PENDING
#:     /LC= idx 0..2 -> [esi+0xC0/C2/C4] u16  the three LUCKY CARD IDS
#:
#: WARNING: Those indices are PIPE indices inside ONE value, not repeated keys -- the
#: multi-value banner above `CARDPRM_RECORDS` is the law here too. The body is
#: `@ExcInit=/N=1/PP=<bal>|<t30>|<lucky>|<rookie>/LC=<a>|<b>|<c>`, and
#: `tmprize.excinit_body` is what builds it.
#:
#: WARNING: THE THREE AWARD SLOTS ARE PENDING PAYMENTS, NOT HISTORY. The notice screen
#: (0x178DD0) credits them only on OK -- `bal += BA+BC` on its ranking page,
#: `bal += BE` on its lucky-card page -- and zeroes them in RAM as it does. The
#: client never writes `U/g/TM0DataFile`, so the paid/unpaid state is OURS to
#: keep: send a pending award twice and it is paid twice. The balance saturates
#: FLAT at 99,999 (0x1869F), which is NOT money's 99,999,999.
#:
#: WARNING: And the ranking page has no zero case: with both slots 0 it still draws
#: "You've won the Top 30 prize … awarded 0 points!". Open it only with an award
#: to pay. The lucky card IDS are announcement only -- the client has exactly one
#: reference to each (the draw) and picks nothing itself.
#:
#: WARNING: `/C=` IS NOT OPTIONAL ON @EcmInit -- OMITTING IT FROZE A CLIENT ON
#: UNINITIALISED STACK. Measured live 2026-08-21T16:11Z (m9/SYSADMIN1): the
#: client accepted our @EcmInit (`find("@EcmInit") -> hit`) and then spun
#: through `find("@N0")..find("@N20731")..` -- a de facto freeze, no @Pong,
#: watchdog teardown. Read at rva 0x1058D5: AFTER /PM= the arm parses
#: **`/C=` as an inline CARD COUNT** and loops `sprintf("@N%d") / find()`
#: over it, storing rows at `[esi + (0x4B9+idx)*0x10]` (30 slots, zeroed at
#: 0x1058B2, ids gated by [0x2FF4FC]=250). `0xAB080` leaves the out-slot
#: UNTOUCHED when the key is absent, so a missing /C= reads STACK GARBAGE:
#: <= 0 skips the loop (why m3/m6 never froze -- luck), huge spins for
#: minutes. So @EcmInit can DELIVER cards inline -- and until that is used
#: deliberately, /C=0 must be on every body. A find() miss does NOT break
#: the loop (0x105A7D increments and continues).
_INIT_ARM_FIELDS = {
    #  arm          rva        fields, in the arm's OWN parse order
    "Init":     (0x105201, ("N", "RA", "M", "CP", "S", "SP", "SN", "CMP", "HD")),
    "ExcInit":  (0x1054EE, ("N", "PP", "LC")),   # /PP= x4, /LC= x3 -- see above
    "EInit":    (0x10563A, ("N", "RA", "M", "CP", "S", "RT", "Stat")),
    "EcmInit":  (0x105788, ("N", "RA", "M", "CP", "S", "RT", "PM", "C")),
}

#: Defaults for the fields `_SHOPINIT_FIELDS` does not already carry. All zero,
#: and zero is the only value that cannot later be mistaken for a measured one.
#: `PP` and `LC` now HAVE meanings (see the banner above `_INIT_ARM_FIELDS`), and
#: zero is the right default for both anyway: no award pending, no lucky card.
#: `C` is @EcmInit's inline-card count and MUST default 0 -- see the banner
#: above `_INIT_ARM_FIELDS`; absent, the client reads stack garbage and can
#: spin for minutes. Each is `POL_TM_SHOP_<FIELD>`, same as the rest.
_INIT_ARM_DEFAULTS = {"SN": 0, "CMP": 0, "HD": 0, "PP": 0, "LC": 0, "RT": 0,
                      "Stat": 0, "C": 0}


def _checkout_init_arm():
    """Which of the four bodies the auction Check Out door is sent.

    `POL_TM_CHECKOUT_INIT=Init|ExcInit|EInit|EcmInit`, read per request, so both
    candidates can be tried against the live client without a redeploy each --
    the point being that the client's own `---->Recv=` line arbitrates, not us.

    The default is `EcmInit` and it is a REASONED GUESS, not a measurement: of
    the three non-shop arms it is the only one that writes `[esi+0xcc]` (`/PM=`,
    zeroed at 0x10589A and then parsed), and `[esi+0xcc]` is the field AUCMONEY
    writes -- so it is the arm whose story matches a screen whose entire job is
    an amount awaiting collection. `Init` stays selectable ONLY so the broken
    behaviour this replaces can be reproduced on demand.
    """
    name = os.environ.get("POL_TM_CHECKOUT_INIT", "EcmInit")
    if name not in _INIT_ARM_FIELDS:
        common._say("tm: POL_TM_CHECKOUT_INIT=%r is not one of %s -- using EcmInit"
              % (name, "/".join(sorted(_INIT_ARM_FIELDS))))
        return "EcmInit"
    return name


#: *** THE ARM WAS NEVER THE GATE, AND THE CLIENT'S DRAIN LOOP SAYS SO. ***
#:
#: `@EcmInit` reached the Check Out scene and was ACCEPTED -- measured on CASPC,
#: 2026-08-20, the client's own trace, and it also shows the four-arm dispatch
#: working exactly as `_INIT_ARM_FIELDS` describes:
#:
#:     [tm] +10520B  ---->Recv=SHOPINIT
#:     [tm] find("@Init")    on obj 001AEE2C -> 0 (MISS -- caller bails)
#:     [tm] find("@EcmInit") on obj 001AEE2C -> 1 (hit)
#:     [tm] +10610E  ---->Recv=AUCMONEY
#:
#: `Now1stPackName=` is GONE, which is the card-shop pack-name setup no longer
#: running. So the arm change did what it was for -- and the screen still hangs,
#: because the scene then drops into a fixed poll and stays there for ever:
#:
#:     code=0x2C / 0x21 / 0x25 / 0x23 / 0x2A / 0x43 / 0xB2 / 0xA2  -> not found
#:
#: That list is the scene's ACCEPT SET (0xB2 is the entry that read FOUND when
#: SHOPINIT and AUCMONEY arrived), and 0xB2 is the only one of the eight we have
#: ever sent. No value of `/M=` and no choice of arm was ever going to satisfy
#: it: Check Out is waiting for a message we have never written.
#:
#: The command-table reading puts `@Tr=` at 0x21/0x25 and leaves 0x23/0x2A/0x2C unrecovered, with
#: `@TrAns=` `@TrCan=` `@TrCanAck=` still unaccounted for -- the trade family,
#: which is what a screen for collecting the results of trades should want.
#:
#: *** THE PROBE, AND WHY IT NEEDS NO GUESSWORK ABOUT BODIES. *** The store
#: lookup matches on the CODE ALONE, and when a message IS taken the client logs
#: every key it tries against it (`find("@Init") -> MISS`, `find("@EcmInit") ->
#: hit`, above). So one message per code with a deliberately unmatchable body
#: makes the client read out the vocabulary of each arm for us. That is the whole
#: trick: we do not have to know the body to learn what the body must be.
#:
#: WARNING: THIS SENDS MESSAGES WE DO NOT UNDERSTAND TO A LIVE CLIENT. The scene is
#: measured 100% stuck, so there is no working behaviour to lose, but a bad frame
#: could wedge or crash the title -- restart the game and it is gone. Set
#: `POL_TM_CHECKOUT_PROBE=` (empty) to switch it off; it is DELIBERATELY on by
#: default while Check Out is dead, because each run of it costs a deploy.
#: *** SOLVED, STATICALLY: `0x2C` TESTS THE MSGID, AND WE SENT ZERO. ***
#:
#: The store fills the message object's tail members with `0xA9780(off)`, which
#: reads TWO ASCII HEX DIGITS out of the raw text buffer at `this+0x25`:
#:
#:     mov edx, [esp+4]            ; the argument is a CHARACTER OFFSET
#:     mov al, [edx + ecx + 0x25]  ; nibble, 'A'-0x37 / '0'-0x30, shl 4
#:     mov al, [edx + ecx + 0x26]  ; ...or the second nibble
#:
#: and the pump stores offset 0 -> +0x3C8, offset 4 -> +0x3CC, offset 6 -> +0x3D0.
#: Those are the three fields of our own header, which decodes it beyond doubt:
#:
#:     B2001300@EcmInit=   off 0 "B2" code   off 4 "13" msgid   off 6 "00" shop
#:     B2002001@Data=/M=0  off 0 "B2"        off 4 "20"         off 6 "01"
#:
#: So `+0x3CC` IS THE MSGID, and the 0x2C arm at 0x08CC7F is:
#:
#:     cmp dword [esp+0x3e8], ebx   ; ebx = 0 -- the msgid, tested for zero
#:     je  0x8cca0
#:     mov byte [0x52136b0], cl     ; msgid != 0 -> flag := 1, return +1
#:     mov byte [0x52136b0], bl     ; msgid == 0 -> flag := 0, return -1
#:
#: Our first probe was `2C000000` -- msgid ZERO -- so it took the second branch
#: and told the scene NO. The message was never unheard; it was answered in the
#: negative. `0x2C:1` is the same message with the one field that matters set.
#: WARNING: AND IT CHANGES NOTHING, MEASURED -- so the default is OFF again. The flag
#: IS set (`out BODY@+0x25='2C000100@Probe=/P=0'` on every run) and the Check Out
#: drain list is byte-identical before and after it: 0x2C 0x21 0x25 0x23 0x2A
#: 0x43 0xB2 0xA2, 368 cycles of each in the last 4000 log lines, no new codes.
#:
#: An earlier reading of this file claimed the scene ADVANCED to wait on 0x41 and
#: 0x1A. That was taken from the TAIL of the shim log rather than from the lines
#: after that run's `Recv=AUCMONEY`, so it was a different screen entirely --
#: retracted. To attribute a
#: drain list to a scene, read the lookups that FOLLOW that scene's own `Recv=`.
#:
#: So the probe is off: it ships a frame that is measured to do nothing, on a
#: screen we do not understand. `POL_TM_CHECKOUT_PROBE=0x2C:1` restores it, and
#: the machinery is kept because the NEXT candidate wants exactly this shape.
_CHECKOUT_PROBE_DEFAULT = ""


def _checkout_probe_codes():
    """The codes to poke the stalled Check Out drain loop with, in order."""
    raw = os.environ.get("POL_TM_CHECKOUT_PROBE", _CHECKOUT_PROBE_DEFAULT)
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        # `code[:msgid]` -- the msgid is NOT decoration here, it is the field the
        # 0x2C arm branches on. Default 1, because 0 is the measured "no".
        code_s, _, msgid_s = tok.partition(":")
        try:
            code = int(code_s, 0)
            msgid = int(msgid_s, 0) if msgid_s else 1
        except ValueError:
            common._say("tm: POL_TM_CHECKOUT_PROBE: %r is not a number -- skipped" % tok)
            continue
        if 0 <= code <= 0xFF:
            out.append((code, msgid))
        else:
            common._say("tm: POL_TM_CHECKOUT_PROBE: 0x%X is not a message code -- "
                  "skipped" % code)
    return out


def _checkout_probe_lines():
    """One message per probe code, each with a body no arm can match.

    `@Probe=` is chosen to MISS on purpose: a hit would run an arm we know
    nothing about, and a miss is what makes the client enumerate the keys it
    tried -- which is the measurement. The code is the only part that has to be
    right, because that is all the store lookup compares.
    """
    lines = []
    for code, msgid in _checkout_probe_codes():
        lines.append(protocol.encode_code(code | (msgid << 16)) + b"@Probe=/P=0")
    return lines


def _init_body(arm, member_id=None, amount=None):
    """`@<arm>=` plus exactly the fields that arm parses, in its parse order.

    `amount` is the Check Out door's proceeds awaiting collection, the same
    number `_aucmoney_line` is handed. It is never served as `/M=`: on
    `@EcmInit` that field assigns the live wallet (see the `M` branch below).

    `@Init` DELEGATES to `_shopinit_body` unchanged. The card shop is the one
    door that is not in question here, and its body stays byte-for-byte what it
    has been -- including the `POL_TM_SHOP_M` / `POL_TM_SHOP_PM` knobs, which are
    deliberately NOT honoured on the other arms: `POL_TM_SHOP_PM` is a 0/1 toggle
    over there, and reading it as a VALUE here would quietly serve `/PM=1` to
    anyone who has it set. Use `POL_TM_CHECKOUT_M` for this door's amount.
    """
    if arm == "Init":
        return _shopinit_body(member_id)
    shop_defaults = dict(cardshop._SHOPINIT_FIELDS)
    out = bytearray(b"@" + arm.encode("ascii") + b"=")
    for name in _INIT_ARM_FIELDS[arm][1]:
        if name == "N":
            # MUST equal the AUCMONEY header's shop byte -- the arm at 0x1060F9
            # compares it against [esi+0x30], where this very field was stored.
            out += b"/N=%d" % _shop_n()
            continue
        if name == "M":
            # WARNING: `/M=` ON `@EcmInit` IS THE LIVE WALLET. "Parsed and
            # discarded" is true of `@Init` only. The EcmInit arm clamps `/M=`
            # to >= 0 and stores it at 0x105816 `mov [esi+0xc8], eax` - save
            # struct +0xC8, the same field `@ComGameInit=/M=` assigns. The
            # money sections then do wallet += [+0xCC] (0x134E4B / 0x135E4B),
            # so the proceeds add on top of THIS. Serving the proceeds here
            # assigned them AS the wallet: a buyer (proceeds 0) who won an
            # auction for 50 went from 450 gil to 0 at Check Out, while the
            # server-side balance stayed right. The same write explains the
            # old "screen read 200 instead of 9800+200" that was pinned on
            # /PM=. Always the real wallet, read before the collect credit;
            # `@EInit` parses /M= but does not store it.
            out += b"/M=%d" % purse.money_of(member_id)
            continue
        if name == "PM":
            # WARNING: `/PM=` IS THE WALLET BASE, NOT THE AMOUNT. The arm zeroes
            # [esi+0xcc] then sets it to /PM= (0x10589A/0x1058AC), and the
            # money A/B AUCMONEY messages ADD their /M= on top. So /PM= MUST be
            # the player's real wallet -- /PM=0 (the old value, = the proceeds
            # for a refund-only checkout) wiped the wallet, then money B added
            # the 200 refund -> the screen read 200 instead of 9800+200
            # (measured live 2026-08-22T00:05Z). money_of is read
            # BEFORE the collect credits the wallet, so it is the pre-collect
            # balance the AUCMONEY amounts correctly add to.
            out += b"/PM=%d" % purse.money_of(member_id)
            continue
        if name == "CP":
            # WARNING: RETRACTED 2026-09-08: "/CP= LANDS ON THE MONEY FIELD".
            #
            # The store is real -- `0x10528F push "/CP=" / 0x1052B1 mov word
            # [esi], cx` -- but struct +0x00 is NOT the wallet. The wallet is
            # struct +0xC8 (`/M=`, written at 0x102DA9 and 0x105816, and the
            # global the VS. COM pot clamps against at 0xEC82C). Struct +0x00
            # is the CARD LEVEL: the save parser derives it there from the
            # collection (0x10146E -> 0x10147F), SEVEN sites recompute it
            # during play (0xCB652, 0xDA648, 0xEAEFD, 0xEFFD3, 0xF0075,
            # 0x117ABC, 0x12D046), and every reader compares it against a
            # `PlPrm.BIN` record's +0x02 unlock threshold -- 0xEC52C, 0xEC763,
            # 0xECAE0, 0xEBC4F and 0x110AE4, the VS. COM opponent gate. No
            # reader anywhere displays it as money.
            #
            # So the 2026-08-20 "both players show 0 money" fix was aimed at
            # the wrong field. What actually zeroed the shop's money that day
            # is named in this same file: `/M=` was the flat `_SHOPINIT_FIELDS`
            # constant, and `money_of` is what repaired it. Sending the BALANCE
            # here has since been writing the player's gil into their card
            # level -- 10,805 gil reads as card level 10805, over every
            # threshold in the ladder (max 2300), so a shop visit unlocked
            # every VS. COM opponent and the next VS. COM open (which sent
            # /CP=0) locked them all again.
            #
            # WARNING: The selftest below has asserted the corrected reading since
            # 2026-08-20 and never fired, because its fixture is a member with
            # no money: `0 == 0` skips the compare. An assertion whose fixture
            # cannot violate it is not a test.
            #
            # POL_TM_SHOP_CP still forces a number for an A/B.
            forced = os.environ.get("POL_TM_SHOP_CP")
            out += b"/CP=%d" % (common._env_int("POL_TM_SHOP_CP", 0) if forced
                                else cardtables.card_level_of(member_id))
            continue
        default = shop_defaults.get(name, _INIT_ARM_DEFAULTS.get(name, 0))
        try:
            v = int(os.environ.get("POL_TM_SHOP_" + name, str(default)), 0)
        except ValueError:
            v = default
        out += b"/%s=%d" % (name.encode("ascii"), v)
    return bytes(out)
