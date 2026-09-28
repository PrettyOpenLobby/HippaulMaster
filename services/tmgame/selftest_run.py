"""The selftest entry point (selftest) and the checks against the live captured opener."""
import contextlib
import json
import os
import tm_cardprm
import tmblob
import tmbattle
import struct
from .deps import tmsave
from . import (
    cardshop, cardtables, careerstats, collection, common, dispatch, opener, protocol, purse,
    pushqueue, savefile, selftest_ingame, selftest_match, selftest_tables, shopdoors, trade,
)


@contextlib.contextmanager
def _blob_sandbox():
    """What a fresh POL_RESOURCE_DIR was for the self-tests: inside the block
    the blob table is EMPTY, and afterwards it holds exactly what it held
    before. Every record is read out, the table emptied, and on the way out
    emptied again and refilled.

    Only on the throwaway database tm_run_all.py makes for a suite
    (TM_TEST_DATABASE=1): anywhere else it refuses, because on a real server
    it would take every player's saves away for the length of the block.
    """
    if os.environ.get("TM_TEST_DATABASE") != "1":
        raise RuntimeError("the blob sandbox runs only on a database "
                           "tm_run_all.py made (TM_TEST_DATABASE=1)")
    from polcore import blobs
    tmblob.names()                       # the core's tables, before the read
    kept = [(i.scope, i.path, blobs.get(i.scope, i.path))
            for i in blobs.listing()]
    tmblob.tmstore.db.execute("DELETE FROM blob")
    try:
        yield
    finally:
        with tmblob.tmstore.db.transaction() as conn:
            tmblob.tmstore.db.execute("DELETE FROM blob", conn=conn)
            for scope, path, data in kept:
                blobs.put(scope, path, data, conn=conn)

def selftest():
    """Run the suite with the SHARED ROSTER SNAPSHOT out of reach.

    WARNING: A SELFTEST MUST NOT BE ABLE TO WRITE `tm-roster.json`. `tmroom.selftest`
    carries this guard already, and the reason is on the record: running it
    inside a live container overwrote prod's snapshot with fixture rows on
    2026-08-20 -- phantom tables, the real member records gone, the room
    sequence reset.

    This module had no such guard and did not need one, because nothing it
    called reached `_publish()`. `_table_rules_put` now does (a
    table's rules have to outlive a restart, so they live in the shared file),
    which puts this suite one `_note_table_settings` away from the same
    accident. So the file is redirected to a scratch path for the duration and
    the module state is put back on the way out.

    WARNING: AND THE BOARD OBJECTS ARE PINNED OFF FOR THE SUITE. Nine of the scripted
    match flows below name a HARDCODED tile (`@PutData=/P=0/H=0/F=12` and
    friends) and were written against an always-empty board; once the deal
    started rolling blocks, a placement onto one is correctly REFUSED and those
    tests failed about a third of the time. That flake is the feature working,
    not a bug in it -- so the legacy flows get the old empty board and the one
    test that is ABOUT the objects turns the knob back on for its own duration.
    Anything added here that plays a hardcoded tile inherits the same pin.
    """
    _saved_objects = os.environ.get("POL_TM_BOARD_OBJECTS")
    os.environ["POL_TM_BOARD_OBJECTS"] = "0"
    try:
        return selftest_match._selftest_locked()
    finally:
        if _saved_objects is None:
            os.environ.pop("POL_TM_BOARD_OBJECTS", None)
        else:
            os.environ["POL_TM_BOARD_OBJECTS"] = _saved_objects


def _selftest_all():
    """Assert against the LIVE captured opener, not just our own output -- the
    same discipline that would have caught the janwire endianness bug."""
    ok = True
    ok = selftest_match._selftest_watch_gap() and ok
    ok = selftest_match._selftest_watch_snapshot_purge() and ok
    ok = selftest_match._selftest_accept_persist() and ok
    ok = selftest_match._selftest_bot_seat() and ok
    ok = selftest_match._selftest_dataerror() and ok
    ok = selftest_match._selftest_champion() and ok
    # THE BLOCK WRITERS FIRST -- this is the assertion the 2026-08-20 crash did
    # not have, and it is placed where it cannot be discarded by a later `ok =`.
    ok = selftest_tables._selftest_blocks() and ok
    # THE BOARD AND THE BATTLE ARITHMETIC, before anything that uses them: a
    # wrong direction order or a wrong roll is invisible on the wire and shows
    # up only as a match that plays itself wrong.
    ok = tmbattle.selftest(say=common._say) and ok
    ok = selftest_tables._selftest_packs() and ok
    ok = selftest_tables._selftest_seating() and ok
    ok = selftest_tables._selftest_cancel() and ok
    ok = selftest_tables._selftest_heartbeat() and ok
    ok = selftest_tables._selftest_seat_move() and ok
    ok = selftest_tables._selftest_audit() and ok
    ok = selftest_tables._selftest_reconcile() and ok
    ok = selftest_tables._selftest_gameml_format() and ok
    ok = selftest_tables._selftest_peer_index() and ok
    ok = selftest_tables._selftest_peer_class() and ok
    ok = selftest_tables._selftest_value0() and ok
    ok = selftest_ingame._selftest_ingame() and ok
    ok = selftest_match._selftest_loss_combo() and ok
    ok = selftest_match._selftest_replay_3p() and ok
    ok = selftest_match._selftest_rotating_turns() and ok
    ok = selftest_match._selftest_rotating_wraps() and ok
    ok = selftest_match._selftest_colorshift_ends() and ok
    ok = selftest_match._selftest_block_no_verdict() and ok
    ok = selftest_match._selftest_card_level() and ok
    ok = selftest_match._selftest_cardselect_leave() and ok
    ok = selftest_match._selftest_owner_name() and ok
    ok = selftest_match._selftest_abandon_cleanup() and ok
    ok = selftest_match._selftest_deck_names() and ok
    ok = selftest_match._selftest_battleselect() and ok
    ok = selftest_match._selftest_continue() and ok
    ok = selftest_tables._selftest_chat() and ok
    # logs/authserv.log 2026-08-13: b'GE1000000@TeachDV=' is payload (class 'G'
    # + E-body). The E-body handed to handle_line is payload[1:].
    LIVE_EBODY = b"E1000000@TeachDV="

    code, cmd = protocol.decode_ebody(LIVE_EBODY)
    common._say("decode:", code, cmd)
    if code != protocol.MSG_TEACHDV:
        common._say("FAIL: opener code %r, expected %d" % (code, protocol.MSG_TEACHDV)); ok = False
    if cmd != b"@TeachDV=":
        common._say("FAIL: command %r" % cmd); ok = False

    # The header must round-trip the client's own bytes exactly.
    if protocol.encode_code(protocol.MSG_TEACHDV) != b"E1000000":
        common._say("FAIL: encode_code(225) = %r" % protocol.encode_code(protocol.MSG_TEACHDV)); ok = False
    if protocol.encode_code(protocol.MSG_TEACHDVANS) != b"E2000000":
        common._say("FAIL: encode_code(226) = %r" % protocol.encode_code(protocol.MSG_TEACHDVANS)); ok = False

    reply = dispatch.handle_line(LIVE_EBODY[1:] if LIVE_EBODY[:1] == b"G" else LIVE_EBODY)
    common._say("reply:", reply)
    if not reply or not reply.startswith(b"E2000000"):
        common._say("FAIL: reply must carry code 226"); ok = False
    if b"@TeachDVAns" not in reply or b"/D=" not in reply or b"/V=" not in reply:
        common._say("FAIL: reply must contain @TeachDVAns/D=/V="); ok = False
    # The `=` straight after the name is the PC container-parser's key delimiter
    # (banner above `_teachdvans_body`). Without it the key becomes
    # `@TeachDVAns/D`, every lookup misses, and NOTHING is parsed -- invisible on
    # the wire, and it cost this investigation a fortnight. Assert the byte.
    if b"@TeachDVAns=" not in reply:
        common._say("FAIL: the PC key delimiter `=` is missing after @TeachDVAns"); ok = False

    # THE PC FIELD SET. The PC build's parser (unpacked TM.dll 0x053c134d) reads
    # `/AC=` and `/RK=` TWICE each and `/Shm=` THREE times, by occurrence index --
    # so these counts, not the field order, are what make the reply valid. Getting
    # them wrong is invisible on the wire and shows up only as TRM-0-37088
    # "Timed out while reading default data" many seconds later, so assert them.
    for field, want in ((b"/AC=", 2), (b"/RK=", 2), (b"/Shm=", 3)):
        if reply.count(field) != want:
            common._say("FAIL: %r appears %d time(s), the PC parser reads %d"
                  % (field, reply.count(field), want)); ok = False

    # `ext=0` must restore the PS2-only reply exactly.
    import tempfile as _tf
    _keep = opener._TEACH_FILE
    try:
        opener._TEACH_FILE = os.path.join(_tf.gettempdir(), "tm_teach_selftest.txt")
        with open(opener._TEACH_FILE, "w") as f:
            f.write("0 0\next=0\n")
        plain = dispatch.handle_line(LIVE_EBODY[1:])
        if plain != b"E2000000@TeachDVAns=/D=0/V=0":
            common._say("FAIL: ext=0 must give the old reply, got %r" % plain); ok = False
    finally:
        opener._TEACH_FILE = _keep

    # Unknown commands stay silent (capture, don't invent). `@Init=` is NOT the
    # example to use here -- it grew a real handler on 2026-08-15 and this
    # assertion kept failing against it, which is why the selftest was red.
    if dispatch.handle_line(b"E3000000@ThereIsNoSuchCommand=") is not None:
        common._say("FAIL: undecoded command must stay silent"); ok = False
    if dispatch.handle_line(b"E3000000@Init=") is None:
        common._say("FAIL: @Init= is handled now and must answer"); ok = False

    # THE TRADE BOARD ENTRY (live-corrected 2026-08-22 by a tester's dump: the
    # members-list trade uses the (0xA2,1) board, not the @TrID/@Init handshake).
    # PARTIAL: LENGTH-A/B DEFAULTS (2026-08-23 late): a paired accept queues EXACTLY
    # two pushes, in order: (0xA2,1) @EventGameInit then ONE SHORT (0xA2,26)
    # @Card list (cap 10 cards -- the crash tracks logged-line length, see
    # note_trade_accept). Never a second 26, never a terminator/partner list.
    # Numeric ids: the @Pong path below runs the seat check, which int()s the
    # member id -- a string id would throw where a real member never can.
    # WARNING: THE AUTHORED SERVE IS NO LONGER THE DEFAULT (2026-08-24 -- see
    # note_trade_accept), so this block has to ARM IT EXPLICITLY. The machinery
    # is still correct and still reachable with POL_TM_TRADE_SERVE=1 for an A/B,
    # and a test that silently passed because the feature was off would be worse
    # than no test: it is exactly how a disabled path rots.
    _serve_keep = os.environ.get("POL_TM_TRADE_SERVE")
    os.environ["POL_TM_TRADE_SERVE"] = "1"
    _ta, _tb = 987001, 987002
    trade.note_trade_accept((_ta, _tb, b"UPARTNERB", "acc"),
                      (_tb, _ta, b"UPARTNERA", "ini"))
    _due = pushqueue._pending_pushes(_ta)
    _a2_1, _26 = protocol.encode_code(0xA2 | (1 << 16)), protocol.encode_code(0xA2 | (26 << 16))
    if len(_due) != 3:
        common._say("FAIL: the ACCEPTER's serve must queue (0xA2,1) + own /P=0 + "
             "partner /P=1 lists (got %d push(es))" % len(_due)); ok = False
    else:
        if not _due[0].startswith(_a2_1) \
                or b"@EventGameInit=/N=2" not in _due[0]:
            common._say("FAIL: first push must be (0xA2,1) @EventGameInit=/N=2, "
                 "got %r" % (_due[0][:40],)); ok = False
        if not _due[1].startswith(_26) or b"/P=0" not in _due[1]:
            common._say("FAIL: second push must be the own /P=0 list, got %r"
                 % (_due[1][:40],)); ok = False
        if not _due[2].startswith(_26) or b"/P=1" not in _due[2]:
            common._say("FAIL: third push must be the partner /P=1 list (the scene "
                 "waits on it after consuming /P=0 -- measured 01:11Z), "
                 "got %r" % (_due[2][:40],)); ok = False
    _dueb = pushqueue._pending_pushes(_tb)
    if len(_dueb) != 2 or not all(d.startswith(_26) for d in _dueb):
        common._say("FAIL: the INITIATOR's serve is the two (0xA2,26) lists ONLY "
             "(its scene never polls (0xA2,1)); got %r"
             % ([d[:20] for d in _dueb],)); ok = False
    # THE RE-SERVE: a @Pong while armed re-queues the entry (racing the
    # client's one-shot @TrAns RecvCLEAR purge); it is gap-limited, so wind
    # the clock back to simulate the next heartbeat.
    if pushqueue._pending_pushes(_ta):
        common._say("FAIL: nothing may be due before the re-serve gap"); ok = False
    _st = trade._TRADE_PENDING.get(pushqueue._push_key(_ta))
    if not _st or _st.get("left") != common._env_int("POL_TM_TRADE_RESERVE_N", 4):
        common._say("FAIL: re-serves must be armed after the accept"); ok = False
    else:
        _st["last"] -= 10
        _pong = dispatch.handle_line(b"1E000000@Pong=/Cnt=0/RM=0", member_id=_ta)
        _pp = [p for p in (_pong or []) if p.startswith(_a2_1)
               or p.startswith(_26)]
        if len(_pp) != 2 or not all(p.startswith(_26) for p in _pp):
            common._say("FAIL: a heartbeat while armed must re-serve the two CARD "
                 "LISTS only (neither role polls (0xA2,1) -- measured); "
                 "got %r" % ([p[:20] for p in _pp],)); ok = False
        if _st.get("left") != common._env_int("POL_TM_TRADE_RESERVE_N", 4) - 1:
            common._say("FAIL: a re-serve must decrement the budget"); ok = False
        _pong2 = dispatch.handle_line(b"1E000000@Pong=/Cnt=0/RM=0", member_id=_ta)
        if [p for p in (_pong2 or []) if p.startswith(_26)]:
            common._say("FAIL: re-serves must respect the 3s gap"); ok = False
    trade.note_trade_cancel(_ta, _tb)
    if _serve_keep is None:
        os.environ.pop("POL_TM_TRADE_SERVE", None)
    else:
        os.environ["POL_TM_TRADE_SERVE"] = _serve_keep
    # ...and prove the DEFAULT is off, which is what production now runs.
    trade.note_trade_accept((_ta, _tb, b"UPARTNERB", "acc"))
    if pushqueue._pending_pushes(_ta):
        common._say("FAIL: with POL_TM_TRADE_SERVE unset the authored board serve "
             "must author NOTHING -- the go flag is the accept path now")
        ok = False
    trade.note_trade_cancel(_ta, _tb)
    if trade._TRADE_PENDING.get(pushqueue._push_key(_ta)) is not None:
        common._say("FAIL: a cancel must disarm the re-serve"); ok = False

    # THE LINE-LENGTH INVARIANT (the A/B's whole point): even against an
    # oversized collection, the default cap must keep the (0xA2,26) body short
    # enough that the client's sqMg recv-log line (~50-byte prefix + body)
    # stays under the suspected ~630-byte second-stage buffer. Every crash
    # dump's line was 637 bytes (20 cards); 550 leaves margin for the prefix.
    _fake = [[166, 20, 1, 40, 45, 10, 13, 2]] * 40   # realistic digit widths
    _saved_cc = getattr(collection, '_collection_cards', None)
    try:
        collection._collection_cards = lambda mid: _fake
        _line = trade._trade_card_line("slottest")
        import re as _re
        _hi = max((int(x) for x in _re.findall(rb"@N(\d+)=", _line)), default=-1)
        if _hi >= trade.TRADE_BOARD_SLOTS:
            common._say("FAIL: (0xA2,26) must be capped at %d cards, got @N up to %d"
                 % (trade.TRADE_BOARD_SLOTS, _hi)); ok = False
        if len(_line) > 550:
            common._say("FAIL: (0xA2,26) body must stay under 550 bytes at the "
                 "default cap (sqMg logger overflow), got %d" % len(_line))
            ok = False
        if b"/E=%d/S=0/C=%d" % (_hi + 1, _hi + 1) not in _line:
            common._say("FAIL: the list must be the COMPLETE form /E=n/S=0/C=n "
                 "(S+C==E) -- the incomplete form is measured to park the "
                 "26-waiter; got %r" % (_line[:40],)); ok = False
    finally:
        if _saved_cc is not None:
            collection._collection_cards = _saved_cc

    # A bare member_id records the pair but authors nothing (no partner).
    _tmid = "selftest-trade"
    trade.note_trade_accept(_tmid)
    if pushqueue._pending_pushes(_tmid):
        common._say("FAIL: a bare-member trade arm must not author any push"); ok = False
    trade.note_trade_cancel(_tmid)
    # The legacy game-band card serve is default OFF now (a trade never opens
    # the game band).
    trade.note_trade_accept(_tmid)
    if isinstance(dispatch.handle_line(b"E3000000@Init=/NN=0", member_id=_tmid), list):
        common._say("FAIL: the game-band @Init= card serve must be off by default "
             "(POL_TM_TRADE_INIT_CARDSERVE)"); ok = False
    trade.note_trade_cancel(_tmid)

    # `@Opt=` -- the option write-back. Same delimiter rule as every other Ans,
    # and `/Ans=` must be > 0 or the sending scene reads it as "still pending"
    # and polls forever (caller 0x13F83A, `cmp edi,0 ; jg <success>`).
    opt = dispatch.handle_line(b"84000000@Opt=/NN=0/HID=0/Dm=0/Vol=0/CN=1000000002"
                      b"/Ar=0/Cu=0/Vi=0/Se=0/Bgm=0/Per=0/Ran=0"
                      b"/CMD=0/CAD=0/CL=8/CT=60/HNSS=1")
    if not opt or not opt.startswith(protocol.encode_code(protocol.MSG_OPTANS)):
        common._say("FAIL: @Opt= must answer with code %d, got %r"
              % (protocol.MSG_OPTANS, opt)); ok = False
    if not opt or b"@OptAns=" not in opt:
        common._say("FAIL: @OptAns needs the `=` key delimiter"); ok = False
    if not opt or b"/Ans=0" in opt:
        common._say("FAIL: @OptAns /Ans= must be > 0"); ok = False

    # The field map is what actually persists the two screens' settings, so
    # assert it rather than the message. `/HNSS=` is the one field with a
    # confirmed meaning; a silently-renamed key here would look like a working
    # reply and a prompt that never stops coming back.
    fields = savefile._opt_fields(b"@Opt=/Ar=1/Cu=2/Vi=3/Se=4/Bgm=5/Per=6/Ran=7"
                         b"/CMD=1/CAD=0/CL=8/CT=60/HNSS=1")
    want = {0x0B0: 1, 0x0B1: 2, 0x0B2: 3, 0x0B3: 4, 0x0B4: 5, 0x0B5: 6,
            0x0B6: 7, 0x100: 1, 0x101: 0, 0x102: 8, 0x103: 60,
            savefile.SAVE_OFF_HNSS: 1}
    if fields != want:
        common._say("FAIL: @Opt field map %r, expected %r" % (fields, want)); ok = False
    # A negative value is legal on the wire (the builder prints `-` and negates,
    # 0x18933) and must survive as the byte the client will read back.
    if savefile._opt_fields(b"@Opt=/HNSS=-1") != {savefile.SAVE_OFF_HNSS: 0xFF}:
        common._say("FAIL: a negative @Opt value must truncate to its byte"); ok = False
    # THE RESERVATION, asserted against the LIVE line off a second machine
    # (`logs/uploads/FRAMEWORK-12960-20260818T033537Z.log` line 15598, and
    # authserv.log 03:30:47). Three things have to hold at once or the client
    # goes on polling with "Retrieving table info..." on screen, which is exactly
    # what the unanswered version looked like:
    #   the code+msgid must be 0x41/2 -- the waiter is 0x827F0(buf, 0x41, 2) and
    #     nothing else in the store will be looked at;
    #   the `=` delimiter, the same rule as every other Ans on this channel;
    #   `/EN=` must be > 0 -- 0 becomes the client's own -99, "Timed out while
    #     updating table data" (tm-errors 37399).
    LIVE_GAMEEN = (b"41000100@GameEN=/NN=AB12CD56EB0F5932/HID=0/Dm=0/Vol=0"
                   b"/CN=/HN=4C6170746F705465737432/L=1")
    en = dispatch.handle_line(LIVE_GAMEEN, member_id="selftest")
    if not en or not en.startswith(b"41000200"):
        common._say("FAIL: @GameEN= must answer with code 0x41 msgid 2, got %r"
              % (en,)); ok = False
    if not en or b"@GameEA=" not in en:
        common._say("FAIL: @GameEA needs the `=` key delimiter"); ok = False
    if not en or b"/EN=0" in en or b"/EN=" not in en:
        common._say("FAIL: @GameEA /EN= must be present and > 0, got %r" % (en,)); ok = False
    # CONFIRM. Measured 04:25:07Z: `41000500@GameQT=`, no fields, unanswered ->
    # Lobby.BIN 337 on screen. The reply is code 0x41 msgid 6 and `/QT=` > 0.
    qt = dispatch.handle_line(b"41000500@GameQT=", member_id="selftest")
    if not qt or not qt.startswith(b"41000600"):
        common._say("FAIL: @GameQT= must answer with code 0x41 msgid 6, got %r"
              % (qt,)); ok = False
    if not qt or b"@GameQA=" not in qt or b"/QT=0" in qt:
        common._say("FAIL: @GameQA needs the `=` delimiter and /QT= > 0, got %r"
              % (qt,)); ok = False

    # `@GameENC=` is a DIFFERENT command and must not be swallowed by the
    # `@GameEN=` branch -- the two literals sit 0x104 bytes apart in TM.dll and
    # only the substring test keeps them apart here. It is now ANSWERED, with
    # `@GameECA` (the VS. COM session's first reply), so the assertion is no
    # longer "stays silent" but "answers as ITSELF": right command, and NOT
    # `@GameEA`, which is what routing it into the reservation branch would
    # produce. That is the regression this test has always really been about.
    enc = dispatch.handle_line(b"41001200@GameENC=/NN=0", member_id="selftest")
    if enc is None:
        common._say("FAIL: @GameENC= must be answered (@GameECA) -- got silence"); ok = False
    elif b"@GameECA=" not in enc or b"@GameEA=" in enc:
        common._say("FAIL: @GameENC= must answer @GameECA, never @GameEA, got %r"
              % (enc,)); ok = False
    elif b"/EN=" not in enc:
        # /EN= AND NOT /TblId=: the parser at TM.dll 0x8538D reads `/EN=` and
        # takes the -99 failure arm when it is 0 or absent. /TblId= and /TblNo=
        # belong to @MuchMake, a later message -- serving them here is what
        # produced Lobby.BIN 448 twice live.
        common._say("FAIL: @GameECA needs /EN= (TM.dll 0x853AF), got %r" % (enc,))
        ok = False
    elif b"/EN=0" in enc:
        common._say("FAIL: @GameECA /EN= must be > 0 -- 0 is the -99 arm = 448, got %r"
             % (enc,)); ok = False
    # AND THE SILENT CONTROL STILL WORKS -- this reply is built from an inferred
    # msgid, so the A/B that puts the old behaviour back must stay live.
    _saved_eca = os.environ.get("POL_TM_GAMEECA")
    try:
        os.environ["POL_TM_GAMEECA"] = "0"
        # WARNING: A DIFFERENT MEMBER, and that is the point rather than a dodge. The
        # assertion above already ran `@GameENC=` for "selftest", which QUEUED
        # the `@MuchMake` pair -- and a queued push drains onto whatever reply
        # comes next, so re-asking on the same member returns those pushes and
        # the control reads as "not silent" no matter what the branch does. The
        # control is about the `@GameENC=` BRANCH, so it needs a member with no
        # queue behind it. (The real-world edge is the same and is acceptable:
        # flipping the knob mid-session does not un-queue what was already
        # handed out; it is an A/B switch to set before a session, not a kill
        # switch for one in flight.)
        if dispatch.handle_line(b"41001200@GameENC=/NN=0",
                       member_id="selftest-eca-off") is not None:
            common._say("FAIL: POL_TM_GAMEECA=0 must restore silence"); ok = False
    finally:
        if _saved_eca is None:
            os.environ.pop("POL_TM_GAMEECA", None)
        else:
            os.environ["POL_TM_GAMEECA"] = _saved_eca

    # Persistence must be a no-op without a member -- a stateless run must never
    # write somebody's save, and `member_id` is None on any unbound session.
    if savefile._save_patch_many(None, {savefile.SAVE_OFF_HNSS: 1}) != {}:
        common._say("FAIL: _save_patch_many must do nothing without a member"); ok = False

    # --- MONEY AND THE SALE ---------------------------------------------------
    # Against the REAL bytes: member 1's stored collection and the `@Sell=` the
    # client actually sent on 2026-08-18T18:52:06Z.
    _old_wr = os.environ.get("POL_TM_SAVE_WRITE")
    _sandbox = _blob_sandbox()
    _sandbox.__enter__()                         # an empty store, as a fresh dir was
    os.environ["POL_TM_SAVE_WRITE"] = "0"       # the binary save has its own test
    try:
        _cards = [[160, 84, 0, 99, 75, 23, 20, 0], [42, 43, 1, 20, 18, 7, 4, 2],
                  [220, 55, 0, 78, 80, 19, 18, 2], [20, 18, 1, 11, 10, 4, 2, 2]]
        tmblob.write_json("1.tm_collection.json", {"cards": _cards})

        # The wire entry and the stored row DIVERGE past field 5, which is why
        # the key is five fields and not the whole row.
        parsed = cardshop._parse_card_list(b"@Sell=/C=1@0=/D=220|55|0|78|80|18")
        if parsed != [[220, 55, 0, 78, 80, 18]]:
            common._say("FAIL: @Sell= did not parse, got %r" % (parsed,)); ok = False
        if parsed and parsed[0][:cardshop._CARD_KEY_FIELDS] != _cards[2][:cardshop._CARD_KEY_FIELDS]:
            common._say("FAIL: the five-field key must match the stored row"); ok = False
        if parsed and parsed[0][:6] == _cards[2][:6]:
            common._say("FAIL: fields 0..5 must NOT match -- if they do, the wire and "
                  "the row agree further than measured and the key can grow")
            ok = False

        # --- @Decks= STAT SYNC (A/B feature, DEFAULT OFF) --------------------
        # The merge remaps a @Decks= report's 7-field /D= (id|atk|type|pdef|mdef|
        # ARROWS|flag) onto the stored 8-field row (power at idx 5 left alone).
        # It is DEFAULT OFF (the SERVER owns card stats now -- growth would be
        # undone by taking client-reported stats), so opt in to test the mapping.
        # Own member id so it cannot perturb member 1's fixtures below.
        _saved_sync = os.environ.get("POL_TM_DECK_STAT_SYNC")
        os.environ["POL_TM_DECK_STAT_SYNC"] = "1"
        tmblob.write_json("dm.tm_collection.json",
                          {"cards": [[220, 55, 0, 78, 80, 19, 18, 2],
                                     [220, 55, 0, 78, 80, 19, 18, 2],   # a duplicate id
                                     [160, 84, 0, 99, 75, 23, 20, 0]]})
        collection._collection_set_deck("dm", b"@Decks=/C=1@0=/D=220|99|0|78|90|31|0")
        _dmc = collection._collection_load("dm").get("cards", [])
        _m220 = next((c for c in _dmc if c and c[0] == 220), None)
        if not _m220 or _m220[1:5] != [99, 0, 78, 90]:
            common._say("FAIL: @Decks= must merge the reported combat stats onto the "
                 "stored card by id -- got %r" % (_m220,)); ok = False
        if _m220 and _m220[6] != 31:
            common._say("FAIL: @Decks= arrows (wire idx 5) must land on stored idx 6, "
                 "not be copied positionally -- got %r" % (_m220,)); ok = False
        if _m220 and (_m220[5] != 19 or _m220[7] != 2):
            common._say("FAIL: @Decks= must leave the derived power (idx 5) and the flag "
                 "(idx 7) untouched -- got %r" % (_m220,)); ok = False
        if sum(1 for c in _dmc if c and c[0] == 220 and c[1] == 55) != 1:
            common._say("FAIL: only ONE duplicate of id 220 must upgrade -- the other "
                 "stays nominal (no per-instance id) -- got %r" % (_dmc,)); ok = False
        if next((c for c in _dmc if c and c[0] == 160), None) != \
                [160, 84, 0, 99, 75, 23, 20, 0]:
            common._say("FAIL: a card NOT in the @Decks= report must not change"); ok = False
        if collection._merge_deck_stats_into("dm", {"cards": list(_dmc)},
                                  b"@Decks=/C=1@0=/D=220|99|0|78|90|31|0"):
            common._say("FAIL: re-reporting the same @Decks= must be idempotent"); ok = False
        if _saved_sync is None:
            os.environ.pop("POL_TM_DECK_STAT_SYNC", None)
        else:
            os.environ["POL_TM_DECK_STAT_SYNC"] = _saved_sync

        # --- CARD GROWTH: a used card grows toward its ceiling, never past ----
        tmblob.write_json("gm.tm_collection.json", {
            "cards": [list(roll_card_row) for roll_card_row in
                      ([160, 40, 0, 50, 40, 10, 20, 0],   # Jecht, rolled low
                       [166, 10, 1, 20, 22, 5, 13, 0])]})  # Shelinda, rolled low
        _grew = careerstats._grow_used_cards("gm", [b"160|40|0|50|40|10|20|0",
                                        b"166|10|1|20|22|5|13|0"])
        _gmc = collection._collection_load("gm").get("cards", [])
        _g160 = next((c for c in _gmc if c and c[0] == 160), None)
        _b160 = tm_cardprm.row(160)
        if _grew < 1:
            common._say("FAIL: using a below-ceiling card must grow it"); ok = False
        if _g160 and (_g160[1] < 40 or _g160[3] < 50 or _g160[4] < 40):
            common._say("FAIL: growth must not LOWER a stat -- got %r" % (_g160,)); ok = False
        if _g160 and (_g160[1] > _b160[0] or _g160[3] > _b160[2]
                      or _g160[4] > _b160[3]):
            common._say("FAIL: growth must never exceed the CardPrm ceiling -- got %r "
                 "vs ceiling [%d,_,%d,%d]" % (_g160, _b160[0], _b160[2],
                                              _b160[3])); ok = False
        # a maxed card that is used must NOT grow (no room)
        tmblob.write_json("gx.tm_collection.json",
                          {"cards": [[160, _b160[0], 0, _b160[2], _b160[3], 23, 20, 0]]})
        if careerstats._grow_used_cards("gx", [b"160|%d|0|%d|%d|23|20|0"
                                   % (_b160[0], _b160[2], _b160[3])]) != 0:
            common._say("FAIL: a maxed card must not grow (no room)"); ok = False

        if purse.money_of(1) != purse._start_money():
            common._say("FAIL: an unstored balance must read as the start money"); ok = False
        if b"/M=%d" % purse._start_money() not in shopdoors._shopinit_body(1):
            common._say("FAIL: SHOPINIT must carry the member's money"); ok = False

        # WARNING: AND THE START MUST BE **ZERO** -- THE 2026-08-25 REGRESSION.
        # `_start_money()` defaulted to an unmeasured 10000, so every new member
        # was minted 10000 gold and then handed it, late, by whichever channel
        # they touched first: `@ComGameInit=/M=` writes straight into the live
        # wallet (0x102DA9, save struct +0xC8), and the authored save carries it
        # at `tmsave.MONEY_OFF` for the next launch. Two players reported it the
        # same minute -- "quit, lost a card, and was given 10000 gil" and "backed
        # out ... given 10000 money upon logging back in" -- and neither was a
        # payout: it was this constant arriving. SE's own ladder says 0 is right:
        # `PackPrm.BIN` opens with the FREE Pauper's Pack, which is the bootstrap
        # for a player who has nothing. See the banner above `_start_money`.
        if "POL_TM_START_MONEY" not in os.environ and purse._start_money() != 0:
            common._say("FAIL: a new member must start with NOTHING -- _start_money() "
                 "is %d, and every gold of it gets minted and pushed into the "
                 "client's wallet" % purse._start_money()); ok = False

        # ...AND READING IT MUST HAVE OPENED THE ACCOUNT. WARNING: This is the free-gold
        # bug: `_start_money()` used to be returned without ever being stored, so
        # every read answered 10000 again and nothing the client spent could
        # stick. Measured 2026-08-20: a member bid 50 on an auction, their client
        # went to 9950, and `money_of` still said 10000 because their collection
        # carried no `money` key. See `money_of`.
        _acct = collection._collection_load(1)
        if _acct.get("money") is None:
            common._say("FAIL: reading an unstored balance must OPEN THE ACCOUNT -- a "
                 "fallback that stores nothing refunds every purchase on the "
                 "next serve."); ok = False
        # ...and it must be an account, not a reset: a spend must now survive.
        # WARNING: RESTORED AFTERWARDS. Later assertions in this block are written
        # against `_start_money()`, so a probe that leaves the balance moved
        # fails THEM instead -- which is how this very check first "found" a
        # sale-pricing bug that did not exist.
        _was = purse.money_of(1)
        purse._set_money(1, 9950, "selftest: the client spent 50")
        if purse.money_of(1) != 9950:
            common._say("FAIL: a stored balance must survive a re-read -- got %d"
                 % (purse.money_of(1),)); ok = False
        purse._set_money(1, _was, "selftest: restoring")

        # WARNING: PRICED FROM THE CARD'S OWN LEVEL, not a flat rate. The WIRE row's
        # index 5 is the level (18 here -> CardPri[18] = 2000 -> *7/8 = 1750);
        # the STORED row's index 5 is something else, which is exactly why the
        # sell path must price from the wire row and not the identity key.
        _wire = [220, 55, 0, 78, 80, 18]
        _expect = cardshop._sell_price(_wire)
        if _expect != 1750:
            common._say("FAIL: level 18 must price at floor(CardPri[18]*7/8)=1750, got "
                 "%d" % _expect); ok = False
        cardshop._sell_cards(1, b"@Sell=/C=1@0=/D=220|55|0|78|80|18")
        if purse.money_of(1) != purse._start_money() + _expect:
            common._say("FAIL: the sale must credit the CARD'S price (%d), money is %d"
                 % (_expect, purse.money_of(1))); ok = False
        left = tmblob.read_json("1.tm_collection.json")["cards"]
        if len(left) != 3 or any(c[0] == 220 for c in left):
            common._say("FAIL: the sold card is still in the collection: %r" % (left,)); ok = False
        if b"/M=%d" % purse.money_of(1) not in shopdoors._shopinit_body(1):
            common._say("FAIL: SHOPINIT must serve the NEW balance -- this is the whole "
                  "point; a constant here is what reset money on every visit")
            ok = False

        # --- PACK PRICES: /No= indexes PackPrm.BIN --------------------------
        # Validated against a tester's own purchases: /No=3 cost 9000 and
        # /No=0 is the free Pauper's Pack.
        # /No=4 is 30000 in the PC build's table and 40000 in the PS2's: the
        # 2026-08-25 "39980 -> 0 over a 20 shortfall, and the client delivered
        # the pack anyway" incident was a PC client priced off the PS2 table.
        _ladder = [cardtables._pack_price(i) for i in range(6)]
        if _ladder[:4] != [0, 1000, 1500, 9000] or _ladder[4] not in (30000, 40000)                 or _ladder[5] != 100000:
            common._say("FAIL: PackPrm.BIN must decode (52-byte records from +16, price "
                 "at +4) to the shop's ladder -- got %r" % (_ladder,)); ok = False
        # A buy must DEBIT, and the free pack must not. WARNING: FUND THE MEMBER
        # FIRST: at the (correct) zero start, `max(0, bal - 9000)` is 0 whether
        # the debit ran or not, so the assertion would pass on a path that never
        # subtracts anything. The balance has to be able to fall.
        purse._set_money(1, 20000, "selftest: funding the pack-price assertions")
        _b4 = purse.money_of(1)
        dispatch.handle_line(b"B2001401@Buy=/No=3", member_id=1)
        if purse.money_of(1) != max(0, _b4 - 9000):
            common._say("FAIL: pack /No=3 must debit 9000 -- %d -> %d"
                 % (_b4, purse.money_of(1))); ok = False
        _b5 = purse.money_of(1)
        dispatch.handle_line(b"B2001401@Buy=/No=0", member_id=1)
        if purse.money_of(1) != _b5:
            common._say("FAIL: the Pauper's Pack is FREE and must not debit -- %d -> %d"
                 % (_b5, purse.money_of(1))); ok = False

        # WARNING: AND A PACK WE THINK THEY CANNOT AFFORD MUST NOT MOVE MONEY AT ALL.
        # The old arm was `max(0, bal - cost)` unconditionally, which on
        # 2026-08-25T04:25:54Z took member 3 from 39980 to **0** over a 20-gold
        # shortfall on a 40000 pack -- and the client then delivered that pack
        # anyway, proving OUR number was the wrong one. A clamp here is
        # unrecoverable and `@ComGameInit=/M=` assigns it into the live wallet.
        _short = cardtables._pack_price(4) - 20
        purse._set_money(1, _short, "selftest: the 20-gold-short case")
        dispatch.handle_line(b"B2001401@Buy=/No=4", member_id=1)   # 20 short of /No=4
        if purse.money_of(1) != _short:
            common._say("FAIL: an unaffordable pack must not debit -- %d -> %d "
                 "(a clamp to 0 destroys the balance to cover a 20 shortfall)"
                 % (_short, purse.money_of(1))); ok = False

        # --- THE DISCARD removes a card and moves NOTHING else ---------------
        # Captured live 2026-08-20T15:40:38Z on (0xB2, 0x10):
        #     @Erase=/Mode=1/C=1@0=/D=201|69|0|55|56|16
        # Fire-and-forget (0x1080E4 drains no slot), but the card must leave the
        # collection or the next save write puts it back and the discard undoes
        # itself. The pair is also a control: a discard that moves money means
        # the MONEY path is wrong, not the card path.
        _bal = purse.money_of(1)
        _n = len(tmblob.read_json("1.tm_collection.json")["cards"])
        cardshop._erase_cards(1, b"@Erase=/Mode=1/C=1@0=/D=99|1|0|1|1|1")
        _after = tmblob.read_json("1.tm_collection.json")
        if purse.money_of(1) != _bal:
            common._say("FAIL: a discard must not move money -- %d -> %d"
                 % (_bal, purse.money_of(1))); ok = False
        # (that card was not in the collection, so the count must be unchanged
        #  and the drift reported rather than silently dropping a real card)
        if len(_after["cards"]) != _n:
            common._say("FAIL: discarding a card we do not hold must not remove one we "
                 "do; %d -> %d" % (_n, len(_after["cards"]))); ok = False
        # ...and one we DO hold must actually go.
        _held = list(_after["cards"][0])
        cardshop._erase_cards(1, b"@Erase=/Mode=1/C=1@0=/D=%s"
                     % "|".join(str(v) for v in _held[:5]).encode("ascii"))
        _left = tmblob.read_json("1.tm_collection.json")["cards"]
        if len(_left) != _n - 1 or any(list(c)[:5] == _held[:5] for c in _left):
            common._say("FAIL: a discarded card must leave the collection -- %r still "
                 "holds %r" % (_left, _held[:5])); ok = False

        # A card we never recorded is still a real sale: credit it, do not drop it.
        _before = purse.money_of(1)
        # ...at ITS OWN price: level 1 -> CardPri[1] = 25 -> *7/8 = 21.
        _unrec = cardshop._sell_price([99, 1, 0, 1, 1, 1])
        cardshop._sell_cards(1, b"@Sell=/C=1@0=/D=99|1|0|1|1|1")
        if purse.money_of(1) != _before + _unrec:
            common._say("FAIL: an unrecorded card must still credit the player, at its "
                 "own price (%d); money %d -> %d" % (_unrec, _before, purse.money_of(1)))
            ok = False

        # A `POL_TM_SHOP_M` override still wins -- that knob is how the field's
        # meaning was A/B'd and taking it away would break the experiment.
        os.environ["POL_TM_SHOP_M"] = "777"
        if b"/M=777" not in shopdoors._shopinit_body(1):
            common._say("FAIL: POL_TM_SHOP_M must override the stored balance"); ok = False
        del os.environ["POL_TM_SHOP_M"]

        # THE BALANCE HAS TO REACH THE SAVE, because that is where the client
        # reads it -- SHOPINIT's `/M=` does not feed the Money display (measured:
        # /M=4321 served, screen stayed at 87 across a shop exit and re-entry).
        os.environ["POL_TM_SAVE_WRITE"] = "1"
        purse._set_money(1, 4321, "selftest")
        collection._collection_to_save(1, [[220, 55, 0, 78, 80, 19, 18, 2]])
        _blob = tmblob.read("1.U_g_TM0DataFile.bin")
        # WARNING: MONEY IS A DWORD AT +0x34, NOT A u16 AT +0x38. Corrected
        # 2026-08-20: +0x38 is Card Points (struct +0x00, where `/CP=` lands);
        # money is file +0x34 -> struct +0xC8, the field BOTH GameInits' `/M=`
        # writes with the same clamp-to-zero (0x1028CE, 0x102DA9). Every save on
        # the server read 0 at +0x34, which is why every player had 0 money.
        if struct.unpack_from("<I", _blob, tmsave.MONEY_OFF)[0] != 4321:
            common._say("FAIL: the save must carry the balance as a DWORD at +0x%02X, "
                  "got %d" % (tmsave.MONEY_OFF,
                              struct.unpack_from("<I", _blob, tmsave.MONEY_OFF)[0]))
            ok = False
        # ...and it must NOT have landed in Card Points, the old wrong offset.
        if struct.unpack_from("<H", _blob, tmsave.CARDPOINTS_OFF)[0] == 4321:
            common._say("FAIL: the balance was written into Card Points (+0x%02X) -- "
                  "that is the off-by-four this corrects"
                  % (tmsave.CARDPOINTS_OFF,)); ok = False
        if struct.unpack_from("<H", _blob, tmsave.COUNT_OFF)[0] != 1:
            common._say("FAIL: the save's card count was disturbed"); ok = False
        # THE THREE ARE CONSECUTIVE DWORD SLOTS in the parser's copy order:
        # +0x34 money (0x101234) -> +0x38 card points (0x10124D) -> +0x3C count.
        # A gap that is not 4 apiece means one of them moved.
        if tmsave.MONEY_OFF + 4 != tmsave.CARDPOINTS_OFF or                 tmsave.CARDPOINTS_OFF + 4 != tmsave.COUNT_OFF:
            common._say("FAIL: +0x38 and +0x3C are four bytes apart in the parser; a "
                  "MONEY_OFF that is not COUNT_OFF-4 means one of them moved")
            ok = False
        # A caller that does not track money must NOT zero somebody's balance.
        _keep = tmsave.build([[220, 55, 0, 78, 80, 19, 18, 2]], _blob)
        if struct.unpack_from("<H", _keep, tmsave.MONEY_OFF)[0] != 4321:
            common._say("FAIL: build() without `money` must leave the balance alone")
            ok = False
        os.environ["POL_TM_SAVE_WRITE"] = "0"

        # `/PM=` IS THE MONEY ON SHOPINIT, and an absent one is an active
        # ZEROING -- the arm writes 0 into [esi+0xcc] before it parses the key.
        purse._set_money(1, 4321, "selftest")
        body = shopdoors._shopinit_body(1)
        if b"/PM=4321" not in body:
            common._say("FAIL: SHOPINIT must carry /PM= with the balance, got %r"
                  % (body,)); ok = False
        # ...and it must be a SEPARATE key from /M=, which this arm discards.
        if b"/M=" not in body:
            common._say("FAIL: /M= should still be served alongside /PM="); ok = False
        if body.index(b"/PM=") <= body.index(b"/M="):
            common._say("FAIL: /PM= must not be mistaken for /M= -- a parser looking "
                  "for '/M=' would match inside '/PM='; keep /M= first so the "
                  "served order matches the client's own parse order")
            ok = False

        # THE WALLET MESSAGE. AUCMONEY is the only thing that carries money;
        # SHOPINIT parses the same `/M=` key and throws it away.
        _old_n = os.environ.get("POL_TM_SHOP_N")
        os.environ["POL_TM_SHOP_N"] = "1"
        purse._set_money(1, 4321, "selftest")
        line = shopdoors._aucmoney_line(1)
        if line != b"B2002001@Data=/M=4321":
            common._say("FAIL: AUCMONEY line is %r" % (line,)); ok = False
        # The header is code | msgid<<16 | shop<<24, and the SHOP BYTE MUST
        # TRACK `/N=` -- the arm at 0x1060F9 compares it against [esi+0x30],
        # which is where SHOPINIT stored /N=. A mismatch is silently dropped.
        os.environ["POL_TM_SHOP_N"] = "11"
        if shopdoors._aucmoney_line(1)[:8] != b"B200200B":
            common._say("FAIL: the shop byte must follow /N=, got %r"
                  % (shopdoors._aucmoney_line(1)[:8],)); ok = False
        if shopdoors._shopinit_body(1)[:9] != b"@Init=/N=":
            common._say("FAIL: SHOPINIT must still lead with /N="); ok = False
        if b"/N=11" not in shopdoors._shopinit_body(1):
            common._say("FAIL: /N= and the AUCMONEY shop byte must be the SAME value")
            ok = False
        if _old_n is None:
            os.environ.pop("POL_TM_SHOP_N", None)
        else:
            os.environ["POL_TM_SHOP_N"] = _old_n

        # BOTH DOORS GET AUCMONEY; THE AMOUNT IS WHAT DIFFERS. They send the same
        # `@ShReq=` body and are told apart ONLY by the msgid, so that is what
        # this asserts on -- see the banner above `SHREQ_MSGID_CHECKOUT`.
        # Withholding the message from Check Out is measured to hang the screen
        # before it renders, and sending the WALLET there announced a card sale
        # that never happened; the honest value is the proceeds awaiting
        # collection, which is 0 while no auction has settled.
        purse._set_money(1, 4321, "selftest")
        _req = b"@ShReq=/NN=0/HID=0/Dm=0/Vol=0/CN=0"
        _shop = dispatch.handle_line(protocol.encode_code(protocol.MSG_SHREQ) + _req, member_id=1)
        _chk = dispatch.handle_line(protocol.encode_code(protocol.MSG_SHREQ | (shopdoors.SHREQ_MSGID_CHECKOUT << 16))
                           + _req, member_id=1)
        if not any(b"@Data=/M=4321" in l for l in (_shop or [])):
            common._say("FAIL: the CARD SHOP door (msgid 0x00) must get AUCMONEY "
                  "carrying the WALLET -- it is the only message that does")
            ok = False
        if not any(b"@Data=/M=0" in l for l in (_chk or [])):
            common._say("FAIL: the Check Out door (msgid 0x%02X) must get AUCMONEY too "
                  "-- the scene blocks until it arrives -- but carrying the "
                  "proceeds awaiting collection (0), not the wallet"
                  % shopdoors.SHREQ_MSGID_CHECKOUT); ok = False
        if any(b"@Data=/M=4321" in l for l in (_chk or [])):
            common._say("FAIL: Check Out must NOT be told the wallet -- that is what "
                  "announced a sale of the player's own balance"); ok = False
        if not any(b"@ShEnter=" in l for l in (_chk or [])):
            common._say("FAIL: Check Out must still be handed its endpoint"); ok = False

        # THE DRAIN-PACED STREAM. door = money A + cards A; @Get #1 -> cards B
        # (empty, fresh 0x21 after cards A drained); @Get #2 -> money B + @EQuit
        # (fresh 0x20 after money A drained). NO @Dead (0x27 = the error). See
        # the _CHECKOUT_PULL banner.
        _msgids = [l[4:6] for l in (_chk or []) if l[:2] == b"B2"]
        if _msgids != [b"13", b"20", b"21"]:
            common._say("FAIL: the Check Out door batch is money A + cards A only "
                  "(same-type records drain, so cards B / money B pace on the "
                  "@Gets); it queued %r" % _msgids)
            ok = False
        if any(l[4:6] == b"27" for l in (_chk or []) if l[:2] == b"B2"):
            common._say("FAIL: @Dead (0x27) must NEVER ride the Check Out stream -- "
                  "it is the scene's error channel (dialog 221)"); ok = False
        _p1 = dispatch.handle_line(b"B2001501@Get=/No=1", member_id=1)
        _p2 = dispatch.handle_line(b"B2001501@Get=/No=1", member_id=1)
        _p3 = dispatch.handle_line(b"B2001501@Get=/No=1", member_id=1)
        _sched = [[l[4:6] for l in (p or []) if l[:2] == b"B2"]
                  for p in (_p1, _p2, _p3)]
        if _sched != [[b"21"], [b"20", b"17"], []]:
            common._say("FAIL: @Get #1 -> cards B (0x21), @Get #2 -> money B (0x20) + "
                  "@EQuit (0x17), then silence; got %r" % _sched); ok = False
        if not (_p2 and any(b"@EQuit=/D=0" in l for l in _p2)):
            common._say("FAIL: @Get #2 must carry @EQuit=/D=0 -- state 0x0C polls "
                  "msgid 23; got %r" % _p2); ok = False
        os.environ["POL_TM_SHOPQUIT_PS2"] = "0"
        _q = dispatch.handle_line(b"B2001701@Quit=/No=1", member_id=1)
        del os.environ["POL_TM_SHOPQUIT_PS2"]
        if isinstance(_q, list) or b"@EQuit=" not in _q:
            common._say("FAIL: a @Quit= after the schedule ran must be the plain "
                  "@EQuit; got %r" % _q); ok = False
        # The knob must select the legacy shape, or the bisect needs a deploy.
        os.environ["POL_TM_CHECKOUT_STREAM"] = "0"
        _leg = dispatch.handle_line(protocol.encode_code(protocol.MSG_SHREQ | (shopdoors.SHREQ_MSGID_CHECKOUT << 16))
                           + _req, member_id=1)
        del os.environ["POL_TM_CHECKOUT_STREAM"]
        _legids = [l[4:6] for l in (_leg or []) if l[:2] == b"B2"]
        if _legids != [b"13", b"20"]:
            common._say("FAIL: POL_TM_CHECKOUT_STREAM=0 must reproduce the legacy "
                  "SHOPINIT+AUCMONEY shape; it queued %r" % _legids)
            ok = False
        shopdoors._CHECKOUT_PULL.pop(1, None)

        # AND THE TWO DOORS MUST NOT BE SET UP THE SAME WAY. The client logged
        # `Now1stPackName=` at RVA 0x105390 on the Check Out screen -- inside the
        # CARD SHOP's `@Init` arm -- because that is the body we sent it. See the
        # banner above `_INIT_ARM_FIELDS`.
        if not any(b"@Init=" in l for l in (_shop or [])):
            common._say("FAIL: the card shop door must still get @Init= -- that half "
                  "is not in question and must not move"); ok = False
        if any(b"@Init=" in l for l in (_chk or [])):
            common._say("FAIL: Check Out must NOT be sent the card shop's @Init= -- "
                  "that is the arm that ran the pack-name setup on it"); ok = False
        if not any(b"@EcmInit=" in l for l in (_chk or [])):
            common._say("FAIL: Check Out must get the arm _checkout_init_arm() picked")
            ok = False
        # Every arm serves /N= FIRST and it must equal the AUCMONEY shop byte,
        # whichever body the knob selects -- the gate at 0x1060F9 compares them.
        for _arm in sorted(shopdoors._INIT_ARM_FIELDS):
            _b = shopdoors._init_body(_arm, 1, 0)
            if not _b.startswith(b"@" + _arm.encode() + b"=/N="):
                common._say("FAIL: @%s= must lead with /N=, got %r" % (_arm, _b))
                ok = False
            if _arm != "Init":
                # `@Init` is EXEMPT and stays so: `_shopinit_body` deliberately
                # serves six of its nine fields plus /PM=, and the card shop is
                # working to the extent it works with exactly those. The new arms
                # have no such history, so they serve their field set whole.
                for _f in shopdoors._INIT_ARM_FIELDS[_arm][1]:
                    if b"/" + _f.encode() + b"=" not in _b:
                        common._say("FAIL: @%s= is missing /%s=, which the arm parses"
                              % (_arm, _f)); ok = False
            # /PM= must never be mistaken for /M=, the same trap as on @Init.
            if b"/PM=" in _b and b"/M=" in _b and _b.index(b"/PM=") <= _b.index(b"/M="):
                common._say("FAIL: @%s= must serve /M= before /PM=" % _arm); ok = False
        # A bad knob value must fall back, not crash and not serve `@garbage=`.
        os.environ["POL_TM_CHECKOUT_INIT"] = "nonsense"
        if shopdoors._checkout_init_arm() != "EcmInit":
            common._say("FAIL: an unknown POL_TM_CHECKOUT_INIT must fall back"); ok = False
        os.environ["POL_TM_CHECKOUT_INIT"] = "ExcInit"
        if b"@ExcInit=" not in shopdoors._init_body(shopdoors._checkout_init_arm(), 1, 0):
            common._say("FAIL: POL_TM_CHECKOUT_INIT must select the arm -- if it does "
                  "not, the A/B needs a redeploy per guess and that is the whole "
                  "point of the knob"); ok = False
        del os.environ["POL_TM_CHECKOUT_INIT"]

        # Money must never go negative, whatever it is asked to store.
        # Assert on the STORED value, not on money_of() -- both clamp, so a
        # reader-side clamp would hide a writer that persisted -50.
        purse._set_money(1, -50)
        _stored = tmblob.read_json("1.tm_collection.json")
        if _stored.get("money") != 0:
            common._say("FAIL: a negative balance must be clamped BEFORE it is stored, "
                  "got %r" % (_stored.get("money"),)); ok = False
    finally:
        if _old_wr is None:
            os.environ.pop("POL_TM_SAVE_WRITE", None)
        else:
            os.environ["POL_TM_SAVE_WRITE"] = _old_wr
        _sandbox.__exit__(None, None, None)

    common._say("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1
