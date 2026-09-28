"""Selftest: the code-0x43 in-match family, end to end."""
import uuid
import inspect
import json
import os
import shutil
import tmbattle
import re
import struct
import time
from .deps import tmprize, tmsave
from . import (
    boardrules, cardshop, cardtables, careerstats, collection, common, dispatch, matchend,
    matchmaking, matchstart, peers, placement, pots, protocol, purse, pushqueue, rematch,
    reservation, ruleset, savefile, scoring, seating, selftest_tables, shopdoors, tablerow,
    tablesettings, turns, vscom, watchers, webwatch,
)


def _selftest_ingame():
    """The code-0x43 family: the slot, the seat search and the alone guard.

    Asserted against the PARSER, since nothing here has been on a wire yet --
    every check below names the instruction that would reject the message.

    WARNING: Sandboxed exactly like `_selftest_seating`: `_queue_vsgameinit` reads the
    live room registry and writes the push queue, and a test that can reach the
    snapshot two containers share has already cost this project one incident.
    """
    import os as _os, tempfile, tmroom
    ok = True
    saved = (tmroom._KEY, tmroom._OWNER[0],
             dict(tmroom._TABLES), dict(tmroom._PEERS),
             dict(tmroom._RECORDS), dict(tmroom._ROOMS_SEQ))
    saved_seated, saved_pushes = dict(seating._SEATED), dict(pushqueue._PUSHES)
    saved_roster, saved_accepts = dict(matchmaking._MATCH_ROSTER), dict(matchmaking._MATCH_ACCEPTS)
    saved_ready, saved_peer = dict(matchstart._CARD_READY), dict(matchmaking._MATCH_PEER)
    saved_conf = set(seating._TABLE_CONFIRMED)
    saved_hands, saved_turn = dict(boardrules._MATCH_HANDS), dict(boardrules._MATCH_TURN)
    saved_board, saved_rand = dict(boardrules._MATCH_BOARD), dict(matchstart._TURN_RAND)
    try:
        tmroom._KEY = "tm:selftest:tm-game:" + uuid.uuid4().hex
        tmroom._OWNER[0] = True
        for d in (tmroom._TABLES, tmroom._PEERS, tmroom._RECORDS,
                  tmroom._ROOMS_SEQ, seating._SEATED, pushqueue._PUSHES, matchmaking._MATCH_ROSTER,
                  matchmaking._MATCH_ACCEPTS, matchstart._CARD_READY, matchmaking._MATCH_PEER,
                  boardrules._MATCH_HANDS, boardrules._MATCH_TURN, boardrules._MATCH_BOARD, matchstart._TURN_RAND,
                  seating._TABLE_CONFIRMED):
            d.clear()

        # --- the body, built straight from a two-seat table ------------------
        seats = [(0x860FB3E2A2, 0xAB12CD56EB0F5932), (0x860FB3E2A3, 0xAB12CEB20D9067C4)]
        rules = ruleset._rule_seven({"bm": 1, "du": 2, "st": 3,
                             "cb": 4, "ca": 5, "gs": 6, "tl": 7})
        if rules != [1, 2, 3, 4, 5, 6, 7]:
            common._say("FAIL: the seven /R= values must come from @Tet= in "
                 "TABLE_INFO_TET order, got %r" % (rules,)); ok = False
        for me in (0, 1):
            body = matchstart._vsgame_body(seats, me, rules)
            # The `=` after the name. 0xAA920 is a lookup in a map keyed on
            # `name=`; this is the delimiter whose absence cost ~10 retracted
            # TRM-0-37088 conclusions (see the module header).
            if not body.startswith(b"@VsGameInit=/N=2"):
                common._say("FAIL: @VsGameInit needs the `=` delimiter and /N= first, "
                     "got %r" % (body[:40],)); ok = False
            # /N= must be > 1 or 0x1024D5 goes to StartAloneERROR.
            ids = selftest_tables._pipe_vals(body, b"/ID=")
            if len(ids) != 2:
                common._say("FAIL: one /ID= per seat, got %r" % (ids,)); ok = False
            # THE SEAT SEARCH. 0x1024F9 takes the FIRST /ID= equal to the
            # client's own GAME-ID; the recipient's must sit at their index and
            # no other occurrence may collide with it.
            # THE RECIPIENT IS ALWAYS OCCURRENCE 0 -- the client can only ever
            # match there (see `_vsgame_body`), so its own id leads the list
            # whatever seat it holds at the table.
            elif ids[0] != b"%d" % common._env_int("POL_TM_VSGAME_SELFID", 0):
                common._say("FAIL: the recipient's own /ID= must be FIRST, got %r"
                     % (ids,)); ok = False
            elif ids[1] == ids[0]:
                common._say("FAIL: a peer's /ID= must differ from the recipient's, or "
                     "0x1024F9 stops on the wrong player -- got %r" % (ids,))
                ok = False
            if len(selftest_tables._pipe_vals(body, b"/CN=")) != 2 or                     len(selftest_tables._pipe_vals(body, b"/R=")) != 7:
                common._say("FAIL: /CN= must carry one name per seat and /R= seven "
                     "values, PIPE-separated under one key -- got %r / %r"
                     % (selftest_tables._pipe_vals(body, b"/CN="), selftest_tables._pipe_vals(body, b"/R=")))
                ok = False
            # One `@No<i>=` per ABSOLUTE seat, named by sprintf("@No%d") at
            # 0x10282F -- and `=`-delimited for the same reason as the header.
            for i in range(2):
                if b"@No%d=" % i not in body:
                    common._say("FAIL: @VsGameInit needs an @No%d= section" % i)
                    ok = False
            # /M= is read only out of the recipient's own section (0x1028BE),
            # but every section carries it: the client picks, we do not.
            if body.count(b"/M=") != 2:
                common._say("FAIL: each @No section carries its own /M=, got %d"
                     % body.count(b"/M=")); ok = False

        # --- the slot ---------------------------------------------------------
        # code 0x43 in header byte 0, command 1 in byte 2. `0x825A0(buf, 0x43,
        # 1)` will not look at anything else, and `0xA9780(rec, 4)` is what reads
        # the msgid out of the 8-hex header.
        if protocol.encode_code(protocol.VSGAME_MSGID) != b"43000100":
            common._say("FAIL: @VsGameInit must be addressed (0x43, 1), got %r"
                 % (protocol.encode_code(protocol.VSGAME_MSGID),)); ok = False
        if protocol.encode_code(protocol.COMGAME_MSGID) != b"43000200":
            common._say("FAIL: @ComGameInit must be addressed (0x43, 2), got %r"
                 % (protocol.encode_code(protocol.COMGAME_MSGID),)); ok = False

        # --- vs-COM answers in the slot it was asked in -----------------------
        ans = dispatch.handle_line(b"43000200@ComGame=/No=0", member_id=None)
        if not ans or not ans.startswith(b"43000200"):
            common._say("FAIL: @ComGame= must be answered on its OWN (code, msgid) -- "
                 "0x101A10 looks the answer up by the id it was called with; "
                 "got %r" % (ans,)); ok = False
        if not ans or b"@ComGameInit=" not in ans:
            common._say("FAIL: the vs-COM answer is @ComGameInit= (arm 0x102D21), "
                 "got %r" % (ans,)); ok = False
        if ans and len(selftest_tables._pipe_vals(ans, b"/R=")) != 7:
            common._say("FAIL: @ComGameInit carries seven PIPE-separated /R= values, "
                 "got %r" % (selftest_tables._pipe_vals(ans, b"/R="),)); ok = False
        # A msgid we did not choose must be echoed, not normalised to 2.
        ans5 = dispatch.handle_line(b"43000500@ComGame=", member_id=None)
        if not ans5 or not ans5.startswith(b"43000500"):
            common._say("FAIL: @ComGame= must be answered in the msgid it arrived on, "
                 "got %r" % (ans5,)); ok = False

        # stale in-match pushes from an earlier game die at a new @GameENC=
        pushqueue._queue_push("staletest", b"43000906@TurnData=/A=1/S=1|0", "stale turn")
        pushqueue._queue_push("staletest", b"41000E00@GameEA=/Exit=1", "not in-match")
        if pushqueue._drop_stale_match_pushes("staletest", "selftest") != 1 or \
                len(pushqueue._PUSHES.get(pushqueue._push_key("staletest")) or []) != 1:
            common._say("FAIL: a stale @TurnData must be dropped and a non-match push kept")
            ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("staletest"), None)

        # --- the VS. COM roster: /Com= is the player count, and every -------
        # --- in-match push must be framed from the game peer ----------------
        # Live 2026-08-22T13:57Z: a 3-player COM game (@ComGame=/Com=1|2) was
        # answered `/Ans=1|1` and `/L=0|0` because N=2 was hardcoded with the
        # /Com= field discarded; the deal went out UNPINNED, framed from the
        # recipient's own nick, and the session-pair gate (0x1034FD compares
        # the delivering record's sender u64 against [obj+0x28]/[0x2C], which
        # the @ComGameInit arm 0x102D3C filled with the GAME PEER's guid) threw
        # it away without a log line anywhere; and turn 0 was never queued at
        # all (the fifth seat gate). This chain is the whole COM start path.
        pushqueue._PUSHES.pop(pushqueue._push_key("comtest"), None)
        _svcom, _svpeer = dict(vscom._COM_GAME), dict(matchmaking._MATCH_PEER)
        vscom._COM_GAME.clear()
        dispatch.handle_line(b"41001200@GameENC=/NN=0", member_id="comtest",
                    peer_nick=b"UTESTPEER")
        if matchmaking._MATCH_PEER.get(pushqueue._push_key("comtest")) != b"UTESTPEER":
            common._say("FAIL: @GameENC= must record the game peer in _MATCH_PEER -- "
                 "a COM game never sends @GameOK=, and unframed pushes die on "
                 "the session-pair gate"); ok = False
        _q0 = pushqueue._PUSHES.get(pushqueue._push_key("comtest")) or []
        if not any(e[2] == b"UTESTPEER" for e in _q0
                   if b"@ComGameInit=" in e[1]):
            common._say("FAIL: the queued (0x43,2) @ComGameInit must be PINNED to the "
                 "game peer -- delivered from any other nick it would set the "
                 "session pair to the wrong guid"); ok = False
        dispatch.handle_line(b"43000603@ComGame=/Rule=100|0|1|1|1|0|0|0/Com=1|2",
                    member_id="comtest", peer_nick=b"UTESTPEER")
        if vscom._com_n("comtest") != 3:
            common._say("FAIL: /Com=1|2 is TWO COM opponents -> 3 players, got %r"
                 % (vscom._com_n("comtest"),)); ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("comtest"), None)
        _cs3 = dispatch.handle_line(b"43000700@CardSelect=/C=5"
                           b"@N0=/D=42|43|1|20|18|7|4|255"
                           b"@N1=/D=47|38|1|28|11|7|3|255"
                           b"@N2=/D=58|56|1|56|45|14|7|2"
                           b"@N3=/D=59|45|1|45|56|14|7|2"
                           b"@N4=/D=87|60|2|60|87|21|12|4",
                           member_id="comtest", peer_nick=b"UTESTPEER")
        if not _cs3 or b"/Ans=1|1|1" not in _cs3 or b"/Ok=1" not in _cs3:
            common._say("FAIL: a 3-player COM card select answers /Ans=1|1|1/Ok=1 "
                 "(one occurrence per seat, arm 0x1030C9), got %r"
                 % (_cs3,)); ok = False
        _qs = pushqueue._PUSHES.get(pushqueue._push_key("comtest")) or []
        _sd3 = next((e for e in _qs if b"@StartData=" in e[1]), None)
        _td3 = next((e for e in _qs if b"@TurnData=" in e[1]), None)
        if _sd3 is None:
            common._say("FAIL: the COM deal must queue @StartData"); ok = False
        else:
            if len(selftest_tables._pipe_vals(_sd3[1], b"/L=")) != 3:
                common._say("FAIL: a 3-player deal carries three /L= values "
                     "(0x103689 zeroes a failed parse -- the count must be "
                     "exact), got %r" % (selftest_tables._pipe_vals(_sd3[1], b"/L="),))
                ok = False
            if len(selftest_tables._pipe_vals(_sd3[1], b"/F=")) != 25:
                common._say("FAIL: a 3-player board is 25 tiles (TILES_BY_PLAYERS[1]),"
                     " got %d" % len(selftest_tables._pipe_vals(_sd3[1], b"/F="))); ok = False
            if _sd3[2] != b"UTESTPEER" or _sd3[4] != b"UTESTPEER":
                common._say("FAIL: the COM deal must be pinned AND framed to the game "
                     "peer (session-pair gate 0x1034FD), got peer=%r source=%r"
                     % (_sd3[2], _sd3[4])); ok = False
        if _td3 is None:
            common._say("FAIL: the COM game must queue turn 0's @TurnData -- the "
                 "FIFTH seat gate: the loop iterated `seats`, which is empty "
                 "in a COM game, so the scene polled command 9 for ever")
            ok = False
        else:
            if len(selftest_tables._pipe_vals(_td3[1], b"/S=")) != 3:
                common._say("FAIL: a 3-player @TurnData carries three /S= values, "
                     "got %r" % (selftest_tables._pipe_vals(_td3[1], b"/S="),)); ok = False
            if _td3[2] != b"UTESTPEER":
                common._say("FAIL: turn 0 must be pinned to the game peer too, "
                     "got %r" % (_td3[2],)); ok = False
        # The COM hands were dealt at CardSelect time -- the server plays the
        # COM (measured 14:31Z: the client acks the COM's turn announcement
        # and then just polls (0x43, 10) -- it never sends @PutData for a COM
        # seat).
        _ckey = (None, pushqueue._push_key("comtest"))
        _chands = boardrules._MATCH_HANDS.get(_ckey) or {}
        if len(_chands.get(("com", 1)) or []) != 5 \
                or len(_chands.get(("com", 2)) or []) != 5:
            common._say("FAIL: both COM seats must be dealt five cards at the deal, "
                 "got %r" % ({k: len(v) for k, v in _chands.items()},))
            ok = False
        # ...and the human's placement starts the ACK-GATED chain: announce
        # the COM's turn 1, but hold its @PutCard until the client's own
        # @TurnData= ack proves it is past the GamePlayEnd purge. THE CRASH
        # THIS ENCODES (2026-08-22T17:03Z, 3-player): 0xC7642 purges
        # (0x43, 8/0xA/0xC/0x28) from the store at every GamePlayEnd, so a
        # COM put delivered while the previous turn still animates is
        # DELETED (popped, no Recv=PUTCARD), and the next put the client
        # sees is a turn ahead -- Errot!!Turn -> -1 -> 0xC72A6's "invalid
        # card data detected" -> CardGameOutClose.
        _prev = list(pushqueue._PUSHES.get(pushqueue._push_key("comtest")) or [])
        _pd = dispatch.handle_line(b"43000A00@PutData=/P=0/H=0/F=12",
                          member_id="comtest", peer_nick=b"UTESTPEER")
        _pdl = _pd if isinstance(_pd, list) else ([_pd] if _pd else [])
        if not any(b"@PutCard=" in ln for ln in _pdl):
            common._say("FAIL: the human's @PutData= must answer @PutCard=, got %r"
                 % (_pdl,)); ok = False
        _qp = [e for e in (pushqueue._PUSHES.get(pushqueue._push_key("comtest")) or [])
               if not any(e is p for p in _prev)]
        if any(b"@PutCard=" in e[1] for e in _qp):
            common._say("FAIL: no COM @PutCard may be QUEUED before its turn is "
                 "acked -- an early put is purged at GamePlayEnd (0xC7642) "
                 "and the desync is the 3-player 'invalid card data' crash")
            ok = False
        if len([e for e in _qp if b"@TurnData=" in e[1]]) != 1:
            common._say("FAIL: the placement announces exactly ONE turn (the next "
                 "COM turn; its put waits for the ack), got %r"
                 % ([e[1][:20] for e in _qp],)); ok = False
        _st_ack = boardrules._MATCH_TURN.get((None, pushqueue._push_key("comtest"))) or {}
        if _st_ack.get("await_ack") != 1:
            common._say("FAIL: the announced COM turn must arm await_ack=1, got %r"
                 % (_st_ack.get("await_ack"),)); ok = False
        # The client enters turn 1 and acks it: the pending put ANSWERS THE
        # ACK (on its own (0x43,10) slot -- the cmd-9 reader never sees it).
        _a1 = dispatch.handle_line(b"43000901@TurnData=/Ans=0", member_id="comtest",
                          peer_nick=b"UTESTPEER")
        _a1l = _a1 if isinstance(_a1, list) else ([_a1] if _a1 else [])
        if not any(ln.startswith(b"43000A01") and b"@PutCard=/A=1" in ln
                   for ln in _a1l):
            common._say("FAIL: acking COM turn 1 must release its @PutCard as the "
                 "direct reply (header 43000A01, /A=1), got %r" % (_a1l,))
            ok = False
        if (boardrules._MATCH_TURN.get((None, pushqueue._push_key("comtest"))) or {}) \
                .get("await_ack") != 2:
            common._say("FAIL: releasing turn 1 must announce turn 2 and re-arm "
                 "await_ack=2"); ok = False
        _a2 = dispatch.handle_line(b"43000902@TurnData=/Ans=0", member_id="comtest",
                          peer_nick=b"UTESTPEER")
        _a2l = _a2 if isinstance(_a2, list) else ([_a2] if _a2 else [])
        if not any(ln.startswith(b"43000A02") and b"@PutCard=/A=2" in ln
                   for ln in _a2l):
            common._say("FAIL: acking COM turn 2 must release the second COM seat's "
                 "@PutCard (43000A02, /A=2), got %r" % (_a2l,)); ok = False
        _qp2 = pushqueue._PUSHES.get(pushqueue._push_key("comtest")) or []
        if not any(b"@TurnData=/A=0" in e[1] and e[1].startswith(b"43000903")
                   for e in _qp2):
            common._say("FAIL: after the last COM seat the human's turn 3 must be "
                 "announced (43000903, /A=0)"); ok = False
        if (boardrules._MATCH_TURN.get((None, pushqueue._push_key("comtest"))) or {}) \
                .get("await_ack") is not None:
            common._say("FAIL: a human turn arms no await_ack"); ok = False
        # A duplicate ack must not double-play the turn. (The reply may still
        # carry DRAINED pushes -- handle_line attaches anything due -- so the
        # assertion is "no put released", not "no reply".)
        _dup = dispatch.handle_line(b"43000902@TurnData=/Ans=0", member_id="comtest",
                           peer_nick=b"UTESTPEER")
        _dupl = _dup if isinstance(_dup, list) else ([_dup] if _dup else [])
        if any(b"@PutCard=" in ln for ln in _dupl):
            common._say("FAIL: a duplicate turn ack must release no @PutCard "
                 "(await_ack was consumed)"); ok = False
        _afters = [e[0] for e in _qp]
        if _afters != sorted(_afters):
            common._say("FAIL: the COM chain's queued pushes must be in "
                 "non-decreasing after order, got %r" % (_afters,))
            ok = False
        if any(e[2] != b"UTESTPEER" for e in _qp):
            common._say("FAIL: every COM chain push must be pinned to the game peer")
            ok = False
        # --- the post-game family: @Ready, @GetSelect, @Continue (COM) ------
        # The win-screen hang, live 14:51:56Z: @ResultData read, the win
        # screen drew, the client sent (0x43,14) @Ready=/Ans=0 and nothing
        # answered. Arm 0x1045E7: any well-formed echo with /Go= releases it.
        _rd = dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id="comtest",
                          peer_nick=b"UTESTPEER")
        _rdl = _rd if isinstance(_rd, list) else ([_rd] if _rd else [])
        if not any(ln.startswith(b"43000E0A") and b"@Ready=/Go=1" in ln
                   for ln in _rdl):
            common._say("FAIL: (0x43,14) @Ready= must be echoed with /Go= (arm "
                 "0x1045E7 parses /Go= and sets result 1), got %r" % (_rdl,))
            ok = False
        # ...and @GameReady= (0x41) must NOT fall into the @Ready arm.
        if dispatch.handle_line(b"43000E0A@GameReady=", member_id=None) is not None:
            common._say("FAIL: @GameReady= contains '@Ready=' as a substring and "
                 "must not reach the READY echo"); ok = False
        _gs = dispatch.handle_line(b"43000F00@GetSelect=/H=2", member_id="comtest",
                          peer_nick=b"UTESTPEER")
        _gsl = _gs if isinstance(_gs, list) else ([_gs] if _gs else [])
        if not any(ln.startswith(b"43000F00") and b"/E=0/H=2" in ln
                   for ln in _gsl):
            common._say("FAIL: (0x43,15) @GetSelect=/H=2 must echo /E=/H= (arm "
                 "0x104E5B stores /H= into [obj+0x1B2]), got %r" % (_gsl,))
            ok = False
        # @Continue in a COM game: the COM always agrees, the human's answer
        # is the vote. /Ans=1 -> sh=1 everyone-1 (rematch ON); /Ans=2 -> the
        # human's own slot 3 under sh>=2 (the only test state 7 makes).
        _cy = dispatch.handle_line(b"43001100@Continue=/Ans=1/Conf=0/PR=0",
                          member_id="comtest", peer_nick=b"UTESTPEER")
        _cyl = _cy if isinstance(_cy, list) else ([_cy] if _cy else [])
        if not any(ln.startswith(b"43001101") and b"/Ans=1|1|1" in ln
                   for ln in _cyl):
            common._say("FAIL: COM @Continue=/Ans=1 must answer sh=1 with every "
                 "slot 1 (rematch ON), got %r" % (_cyl,)); ok = False
        # WARNING: /Conf=1 IS "CHANGE THE SETTINGS FIRST", and that path waits on
        # (0x43, 4) @ComList. Without it the screen froze and the client went
        # SILENT -- measured three times live 2026-09-07, and Conf=0 never did.
        pushqueue._PUSHES.pop(pushqueue._push_key("comtest"), None)
        dispatch.handle_line(b"43001100@Continue=/Ans=1/Conf=0/PR=0",
                    member_id="comtest", peer_nick=b"UTESTPEER")
        if any(b"@ComList=" in e[1]
               for e in (pushqueue._PUSHES.get(pushqueue._push_key("comtest")) or [])):
            common._say("FAIL: /Conf=0 is a plain rematch and must NOT send @ComList")
            ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("comtest"), None)
        dispatch.handle_line(b"43001100@Continue=/Ans=1/Conf=1/PR=0",
                    member_id="comtest", peer_nick=b"UTESTPEER")
        _clq = [e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key("comtest")) or [])
                if b"@ComList=" in e[1]]
        if len(_clq) != 1:
            common._say("FAIL: /Conf=1 must queue exactly one @ComList, got %r"
                 % (_clq,)); ok = False
        elif not _clq[0].startswith(b"43000400"):
            common._say("FAIL: @ComList rides (0x43, 4) -- arm 0x102F74; got %r"
                 % (_clq[0][:8],)); ok = False
        elif _clq[0].count(b"|") + 1 != int(
                re.search(rb"/L=(\d+)", _clq[0]).group(1)):
            common._say("FAIL: /L= is the ROW COUNT and must match the number of /D= "
                 "occurrences (0x102FDC reads it as the loop bound) -- %r"
                 % (_clq[0],)); ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("comtest"), None)
        _cn = dispatch.handle_line(b"43001100@Continue=/Ans=2/Conf=0/PR=0",
                          member_id="comtest", peer_nick=b"UTESTPEER")
        _cnl = _cn if isinstance(_cn, list) else ([_cn] if _cn else [])
        if not any(ln.startswith(b"43001102") and b"/Ans=3|" in ln
                   for ln in _cnl):
            common._say("FAIL: COM @Continue=/Ans=2 must answer sh>=2 with the "
                 "human's own slot 3 (CONTINUE_FINAL tears the panel down), "
                 "got %r" % (_cnl,)); ok = False
        # A vote whose live roster died mid-quorum RESUMES from the tally's
        # own seats snapshot (2026-08-22T19:43Z: a deploy restart between the
        # two votes hung both clients on "Now loading")...
        rematch._MATCH_CONTINUE[(None, "q-test")] = {
            "seats": [("qva", 0), ("qvb", 0)],
            "votes": {pushqueue._push_key("qva"): 2}}
        _qv = dispatch.handle_line(b"43001100@Continue=/Ans=2/Conf=0/PR=0",
                          member_id="qvb")
        _qvl = _qv if isinstance(_qv, list) else ([_qv] if _qv else [])
        if not any(ln.startswith(b"43001102") for ln in _qvl):
            common._say("FAIL: a vote with no live roster must resume from the "
                 "pending tally's seats snapshot and complete the quorum, "
                 "got %r" % (_qvl,)); ok = False
        rematch._MATCH_CONTINUE.pop((None, "q-test"), None)
        # ...and a vote NOTHING remembers gets the orphan teardown, not a
        # hang: sh=2, full-width all-3 (longer than N is safe; short is the
        # stack-garbage hazard).
        _qo = dispatch.handle_line(b"43001100@Continue=/Ans=2/Conf=0/PR=0",
                          member_id="q-orphan")
        _qol = _qo if isinstance(_qo, list) else ([_qo] if _qo else [])
        if not any(ln.startswith(b"43001102") and b"/Ans=3|3|3|3" in ln
                   for ln in _qol):
            common._say("FAIL: an orphaned @Continue (restart raced the vote) must "
                 "answer the all-3 teardown, got %r" % (_qol,)); ok = False
        # --- the take flow: @TCList row shape and decisive-only --------------
        _rowA = b"42|43|1|20|18|7|4|255"
        _tb = matchend._tclist_body([[_rowA], [_rowA, _rowA]])
        if not _tb.startswith(b"@TCList=/H=1|2"):
            common._say("FAIL: @TCList /H= is one count per player, got %r"
                 % (_tb[:24],)); ok = False
        # ...in the RECIPIENT's frame (41c, measured 15:28Z): seat 1's copy
        # lists its own pool first.
        _tb1 = matchend._tclist_body(matchend._take_frame([[_rowA], [_rowA, _rowA]], 1))
        if not _tb1.startswith(b"@TCList=/H=2|1"):
            common._say("FAIL: the take list must be rotated so the recipient's own "
                 "pool is occurrence 0 (the Deck picked blind from seat 1), "
                 "got %r" % (_tb1[:24],)); ok = False
        # occ 0 = the seat ENTITLED to take the row, in the recipient's frame
        # (state 0x3A, 0xC91B9): winner seat 0's copy keeps 0 everywhere; the
        # loser (seat 1) sees its own rows tagged 1 (the winner in its frame)
        # and the winner's rows 0 -- without it the loser's scene finds no
        # target and leaves before the pick (2026-09-07).
        _p2 = [[_rowA], [_rowA]]
        if matchend._take_occ0(matchend._take_frame(_p2, 0), 0, 0) != [0, 0]:
            common._say("FAIL: the winner's own copy keeps occ0 = 0 (its targets)"); ok = False
        if matchend._take_occ0(matchend._take_frame(_p2, 1), 1, 0) != [1, 0]:
            common._say("FAIL: the loser's copy must tag its own rows with the winner's "
                 "index (1) and the winner's rows 0, got %r"
                 % (matchend._take_occ0(matchend._take_frame(_p2, 1), 1, 0),)); ok = False
        _tbl = matchend._tclist_body(matchend._take_frame(_p2, 1), matchend._take_occ0(matchend._take_frame(_p2, 1), 1, 0))
        if b"/D0=1|" not in _tbl or b"/D1=0|" not in _tbl:
            common._say("FAIL: the loser's take list rows must carry the taker byte, "
                 "got %r" % (_tbl,)); ok = False
        if matchend._losers_from(1, [[_rowA], [_rowA], [_rowA]]) != [2, 0]:
            common._say("FAIL: the winner's take screen walks the losers in rotated "
                 "order after itself, got %r"
                 % (matchend._losers_from(1, [[_rowA], [_rowA], [_rowA]]),)); ok = False
        # occ 0 must stay 0 (small): it is a validity flag, not the name -- occ
        # 0 = id made the take cards INVISIBLE live (2026-09-02). Row shape is
        # 0|id|atk|type|pdef|mdef|arrows, flat-indexed across players.
        if b"/D0=0|42|43|1|20|18|4" not in _tb \
                or b"/D2=0|42|43|1|20|18|4" not in _tb:
            common._say("FAIL: @TCList /D<k>= is flat-indexed, occurrences "
                 "0|id|atk|type|pdef|mdef|arrows (arm 0x104648); occ 0 must be "
                 "0 or the card goes invisible, got %r" % (_tb,)); ok = False
        _tk = (None, "tcl-test")
        boardrules._MATCH_HANDS[_tk] = {("deck", 0): [_rowA], ("deck", 1): [_rowA]}
        boardrules._MATCH_BOARD[_tk] = {0: tmbattle.Card(_rowA, 0)}   # 1-0: decisive
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        turns._queue_next_turn(None, "tcl-test", [], 10, 0,
                         roster=[("tclm", 0)], n_solo=2)
        _qt = pushqueue._PUSHES.get(pushqueue._push_key("tclm")) or []
        if not any(b"@TCList=" in e[1] for e in _qt) \
                or any(b"@GetAway=" in e[1] for e in _qt):
            common._say("FAIL: a decisive COM result must queue @TCList and NO "
                 "@GetAway -- the win screen picks without it and to a "
                 "recipient it means 'seat P LEFT' (measured 2026-09-07 "
                 "15:13Z; the 16:49Z 'hangs without it' read is retracted)")
            ok = False
        boardrules._MATCH_BOARD[_tk] = {}                              # 0-0: a draw
        boardrules._MATCH_TURN.pop(_tk, None)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        turns._queue_next_turn(None, "tcl-test", [], 10, 0,
                         roster=[("tclm", 0)], n_solo=2)
        if any(b"@TCList=" in e[1]
               for e in (pushqueue._PUSHES.get(pushqueue._push_key("tclm")) or [])):
            common._say("FAIL: a DRAW must queue no take flow -- the draw screen "
                 "polls neither 26 nor 35 and flows to @Continue by itself")
            ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND,
                   rematch._MATCH_CONTINUE, matchstart._CARD_READY):
            _d.pop(_tk, None)

        # --- the take is a TRANSFER now -------------------------------
        # (1) HUMAN WON: @GetSelect=/H=<slot> adds the loser's card to the
        # winner's collection, once. The fixture key must be the real COM key
        # (chan from _match_of, index = push_key) or the handler cannot find
        # the stashed take state -- which is itself part of the contract.
        _tkm = (None, pushqueue._push_key("tclm"))
        vscom._COM_GAME[pushqueue._push_key("tclm")] = {"n": 2, "coms": [1]}
        boardrules._MATCH_HANDS[_tkm] = {("deck", 0): [_rowA], ("deck", 1): [_rowA]}
        # human wins 2-1: NOT a perfect (the COM still holds a tile), so
        # this is the one-pick flow -- a 1-0 board is a PERFECT win now.
        boardrules._MATCH_BOARD[_tkm] = {0: tmbattle.Card(_rowA, 0),
                              1: tmbattle.Card(_rowA, 0),
                              2: tmbattle.Card(_rowA, 1)}
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        collection._collection_store("tclm", {"cards": []}, sync_save=False)
        turns._queue_next_turn(None, pushqueue._push_key("tclm"), [], 10, 0,
                         roster=[("tclm", 0)], n_solo=2)
        _gsw = dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id="tclm")
        _gswl = _gsw if isinstance(_gsw, list) else ([_gsw] if _gsw else [])
        _col = (collection._collection_load("tclm").get("cards")) or []
        if _col != [[42, 43, 1, 20, 18, 7, 4, 255]]:
            common._say("FAIL: a winning @GetSelect must ADD the picked card to the "
                 "collection (-- the take was display only), got %r"
                 % (_col,)); ok = False
        _gsw2 = dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id="tclm")
        if len((collection._collection_load("tclm").get("cards")) or []) != 1:
            common._say("FAIL: a 2-player win has ONE loser -- a re-sent @GetSelect "
                 "must not take twice"); ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        # (1b) a 3-PLAYER win takes ONE CARD PER LOSING SEAT (reported live,
        # 2026-08-22: "it lets me take one card from each com") -- two picks
        # land, a third is refused.
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND):
            _d.pop(_tkm, None)
        vscom._COM_GAME[pushqueue._push_key("tclm")] = {"n": 3, "coms": [1, 2]}
        boardrules._MATCH_HANDS[_tkm] = {("deck", 0): [_rowA], ("deck", 1): [_rowA],
                              ("deck", 2): [_rowA]}
        boardrules._MATCH_BOARD[_tkm] = {0: tmbattle.Card(_rowA, 0),  # human wins
                              1: tmbattle.Card(_rowA, 0),  # 2-1-0: not a
                              2: tmbattle.Card(_rowA, 1)}  # perfect
        collection._collection_store("tclm", {"cards": []}, sync_save=False)
        turns._queue_next_turn(None, pushqueue._push_key("tclm"), [], 15, 0,
                         roster=[("tclm", 0)], n_solo=3)
        dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id="tclm")
        dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id="tclm")
        dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id="tclm")
        if len((collection._collection_load("tclm").get("cards")) or []) != 2:
            common._say("FAIL: a 3-player win takes ONE card per losing seat (two "
                 "picks land, the third is refused), got %r"
                 % (collection._collection_load("tclm").get("cards"),)); ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        vscom._COM_GAME[pushqueue._push_key("tclm")] = {"n": 2, "coms": [1]}
        # (2) COM WON: the server makes the pick itself -- the loser's screen
        # polls (0x43, 15) (0xCA481) and parks without it -- and the human's
        # card leaves the collection.
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND):
            _d.pop(_tkm, None)
        boardrules._MATCH_HANDS[_tkm] = {("deck", 0): [_rowA], ("deck", 1): [_rowA]}
        boardrules._MATCH_BOARD[_tkm] = {0: tmbattle.Card(_rowA, 1),  # the COM wins
                              1: tmbattle.Card(_rowA, 1),  # 2-1: not a
                              2: tmbattle.Card(_rowA, 0)}  # perfect
        collection._collection_store("tclm", {"cards": [[42, 43, 1, 20, 18, 7, 4, 255]]},
                          sync_save=False)
        turns._queue_next_turn(None, pushqueue._push_key("tclm"), [], 10, 0,
                         roster=[("tclm", 0)], n_solo=2)
        _qt3 = pushqueue._PUSHES.get(pushqueue._push_key("tclm")) or []
        _gsel = next((e for e in _qt3 if b"@GetSelect=" in e[1]), None)
        if _gsel is None or not _gsel[1].startswith(b"43000F01"):
            common._say("FAIL: a COM win must push the COM's own @GetSelect (header "
                 "43000F01 -- sh is the winner's seat), got %r"
                 % (_gsel[1][:20] if _gsel else None)); ok = False
        if (collection._collection_load("tclm").get("cards")) or []:
            common._say("FAIL: the COM's take must remove the picked card from the "
                 "human's collection (POL_TM_TAKE_LOSS)"); ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND,
                   rematch._MATCH_CONTINUE, matchstart._CARD_READY):
            _d.pop(_tkm, None)

        # --- the PvP take: flow queued for BOTH, transfer moves a card ------
        # between two REAL collections (winner gains, loser loses), gated on
        # the take-roster seat so a loser's own @GetSelect stays echo-only.
        _rowB = b"47|38|1|28|11|7|3|255"
        _tkp = (None, "pvp-take-test")
        boardrules._MATCH_HANDS[_tkp] = {("deck", 0): [_rowA], ("deck", 1): [_rowB]}
        boardrules._MATCH_BOARD[_tkp] = {0: tmbattle.Card(_rowA, 1),  # seat 1 wins
                              1: tmbattle.Card(_rowA, 1),  # 1-2: not a
                              2: tmbattle.Card(_rowB, 0)}  # perfect
        collection._collection_store("tclm", {"cards": []}, sync_save=False)
        collection._collection_store("tclo", {"cards": [[42, 43, 1, 20, 18, 7, 4, 255]]},
                          sync_save=False)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclo"), None)
        _pvp_seats = [("tclo", 0), ("tclm", 0)]     # tclo seat 0, tclm seat 1
        turns._queue_next_turn(None, "pvp-take-test", _pvp_seats, 10, 0,
                         roster=_pvp_seats)
        for _pm in ("tclm", "tclo"):
            _qv = pushqueue._PUSHES.get(pushqueue._push_key(_pm)) or []
            if not any(b"@TCList=" in e[1] for e in _qv) \
                    or any(b"@GetAway=" in e[1] for e in _qv):
                common._say("FAIL: a decisive PvP result must queue @TCList and NO "
                     "@GetAway to member %s (@GetAway = 'seat P LEFT', "
                     "measured 2026-09-07 15:13Z)" % _pm)
                ok = False
        # the LOSER's pick is echo-only...
        _sv_mof = matchmaking._match_of
        matchmaking._match_of = lambda m: (None, "pvp-take-test", _pvp_seats)
        try:
            dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id="tclo")
            if (collection._collection_load("tclo").get("cards") or []) == []:
                common._say("FAIL: a LOSER's @GetSelect must transfer nothing")
                ok = False
            # ...the WINNER's pick moves the card between the collections.
            dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id="tclm")
            if [c[0] for c in (collection._collection_load("tclm").get("cards") or [])] \
                    != [42]:
                common._say("FAIL: the PvP winner's @GetSelect must ADD the loser's "
                     "picked card, got %r"
                     % (collection._collection_load("tclm").get("cards"),)); ok = False
            if (collection._collection_load("tclo").get("cards") or []):
                common._say("FAIL: the PvP loser must LOSE the picked card "
                     "(POL_TM_TAKE_LOSS), got %r"
                     % (collection._collection_load("tclo").get("cards"),)); ok = False
        finally:
            matchmaking._match_of = _sv_mof
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclo"), None)
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND,
                   rematch._MATCH_CONTINUE, matchstart._CARD_READY):
            _d.pop(_tkp, None)
        for _pth in (collection._collection_file("tclo"), collection._save_resource_file("tclo")):
            try:
                if _pth and os.path.exists(_pth):
                    os.remove(_pth)
            except OSError:
                pass

        # --- the PERFECT take: the winner owns EVERY owned card on the board --
        # (client result pass 0xCC58D: score == board total -> flag 3). No
        # take-select scene on either client: the winner's client moves ALL of
        # each loser's pool cards to itself and sends no @GetSelect, the
        # loser's does not poll (0x43, 15). The server commits the whole pool
        # at the result, and a COM winner's pick is NOT pushed.
        _prows = [b"%d|40|1|20|18|7|4|255" % (60 + _i) for _i in range(5)]
        _qrows = [b"%d|41|2|21|19|8|5|255" % (80 + _i) for _i in range(5)]
        _pcards = [[int(v) for v in r.split(b"|")] for r in _prows]
        _qcards = [[int(v) for v in r.split(b"|")] for r in _qrows]

        def _perf_board(owner_tiles):
            # tiles -> owner; an unowned BLOCK (owner 4) is on the board too
            # and is in nobody's count -- it must not spoil a perfect.
            b_ = {t: tmbattle.Card(_prows[0], o) for t, o in owner_tiles}
            b_[15] = tmbattle.Card(b"32769|0|0|0|0|0|0|0",
                                   tmbattle.OWNER_BLOCK)
            return b_

        def _perf_clean(key):
            boardrules._MATCH_OBJECTS.clear()
            for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND,
                       rematch._MATCH_CONTINUE, matchstart._CARD_READY):
                _d.pop(key, None)
            rematch._CONTINUE_READY.pop(key, None)
            for _m in ("pfw", "pfl", "tclm"):
                pushqueue._PUSHES.pop(pushqueue._push_key(_m), None)

        def _pcards_of(m):
            return collection._collection_load(m).get("cards") or []

        def _pgs(m):
            return any(b"@GetSelect=" in e[1]
                       for e in (pushqueue._PUSHES.get(pushqueue._push_key(m)) or []))

        _saved_pt = os.environ.get("POL_TM_PERFECT_TAKE")
        _saved_hold2 = os.environ.get("POL_TM_READY_HOLD_LOSER")
        _saved_com_tclm = vscom._COM_GAME.get(pushqueue._push_key("tclm"))
        _sv_mof2 = matchmaking._match_of
        _tkq = (None, "pvp-perf-test")
        _tkc = (None, pushqueue._push_key("tclm"))
        _pseats = [("pfl", 0), ("pfw", 0)]      # pfl seat 0, pfw seat 1
        try:
            os.environ.pop("POL_TM_PERFECT_TAKE", None)     # default = on
            vscom._COM_GAME.pop(pushqueue._push_key("pfw"), None)
            vscom._COM_GAME.pop(pushqueue._push_key("pfl"), None)
            # (P1) 2-PLAYER PvP PERFECT: seat 1 (pfw) owns all 3 owned tiles,
            # seat 0 (pfl) none. pfw gains all 5 of pfl's pool, pfl loses all
            # 5, nothing waits on a @GetSelect, and @Ready completes.
            _perf_clean(_tkq)
            boardrules._MATCH_HANDS[_tkq] = {("deck", 0): list(_prows),
                                  ("deck", 1): list(_qrows)}
            boardrules._MATCH_BOARD[_tkq] = _perf_board([(0, 1), (1, 1), (5, 1)])
            collection._collection_store("pfw", {"cards": [list(c) for c in _qcards]},
                              sync_save=False)
            collection._collection_store("pfl", {"cards": [list(c) for c in _pcards]},
                              sync_save=False)
            turns._queue_next_turn(None, "pvp-perf-test", _pseats, 10, 0,
                             roster=_pseats)
            _tkP = (boardrules._MATCH_TURN.get(_tkq) or {}).get("take") or {}
            if not _tkP.get("perfect"):
                common._say("FAIL: PERFECT: a 3-0 PvP board (plus an unowned block) "
                     "is a perfect win -- take['perfect'] must be set, got %r"
                     % (_tkP,)); ok = False
            _wc = sorted(c[0] for c in _pcards_of("pfw"))
            if _wc != sorted(c[0] for c in _qcards + _pcards):
                common._say("FAIL: PERFECT: the PvP winner must gain ALL 5 of the "
                     "loser's pool cards at the result, got %r" % (_wc,))
                ok = False
            if _pcards_of("pfl"):
                common._say("FAIL: PERFECT: the PvP loser must lose ALL 5 pool cards "
                     "(POL_TM_TAKE_LOSS), got %r" % (_pcards_of("pfl"),))
                ok = False
            if _pgs("pfw") or _pgs("pfl"):
                common._say("FAIL: PERFECT: a PvP perfect queues no @GetSelect")
                ok = False
            matchmaking._match_of = lambda m: (None, "pvp-perf-test",
                                                _pseats)
            os.environ["POL_TM_READY_HOLD_LOSER"] = "1"
            dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id="pfw")
            dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id="pfl")
            _pr2 = dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id="pfl")
            # (the reply may also carry drained result pushes -- look for the
            # @Ready answer itself, and for no held entry)
            _pr2b = _pr2 if isinstance(_pr2, bytes) else b"".join(_pr2 or [])
            if b"@Ready=/Go=" not in _pr2b or (
                    (boardrules._MATCH_TURN.get(_tkq) or {}).get("take") or {}).get(
                        "held_ready"):
                common._say("FAIL: PERFECT: the loser's 2nd @Ready= must NOT be held "
                     "-- there is no pick coming, got %r" % (_pr2,)); ok = False
            # a stray winner @GetSelect only echoes: no 2nd transfer, and
            # nothing relayed into the loser's queue.
            pushqueue._PUSHES.pop(pushqueue._push_key("pfl"), None)
            _sg = dispatch.handle_line(b"43000F01@GetSelect=/H=0", member_id="pfw")
            _sgb = _sg if isinstance(_sg, bytes) else b"".join(_sg or [])
            if len(_pcards_of("pfw")) != 10 or b"@GetSelect=" not in _sgb:
                common._say("FAIL: PERFECT: a stray @GetSelect must echo and move "
                     "nothing, got %r / %d cards"
                     % (_sg, len(_pcards_of("pfw")))); ok = False
            if _pgs("pfl"):
                common._say("FAIL: PERFECT: a stray @GetSelect must not be relayed "
                     "to a loser that never polls (0x43, 15)"); ok = False
            dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id="pfw")
            _cr = rematch._CONTINUE_READY.get(_tkq) or set()
            if pushqueue._push_key("pfw") not in _cr or pushqueue._push_key("pfl") not in _cr:
                common._say("FAIL: PERFECT: both 2nd @Ready=s must open the panel "
                     "(the take is complete), got %r" % (_cr,)); ok = False
            matchmaking._match_of = _sv_mof2
            _perf_clean(_tkq)

            # (P2) NON-PERFECT PvP is unchanged: 2-1, no transfer at the
            # result, the winner's one @GetSelect moves one card.
            boardrules._MATCH_HANDS[_tkq] = {("deck", 0): list(_prows),
                                  ("deck", 1): list(_qrows)}
            boardrules._MATCH_BOARD[_tkq] = _perf_board([(0, 1), (1, 1), (5, 0)])
            collection._collection_store("pfw", {"cards": []}, sync_save=False)
            collection._collection_store("pfl", {"cards": [list(c) for c in _pcards]},
                              sync_save=False)
            turns._queue_next_turn(None, "pvp-perf-test", _pseats, 10, 0,
                             roster=_pseats)
            _tkN = (boardrules._MATCH_TURN.get(_tkq) or {}).get("take") or {}
            if not _tkN or _tkN.get("perfect") or _pcards_of("pfw"):
                common._say("FAIL: PERFECT: a 2-1 board is NOT perfect -- nothing "
                     "moves at the result, got %r" % (_tkN,)); ok = False
            matchmaking._match_of = lambda m: (None, "pvp-perf-test",
                                                _pseats)
            dispatch.handle_line(b"43000F01@GetSelect=/H=2", member_id="pfw")
            dispatch.handle_line(b"43000F01@GetSelect=/H=3", member_id="pfw")
            matchmaking._match_of = _sv_mof2
            if [c[0] for c in _pcards_of("pfw")] != [62] \
                    or len(_pcards_of("pfl")) != 4:
                common._say("FAIL: PERFECT: a non-perfect PvP take is ONE pick "
                     "(slot 2 = card 62), got %r / %r"
                     % (_pcards_of("pfw"), _pcards_of("pfl"))); ok = False
            _perf_clean(_tkq)

            # (P3) KNOB 0 = the old flow on a perfect board: no transfer at
            # the result, one pick by @GetSelect.
            os.environ["POL_TM_PERFECT_TAKE"] = "0"
            boardrules._MATCH_HANDS[_tkq] = {("deck", 0): list(_prows),
                                  ("deck", 1): list(_qrows)}
            boardrules._MATCH_BOARD[_tkq] = _perf_board([(0, 1), (1, 1), (5, 1)])
            collection._collection_store("pfw", {"cards": []}, sync_save=False)
            collection._collection_store("pfl", {"cards": [list(c) for c in _pcards]},
                              sync_save=False)
            turns._queue_next_turn(None, "pvp-perf-test", _pseats, 10, 0,
                             roster=_pseats)
            if ((boardrules._MATCH_TURN.get(_tkq) or {}).get("take") or {}).get(
                    "perfect") or _pcards_of("pfw"):
                common._say("FAIL: PERFECT: POL_TM_PERFECT_TAKE=0 must restore the "
                     "one-pick flow (nothing moves at the result)"); ok = False
            matchmaking._match_of = lambda m: (None, "pvp-perf-test",
                                                _pseats)
            dispatch.handle_line(b"43000F01@GetSelect=/H=0", member_id="pfw")
            matchmaking._match_of = _sv_mof2
            if len(_pcards_of("pfw")) != 1 or len(_pcards_of("pfl")) != 4:
                common._say("FAIL: PERFECT: POL_TM_PERFECT_TAKE=0 -- the @GetSelect "
                     "must take ONE card, got %r" % (_pcards_of("pfw"),))
                ok = False
            _perf_clean(_tkq)
            os.environ.pop("POL_TM_PERFECT_TAKE", None)

            # (P4) VS. COM, HUMAN PERFECT: tclm gets all 5 COM cards; no
            # @GetSelect is pushed.
            _perf_clean(_tkc)
            vscom._COM_GAME[pushqueue._push_key("tclm")] = {"n": 2, "coms": [1]}
            boardrules._MATCH_HANDS[_tkc] = {("deck", 0): list(_prows),
                                  ("deck", 1): list(_qrows)}
            boardrules._MATCH_BOARD[_tkc] = _perf_board([(0, 0), (3, 0)])
            collection._collection_store("tclm", {"cards": [list(c) for c in _pcards]},
                              sync_save=False)
            turns._queue_next_turn(None, pushqueue._push_key("tclm"), [], 10, 0,
                             roster=[("tclm", 0)], n_solo=2)
            _hc = sorted(c[0] for c in _pcards_of("tclm"))
            if _hc != sorted(c[0] for c in _pcards + _qcards):
                common._say("FAIL: PERFECT: a VS. COM human perfect must add ALL 5 "
                     "COM cards (and remove none of the human's), got %r"
                     % (_hc,)); ok = False
            if _pgs("tclm"):
                common._say("FAIL: PERFECT: a human perfect pushes no @GetSelect")
                ok = False
            _perf_clean(_tkc)

            # (P5) VS. COM, COM PERFECT: the COM's pick is NOT pushed (the
            # loser's client does not poll 15 on a perfect) and the human
            # loses all 5 pool cards.
            boardrules._MATCH_HANDS[_tkc] = {("deck", 0): list(_prows),
                                  ("deck", 1): list(_qrows)}
            boardrules._MATCH_BOARD[_tkc] = _perf_board([(0, 1), (3, 1), (7, 1)])
            collection._collection_store("tclm", {"cards": [list(c) for c in _pcards]},
                              sync_save=False)
            turns._queue_next_turn(None, pushqueue._push_key("tclm"), [], 10, 0,
                             roster=[("tclm", 0)], n_solo=2)
            if _pgs("tclm"):
                common._say("FAIL: PERFECT: a COM perfect must NOT push the COM's "
                     "@GetSelect"); ok = False
            if _pcards_of("tclm"):
                common._say("FAIL: PERFECT: a COM perfect must remove ALL 5 of the "
                     "human's pool cards, got %r" % (_pcards_of("tclm"),))
                ok = False
            _perf_clean(_tkc)

            # (P6) KNOB 0, COM perfect: the COM's @GetSelect is pushed again
            # and exactly one card leaves.
            os.environ["POL_TM_PERFECT_TAKE"] = "0"
            boardrules._MATCH_HANDS[_tkc] = {("deck", 0): list(_prows),
                                  ("deck", 1): list(_qrows)}
            boardrules._MATCH_BOARD[_tkc] = _perf_board([(0, 1), (3, 1), (7, 1)])
            collection._collection_store("tclm", {"cards": [list(c) for c in _pcards]},
                              sync_save=False)
            turns._queue_next_turn(None, pushqueue._push_key("tclm"), [], 10, 0,
                             roster=[("tclm", 0)], n_solo=2)
            if not _pgs("tclm") or len(_pcards_of("tclm")) != 4:
                common._say("FAIL: PERFECT: POL_TM_PERFECT_TAKE=0 -- a COM win "
                     "pushes its one @GetSelect and takes ONE card")
                ok = False
            _perf_clean(_tkc)
        finally:
            matchmaking._match_of = _sv_mof2
            for _k, _v in (("POL_TM_PERFECT_TAKE", _saved_pt),
                           ("POL_TM_READY_HOLD_LOSER", _saved_hold2)):
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v
            if _saved_com_tclm is None:
                vscom._COM_GAME.pop(pushqueue._push_key("tclm"), None)
            else:
                vscom._COM_GAME[pushqueue._push_key("tclm")] = _saved_com_tclm
            _perf_clean(_tkq)
            _perf_clean(_tkc)
            for _m in ("pfw", "pfl"):
                for _pth in (collection._collection_file(_m), collection._save_resource_file(_m)):
                    try:
                        if _pth and os.path.exists(_pth):
                            os.remove(_pth)
                    except OSError:
                        pass
        common._say("selftest: the PERFECT take (all pools, no @GetSelect)")

        # --- the PvP WAGER: escrow at the result ----------------------------
        # (The stake-in half rides the accept quorum in _queue_vsgameinit;
        # here the settlement is exercised directly: a unique winner takes
        # the pot, a tie refunds each player their own stake.)
        _pk = ("#TM0RTEST", 8)
        collection._collection_store("tclm", {"cards": [], "money": 500}, sync_save=False)
        collection._collection_store("tclo", {"cards": [], "money": 300}, sync_save=False)
        pots._PVP_STAKES[_pk] = {pushqueue._push_key("tclm"): ("tclm", 200),
                            pushqueue._push_key("tclo"): ("tclo", 200)}
        pots._stake_persist("tclm", 200); pots._stake_persist("tclo", 200)
        boardrules._MATCH_BOARD[_pk] = {0: tmbattle.Card(_rowA, 0)}    # tclm wins 1-0
        boardrules._MATCH_TURN.pop(_pk, None)
        turns._queue_next_turn("#TM0RTEST", 8, [("tclm", 0), ("tclo", 0)], 10, 0,
                         roster=[("tclm", 0), ("tclo", 0)])
        if purse.money_of("tclm") != 500 + 400:
            common._say("FAIL: a unique PvP winner takes the whole pot (own 200 "
                 "back + 200 winnings), got %d" % purse.money_of("tclm")); ok = False
        if purse.money_of("tclo") != 300:
            common._say("FAIL: the PvP loser's stake stays forfeited, got %d"
                 % purse.money_of("tclo")); ok = False
        if (collection._collection_load("tclo").get("staked_wager")) is not None:
            common._say("FAIL: PvP settlement must clear BOTH durable stakes")
            ok = False
        _rk2 = (collection._collection_load("tclm").get("rank")) or {}
        if _rk2.get("prize_total", 0) < 200:
            common._say("FAIL: the PvP winner's net gain must feed the prize tally, "
                 "got %r" % (_rk2,)); ok = False
        # ...and the winner's own @No0 section DELIVERS the settled prize on
        # /D= occ 1 ([obj+0xCC] -- 0xCBD80 max-updates it into [obj+0x78],
        # the Biggest Prize `max`). Reported live 2026-09-02: the result screen's
        # wager row read 0 for ever because the settlement ran after the
        # bodies were built and nothing carried the figure.
        _prb = next((e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key("tclm")) or [])
                     if b"@ResultData=" in e[1]), None)
        _prd = selftest_tables._pipe_vals(_prb or b"", b"/D=")
        if len(_prd) < 2 or _prd[1] != b"200":
            common._say("FAIL: the winner's @ResultData /D= occ 1 must carry the "
                 "settled net prize (200), got %r" % (_prb,)); ok = False
        # ...a TIE refunds each player their own stake.
        pots._PVP_STAKES[_pk] = {pushqueue._push_key("tclm"): ("tclm", 100),
                            pushqueue._push_key("tclo"): ("tclo", 100)}
        boardrules._MATCH_BOARD[_pk] = {}                              # 0-0
        boardrules._MATCH_TURN.pop(_pk, None)
        _wm_before = purse.money_of("tclm")
        _wo_before = purse.money_of("tclo")
        turns._queue_next_turn("#TM0RTEST", 8, [("tclm", 0), ("tclo", 0)], 10, 0,
                         roster=[("tclm", 0), ("tclo", 0)])
        if purse.money_of("tclm") != _wm_before + 100 \
                or purse.money_of("tclo") != _wo_before + 100:
            common._say("FAIL: a PvP tie must refund each stake, got %d/%d"
                 % (purse.money_of("tclm"), purse.money_of("tclo"))); ok = False
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND, boardrules._MATCH_HANDS,
                   rematch._MATCH_CONTINUE, matchstart._CARD_READY):
            _d.pop(_pk, None)
        pots._PVP_STAKES.pop(_pk, None)

        # --- DOUBLE UP (`du`): a POST-GAME STAKE ESCALATOR ------------------
        # KEY: NOT a pre-game multiplier, which is the answer to "why not just
        # wager double". Both `du` read sites sit inside 0x10B780 -- the "Play
        # again?" panel constructor (`Playmes.BIN` 93, accessors all index
        # 0x11 = @Continue) -- so the rule doubles the pot the REMATCH is
        # played for (0x10BF5C, clamped to 0x7D0) and switches ITSELF off when
        # the wallet can no longer cover it (0x10D9C9). @Continue= carries no
        # field that reports the new figure, so this server has to run the
        # same arithmetic or the panel and the escrow disagree from the first
        # rematch -- the gold divergence flagged earlier and left open.
        _dk = ("#TM0RDU", 3)
        _sv_trg = tablesettings._table_rules_get
        try:
            _du_rules = {"bm": 300, "du": 1}
            tablesettings._table_rules_get = lambda _c, _i: dict(_du_rules)
            pots._TABLE_POT.pop(_dk, None)
            collection._collection_store("duA", {"cards": [], "money": 9000},
                              sync_save=False)
            collection._collection_store("duB", {"cards": [], "money": 9000},
                              sync_save=False)
            _duseats = [("duA", 0), ("duB", 0)]
            if pots._table_pot(_dk[0], _dk[1]) != 300:
                common._say("FAIL: the table pot is seeded from the `bm` rule, got %r"
                     % (pots._table_pot(_dk[0], _dk[1]),)); ok = False
            # ...and it COMPOUNDS across rematches, up to the 2000 clamp. That
            # clamp is the point of the rule: the pot is capped everywhere, so
            # doubling is the only way to climb to the ceiling from below --
            # and it does nothing at all for a wager above 1000.
            _ladder = [pots._double_up_pot(_dk[0], _dk[1], _duseats)
                       for _ in range(4)]
            if _ladder != [600, 1200, 2000, 2000]:
                common._say("FAIL: du doubles the pot each rematch and clamps at "
                     "%d (0x10BF5C/0x10BF69) -- got %r"
                     % (pots._wager_cap(), _ladder)); ok = False
            # `du = 0` never escalates, which is why the rule was inert.
            _du_rules["du"] = 0
            pots._TABLE_POT[_dk] = 300
            if pots._double_up_pot(_dk[0], _dk[1], _duseats) != 300:
                common._say("FAIL: with du=0 a rematch is played for the same pot")
                ok = False
            # WARNING: THE WALLET GATE, 0x10D9C9. A seat that cannot cover the
            # DOUBLED stake turns the rule off -- the client raises its own
            # "can't afford it" dialog on the same compare, so escalating
            # anyway would stake a player into an overdraft their own screen
            # already refused them.
            _du_rules["du"] = 1
            pots._TABLE_POT[_dk] = 300
            collection._collection_store("duB", {"cards": [], "money": 500},
                              sync_save=False)
            if pots._double_up_pot(_dk[0], _dk[1], _duseats) != 300:
                common._say("FAIL: du switches itself off when a seat cannot cover "
                     "the doubled pot (0x10D9C9), got %r"
                     % (pots._TABLE_POT.get(_dk),)); ok = False
            # ...and the escrow takes the ESCALATED pot off every seat, which
            # is the half that used to be missing: a rematch sends no
            # @VsGameInit, so nothing staked it at all.
            collection._collection_store("duB", {"cards": [], "money": 9000},
                              sync_save=False)
            pots._TABLE_POT[_dk] = 300
            _dpot = pots._double_up_pot(_dk[0], _dk[1], _duseats)
            pots._stake_pvp_pot(_dk[0], _dk[1], _duseats, _dpot, "rematch pot")
            if _dpot != 600 or purse.money_of("duA") != 8400                     or purse.money_of("duB") != 8400:
                common._say("FAIL: the rematch escrow takes the doubled pot (600) "
                     "off each seat, got pot %r and %d/%d"
                     % (_dpot, purse.money_of("duA"), purse.money_of("duB"))); ok = False
            if sorted(a for _m, a in (pots._PVP_STAKES.get(_dk) or {}).values())                     != [600, 600]:
                common._say("FAIL: ...and it lands in the same escrow the result "
                     "settles, got %r" % (pots._PVP_STAKES.get(_dk),)); ok = False
            # ...and a FRESH @VsGameInit resets the escalation: du compounds
            # across rematches at one table session, not for ever.
            if pots._table_pot(_dk[0], _dk[1], {"bm": 300}, reset=True) != 300:
                common._say("FAIL: a fresh announcement re-seeds the pot from `bm`")
                ok = False
        finally:
            tablesettings._table_rules_get = _sv_trg
            pots._PVP_STAKES.pop(_dk, None)
            pots._TABLE_POT.pop(_dk, None)
            pots._stake_clear("duA"); pots._stake_clear("duB")
            for _pth in (collection._collection_file("duA"), collection._collection_file("duB")):
                try:
                    if _pth and os.path.exists(_pth):
                        os.remove(_pth)
                except OSError:
                    pass

        # --- the OBSERVER: @Data=/Watch= answers a @WatchInfo snapshot and
        # --- joins the push relay -------------------------------------------
        _wk = ("#TM0RTEST", 9)
        matchmaking._MATCH_ROSTER[pushqueue._push_key("wplA")] = ("#TM0RTEST", 9,
                                            [("wplA", 0), ("wplB", 0)])
        boardrules._MATCH_TURN[_wk] = {"turn": 3, "active": 1, "n": 2}
        boardrules._MATCH_BOARD[_wk] = {5: tmbattle.Card(_rowA, 0)}
        boardrules._MATCH_HANDS[_wk] = {pushqueue._push_key("wplA"): [_rowA],
                             pushqueue._push_key("wplB"): [_rowB]}
        pushqueue._PUSHES.pop(pushqueue._push_key("wobs"), None)
        _wr = dispatch.handle_line(b"43002500@Data=/Watch=0", member_id="wobs",
                          peer_nick=b"UNOTATABLE")
        _wrl = _wr if isinstance(_wr, list) else ([_wr] if _wr else [])
        _wsnap = next((ln for ln in _wrl if b"@WatchInfo=" in ln), None)
        if _wsnap is None or not _wsnap.startswith(b"43002501"):
            common._say("FAIL: @Data=/Watch= must answer @WatchInfo with sh=1 for a "
                 "running match (the arm reads the board only under sh=1), "
                 "got %r" % (_wsnap[:30] if _wsnap else _wrl,)); ok = False
        if _wsnap is not None and (b"/T=3" not in _wsnap
                                   or b"/F=" not in _wsnap):
            common._say("FAIL: the sh=1 snapshot carries the turn and the board "
                 "(/T=, /F=), got %r" % (_wsnap,)); ok = False
        if pushqueue._push_key("wobs") not in (watchers._MATCH_WATCHERS.get(_wk) or {}):
            common._say("FAIL: the observer must join the match's watcher set")
            ok = False
        # ...and the watcher gets BOTH the in-match stream (it reads cmd 9/10/12
        # once its play scene is up) AND @WFCard (the play-scene trigger).
        pushqueue._PUSHES.pop(pushqueue._push_key("wobs"), None)
        watchers._watch_push("#TM0RTEST", 9, b"43000903@TurnData=/A=1", "relay probe")
        if not any(b"@TurnData=" in e[1]
                   for e in (pushqueue._PUSHES.get(pushqueue._push_key("wobs")) or [])):
            common._say("FAIL: _watch_push must relay the in-match stream to a watcher")
            ok = False
        watchers._watch_push("#TM0RTEST", 9, b"43002600@WFCard=/T=0@5/D=1|1|0|1|1|0",
                    "wfcard probe")
        if not any(b"@WFCard=" in e[1]
                   for e in (pushqueue._PUSHES.get(pushqueue._push_key("wobs")) or [])):
            common._say("FAIL: @WFCard (the play-scene trigger) must reach a watcher")
            ok = False
        # ...and @GameExit= removes the watcher.
        dispatch.handle_line(b"41000D00@GameExit=", member_id="wobs",
                    peer_nick=b"UNOTATABLE")
        if pushqueue._push_key("wobs") in (watchers._MATCH_WATCHERS.get(_wk) or {}):
            common._say("FAIL: a departing watcher must leave the watcher set")
            ok = False
        matchmaking._MATCH_ROSTER.pop(pushqueue._push_key("wplA"), None)
        watchers._MATCH_WATCHERS.pop(_wk, None)
        pushqueue._PUSHES.pop(pushqueue._push_key("wobs"), None)
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND):
            _d.pop(_wk, None)

        # --- a VS. COM game is AT A TABLE: the room sees Playing, and an
        # --- observer of that table gets the COM match's snapshot ----------
        #
        # The peer is built the way the client builds one -- the table id
        # xor'd with the client key, base-36'd, scrambled -- so this exercises
        # the real `_peer_table` decode rather than a stub. See `_COM_AT`.
        import polnick as _pn
        _ct_room, _ct_no = 1, 5
        _ct_id = tmroom.canonical_table_id(_ct_no - 1, _ct_room)
        _ct_v = (_ct_id ^ tmroom.client_key()) & 0xFFFFFFFFFF
        _ct_pid = ""
        for _i in range(8):
            _ct_v, _d8 = divmod(_ct_v, 36)
            _ct_pid = _pn.ALPHA[_d8] + _ct_pid
        _ct_peer = _pn.nick_for_polid(_ct_pid).encode("ascii")
        _ct_chan = "#TM0R%03d" % _ct_room
        if matchmaking._peer_table(_ct_peer) != (_ct_chan, _ct_no):
            common._say("FAIL: the selftest's own table peer must decode to "
                 "(%s, %d), got %r"
                 % (_ct_chan, _ct_no, (matchmaking._peer_table(_ct_peer),)))
            ok = False
        vscom._COM_AT.clear()
        _cr = dispatch.handle_line(
            b"41001200@GameENC=/NN=AB12CD31DC3F63B6/HID=0/Dm=0/Vol=0/CN=/HN=43",
            member_id="ctcom", peer_nick=_ct_peer)
        if not _cr or b"@GameECA=" not in (
                _cr if isinstance(_cr, bytes) else b"".join(_cr)):
            common._say("FAIL: @GameENC= must still answer @GameECA=, got %r" % (_cr,))
            ok = False
        if vscom._COM_AT.get((_ct_chan, _ct_no)) != pushqueue._push_key("ctcom"):
            common._say("FAIL: @GameENC= must bind the COM game to the table its "
                 "peer names (_COM_AT = %r)" % (vscom._COM_AT,))
            ok = False
        if not matchmaking._match_running(_ct_chan, _ct_no):
            common._say("FAIL: a COM game at a table must read as a RUNNING match -- "
                 "that is what publishes state 2 (Playing/Observe)")
            ok = False
        if tablerow._occupancy_state(vscom._com_who(_ct_chan, _ct_no), 7, inplay=True) != "2":
            common._say("FAIL: the COM table's row must publish state 2")
            ok = False
        # ...and the observer of THAT table reaches the COM match's state.
        vscom._COM_GAME[pushqueue._push_key("ctcom")]["n"] = 2
        vscom._COM_GAME[pushqueue._push_key("ctcom")]["coms"] = [3]
        _ck = (None, pushqueue._push_key("ctcom"))
        boardrules._MATCH_TURN[_ck] = {"turn": 2, "active": 1, "n": 2}
        boardrules._MATCH_BOARD[_ck] = {4: tmbattle.Card(_rowA, 0)}
        boardrules._MATCH_HANDS[_ck] = {pushqueue._push_key("ctcom"): [_rowA],
                             ("com", 1): [_rowB, _rowB]}
        pushqueue._PUSHES.pop(pushqueue._push_key("cobs"), None)
        _cw = dispatch.handle_line(b"43002500@Data=/Watch=0", member_id="cobs",
                          peer_nick=_ct_peer)
        _cwl = _cw if isinstance(_cw, list) else ([_cw] if _cw else [])
        _csnap = next((ln for ln in _cwl if b"@WatchInfo=" in ln), None)
        if _csnap is None or b"/N=2" not in _csnap or b"/T=2" not in _csnap:
            common._say("FAIL: watching a COM table must answer a two-player sh=1 "
                 "@WatchInfo built from the COM key, got %r"
                 % (_csnap[:60] if _csnap else _cwl,))
            ok = False
        if _csnap is not None and b"/H=1|2" not in _csnap:
            common._say("FAIL: the COM seat's hand count comes from ('com', seat) -- "
                 "expected /H=1|2, got %r" % (_csnap,))
            ok = False
        if pushqueue._push_key("cobs") not in (watchers._MATCH_WATCHERS.get(_ck) or {}):
            common._say("FAIL: a COM table's watcher must be registered under the "
                 "COM MATCH KEY, or no COM push ever reaches them")
            ok = False
        # ...and the machine's own turns relay to that watcher.
        pushqueue._PUSHES.pop(pushqueue._push_key("cobs"), None)
        watchers._watch_push(None, pushqueue._push_key("ctcom"), b"43000902@TurnData=/A=1",
                    "COM relay probe")
        if not any(b"@TurnData=" in e[1]
                   for e in (pushqueue._PUSHES.get(pushqueue._push_key("cobs")) or [])):
            common._say("FAIL: a COM turn must relay to the table's watchers")
            ok = False
        # ...and @GameExit= gives the table back.
        dispatch.handle_line(b"41000D00@GameExit=", member_id="ctcom",
                    peer_nick=_ct_peer)
        if (_ct_chan, _ct_no) in vscom._COM_AT or matchmaking._match_running(_ct_chan, _ct_no):
            common._say("FAIL: @GameExit= must release the COM table -- §15c, the "
                 "tile must not outlive the game")
            ok = False
        vscom._COM_AT.clear()
        vscom._COM_GAME.pop(pushqueue._push_key("ctcom"), None)
        watchers._MATCH_WATCHERS.pop(_ck, None)
        pushqueue._PUSHES.pop(pushqueue._push_key("cobs"), None)
        pushqueue._PUSHES.pop(pushqueue._push_key("ctcom"), None)
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND):
            _d.pop(_ck, None)

        # --- the player's OWN rules ride /R=, and the kill switch works -----
        #
        # `/Rule=` and `/R=` share their first seven slots (builder 0x106F87,
        # parser 0x102DE3 -- DC, E0..E4, D9), so the reply is the client's own
        # list verbatim. Serving `_rule_seven` there was the `@Tet=` order on
        # the wrong slots and overwrote every rule the player picked.
        collection._collection_store("tclmoff", {"cards": [], "money": 1000},
                          sync_save=False)
        _wsaved_com = os.environ.pop("POL_TM_WAGER_COM", None)
        _wecho = dispatch.handle_line(b"43000600@ComGame=/Rule=250|7|6|5|4|3|2|1/Com=1",
                             member_id="tclmoff", peer_nick=b"UTESTPEER")
        _wecho = _wecho if isinstance(_wecho, bytes) else b"".join(_wecho or [])
        if b"/R=250|7|6|5|4|3|2" not in _wecho:
            common._say("FAIL: @ComGameInit= must echo the player's own /Rule= in its "
                 "first seven positions (they are the same slots) -- got %r"
                 % (_wecho,)); ok = False
        vscom._COM_GAME.pop(pushqueue._push_key("tclmoff"), None)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclmoff"), None)

        # ...and POL_TM_WAGER_COM=0 stops the stake moving at all.
        collection._collection_store("tclmoff", {"cards": [], "money": 1000},
                          sync_save=False)
        os.environ["POL_TM_WAGER_COM"] = "0"
        dispatch.handle_line(b"43000600@ComGame=/Rule=250|0|1|1|1|0|0|0/Com=1",
                    member_id="tclmoff", peer_nick=b"UTESTPEER")
        if purse.money_of("tclmoff") != 1000:
            common._say("FAIL: POL_TM_WAGER_COM=0 must move NO gold, got %d"
                 % purse.money_of("tclmoff")); ok = False
        vscom._COM_GAME.pop(pushqueue._push_key("tclmoff"), None)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclmoff"), None)
        collection._collection_store("tclmoff", {"cards": [], "money": 0},
                          sync_save=False)

        # --- the wager money: the WAGER rides /R= occ 0 and settles server-side -
        # WARNING: RUN WITH THE KNOB ON. The mechanism is unchanged and still has to
        # work; only the default moved.
        os.environ["POL_TM_WAGER_COM"] = "1"
        collection._collection_store("tclm", {"cards": [], "money": 1000},
                          sync_save=False)
        _wg = dispatch.handle_line(b"43000600@ComGame=/Rule=250|0|1|1|1|0|0|0/Com=1",
                          member_id="tclm", peer_nick=b"UTESTPEER")
        if not _wg or b"/R=250|" not in _wg:
            common._say("FAIL: the client's /Rule= occ 0 (the wager) must ride /R= "
                 "occ 0 -- it is the POT at 0x52464BC, and /R=0 is why every "
                 "win paid 0; got %r" % (_wg,)); ok = False
        if purse.money_of("tclm") != 750:
            common._say("FAIL: serving a wager must stake it in (the client runs "
                 "wallet -= pot at 0xEC1CA), got %d" % purse.money_of("tclm"))
            ok = False
        boardrules._MATCH_HANDS[_tkm] = {("deck", 0): [_rowA], ("deck", 1): [_rowA]}
        boardrules._MATCH_BOARD[_tkm] = {0: tmbattle.Card(_rowA, 0)}   # human wins
        turns._queue_next_turn(None, pushqueue._push_key("tclm"), [], 10, 0,
                         roster=[("tclm", 0)], n_solo=2)
        # A PERFECT win over /Com=1 with 250 staked pays
        # ((400 - its average rank)//2 + 100) x (250 + 100) x 3 // 100
        # (1575 against the client's table, where /Com=1 is rank 300).
        _hp1 = ((400 - vscom._com_avg_rank(1)) // 2 + 100) * 350 * 3 // 100
        if purse.money_of("tclm") != 750 + _hp1:
            common._say("FAIL: a perfect COM win pays the house prize "
                 "(0xCC719: %d) -- got %d" % (_hp1, purse.money_of("tclm"))); ok = False
        if (vscom._COM_GAME.get(pushqueue._push_key("tclm")) or {}).get("staked") is not None:
            common._say("FAIL: the settlement must clear `staked` (or @GameExit "
                 "double-refunds)"); ok = False
        # ...and the RANKING TALLIES advanced: the result wrote the VS.
        # Rating (average score x100 -- what menus 0/1/2 sort on) and the
        # win's net gil landed in the prize counters (menus 4/5).
        _rk = (collection._collection_load("tclm").get("rank")) or {}
        import tmrank as _trk
        if _rk.get("rating") != _trk.rating_of(_rk):
            common._say("FAIL: a result must write rank.rating = tmrank.rating_of "
                 "(the ranking lists sort on it), got %r" % (_rk,))
            ok = False
        if (_rk.get("tiled_games") != _trk.tiled_games_of(dict(_rk, tiled_games=None))
                or not _rk.get("last_played")):
            common._say("FAIL: a result must stamp tiled_games (the games tiles_total "
                 "covers -- rating_of's denominator) and last_played (the Top "
                 "30 activity rule), got %r" % (_rk,)); ok = False
        if _rk.get("prize_total") != _hp1 or _rk.get("prize_week") != _hp1:
            common._say("FAIL: a wager win must add the net winnings to "
                 "prize_total AND prize_week (the Grand/Weekly Total "
                 "lists), got %r" % (_rk,)); ok = False
        if (collection._collection_load("tclm").get("staked_wager")) is not None:
            common._say("FAIL: the settlement must clear the DURABLE stake record "
                 "too (staked_wager -- the restart-orphan ledger)"); ok = False
        if time.time() < _trk.elo_from() and "elo" in _rk:
            common._say("FAIL: a result before POL_TM_ELO_FROM must not rate, got %r"
                 % (_rk,)); ok = False
        # --- THE HOUSE PRIZE AT STAKE 0 -------------------------------------
        # A broke player vs an opponent of average rank 2.93: the COM's
        # wager clamps to the empty wallet, and the win STILL pays.
        if (vscom._house_prize(0, [293], [2, 1]) != 153
                or vscom._house_prize(0, [293], [3, 0]) != 459
                or vscom._house_prize(0, [300], [2, 1]) != 150
                or vscom._house_prize(0, [100], [2, 1]) != 250
                or vscom._house_prize(0, [293], [1, 2]) != 0
                or vscom._house_prize(300, [293], [2, 2]) != 300
                # 3 players, 1st of 3: mean rank (300+100)/2 = 200 ->
                # 200 x 100 x 2 beaten / 100 = 400; 2nd of 3 beats one.
                or vscom._house_prize(0, [300, 100], [3, 2, 1]) != 400
                or vscom._house_prize(0, [300, 100], [2, 3, 1]) != 200):
            common._say("FAIL: _house_prize must match the guidebook / 0xCC719 -- "
                 "got %r" % ([vscom._house_prize(0, [293], [2, 1]),
                              vscom._house_prize(0, [293], [3, 0]),
                              vscom._house_prize(0, [300, 100], [3, 2, 1])],))
            ok = False
        # Out of range is the placeholder row, table or no table.
        if vscom._com_avg_rank(99) != vscom._com_avg_rank(0) or vscom._com_avg_rank(None) != vscom._com_avg_rank(0):
            common._say("FAIL: _com_avg_rank must fall back to the placeholder row")
            ok = False
        if not vscom._plprm():
            common._say("selftest: services/tmdata/PlPrm.BIN missing -- COM average "
                 "ranks use the default (run tools/tmdata_build.py)")
        _hp2 = ((400 - vscom._com_avg_rank(2)) // 2 + 100) * 100 // 100
        collection._collection_store("tclz", {"cards": [], "money": 0}, sync_save=False)
        dispatch.handle_line(b"43000600@ComGame=/Rule=0|1|1|1|3|0|0|0/Com=2",
                    member_id="tclz", peer_nick=b"UTESTPEER")
        _tkz = (None, pushqueue._push_key("tclz"))
        boardrules._MATCH_HANDS[_tkz] = {("deck", 0): [_rowA], ("deck", 1): [_rowA]}
        boardrules._MATCH_BOARD[_tkz] = {0: tmbattle.Card(_rowA, 0),
                              1: tmbattle.Card(_rowA, 0),
                              2: tmbattle.Card(_rowA, 1)}   # 2-1, not perfect
        pushqueue._PUSHES.pop(pushqueue._push_key("tclz"), None)
        turns._queue_next_turn(None, pushqueue._push_key("tclz"), [], 10, 0,
                         roster=[("tclz", 0)], n_solo=2)
        if purse.money_of("tclz") != _hp2:
            common._say("FAIL: a stake-0 COM win over /Com=2 must pay the %d house "
                 "prize (the 0-gil bootstrap), got %d" % (_hp2, purse.money_of("tclz")))
            ok = False
        _rz = [e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key("tclz")) or [])
               if b"@ResultData=" in e[1]]
        _dz = selftest_tables._pipe_vals(_rz[0], b"/D=") if _rz else []
        if len(_dz) < 2 or int(_dz[1]) != _hp2:
            common._say("FAIL: @ResultData /D= occ 1 must carry the %d the server "
                 "credited (the client's count-up adds it), got %r" % (_hp2, _dz))
            ok = False
        # ...and the kill switch restores the old settlement: nothing at stake 0.
        collection._collection_store("tclz", {"cards": [], "money": 0}, sync_save=False)
        os.environ["POL_TM_HOUSE_PRIZE"] = "0"
        try:
            dispatch.handle_line(b"43000600@ComGame=/Rule=0|1|1|1|3|0|0|0/Com=2",
                        member_id="tclz", peer_nick=b"UTESTPEER")
            boardrules._MATCH_HANDS[_tkz] = {("deck", 0): [_rowA], ("deck", 1): [_rowA]}
            boardrules._MATCH_BOARD[_tkz] = {0: tmbattle.Card(_rowA, 0)}
            turns._queue_next_turn(None, pushqueue._push_key("tclz"), [], 10, 0,
                             roster=[("tclz", 0)], n_solo=2)
            if purse.money_of("tclz") != 0:
                common._say("FAIL: POL_TM_HOUSE_PRIZE=0 must pay nothing at stake 0, "
                     "got %d" % purse.money_of("tclz")); ok = False
        finally:
            os.environ.pop("POL_TM_HOUSE_PRIZE", None)
        for _d in (boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND, boardrules._MATCH_HANDS,
                   rematch._MATCH_CONTINUE, matchstart._CARD_READY):
            _d.pop(_tkz, None)
        vscom._COM_GAME.pop(pushqueue._push_key("tclz"), None)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclz"), None)
        # --- A COM BOARD HONOURS THE PLAYER'S RULES --------------------------
        # /Rule= in COM order is bm,st,cb,ca,tl,du: all three object rules off
        # must deal no special / chance / rotating tile, ever (PLAIN blocks
        # are every board's and no rule removes them); on, they must appear.
        _sv_bo = os.environ.get("POL_TM_BOARD_OBJECTS")
        os.environ["POL_TM_BOARD_OBJECTS"] = "1"     # the suite pins it off
        for _rule, _want_empty in ((b"0|0|0|0|3|0|0|0", True),
                                   (b"0|1|1|1|3|0|0|0", False)):
            collection._collection_store("tcr", {"cards": [], "money": 0}, sync_save=False)
            dispatch.handle_line(b"43000600@ComGame=/Rule=" + _rule + b"/Com=1",
                        member_id="tcr", peer_nick=b"UTESTPEER")
            _objs = [boardrules._match_objects(None, pushqueue._push_key("tcr"), 2, fresh=True)
                     for _ in range(20)]
            _empty = all(tmbattle.object_kind(c) == "plain"
                         for o in _objs for c in o if c)
            if _empty != _want_empty:
                common._say("FAIL: COM board with /Rule=%s must be %s, got %r (rules %r)"
                     % (_rule.decode(), "empty" if _want_empty else "dealt",
                        _objs[:3], tablesettings._com_rules_of(None, pushqueue._push_key("tcr"))))
                ok = False
            boardrules._MATCH_OBJECTS.pop((None, pushqueue._push_key("tcr")), None)
            boardrules._MATCH_BOARD.pop((None, pushqueue._push_key("tcr")), None)
            vscom._COM_GAME.pop(pushqueue._push_key("tcr"), None)
            pushqueue._PUSHES.pop(pushqueue._push_key("tcr"), None)
        if _sv_bo is None:
            os.environ.pop("POL_TM_BOARD_OBJECTS", None)
        else:
            os.environ["POL_TM_BOARD_OBJECTS"] = _sv_bo
        # --- A 10-CARD PACK KEEPS TEN ----------------------------------------
        # The Beginner's Pack deals ten; `@Get=` used to keep the first five.
        _pk10 = [[i, 1, 0, 1, 1, 0, 1, 255] for i in range(10)]
        for _cap, _want in ((None, 10), ("5", 5)):
            collection._collection_store("tpk", {"cards": [], "money": 0}, sync_save=False)
            if _cap:
                os.environ["POL_TM_PACK_KEEP_MAX"] = _cap
            try:
                collection._collection_offer("tpk", _pk10)
                collection._collection_commit_offer("tpk")
            finally:
                os.environ.pop("POL_TM_PACK_KEEP_MAX", None)
            _got = len(collection._collection_load("tpk").get("cards") or [])
            if _got != _want:
                common._say("FAIL: a 10-card pack must keep %d card(s) (cap %s), got %d"
                     % (_want, _cap, _got)); ok = False
        # ...and from POL_TM_ELO_FROM on, the same COM win moves an ELO: the
        # human's rises off the anchor, against /Com=1's fixed rating, and
        # the VS. Rating shown is that Elo.
        _saved_ef = os.environ.get("POL_TM_ELO_FROM")
        os.environ["POL_TM_ELO_FROM"] = "1"
        try:
            collection._collection_store("tclme", {"cards": [], "money": 0}, sync_save=False)
            dispatch.handle_line(b"43000600@ComGame=/Rule=0|0|1|1|1|0|0|0/Com=1",
                        member_id="tclme", peer_nick=b"UTESTPEER")
            _tke = (None, pushqueue._push_key("tclme"))
            boardrules._MATCH_HANDS[_tke] = {("deck", 0): [_rowA], ("deck", 1): [_rowA]}
            boardrules._MATCH_BOARD[_tke] = {0: tmbattle.Card(_rowA, 0)}   # human wins
            turns._queue_next_turn(None, pushqueue._push_key("tclme"), [], 10, 0,
                             roster=[("tclme", 0)], n_solo=2)
            _rke = (collection._collection_load("tclme").get("rank")) or {}
            _want = _trk.elo_match([_trk.ELO_ANCHOR, careerstats._com_elo(1)], [1, 0],
                                   [_trk.elo_k(0), None])[0]
            if (_rke.get("elo_games") != 1 or abs(_rke.get("elo", 0) - round(_want, 2)) > 0.01
                    or _rke.get("rating") != _trk.elo_display(_rke.get("elo", 0))):
                common._say("FAIL: a COM win from POL_TM_ELO_FROM on must store the "
                     "Elo (want %.2f) and show it as the rating, got %r"
                     % (_want, _rke)); ok = False
            # PvP: every seat is a member, and a member in two seats rates no one
            collection._collection_store("tclmp", {"cards": [], "money": 0}, sync_save=False)
            _pv = careerstats._match_elos([("tclme", 0), ("tclmp", 0)], [3, 7], 2)
            if not (set(_pv) == {0, 1} and _pv[1] > _trk.ELO_ANCHOR
                    and _pv[0] < _rke.get("elo", 0)):
                common._say("FAIL: a PvP result must rate both seats, the higher "
                     "score up, got %r" % (_pv,)); ok = False
            if careerstats._match_elos([("tclme", 0), ("tclme", 0)], [3, 7], 2):
                common._say("FAIL: one member in two seats must not rate"); ok = False
        finally:
            if _saved_ef is None:
                os.environ.pop("POL_TM_ELO_FROM", None)
            else:
                os.environ["POL_TM_ELO_FROM"] = _saved_ef
            vscom._COM_GAME.pop(pushqueue._push_key("tclme"), None)
            pushqueue._PUSHES.pop(pushqueue._push_key("tclme"), None)
            for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND):
                _d.pop((None, pushqueue._push_key("tclme")), None)
        # ...and the AVERAGE RANK moved, which is the stat every TITLE in the
        # game is gated on and which had no producer at all before 2026-08-24.
        # A decisive win is a 1st place, so one game must read exactly 1.00.
        if _rk.get("games") == 1 and _rk.get("avg_rank") != 100:
            common._say("FAIL: winning the only game played is an average rank of "
                 "1.00 -- x100, so 100 -- got %r" % (_rk.get("avg_rank"),))
            ok = False
        if _rk.get("place_total") != 100 * (_rk.get("placed") or 0):
            common._say("FAIL: place_total accumulates places ALREADY x100 (one win "
                 "== 100), so the average stays exact across restarts -- "
                 "got %r" % (_rk,)); ok = False
        # WARNING: AND THE DIVISOR IS `placed`, NOT `games`. A result that carries no
        # placement must move neither, or the average is deflated by it -- the
        # bug that put a floored 1.00 on a player who had never placed 1st.
        _rk_av = dict(_rk)
        _av_before = (collection._collection_load("tclm").get("rank") or {}).get("avg_rank")
        careerstats._bump_result_stats("tclm", place=None, score=0)
        _rk_after = (collection._collection_load("tclm").get("rank")) or {}
        if _rk_after.get("avg_rank") != _av_before:
            common._say("FAIL: a result with NO placement must leave the average "
                 "alone -- it moved %r -> %r"
                 % (_av_before, _rk_after.get("avg_rank"))); ok = False
        if _rk_after.get("placed") != _rk_av.get("placed"):
            common._say("FAIL: a result with NO placement must not advance `placed`, "
                 "got %r" % (_rk_after.get("placed"),)); ok = False
        # *** AND IT REACHED THE SAVE. *** The stats above live in the
        # collection JSON, which the CLIENT NEVER READS: Player Data -> Status
        # is drawn from `U/g/TM0DataFile`, and every one of these fields was
        # zero there for as long as this server has existed. This is the
        # delivery assertion, not another ledger one.
        try:
            with open(savefile._save_file("tclm"), "rb") as _sf:
                _sb = _sf.read()
            # The DWORD fields, read as dwords. VS. Player Games is NOT among
            # them -- it is a word, and it is checked in the word block below.
            _got = {o: struct.unpack_from("<I", _sb, o)[0]
                    for o in (tmsave.RATING_OFF, tmsave.AVG_RANK_OFF,
                              tmsave.PRIZE_TOTAL_OFF,
                              tmsave.BIGGEST_PRIZE_OFF)}
            if struct.unpack_from("<H", _sb, tmsave.VS_GAMES_OFF)[0] \
                    != _rk.get("games"):
                common._say("FAIL: the save's VS. Player Games (+0x%X) must carry the "
                     "career count -- got %r for %r games"
                     % (tmsave.VS_GAMES_OFF,
                        struct.unpack_from("<H", _sb, tmsave.VS_GAMES_OFF)[0],
                        _rk.get("games"))); ok = False
            if _got[tmsave.RATING_OFF] != _rk.get("rating"):
                common._say("FAIL: the save's VS. Rating (+0x128) must carry the "
                     "rating the ranking lists sort on -- got %r vs %r"
                     % (_got[tmsave.RATING_OFF], _rk.get("rating")))
                ok = False
            if _got[tmsave.AVG_RANK_OFF] != _rk.get("avg_rank"):
                common._say("FAIL: the save's Average Rank (+0x30) must carry the "
                     "computed average -- got %r vs %r"
                     % (_got[tmsave.AVG_RANK_OFF], _rk.get("avg_rank")))
                ok = False
            if _got[tmsave.PRIZE_TOTAL_OFF] != _rk.get("prize_total"):
                common._say("FAIL: the save's Grand Total (+0x50) must carry the "
                     "prize-money tally -- got %r vs %r"
                     % (_got[tmsave.PRIZE_TOTAL_OFF], _rk.get("prize_total")))
                ok = False
            # ...and the WORD block, which is the half a dword write would
            # have corrupted. Read as words, because that is how the client
            # reads them.
            _w = {o: struct.unpack_from("<H", _sb, o)[0]
                  for o in (tmsave.OPPONENTS_OFF, tmsave.CONSEC_WINS_OFF,
                            tmsave.STREAK_OFF, tmsave.VS_GAMES_OFF)}
            if _w[tmsave.OPPONENTS_OFF] != _rk.get("opponents"):
                common._say("FAIL: the save's opponents-faced (+0x40) must carry the "
                     "tally Average Prize divides by -- got %r vs %r"
                     % (_w[tmsave.OPPONENTS_OFF], _rk.get("opponents")))
                ok = False
            if _rk.get("opponents"):
                _ap = struct.unpack_from("<I", _sb,
                                         tmsave.AVERAGE_PRIZE_OFF)[0]
                _want_ap = _rk.get("prize_total", 0) // _rk["opponents"]
                if _ap != _want_ap:
                    common._say("FAIL: Average Prize (+0x4C) is Grand Total over "
                         "OPPONENTS FACED (0xCC7E4), not over matches -- "
                         "got %r, want %r" % (_ap, _want_ap)); ok = False
            if _w[tmsave.CONSEC_WINS_OFF] != (_rk.get("consec_wins", 0)
                                              & 0xFFFF):
                common._say("FAIL: Consecutive Wins (+0x78) must store SIGNED, two's "
                     "complement, as 0xCC68A writes it -- got %#x vs %r"
                     % (_w[tmsave.CONSEC_WINS_OFF], _rk.get("consec_wins")))
                ok = False
            # WARNING: THE ONE THAT CATCHES THE OLD BUG: a dword write at +0x78 would
            # have zeroed this. It must survive a save the streak also touched.
            if _w[tmsave.STREAK_OFF] != _rk.get("streak"):
                common._say("FAIL: Winning Streak (+0x7A) must SURVIVE the write to "
                     "its neighbour +0x78 -- a dword write there destroys it "
                     "-- got %r vs %r"
                     % (_w[tmsave.STREAK_OFF], _rk.get("streak"))); ok = False
            if _got[tmsave.BIGGEST_PRIZE_OFF] != _rk.get("biggest_prize"):
                common._say("FAIL: Biggest Prize (+0x48) is a running max over the "
                     "same amount Grand Total sums -- got %r vs %r"
                     % (_got[tmsave.BIGGEST_PRIZE_OFF],
                        _rk.get("biggest_prize"))); ok = False
        except OSError as _se:
            common._say("FAIL: the result path must WRITE the save's stat block "
                 "(%r) -- the numbers exist only where the client cannot see "
                 "them otherwise" % (_se,)); ok = False
        # THE TIE RULE, measured off the client's own average-rank arithmetic at
        # 0xCC814: `mean((ring[i] + 2) * 50)`, whose scale makes the stored byte
        # `2 * (place - 1)` -- so the odd values are DRAWS and a two-player draw
        # is 1.50, not a shared 1st. FRACTIONAL ranking, x100.
        if matchend._placements([3, 3]) != [150, 150]:
            common._say("FAIL: a two-player draw is 1.50 -- the client's ring stores "
                 "the odd value for it (0xCC81E) -- got %r"
                 % (matchend._placements([3, 3]),)); ok = False
        if matchend._placements([5, 1]) != [100, 200]:
            common._say("FAIL: a decisive two-player match is 1.00 / 2.00, got %r"
                 % (matchend._placements([5, 1]),)); ok = False
        if matchend._placements([1, 5, 3]) != [300, 100, 200]:
            common._say("FAIL: places are 1-based, x100, ordered by score, got %r"
                 % (matchend._placements([1, 5, 3]),)); ok = False
        if matchend._placements([5, 5, 1]) != [150, 150, 300]:
            common._say("FAIL: a shared 1st/2nd is 1.50 each and the loser is 3.00 "
                 "(FRACTIONAL ranking -- NOT the ranking lists' competition "
                 "rule), got %r" % (matchend._placements([5, 5, 1]),)); ok = False
        # ...and the floor. The client's own arithmetic cannot go below 1.00,
        # so nothing here may either -- 0 is what an unwritten save reads as.
        if min(matchend._placements([9, 9, 9])) < 100:
            common._say("FAIL: 1.00 is the floor of this scale, got %r"
                 % (matchend._placements([9, 9, 9]),)); ok = False
        # MOST COMBOS: the chain size one placement took. Counted by owner
        # DIFF, so it cannot drift from however the arms above apply flips.
        _cb = ("#TM0RCOMBO", 1)
        scoring._MATCH_COMBO.pop(_cb, None)
        # The board AFTER: tiles 0 and 1 now belong to seat 0, and `before` says
        # they belonged to seat 1 -- i.e. this placement turned both.
        _bd = {0: tmbattle.Card(_rowA, 0), 1: tmbattle.Card(_rowA, 0),
               2: tmbattle.Card(_rowA, 0)}
        scoring._note_combo(_cb[0], _cb[1], 0, _bd, {0: 1, 1: 1}, 2)
        if (scoring._MATCH_COMBO.get(_cb) or {}).get(0) != 2:
            common._say("FAIL: a placement that turned TWO enemy cards is a chain of "
                 "2 -- got %r" % ((scoring._MATCH_COMBO.get(_cb) or {}).get(0),))
            ok = False
        # ...and it is a MAX, not a sum: a later, smaller chain must not lower it.
        scoring._note_combo(_cb[0], _cb[1], 0, {0: tmbattle.Card(_rowA, 0)}, {0: 1}, 9)
        if (scoring._MATCH_COMBO.get(_cb) or {}).get(0) != 2:
            common._say("FAIL: Most Combos is a career/match MAX (0xD029B), so a "
                 "smaller later chain must not lower it -- got %r"
                 % ((scoring._MATCH_COMBO.get(_cb) or {}).get(0),)); ok = False
        # ...and the PLACED card is never counted as part of its own chain.
        scoring._MATCH_COMBO.pop(_cb, None)
        scoring._note_combo(_cb[0], _cb[1], 0, {5: tmbattle.Card(_rowA, 0)}, {}, 5)
        if scoring._MATCH_COMBO.get(_cb):
            common._say("FAIL: a placement that took NOTHING is not a combo -- the "
                 "placed card must never count as its own chain, got %r"
                 % (scoring._MATCH_COMBO.get(_cb),)); ok = False
        scoring._MATCH_COMBO.pop(_cb, None)
        # THE RESULT BYTE, from the OTHER end: `_placements` derives the place
        # from the scores, `_result_code` turns it back into the byte the client
        # builds by summing 0/1/2 per opponent (0xCC5C7). The two must agree, or
        # one of the two derivations is wrong.
        for _sc, _want in (([5, 1], [0, 2]), ([3, 3], [1, 1]),
                           ([5, 5, 1], [1, 1, 4]), ([9, 9, 9], [2, 2, 2]),
                           ([1, 5, 3], [4, 0, 2])):
            _got = [matchend._result_code(p) for p in matchend._placements(_sc)]
            if _got != _want:
                common._say("FAIL: scores %r must give result bytes %r (win 0 / draw 1 "
                     "/ loss 2 PER OPPONENT, 0xCC5C7) -- got %r"
                     % (_sc, _want, _got)); ok = False
        # THE STREAK MACHINE, arm by arm, against 0xCC643..0xCC69C.
        for _cur, _res, _want, _why in (
                (0, 0, 1, "a win from nothing starts a run"),
                (3, 0, 4, "a win extends a run"),
                (-2, 0, 1, "a win BREAKS a losing run and restarts at 1 "
                           "(0xCC668 `mov cx,1`), it does not resume at -1"),
                (5, 2, 0, "result 2 RESETS to zero (0xCC651), it does not "
                          "start a losing run -- in a 2-player match that is "
                          "the loss case"),
                (0, 4, -1, "a heavy loss from nothing goes to -1"),
                (4, 4, -1, "...and breaks a winning run to -1 (0xCC68A "
                           "`mov word, 0xFFFF`), not to 3"),
                (-2, 4, -3, "...and deepens an existing losing run")):
            _got = matchend._streak_next(_cur, _res)
            if _got != _want:
                common._say("FAIL: streak(%r, result=%r) must be %r -- %s -- got %r"
                     % (_cur, _res, _want, _why, _got)); ok = False
        # ...and an ABORT refunds an unsettled stake, once.
        dispatch.handle_line(b"43000600@ComGame=/Rule=250|0|1|1|1|0|0|0/Com=1",
                    member_id="tclm", peer_nick=b"UTESTPEER")
        if (collection._collection_load("tclm").get("staked_wager")) != 250:
            common._say("FAIL: a stake-in must persist staked_wager in the "
                 "collection (restarts ate two stakes on 2026-08-22)")
            ok = False
        _wab = purse.money_of("tclm")                              # staked again
        dispatch.handle_line(b"41000D00@GameExit=", member_id="tclm",
                    peer_nick=b"UTESTPEER")
        if purse.money_of("tclm") != _wab + 250:
            common._say("FAIL: @GameExit before a result must refund the stake "
                 "(mirror of 0xBC701), got %d" % purse.money_of("tclm")); ok = False
        if (collection._collection_load("tclm").get("staked_wager")) is not None:
            common._say("FAIL: the abort refund must clear staked_wager"); ok = False
        # ...and a stake only the COLLECTION remembers (the restart case) is
        # refunded when the next game begins.
        vscom._COM_GAME.pop(pushqueue._push_key("tclm"), None)
        _cd = collection._collection_load("tclm")
        _cd["staked_wager"] = 77
        collection._collection_store("tclm", _cd, sync_save=False)
        _worph = purse.money_of("tclm")
        dispatch.handle_line(b"41001200@GameENC=/NN=0", member_id="tclm",
                    peer_nick=b"UTESTPEER")
        if purse.money_of("tclm") != _worph + 77:
            common._say("FAIL: a new @GameENC= must refund an ORPHANED stake (a "
                 "restart wiped the match that held it), got %d vs %d+77"
                 % (purse.money_of("tclm"), _worph)); ok = False
        if (collection._collection_load("tclm").get("staked_wager")) is not None:
            common._say("FAIL: the orphan refund must clear staked_wager"); ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        pushqueue._PUSHES.pop(pushqueue._push_key("tclm"), None)
        boardrules._MATCH_OBJECTS.clear()
        for _d in (boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, boardrules._MATCH_TURN, matchstart._TURN_RAND,
                   rematch._MATCH_CONTINUE, matchstart._CARD_READY):
            _d.pop(_tkm, None)
        vscom._COM_GAME.pop(pushqueue._push_key("tclm"), None)
        for _pth in (collection._collection_file("tclm"), collection._save_resource_file("tclm")):
            try:
                if _pth and os.path.exists(_pth):
                    os.remove(_pth)
            except OSError:
                pass

        # ...and put the COM-wager knob back the way it was found.
        if _wsaved_com is None:
            os.environ.pop("POL_TM_WAGER_COM", None)
        else:
            os.environ["POL_TM_WAGER_COM"] = _wsaved_com

        # --- the ring-flood gate: no reservation re-feed into a running match,
        # --- and @GameExit= releases the gate ---------------------------------
        # Measured 16:16Z: one re-feed per @Pong filled the client's global
        # receive ring mid-COM-game (store_count=24) and the COM's @PutCard
        # was dropped on arrival -- "the com seems to have stopped".
        boardrules._MATCH_TURN[(None, pushqueue._push_key("comtest"))] = {"turn": 1, "active": 1,
                                                     "n": 2}
        if reservation._reservation_reply("comtest") is not None:
            common._say("FAIL: the @Pong reservation re-feed must be SUPPRESSED while "
                 "a VS. COM match is running -- each one eats a ring slot the "
                 "match scene never frees"); ok = False
        dispatch.handle_line(b"41000D00@GameExit=", member_id="comtest",
                    peer_nick=b"UTESTPEER")
        if (None, pushqueue._push_key("comtest")) in boardrules._MATCH_TURN:
            common._say("FAIL: @GameExit= must clear the COM match key, or the "
                 "re-feed gate stays closed at the table for ever (+0x108 "
                 "reborn)"); ok = False
        pushqueue._PUSHES.pop(pushqueue._push_key("comtest"), None)
        for _k in ((None, None), _ckey):
            matchstart._CARD_READY.pop(_k, None)
            boardrules._MATCH_HANDS.pop(_k, None)
            boardrules._MATCH_TURN.pop(_k, None)
            matchstart._TURN_RAND.pop(_k, None)
            boardrules._MATCH_BOARD.pop(_k, None)
            boardrules._MATCH_OBJECTS.pop(_k, None)
            rematch._MATCH_CONTINUE.pop(_k, None)
        # --- the PS2 VS. COM entry: a bare `@Req=` must seed the board -------
        # `@GameENC=` does not exist in TMaster.pex, so `@Req=` is the console's
        # only trigger for `@ComGameInit`. Without this the client polls
        # (0x43, 2) and dies with -1-37338. The gate is MATCH CONTEXT: with a
        # COM game already set up (what the PC's `@GameENC=` does) it must NOT
        # fire again.
        _rq_key = pushqueue._push_key(987654321)
        vscom._COM_GAME.pop(_rq_key, None)
        matchmaking._MATCH_ROSTER.pop(_rq_key, None)
        pushqueue._PUSHES.pop(_rq_key, None)
        _rq = dispatch.handle_line(b"43000101@Req=/NN=0000000000000000/HID=0/Dm=0/Vol=0"
                          b"/CN=/HN=", member_id=987654321, peer_nick=b"UTESTPEER")
        if not _rq or b"@PLAYACK=/EN=1" not in _rq:
            common._say("FAIL: @Req= must still answer @PLAYACK=/EN=1, got %r" % (_rq,))
            ok = False
        _rq_q = [e[1] for e in (pushqueue._PUSHES.get(_rq_key) or [])
                 if b"@ComGameInit=" in e[1]]
        if not _rq_q:
            common._say("FAIL: a bare @Req= must queue @ComGameInit -- the PS2 has no "
                 "@GameENC= and polls (0x43, 2) until -1-37338. Queue was %r"
                 % ([e[1] for e in (pushqueue._PUSHES.get(_rq_key) or [])],)); ok = False
        elif not _rq_q[0].startswith(protocol.encode_code(protocol.COMGAME_MSGID)):
            common._say("FAIL: the @Req=-triggered @ComGameInit must be addressed "
                 "(0x43, 2); got %r" % (_rq_q[0][:8],)); ok = False
        # ...and the leave ack must be pre-loaded into (0x43, 5). The console
        # never SENDS @Quit (one xref in TMaster.pex, the receive arm), so
        # without this the leave times out with -1-37339.
        _rq_v = [e[1] for e in (pushqueue._PUSHES.get(_rq_key) or []) if b"@Quit=" in e[1]]
        if not _rq_v:
            common._say("FAIL: VS. COM entry must also pre-load @Quit -- CQMComConfig "
                 "state 4 polls (0x43, 5) on leave and raises 339 on timeout. "
                 "Queue was %r" % ([e[1] for e in (pushqueue._PUSHES.get(_rq_key) or [])],))
            ok = False
        elif not _rq_v[0].startswith(protocol.encode_code(protocol.COMQUIT_MSGID)):
            common._say("FAIL: the leave ack must be addressed (0x43, 5); got %r"
                 % (_rq_v[0][:8],)); ok = False
        elif b"/No=" not in _rq_v[0] or b"/B=" not in _rq_v[0]:
            common._say("FAIL: arm 0x103039 reads /No= and /B=; got %r" % (_rq_v[0],))
            ok = False
        # ...and the part ack into (0x41, 0x0E): CComPart state 1 polls it while
        # leaving and parks at selector 182 without it, so 0x03001f57 is never
        # posted and the parent's progress bar stays clamped at 80.
        _rq_e = [e[1] for e in (pushqueue._PUSHES.get(_rq_key) or []) if b"@GameEA=" in e[1]]
        if not _rq_e:
            common._say("FAIL: VS. COM entry must also pre-load @GameEA -- CComPart "
                 "state 1 polls (0x41, 0x0E). Queue was %r"
                 % ([e[1] for e in (pushqueue._PUSHES.get(_rq_key) or [])],)); ok = False
        elif not _rq_e[0].startswith(protocol.encode_code(matchmaking.GAMEEXIT_ANS_MSGID)):
            common._say("FAIL: the part ack must be addressed (0x41, 0x0E); got %r"
                 % (_rq_e[0][:8],)); ok = False
        elif b"/Exit=" not in _rq_e[0]:
            common._say("FAIL: 0x003b0cdc reads /Exit= (0x00486C80), not /EN=; got %r"
                 % (_rq_e[0],)); ok = False
        # ...and it must NOT fire a second time now a COM game exists.
        pushqueue._PUSHES.pop(_rq_key, None)
        dispatch.handle_line(b"43000101@Req=/NN=0/HID=0/Dm=0/Vol=0/CN=/HN=",
                    member_id=987654321, peer_nick=b"UTESTPEER")
        if any(b"@ComGameInit=" in e[1] for e in (pushqueue._PUSHES.get(_rq_key) or [])):
            common._say("FAIL: @Req= must not re-queue @ComGameInit once a COM game is "
                 "set up -- that is the PC path, where @GameENC= already did it")
            ok = False
        pushqueue._PUSHES.pop(_rq_key, None)
        vscom._COM_GAME.pop(_rq_key, None)
        matchmaking._MATCH_PEER.pop(_rq_key, None)

        vscom._COM_GAME.clear(); vscom._COM_GAME.update(_svcom)
        matchmaking._MATCH_PEER.clear(); matchmaking._MATCH_PEER.update(_svpeer)
        # comtest staked a wager in the roster test and the @GameExit= above
        # refunded it -- both wrote a collection file for a synthetic member;
        # remove it AFTER the last handle_line that can re-create it.
        for _pth in (collection._collection_file("comtest"),
                     collection._save_resource_file("comtest")):
            try:
                if _pth and os.path.exists(_pth):
                    os.remove(_pth)
            except OSError:
                pass

        # --- @Quit= answers in the vocabulary of the code it arrived on -------
        # The live 10:58:54Z bug: a MATCH quit was answered with the CARD SHOP's
        # `@EQuit`, which arm 0x103039 cannot name-match.
        mq = dispatch.handle_line(b"43000508@Quit=/No=0", member_id=None)
        if not mq or not mq.startswith(b"43000508"):
            common._say("FAIL: a match @Quit= must be answered in its own slot, got %r"
                 % (mq,)); ok = False
        if not mq or b"@Quit=" not in mq or b"@EQuit=" in mq:
            common._say("FAIL: code 0x43's quit answer is `@Quit` (0x103039); `@EQuit` "
                 "is 0xB2's and cannot name-match here. Got %r" % (mq,))
            ok = False
        if not mq or b"/No=" not in mq or b"/B=" not in mq:
            common._say("FAIL: 0x103039 reads /No= and /B=, got %r" % (mq,)); ok = False
        # ...and the card shop's is a BUILD SPLIT, so pin BOTH sides of the
        # knob. `POL_TM_SHOPQUIT_PS2` defaults to 1 (the console vocabulary,
        # `@Quit=/N=/D=`); 0 restores the PC's `@EQuit=`. Asserting only the PC
        # shape left this selftest RED from 9e3f4ba3 until 2026-09-08, which is
        # exactly as good as having no gate at all.
        os.environ["POL_TM_SHOPQUIT_PS2"] = "0"
        sq = dispatch.handle_line(b"B2001701@Quit=/No=1", member_id=None)
        del os.environ["POL_TM_SHOPQUIT_PS2"]
        if not sq or b"@EQuit=" not in sq:
            common._say("FAIL: with POL_TM_SHOPQUIT_PS2=0 the SHOP quit must answer "
                 "@EQuit=, got %r" % (sq,))
            ok = False
        sq2 = dispatch.handle_line(b"B2001701@Quit=/No=1", member_id=None)
        if not sq2 or b"@Quit=/N=" not in sq2 or b"@EQuit=" in sq2:
            common._say("FAIL: the DEFAULT shop quit is the PS2's @Quit=/N=/D= -- "
                 "@EQuit is absent from TMaster.pex and hangs the shop. "
                 "Got %r" % (sq2,))
            ok = False

        # --- the alone guard, and it must be OURS, not the client's -----------
        mid = 0x860FB3E2A2
        tmroom.note_member(mid, ["0", "0", "0", "0", "T", "2", "0", "0", "0",
                                 "0x0000002000000001", "T"])
        seating._SEATED["#TM0R001"] = {1: [(mid, 1)]}
        pushqueue._PUSHES.clear()
        vscom._queue_vsgameinit(mid)
        if pushqueue._PUSHES:
            common._say("FAIL: a one-seat table must not be sent @VsGameInit -- /N=1 "
                 "hits 0x1024D5 and prints StartAloneERROR"); ok = False
        seating._SEATED["#TM0R001"] = {1: [(mid, 1), (mid + 1, 2)]}
        matchmaking._remember_match("#TM0R001", 1, [(mid, 1), (mid + 1, 2)])
        # WARNING: ONE ACCEPT IS NOT A GAME. Measured 2026-08-20T10:55Z: the guest
        # accepted first, was handed the board 2 s later while the host had not
        # accepted, and never opened it -- see `_MATCH_ACCEPTS`.
        vscom._queue_vsgameinit(mid)
        if pushqueue._PUSHES:
            common._say("FAIL: the board must NOT go out on the first accept -- it "
                 "lands in a scene that does not exist yet. Got %r" % (pushqueue._PUSHES,))
            ok = False
        # ...and the last accept starts it for EVERYONE, each from their own seat.
        vscom._queue_vsgameinit(mid + 1)
        for who, seat in ((mid, 0), (mid + 1, 1)):
            queued = [e[1] for e in pushqueue._PUSHES.get(who, [])]
            body = next((b for b in queued if b.startswith(b"43000100")), None)
            if body is None:
                common._say("FAIL: the last accept must hand the board to every seat; "
                     "member %s got %r" % (who, queued)); ok = False
                continue
            ids = selftest_tables._pipe_vals(body, b"/ID=")
            if ids[0] != b"%d" % common._env_int("POL_TM_VSGAME_SELFID", 0):
                common._say("FAIL: member %s (table seat %d) must still lead its OWN "
                     "copy -- got %r" % (who, seat, ids)); ok = False
        # WARNING: THE LIVE FAILURE OF 2026-08-20T10:43Z, AS A TEST. Entering a match
        # PARTs the room, so `release_seats` empties `_SEATED` BEFORE `@GameOK=`
        # arrives and both players got "seated at no table". The announced-match
        # snapshot is what has to carry it; assert against the state as it
        # actually is at that moment -- seats gone, room gone.
        matchmaking._remember_match("#TM0R001", 1, [(mid, 1), (mid + 1, 2)])
        seating._SEATED.clear()
        pushqueue._PUSHES.clear()
        tmroom.forget_member(mid)
        vscom._queue_vsgameinit(mid)
        vscom._queue_vsgameinit(mid + 1)
        after_part = [e[1] for e in pushqueue._PUSHES.get(mid, [])]
        if not any(b.startswith(b"43000100") for b in after_part):
            common._say("FAIL: @VsGameInit must survive the PART that entering a match "
                 "performs -- _SEATED is empty and room_of() is None by the time "
                 "@GameOK= lands. Got %r" % (after_part,)); ok = False
        # And it must still be built from THIS member's point of view.
        body = next((b for b in after_part if b.startswith(b"43000100")), b"")
        if b"/N=2" not in body:
            common._say("FAIL: the remembered roster must keep both seats, got %r"
                 % (body,)); ok = False
        # WARNING: THE HOLD-BACK ASSERTION USED TO LIVE HERE AND IS NOW WRONG ON
        # PURPOSE. "Do not arrive before the scene exists" is satisfied by
        # waiting for the last accept, and a reply of extra delay measurably
        # cost 19 s between the two boards (2026-08-20T11:04Z). What has to hold
        # instead is that the board never goes out on anything BUT the last
        # accept -- asserted above -- and that every seat is queued together.
        for _who in (mid, mid + 1):
            if not pushqueue._PUSHES.get(_who):
                common._say("FAIL: both seats must be queued at the same moment, "
                     "member %s has nothing" % (_who,)); ok = False

        # --- a push must ride a reply on the RIGHT BAND -------------------
        # WARNING: Measured 2026-08-20: the SAME board to the SAME member was read
        # when it rode a reply to the TABLE peer (11:22:40, UE7QN1N9G) and
        # invisible when it rode one to the ROOM peer (11:30:54, UKXDBA266) --
        # 130,000 further trace lines with no `---->Recv=` at all. A correct
        # message on the wrong peer is as unread as no message.
        pushqueue._PUSHES.clear()
        pushqueue._queue_push(mid, b"43000100@Board", "band test", after=0,
                    peer=b"UE7QN1N9G")
        if pushqueue._pending_pushes(mid, b"UKXDBA266"):
            common._say("FAIL: a board pinned to the table peer must NOT ride a reply "
                 "on the room peer"); ok = False
        if pushqueue._pending_pushes(mid, b"UE7QN1N9G") != [b"43000100@Board"]:
            common._say("FAIL: it MUST ride a reply on its own peer"); ok = False
        # ...and it must not be lost by the miss: it is still queued.
        pushqueue._PUSHES.clear()
        pushqueue._queue_push(mid, b"43000100@Board", "band test", after=0,
                    peer=b"UE7QN1N9G")
        pushqueue._pending_pushes(mid, b"UKXDBA266")
        if not pushqueue._PUSHES.get(pushqueue._push_key(mid)):
            common._say("FAIL: a wrong-band reply must LEAVE the push queued, not "
                 "drop it"); ok = False
        # An unpinned push still rides anything, which is every other caller.
        pushqueue._PUSHES.clear()
        pushqueue._queue_push(mid, b"13000000@Info", "unpinned", after=0)
        if pushqueue._pending_pushes(mid, b"ANYPEER") != [b"13000000@Info"]:
            common._say("FAIL: an unpinned push must ride any reply"); ok = False
        pushqueue._PUSHES.clear()
        # --- requeue: a pop is not a delivery (09-02 review H3) ------------
        # The consumer hands back what it could not send; the entries must
        # come out again on the next drain, in order, without duplicating.
        pushqueue._queue_push(mid, b"43000800@StartData=/S=0", "requeue test", after=0)
        pushqueue._queue_push(mid, b"43000900@TurnData=/A=0", "requeue test", after=0)
        _rq = pushqueue.idle_pushes(mid, None)
        if len(_rq) != 2:
            common._say("FAIL: requeue test setup expected 2 due, got %r" % (_rq,))
            ok = False
        pushqueue.requeue_pushes(mid, _rq[1:], why="selftest simulated send failure")
        _rq2 = pushqueue.idle_pushes(mid, None)
        if [b for _p, b in _rq2] != [b"43000900@TurnData=/A=0"]:
            common._say("FAIL: an undelivered push must come back out on the next "
                 "drain; got %r" % (_rq2,)); ok = False
        if pushqueue.idle_pushes(mid, None):
            common._say("FAIL: a requeued push must not duplicate"); ok = False
        # And with POL_TM_PUSH_REQUEUE=0 the old drop-on-failure returns.
        os.environ["POL_TM_PUSH_REQUEUE"] = "0"
        pushqueue.requeue_pushes(mid, _rq, why="disabled")
        if pushqueue.idle_pushes(mid, None):
            common._say("FAIL: POL_TM_PUSH_REQUEUE=0 must restore drop-on-failure")
            ok = False
        os.environ.pop("POL_TM_PUSH_REQUEUE", None)
        pushqueue._PUSHES.clear()

        # --- @StartData: the deal, and /S= must be per recipient -----------
        # Measured 2026-08-20T13:38Z: with /Ok=1 the scene reached state 0x19
        # and began polling 7 then 8 (0xC28C8). Command 8 is @StartData.
        _seatsD = [(mid, 1), (mid + 1, 2)]
        _sd0 = matchstart._startdata_body(_seatsD, 0, 0)
        _sd1 = matchstart._startdata_body(_seatsD, 1, 0)
        if selftest_tables._pipe_vals(_sd0, b"/F=") != [b"0"] * 16:
            common._say("FAIL: @StartData carries one /F= per tile, 16 for two players "
                 "(0x23359E[N-2]); got %r" % (selftest_tables._pipe_vals(_sd0, b"/F="),))
            ok = False
        if len(selftest_tables._pipe_vals(_sd0, b"/L=")) != 2:
            common._say("FAIL: /L= is per player, got %r" % (selftest_tables._pipe_vals(_sd0, b"/L="),))
            ok = False
        # WARNING: /S= is run through the seat rotation at 0x10354E, and our copies are
        # recipient-first -- so the SAME starter must be a different number in
        # each copy, or both clients believe they lead.
        if selftest_tables._pipe_vals(_sd0, b"/S=") != [b"0"] or selftest_tables._pipe_vals(_sd1, b"/S=") != [b"1"]:
            common._say("FAIL: with table seat 0 starting, its own copy must say /S=0 "
                 "and the other /S=1 -- got %r / %r"
                 % (selftest_tables._pipe_vals(_sd0, b"/S="), selftest_tables._pipe_vals(_sd1, b"/S=")))
            ok = False

        # --- THE BOARD OBJECTS ---------------------------------------------
        # VERIFIED: 2026-09-06. `/F=`, `/B=` and `/E=` are the board: blocks, chance
        # blocks, rotating blocks and special tiles, all three rules at once.
        # See `tmbattle.roll_board` for where every number comes from.
        _bk = ("#TM0OBJ", 77)
        # The suite pins the board OFF (see `selftest`); this is the one test
        # that is about it, so it owns the knob for its own duration.
        os.environ["POL_TM_BOARD_OBJECTS"] = "1"
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS.pop(_bk, None)
        tablesettings._TABLE_SETTINGS[_bk] = {"st": 1, "cb": 1, "ca": 1}
        _codesB = boardrules._match_objects(_bk[0], _bk[1], 2, fresh=True)
        if len(_codesB) != 16:
            common._say("FAIL: a two-player board is 16 tiles (0x23359E), got %d"
                 % len(_codesB)); ok = False
        _sdB = matchstart._startdata_body(_seatsD, 0, 0, codes=_codesB)
        if [int(v) for v in selftest_tables._pipe_vals(_sdB, b"/F=")] != list(_codesB):
            common._say("FAIL: @StartData /F= is the object-code array VERBATIM -- "
                 "0x10360F stores the byte with no mask; got %r for %r"
                 % (selftest_tables._pipe_vals(_sdB, b"/F="), _codesB)); ok = False
        _bB = int((selftest_tables._pipe_vals(_sdB, b"/B=") or [b"-1"])[0])
        _eB = int((selftest_tables._pipe_vals(_sdB, b"/E=") or [b"-1"])[0])
        if (_bB, _eB) != tmbattle.board_counts(_codesB):
            common._say("FAIL: /B= and /E= are DERIVED from /F=, got (%d, %d) for %r"
                 % (_bB, _eB, _codesB)); ok = False
        if _bB + _eB != sum(1 for c in _codesB if c):
            # WARNING: THE CRASH GUARD. The client's reveal loop divides by the
            # number of tiles still carrying a code; a count that outruns the
            # array reaches `idiv ecx` with ecx = 0 and kills the client.
            common._say("FAIL: /B= + /E= must account for EVERY non-empty tile or the "
                 "client's reveal loop divides by zero -- %d + %d vs %r"
                 % (_bB, _eB, _codesB)); ok = False
        # A BLOCK OCCUPIES ITS TILE; A SPECIAL TILE DOES NOT. That asymmetry is
        # the client's (0xC7015/0xC703C plays a card ON a special tile), and it
        # is what keeps the COM's `t not in board` pick honest.
        _boardB = boardrules._MATCH_BOARD.get(_bk) or {}
        for _t, _c in enumerate(_codesB):
            _kind = tmbattle.object_kind(_c)
            if _kind in ("plain", "chance", "rotating") and _t not in _boardB:
                common._say("FAIL: a %s block must occupy tile %d" % (_kind, _t))
                ok = False
                break
            if _kind in (None, "special") and _t in _boardB:
                common._say("FAIL: tile %d is %r and must stay PLAYABLE -- a special "
                     "tile is a property of a free tile, not an occupant"
                     % (_t, _kind))
                ok = False
                break
        if scoring._board_scores(_bk[0], _bk[1], 2) != [0, 0]:
            common._say("FAIL: a block belongs to nobody (owner 4) and must score for "
                 "nobody -- got %r" % (scoring._board_scores(_bk[0], _bk[1], 2),))
            ok = False
        # ...and with every rule OFF the same table deals plain blocks only.
        tablesettings._TABLE_SETTINGS[_bk] = {"st": 0, "cb": 0, "ca": 0}
        _plainB = set()
        for _ in range(60):
            _plainB |= {c for c in boardrules._match_objects(_bk[0], _bk[1], 2, fresh=True)
                        if c}
        if not _plainB <= set(tmbattle.PLAIN_CODES):
            common._say("FAIL: st=cb=ca=0 must deal plain blocks only (0xC0F40 / "
                 "0xC0F72 / 0xC1051 each gate their own family) -- got %r"
                 % (sorted(_plainB),)); ok = False
        # WARNING: POL_TM_START_F AND `_MATCH_BOARD` MUST BE THE SAME BOARD. This is
        # the regression test for 2026-09-07T00:16Z, the first live game with
        # objects on: the knob was read in `_startdata_body` only, so `/F=` went
        # out as the explicitly authored board while `_match_objects` rolled a
        # DIFFERENT one and seeded the server's model from it. Blocks on tiles
        # 1/4/6/9/11/14 on screen, 4/8/9/11/13 in the server -- and every
        # consumer downstream answering about a board nobody could see.
        os.environ["POL_TM_START_F"] = "0|0|6|0|8|0|9|3"
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS.pop(_bk, None)
        _codesF = boardrules._match_objects(_bk[0], _bk[1], 2, fresh=True)
        _sdF = matchstart._startdata_body(_seatsD, 0, 0, codes=_codesF)
        os.environ.pop("POL_TM_START_F", None)
        _wantF = [0, 0, 6, 0, 8, 0, 9, 3] + [0] * 8
        if [int(v) for v in selftest_tables._pipe_vals(_sdF, b"/F=")] != _wantF:
            common._say("FAIL: POL_TM_START_F must serve the board it names, padded "
                 "to the tile count -- got %r" % (selftest_tables._pipe_vals(_sdF, b"/F="),))
            ok = False
        if list(_codesF) != _wantF:
            common._say("FAIL: the FORCED board must also be the match's own board, "
                 "got %r" % (_codesF,))
            ok = False
        # ...and the seeded board must agree with the wire, tile for tile.
        _bdF = boardrules._MATCH_BOARD.get(_bk) or {}
        for _t, _cF in enumerate(_wantF):
            _occ = tmbattle.object_card(_cF) is not None
            if _occ != (_t in _bdF):
                common._say("FAIL: THE DIVERGENCE. Tile %d is code %d (%s) on the wire "
                     "but %s in `_MATCH_BOARD` -- the server would be playing a "
                     "different board than the client can see"
                     % (_t, _cF, tmbattle.object_kind(_cF) or "empty",
                        "occupied" if _t in _bdF else "free"))
                ok = False
                break
        if boardrules._tile_ability(_bk[0], _bk[1], 7) != 1:
            common._say("FAIL: the forced board's special tile (code 3 -> ability 1) "
                 "must reach `_tile_ability` too")
            ok = False
        if (int((selftest_tables._pipe_vals(_sdF, b"/B=") or [b"-1"])[0]),
                int((selftest_tables._pipe_vals(_sdF, b"/E=") or [b"-1"])[0])) != (3, 1):
            common._say("FAIL: an explicit board's counts are still derived -- three "
                 "blocks (6, 8, 9) and one special (3)")
            ok = False

        # WARNING: AND AN OVERRIDE MUST NOT SMUGGLE PAST THE DEAL A CODE THE DEAL
        # ITSELF IS NOT ALLOWED TO PRODUCE. Regression for 2026-09-07T04:20Z:
        # `roll_board` had already stopped dealing 17, but prod still pinned
        # one on tile 3 through this very knob, so the withdrawal protected
        # nobody and the match hung on the first battle against it. 17 is
        # modelled again, so the knob under test here is `POL_TM_SCRAMBLE=0`
        # -- the switch that withdraws it -- and the assertion is that BOTH
        # halves move together.
        os.environ["POL_TM_SCRAMBLE"] = "0"
        os.environ["POL_TM_START_F"] = "0|17|0|6|0|0|0|0"
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS.pop(_bk, None)
        _codesU = boardrules._match_objects(_bk[0], _bk[1], 2, fresh=True)
        if list(_codesU)[:4] != [0, 0, 0, 6]:
            common._say("FAIL: with POL_TM_SCRAMBLE=0 a forced board must blank every "
                 "code the deal may not produce and keep the rest -- got %r"
                 % (list(_codesU)[:4],))
            ok = False
        if 1 in (boardrules._MATCH_BOARD.get(_bk) or {}):
            common._say("FAIL: a blanked tile must be FREE in `_MATCH_BOARD` too, not "
                 "seeded from the code the environment asked for")
            ok = False
        if 17 in boardrules._dealt_chance_codes():
            common._say("FAIL: POL_TM_SCRAMBLE=0 must take 17 out of the deal ladder "
                 "too -- got %r" % (boardrules._dealt_chance_codes(),))
            ok = False
        # ...and with the knob at its default the SAME board keeps its 17,
        # because the code is modelled. A blanking that cannot be turned off
        # is a withdrawal nobody can undo without a deploy.
        os.environ.pop("POL_TM_SCRAMBLE", None)
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS.pop(_bk, None)
        _codesU2 = boardrules._match_objects(_bk[0], _bk[1], 2, fresh=True)
        os.environ.pop("POL_TM_START_F", None)
        if list(_codesU2)[:4] != [0, 17, 0, 6]:
            common._say("FAIL: 17 is modelled, so a forced board keeps it -- got %r"
                 % (list(_codesU2)[:4],))
            ok = False
        if 17 not in boardrules._dealt_chance_codes():
            common._say("FAIL: the default deal ladder is the client's own "
                 "(6/7/8/17 at 0xC106C) -- got %r" % (boardrules._dealt_chance_codes(),))
            ok = False

        # ...and POL_TM_BOARD_OBJECTS=0 deals the pre-2026-09-06 empty board.
        # That also restores the suite-wide pin for everything after this.
        os.environ["POL_TM_BOARD_OBJECTS"] = "0"
        if any(boardrules._match_objects(_bk[0], _bk[1], 2, fresh=True)):
            common._say("FAIL: POL_TM_BOARD_OBJECTS=0 must deal an empty board")
            ok = False

        # --- A SPECIAL TILE ACTUALLY DOES SOMETHING ------------------------
        # The card played on tile 5 must inherit ability 2 (0xC7015 saves
        # [tile.card+0x24], 0xB3200 zeroes it, 0xC703C puts it back), and the
        # chance block on tile 6 must be FOUGHT -- the neutral arm at 0xD386B,
        # which has no reverse-arrow test at all.
        os.environ["POL_TM_BOARD_OBJECTS"] = "1"
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS[_bk] = [0] * 16
        boardrules._MATCH_OBJECTS[_bk][5] = 4                     # special tile, ability 2
        boardrules._MATCH_OBJECTS[_bk][6] = 6                     # a chance block next door
        boardrules._MATCH_BOARD[_bk] = {6: tmbattle.object_card(6)}
        if boardrules._tile_ability(_bk[0], _bk[1], 5) != 2:
            common._say("FAIL: special tile code 4 carries ability 2 (0xB303C)")
            ok = False
        _fightsB, _ = placement._apply_placement(_bk[0], _bk[1], 2, 0, 5,
                                       b"9|40|0|10|10|0|4|0")   # one EAST arrow
        _placedB = (boardrules._MATCH_BOARD.get(_bk) or {}).get(5)
        if _placedB is None or _placedB.ability != 2:
            common._say("FAIL: a card played onto a special tile INHERITS its ability "
                 "-- got %r" % (_placedB and _placedB.ability,))
            ok = False

        # KEY: ...AND ABILITY 1 IS SPENT ON THE BATTLE IT POWERED, while 2 and 3
        # are not. 0xD30EE reads the ability of the tile just played on
        # (`[obj+0x179]`) at -BattleEnd and zeroes it only when it is EXACTLY 1.
        # The three special tiles are not symmetric and this is the asymmetry.
        for _ab, _code, _want in ((1, 3, 0), (2, 4, 2), (3, 5, 3)):
            boardrules._MATCH_BOARD.pop(_bk, None)
            boardrules._MATCH_OBJECTS[_bk] = [0] * 16
            boardrules._MATCH_OBJECTS[_bk][5] = _code
            # A weak defender on tile 6 so tile 5 has something to fight.
            boardrules._MATCH_BOARD[_bk] = {6: tmbattle.Card(b"9|1|0|1|1|0|255|0", 1)}
            _bA, _ = placement._apply_placement(_bk[0], _bk[1], 2, 0, 5,
                                      b"9|60|0|60|60|0|4|0")   # arrows 4 = E
            if not _bA:
                common._say("FAIL: the ability-burn probe needs a battle to burn on "
                     "(code %d fought nothing)" % _code)
                ok = False
                break
            _after = (boardrules._MATCH_BOARD.get(_bk) or {}).get(5)
            if _after is None or int(_after.ability) != _want:
                common._say("FAIL: special tile code %d (ability %d) must read %d "
                     "after its first battle -- 0xD30EE burns 1 and leaves 2 "
                     "and 3 alone; got %r"
                     % (_code, _ab, _want, _after and _after.ability))
                ok = False
                break
        # ...and a placement that never fights keeps even an ability 1, because
        # a flip-only move does not reach -BattleEnd at all.
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS[_bk] = [0] * 16
        boardrules._MATCH_OBJECTS[_bk][5] = 3                     # ability 1
        boardrules._MATCH_BOARD[_bk] = {}
        _bN, _ = placement._apply_placement(_bk[0], _bk[1], 2, 0, 5,
                                  b"9|60|0|60|60|0|4|0")
        _keptN = (boardrules._MATCH_BOARD.get(_bk) or {}).get(5)
        if _bN or _keptN is None or int(_keptN.ability) != 1:
            common._say("FAIL: with nothing to fight, the attack boost is NOT spent "
                 "-- got battles=%r ability=%r"
                 % (_bN, _keptN and _keptN.ability))
            ok = False
        if not _fightsB or _fightsB[0][1] != 6:
            common._say("FAIL: an EAST arrow into a chance block must start a battle "
                 "on tile 6 -- 0xD386B needs no reverse arrow, only a non-zero "
                 "mask; got %r" % ([(b[0], b[1], b[3]) for b in _fightsB],))
            ok = False
        # A PLAIN block, by contrast, has no arrows at all and is inert.
        boardrules._MATCH_BOARD[_bk] = {6: tmbattle.object_card(1)}
        boardrules._MATCH_OBJECTS[_bk] = [0] * 16
        boardrules._MATCH_OBJECTS[_bk][6] = 1
        _fightsP, _marksP = placement._apply_placement(_bk[0], _bk[1], 2, 0, 5,
                                             b"9|40|0|10|10|0|4|0")
        if _fightsP or _marksP:
            common._say("FAIL: a PLAIN block is a dead tile -- 0xB30DF zeroes its "
                 "arrow mask and 0xD387E drops it; got %r / %r"
                 % (_fightsP, _marksP)); ok = False
        # ...and neither block ever reaches the take list as a card.
        if any(scoring._board_takes(_bk[0], _bk[1], 2)):
            common._say("FAIL: a block is a tile, not a card -- it must never feed "
                 "the lucky-card take list a pseudo id")
            ok = False

        # --- A WHOLE MATCH WITH A BLOCKED BOARD ----------------------------
        # WARNING: THE INVARIANT THE SHIPPED TABLES IMPLY: 16 tiles - 6 blocks = 10 =
        # two players x five cards, and 6 is the MOST LIKELY block count
        # (50%). So a full board must still take every card, and the blocks
        # must score for nobody. This is the interaction the rest of the suite
        # can no longer see, because the legacy scripted flows are pinned off.
        for _codesW, _label in (
                ([1, 0, 0, 0, 6, 0, 0, 2, 0, 9, 0, 0, 17, 0, 0, 1],
                 "six blocks (the modal roll)"),
                ([1, 3, 0, 0, 6, 4, 0, 2, 0, 9, 5, 0, 17, 0, 0, 1],
                 "six blocks and three special tiles")):
            boardrules._MATCH_BOARD.pop(_bk, None)
            boardrules._MATCH_OBJECTS[_bk] = list(_codesW)
            _bd = boardrules._MATCH_BOARD.setdefault(_bk, {})
            for _t, _c in enumerate(_codesW):
                _oc = tmbattle.object_card(_c)
                if _oc is not None:
                    _bd[_t] = _oc
            _freeW = [_t for _t in range(16) if _t not in _bd]
            if len(_freeW) != 10:
                common._say("FAIL: %s must leave exactly ten playable tiles (two "
                     "hands of five), got %d" % (_label, len(_freeW)))
                ok = False
            # WARNING: THE FREE LIST IS RECOMPUTED EVERY PLACEMENT. It used to be
            # taken once up front, which was fine until code 17 (scramble)
            # started MOVING cards mid-match -- a precomputed tile could be
            # occupied by the time its turn came, the occupied-tile guard
            # refused it, and the test failed on its own stale assumption.
            for _i in range(len(_freeW)):
                _bdN = boardrules._MATCH_BOARD.get(_bk) or {}
                _t = next((x for x in range(16) if x not in _bdN), None)
                if _t is None:
                    common._say("FAIL: %s -- ran out of board on card %d"
                         % (_label, _i))
                    ok = False
                    break
                _fW, _mW = placement._apply_placement(
                    _bk[0], _bk[1], 2, _i % 2, _t, b"9|30|0|30|30|0|255|0")
                if not (boardrules._MATCH_BOARD.get(_bk) or {}):
                    common._say("FAIL: %s -- the board emptied on card %d"
                         % (_label, _i))
                    ok = False
                    break
            _bdW = boardrules._MATCH_BOARD.get(_bk) or {}
            # WARNING: THE BOARD NO LONGER HAS TO FILL, and that is the chance blocks
            # doing their job: one that triggers is CONSUMED and its tile goes
            # back to empty (state 0x1D). This assertion used to be `== 16` and
            # it was right until 2026-09-07; it is kept as an inequality so the
            # ten cards are still proved to have landed somewhere.
            # A consumed block frees its tile; a SCRAMBLE also relocates every
            # card, so the only thing still guaranteed is that no card is lost.
            _consumable = sum(1 for _c in _codesW
                              if _c in tmbattle.CONSUMED_CHANCE_CODES)
            _cards_on = sum(1 for _cW in _bdW.values()
                            if int(_cW.row["id"]) < 0x8000)
            if _cards_on != len(_freeW):
                common._say("FAIL: %s -- every card played must still be ON the board "
                     "somewhere; played %d, found %d"
                     % (_label, len(_freeW), _cards_on))
                ok = False
            # A PLAIN block can never change hands: it has no arrows, so
            # 0xD387E drops it and nothing ever contests it.
            for _t, _c in enumerate(_codesW):
                if tmbattle.object_kind(_c) == "plain"                         and _bdW[_t].owner != tmbattle.OWNER_BLOCK:
                    common._say("FAIL: %s -- plain block on tile %d changed hands; it "
                         "has no arrow mask and must never be contested"
                         % (_label, _t))
                    ok = False
                    break
            # ...but a CHANCE or ROTATING block can be, and a captured one
            # scores for its captor -- which is what makes fighting one worth
            # doing, and is why the total is NOT capped at the ten cards
            # played. What must hold is that the accounting is complete.
            _scW = scoring._board_scores(_bk[0], _bk[1], 2)
            _neutralW = sum(1 for _c in _bdW.values()
                            if _c.owner == tmbattle.OWNER_BLOCK)
            _emptyW = 16 - len(_bdW)
            if sum(_scW) + _neutralW + _emptyW != 16:
                common._say("FAIL: %s -- every tile is a seat's, a block's, or EMPTY; "
                     "got scores %r + %d neutral + %d empty on a 16-tile board"
                     % (_label, _scW, _neutralW, _emptyW))
                ok = False
            if _emptyW > _consumable:
                common._say("FAIL: %s -- a tile can only end up empty because a chance "
                     "block was consumed off it; %d empty vs %d consumable"
                     % (_label, _emptyW, _consumable))
                ok = False
            if sum(_scW) < 1:
                common._say("FAIL: %s -- ten cards were played and nobody scored"
                     % (_label,))
                ok = False

        # --- THE CHANCE-BLOCK EFFECT, END TO END ---------------------------
        # VERIFIED: Decoded 2026-09-07 off the high-nibble dispatch at 0xD07F5 and
        # cross-checked against a tester's live commentary four kinds for
        # four. A battle resolving against a code-7 block powers up every card
        # the ACTOR holds by +10 on rec+0x26 -- the field `resolve` has always
        # taken as `att_mod`/`dfn_mod` and which nothing had ever moved.
        for _code, _want in ((7, 10), (6, -10)):
            boardrules._MATCH_BOARD.pop(_bk, None)
            boardrules._MATCH_OBJECTS[_bk] = [0] * 16
            boardrules._MATCH_OBJECTS[_bk][6] = _code
            boardrules._MATCH_BOARD[_bk] = {6: tmbattle.object_card(_code),
                                 0: tmbattle.Card(b"9|30|0|30|30|0|255|0", 0)}
            # Tile 5 places with an EAST arrow (bit 2) into the block on 6.
            _fx, _ = placement._apply_placement(_bk[0], _bk[1], 2, 0, 5,
                                      b"9|40|0|10|10|0|4|0")
            _bdx = boardrules._MATCH_BOARD.get(_bk) or {}
            if not _fx:
                common._say("FAIL: a chance block must be BATTLED (its mask is 0xFF), "
                     "code %d" % _code); ok = False; break
            # WARNING: AND THE BLOCK IS GONE. The tester's screen showed all four
            # triggered tiles BLANK while this server still held them as cards,
            # and three of them were in the score we served.
            if 6 in _bdx:
                common._say("FAIL: a chance block is CONSUMED by its own effect -- "
                     "state 0x1D rebuilds the tile as card id 0xFFFF, so tile 6 "
                     "must be EMPTY and playable, not captured (code %d)"
                     % _code); ok = False; break
            if scoring._board_scores(_bk[0], _bk[1], 2)[0] != 2:
                common._say("FAIL: a consumed block must not count toward anyone's "
                     "score -- seat 0 holds tiles 0 and 5 and nothing else, "
                     "got %r" % (scoring._board_scores(_bk[0], _bk[1], 2),))
                ok = False; break
            if _bdx[0].modifier != _want:
                common._say("FAIL: code %d must move the actor's OTHER card on tile 0 "
                     "by %+d (rec+0x26), got %r"
                     % (_code, _want, _bdx[0].modifier)); ok = False; break
            # ...and the block itself is nobody's card, so it must not move.
            if 6 in _bdx and _bdx[6].placer == tmbattle.OWNER_BLOCK                     and _bdx[6].modifier != 0:
                common._say("FAIL: the block is not the actor's card and must not be "
                     "powered by its own effect"); ok = False; break
        # --- CODE 17 = SCRAMBLE, and the CANDIDATE LIST is `card+0x1C` -----
        # WARNING: THIS ASSERTION HAS FLIPPED THREE TIMES AND EVERY FLIP WAS A LIVE
        # MATCH: "changes nothing" while 17 was undealt, "scrambles and
        # consumes" for the few hours it was first dealt, back to "changes
        # nothing" on the 2026-09-07T04:20Z hang, and now here. What is
        # different this time is that the list the redeal indexes is the
        # CLIENT'S (0xD3A60 arg2=1 -> `card+0x1C`), not "every tile we hold no
        # card on" -- see `_unplayed_specials`.
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS[_bk] = [0] * 16
        boardrules._MATCH_OBJECTS[_bk][6] = 17                    # SCRAMBLE
        boardrules._MATCH_OBJECTS[_bk][9] = 4                     # an UNPLAYED special
        boardrules._MATCH_BOARD[_bk] = {6: tmbattle.object_card(17),
                             0: tmbattle.Card(b"9|30|0|30|30|0|255|0", 0)}
        placement._apply_placement(_bk[0], _bk[1], 2, 0, 5, b"9|40|0|10|10|0|4|0")
        _bdS = boardrules._MATCH_BOARD.get(_bk) or {}
        _cardsS = sorted(t for t, c in _bdS.items()
                         if int(c.row["id"]) < 0x8000)
        if len(_cardsS) != 2:
            common._say("FAIL: a scramble moves the two cards, it does not lose or "
                 "duplicate one -- got %r" % (_cardsS,))
            ok = False
        elif 6 in _bdS:
            common._say("FAIL: a scrambled block is CONSUMED like every other chance "
                 "block -- state 0x1A rebuilds tile 6 as card id 0xFFFF with "
                 "+0x1C/+0x1D/+0x1E all 8 (0xD205A..0xD20B5), so it must be "
                 "empty and playable")
            ok = False
        elif 9 in _cardsS:
            # WARNING: THE ONE THING 04:20Z GOT WRONG. Tile 9 carries an unplayed
            # special, which the client stamps `+0x1C` = 4 (0xC1976) while
            # leaving its owner byte 8 -- so 0xD3A60's arg2=1 arm never
            # collects it and the redeal can never land there.
            common._say("FAIL: an UNPLAYED SPECIAL TILE is not a scramble candidate "
                 "(card+0x1C is 4, not 8) -- a card landed on tile 9")
            ok = False
        elif scoring._board_scores(_bk[0], _bk[1], 2) != [2, 0]:
            # Two: the card that was on 0 and the card just played, wherever
            # the redeal put them. The block is consumed, not captured, so it
            # scores for nobody -- and OWNERSHIP TRAVELS WITH EACH CARD, which
            # is why a score that agrees proves nothing about the board.
            common._say("FAIL: a scramble moves cards without changing owners, and "
                 "the block is consumed rather than captured; got %r"
                 % (scoring._board_scores(_bk[0], _bk[1], 2),))
            ok = False
        # ...and the SPECIAL is spent by the placement that lands on it, so it
        # stops being a candidate-blocker (and stops paying its ability twice).
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS[_bk] = [0] * 16
        boardrules._MATCH_OBJECTS[_bk][9] = 4                     # ability 2, defence up
        boardrules._MATCH_BOARD[_bk] = {}
        if boardrules._unplayed_specials(_bk[0], _bk[1]) != [9]:
            common._say("FAIL: a dealt special tile nobody has played on is the one "
                 "square the scramble may not use -- got %r"
                 % (boardrules._unplayed_specials(_bk[0], _bk[1]),))
            ok = False
        placement._apply_placement(_bk[0], _bk[1], 2, 0, 9, b"9|40|0|10|10|0|4|0")
        _cardS = (boardrules._MATCH_BOARD.get(_bk) or {}).get(9)
        if _cardS is None or int(_cardS.ability) != 2:
            common._say("FAIL: the tile's ability still belongs to the card played on "
                 "it (0xC7015/0xC703C) -- got %r"
                 % (_cardS and _cardS.ability,))
            ok = False
        if boardrules._unplayed_specials(_bk[0], _bk[1]) != []:
            common._say("FAIL: a special tile that has been PLAYED ON is no longer "
                 "one -- 0xC7098 overwrites its +0x1C with the owner, so a "
                 "scramble that lifts the card off may redeal there")
            ok = False
        if boardrules._tile_ability(_bk[0], _bk[1], 9) != 0:
            common._say("FAIL: ...and it must not pay its ability a second time")
            ok = False

        # --- A ROTATING BLOCK IS NEITHER CAPTURED NOR LOST TO --------------
        # WARNING: REGRESSION, 2026-09-07T03:19Z. Written as
        # `verdict == ATTACKER and not _rot`, a rotating battle the attacker
        # WON fell into the `else` -- the LOSS path -- and handed the player's
        # own winning card to owner 4. The log read "board map after tile 10
        # ... 10:4" with the score 0-0. Both invariants hold whoever wins,
        # because neither arm applies at all.
        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS[_bk] = [0] * 16
        boardrules._MATCH_OBJECTS[_bk][6] = 9                     # rotating, fires S
        boardrules._MATCH_BOARD[_bk] = {6: tmbattle.object_card(9)}
        # Tile 10 places pointing N (bit 0) at the block on tile 6.
        _fR, _ = placement._apply_placement(_bk[0], _bk[1], 2, 0, 10,
                                  b"9|40|0|10|10|0|1|0")
        _bdR = boardrules._MATCH_BOARD.get(_bk) or {}
        if not _fR:
            common._say("FAIL: a rotating block has one arrow and a non-zero mask, so "
                 "0xD386B fights it"); ok = False
        elif _bdR.get(10) is None or _bdR[10].owner != 0:
            common._say("FAIL: THE REGRESSION -- the placed card must stay its "
                 "owner's whoever won; a rotating battle applies NEITHER the "
                 "capture arm nor the loss arm. Got owner %r"
                 % (_bdR.get(10) and _bdR[10].owner,))
            ok = False
        elif _bdR.get(6) is None or _bdR[6].owner != tmbattle.OWNER_BLOCK:
            common._say("FAIL: the block itself is never captured -- nothing in "
                 "states 0x1E..0x22 writes its owner (got %r)"
                 % (_bdR.get(6) and _bdR[6].owner,))
            ok = False
        elif scoring._board_scores(_bk[0], _bk[1], 2) != [1, 0]:
            common._say("FAIL: after the placement seat 0 holds exactly its own card, "
                 "got %r" % (scoring._board_scores(_bk[0], _bk[1], 2),))
            ok = False

        boardrules._MATCH_BOARD.pop(_bk, None)
        boardrules._MATCH_OBJECTS.pop(_bk, None)
        tablesettings._TABLE_SETTINGS.pop(_bk, None)
        os.environ["POL_TM_BOARD_OBJECTS"] = "0"       # back to the suite pin

        # --- the TURN BYTE, the hand, and the card that lands on the board --
        # WARNING: The third envelope byte is the TURN and `@PutCard` is COMPARED
        # against it (0x103876 -> `Errot!!Turn%d!=%d`), so a header that drops
        # it is a message the client reads and then throws away.
        # (the u32 is 0x070A0043 and the wire is its little-endian hex, which
        # is why a live line reads `43000A07` -- same shape as the measured
        # `GTM0GB2001701@Quit=`, code 0xB2 command 0x17 shop 1.)
        if protocol._turn_code(protocol.PUTCARD_CMD, 7) != b"43000A07":
            common._say("FAIL: an in-match header is code | cmd<<16 | turn<<24; "
                 "got %r" % (protocol._turn_code(protocol.PUTCARD_CMD, 7),)); ok = False
        if ((struct.unpack("<I", bytes.fromhex(
                protocol._turn_code(protocol.TURNDATA_CMD, 3).decode()))[0] >> 24) & 0xFF) != 3:
            common._say("FAIL: @TurnData must carry the turn in the sh byte -- that is "
                 "the store the client reads it from (0x1036EC)"); ok = False
        # The hand is parsed by its @N<i>= NUMBER, not by order of appearance:
        # /H= indexes this list, and an off-by-one puts a different card down.
        _cs = (b"@CardSelect=/C=3@N1=/D=100|84|0|99|75|23|20|0"
               b"@N0=/D=161|100|2|90|99|31|21|255"
               b"@N2=/D=42|43|1|20|18|7|4|255")
        _hand = boardrules._hands_from_cardselect(_cs)
        if [r.split(b"|")[0] for r in _hand] != [b"161", b"100", b"42"]:
            common._say("FAIL: the hand must come back in @N<i>= order, got %r"
                 % ([r.split(b"|")[0] for r in _hand],)); ok = False
        # ...and the card that goes out is the one at that slot, with the seat
        # rotated per recipient (the actor is 0 in its own copy, 1 in the other).
        _pc_self = boardrules._putcard_body(_seatsD, 0, 0, 2, 5, _hand[2])
        _pc_other = boardrules._putcard_body(_seatsD, 1, 0, 2, 5, _hand[2])
        if b"/A=0/H=2/F=5/D=42|43|1|20|18|7|4|255" not in _pc_self:
            common._say("FAIL: the placer's own @PutCard is %r" % (_pc_self,)); ok = False
        if b"/A=1/" not in _pc_other:
            common._say("FAIL: the watcher must see the actor one seat away, got %r"
                 % (_pc_other,)); ok = False
        # WARNING: AND THE CARD TYPE MUST SURVIVE: 0x1039D9 refuses `type >= 4` with
        # `Error:CardNum=%d`, so a mangled row is a bailed-out arm, not a wrong
        # picture. This is the same eight-field row @CardSelect= carries.
        if len(selftest_tables._pipe_vals(_pc_self, b"/D=")) != 8:
            common._say("FAIL: /D= is eight values (occ0 word, occ1..7 bytes), got %r"
                 % (selftest_tables._pipe_vals(_pc_self, b"/D="),)); ok = False
        if int(selftest_tables._pipe_vals(_pc_self, b"/D=")[2]) >= 4:
            common._say("FAIL: card type must stay < 4 (0x1039D9 Error:CardNum)")
            ok = False
        # --- AN UNPROMPTED PUSH MUST NOT LEAVE ON THE WRONG SOCKET ---------
        # The regression that black-screened a live player: a member has a room
        # connection AND a table connection, and the first `idle_pushes` drained
        # whichever ticked first. Measured on ports 50478 / 50512.
        pushqueue._PUSHES.clear()
        pushqueue._queue_push(mid, b"43000100@VsGameInit=/N=2", "board", after=0,
                    peer=b"UE7QN1N9G")
        if pushqueue.idle_pushes(mid, {b"UKXDBA266"}):
            common._say("FAIL: a board pinned to the TABLE peer must not go out on a "
                 "socket that only carries the ROOM peer"); ok = False
        if not pushqueue._PUSHES.get(pushqueue._push_key(mid)):
            common._say("FAIL: ...and the wrong socket must LEAVE it queued"); ok = False
        if pushqueue.idle_pushes(mid, set()):
            common._say("FAIL: a socket carrying no game traffic at all is not a band")
            ok = False
        _got = pushqueue.idle_pushes(mid, {b"UE7QN1N9G", b"UKXDBA266"})
        if [b for _p, b in _got] != [b"43000100@VsGameInit=/N=2"]:
            common._say("FAIL: the socket that carries the table peer must get it, "
                 "got %r" % (_got,)); ok = False
        pushqueue._PUSHES.clear()

        # --- `:str` FIELDS ARE HEX. Asserted against the CLIENT'S OWN SEND ---
        # `@GameEN=/CN=/HN=4C6170746F705465737432/L=1` is captured in this file's
        # `@GameEN=` banner: the fixture name in the encoding 0xAAD60 decodes. If our
        # /CN= does not round-trip to the same bytes, the name is a row of squares.
        if matchstart._str_field("LaptopTest2") != b"4C6170746F705465737432":
            common._say("FAIL: a :str field is UPPERCASE hex of the cp932 bytes -- the "
                 "client's own @GameEN=/HN= says so; got %r"
                 % (matchstart._str_field("LaptopTest2"),)); ok = False
        # ...and the decoder, run over the PLAIN text, must produce exactly the
        # garbage a tester photographed: 'z' then squares.
        def _aad60(s):
            o = bytearray()
            for i in range(0, len(s) - 1, 2):
                h, l = ord(s[i]), ord(s[i + 1])
                h = h - 0x37 if h > 0x39 else h - 0x30
                l = l - 0x37 if l > 0x39 else l - 0x30
                o.append((((h << 4) & 0xFF) + l) & 0xFF)
            return bytes(o)
        if _aad60("LaptopTest2") != bytes((0x7A, 0xCD, 0xB9, 0xFE, 0xFD)):
            common._say("FAIL: the 0xAAD60 model no longer reproduces the measured "
                 "garbage 7A CD B9 FE FD"); ok = False
        if _aad60(matchstart._str_field("LaptopTest2").decode()) != b"LaptopTest2":
            common._say("FAIL: /CN= must round-trip through the client's own decoder")
            ok = False

        # WARNING: THE HAND COMPACTS -- `/H=` indexes what is LEFT, not the deal.
        # 0xC70F4 left-shifts the 156-byte slots at 0x2B65EC over the played one.
        # This is the live sequence that found it: member 6 held 42,47,58,59,87
        # and played slots 0, 1, 1 -- which is 42, 58, 59, and the server sent
        # 42, 47, 47 until this was fixed.
        boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); pushqueue._PUSHES.clear()
        _six = boardrules._hands_from_cardselect(
            b"@CardSelect=/C=5@N0=/D=42|1|0|1|1|1|1|0@N1=/D=47|1|0|1|1|1|1|0"
            b"@N2=/D=58|1|0|1|1|1|1|0@N3=/D=59|1|0|1|1|1|1|0"
            b"@N4=/D=87|1|0|1|1|1|1|0")
        # WARNING: AND IT MUST RUN THE HANDLER. The first version of this check did the
        # `del` on a local list and passed cleanly with the real one removed --
        # the same dead-assertion class as `ok = _selftest_blocks(); ok = True`.
        matchmaking._remember_match("#TM0R001", 1, _seatsD)
        for _m, _v in _seatsD:
            matchmaking._MATCH_PEER[pushqueue._push_key(_m)] = b"UE7QN1N9G"
            dispatch.handle_line(protocol.encode_code(protocol.IN_MATCH_CODE | (7 << 16))
                        + b"@CardSelect=/C=5@N0=/D=42|1|0|1|1|1|1|0"
                          b"@N1=/D=47|1|0|1|1|1|1|0@N2=/D=58|1|0|1|1|1|1|0"
                          b"@N3=/D=59|1|0|1|1|1|1|0@N4=/D=87|1|0|1|1|1|1|0",
                        peer=b"auth-band", peer_nick=b"UE7QN1N9G", member_id=_m)
        _played = []
        for _turn, _h in ((0, 0), (2, 1), (4, 1)):
            _r = dispatch.handle_line(protocol._turn_code(protocol.PUTDATA_CMD, _turn)
                             + b"@PutData=/P=0/H=%d/F=%d" % (_h, _turn),
                             peer=b"auth-band", peer_nick=b"UE7QN1N9G",
                             member_id=_seatsD[0][0])
            _lines = _r if isinstance(_r, (list, tuple)) else [_r or b""]
            _pcl = next((l for l in _lines if b"@PutCard=" in l), b"")
            _played.append((selftest_tables._pipe_vals(_pcl, b"/D=") or [b"?"])[0])
        if _played != [b"42", b"58", b"59"]:
            common._say("FAIL: slots 0,1,1 out of 42,47,58,59,87 are cards 42,58,59 -- "
                 "a hand that does not compact answers %r" % (_played,)); ok = False
        boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); pushqueue._PUSHES.clear()

        # The last turn must not be announced: the client prints `End` at
        # 5 * N (0xC7701) and stops polling command 9.
        pushqueue._PUSHES.clear()
        boardrules._MATCH_TURN.clear()
        # The stats bump persists games/score_total into the collection dir;
        # sandbox it so a selftest never writes member files into the live
        # resource tree.
        _rsave = os.environ.get("POL_RESOURCE_DIR")
        _rtmp = tempfile.mkdtemp(prefix="tm-result-")
        os.environ["POL_RESOURCE_DIR"] = _rtmp
        try:
            turns._queue_next_turn("#TM0R001", 1, _seatsD, 10, 0)
            # Past 5xN there is no turn 10 -- but there IS a result, and the
            # client polls (0x43, 13) for it for ever if it does not come
            # (measured live: both stopped exactly there after
            # GamePlayEnd:Turn=9).
            _past = [e[1] for v in pushqueue._PUSHES.values() for e in v]
            if any(b"@TurnData=" in b for b in _past):
                common._say("FAIL: turn 10 of a two-player match is past the 5xN limit "
                     "and must not be announced -- got %r" % (_past,)); ok = False
            if len(_past) != 2 or not all(b"@ResultData=" in b for b in _past):
                common._say("FAIL: the last turn must hand both players @ResultData on "
                     "(0x43, 13) -- got %r" % (_past,)); ok = False
            # WARNING: /P=0 CRASHED BOTH CLIENTS (2026-08-21, live): 0x10F6A5 divides
            # by [obj+0x84], and only the recipient's own @No section writes
            # it. One section per seat, and the first /D= in each copy is the
            # recipient's own (@No0 -- recipient-first rotation).
            for _b in _past:
                if b"/P=2" not in _b:
                    common._say("FAIL: @ResultData must carry one @No section per "
                         "seat -- /P=0 is the measured divide-by-zero crash; "
                         "got %r" % (_b,)); ok = False
                _d = selftest_tables._pipe_vals(_b, b"/D=")
                if len(_d) != 7:
                    common._say("FAIL: /D= is read by occurrence, SEVEN slots, none "
                         "checked -- a short list stores stack garbage, the "
                         "divisor included; got %r" % (_d,)); ok = False
                elif int(_d[5] or 0) < 1:
                    common._say("FAIL: /D= occ 5 is the divisor at 0x10F6A5 and must "
                         "be >= 1; got %r" % (_d,)); ok = False
                if b"/RN=" not in _b:
                    common._say("FAIL: the own section carries /RN= (hex :str, "
                         "[obj+0x128]); got %r" % (_b,)); ok = False
            # ...and the counters must actually persist, or every match is
            # for ever the player's first.
            _blk = (collection._collection_load(_seatsD[0][0]) or {}).get("rank") or {}
            if _blk.get("games") != 1:
                common._say("FAIL: a finished match must record games=1 in the rank "
                     "block; got %r" % (_blk,)); ok = False
            # THE ONCE-GUARD: a re-sent final placement must not double
            # anything -- not the pushes, not the games count.
            _n0 = sum(len(v) for v in pushqueue._PUSHES.values())
            turns._queue_next_turn("#TM0R001", 1, _seatsD, 10, 0)
            if sum(len(v) for v in pushqueue._PUSHES.values()) != _n0:
                common._say("FAIL: the end branch must fire ONCE per match -- a "
                     "re-sent @PutData re-pushed the result"); ok = False
            _blk = (collection._collection_load(_seatsD[0][0]) or {}).get("rank") or {}
            if _blk.get("games") != 1:
                common._say("FAIL: a re-fired end branch double-counted games; got "
                     "%r" % (_blk,)); ok = False
        finally:
            if _rsave is None:
                os.environ.pop("POL_RESOURCE_DIR", None)
            else:
                os.environ["POL_RESOURCE_DIR"] = _rsave
            shutil.rmtree(_rtmp, ignore_errors=True)
        pushqueue._PUSHES.clear()
        boardrules._MATCH_TURN.clear()
        turns._queue_next_turn("#TM0R001", 1, _seatsD, 9, 1)
        if len(pushqueue._PUSHES) != 2:
            common._say("FAIL: turn 9 IS in range and must reach both players, got %r"
                 % (list(pushqueue._PUSHES),)); ok = False
        pushqueue._PUSHES.clear(); boardrules._MATCH_TURN.clear()

        # --- the sell price is a LADDER LOOKUP, against three real prices ---
        # Measured 2026-08-20 off the shop screen, with the cards from the wire.
        # A flat price desyncs the wallet silently: the CLIENT prices the card
        # and only tells us which one went.
        _lad = cardtables._cardpri_table()
        if _lad[:6] != [0, 25, 50, 100, 250, 500]:
            common._say("FAIL: CardPri.BIN must ship and decode -- got %r" % (_lad[:6],))
            ok = False
        # All six measured cards, spanning every card type. The multiplier is
        # the TYPE in eighths -- (7 + max(0, type-1))/8 -- and the STATS do not
        # enter it: 15 and 102 differ wildly but are both type 0 and both 7/8.
        for _c, _want in (([15, 14, 0, 10, 12, 2], 43),
                          ([102, 65, 0, 56, 40, 6], 656),
                          ([49, 54, 1, 43, 13, 5], 437),
                          ([234, 5, 1, 50, 100, 15], 437),
                          ([161, 100, 2, 90, 99, 21], 10000),
                          ([158, 97, 3, 89, 95, 24], 19687),
                          # the wire's 6th value is the ARROW MASK since
                          # 422bc0c4 -- a Fat Chocobo (table level 6) with
                          # arrows 20 must still sell at ladder[6]*7/8:
                          ([89, 34, 0, 76, 32, 20], 656)):
            _got = cardshop._sell_price(_c)
            if _got != _want:
                common._say("FAIL: type %d level %d must sell for %d, got %d"
                     % (_c[2], _c[5], _want, _got)); ok = False
        # THE CARD TABLE: an id alone must be enough to price a card, or a wire
        # row missing its level silently desyncs the wallet.
        if cardtables._cardprm(158) != (3, 24) or cardtables._cardprm(161) != (2, 21):
            common._say("FAIL: CardPrm.BIN must ship and decode (12-byte records from "
                 "+16; byte 1 = type, byte 5 = level) -- got %r / %r"
                 % (cardtables._cardprm(158), cardtables._cardprm(161))); ok = False
        if cardshop._sell_price([158]) != 19687 or cardshop._sell_price([161]) != 10000:
            common._say("FAIL: a card id alone must price correctly, got %d / %d"
                 % (cardshop._sell_price([158]), cardshop._sell_price([161]))); ok = False
        # There is no type 4 in the real records; the rule covers every card.
        _types = {t for t, _l in (cardtables._cardprm(i) for i in range(480)) if t is not None}
        if _types != {0, 1, 2, 3}:
            common._say("FAIL: the card table should carry types 0..3 only, got %r"
                 % (sorted(_types),)); ok = False

        # The type must MOVE the price -- same level and stats, different type.
        if cardshop._sell_price([1, 9, 3, 9, 9, 21]) <= cardshop._sell_price([1, 9, 0, 9, 9, 21]):
            common._say("FAIL: a type-3 card must sell for more than a type-0 one at "
                 "the same level"); ok = False
        # A card with no level must NOT silently take the ladder's level 0.
        if cardshop._sell_price([1, 2, 3]) == _lad[0]:
            common._say("FAIL: a level-less card must fall back loudly, not price at "
                 "ladder[0]"); ok = False

        # --- `/CP=` is the CARD LEVEL, and must not carry the wallet -------
        # WARNING: Money is file +0x34 -> struct +0xC8 (`/M=`); struct +0x00 is the
        # CARD LEVEL, which the VS. COM opponent unlock compares against each
        # `PlPrm.BIN` +0x02 threshold (0x110AE4 and four more). Sending the
        # balance here wrote the player's gil into their card level; sending 0
        # locked every opponent. Both shipped. See `_init_body`.
        #
        # WARNING: THE OLD FIXTURE COULD NOT FAIL: it built the body for a member with
        # no money, so the `/CP= == /M=` compare was `0 == 0` and skipped. This
        # one uses a member who holds both cards and gold.
        _cl_rows = [[89, 34, 0, 76, 32, 8, 20, 255],
                    [158, 97, 3, 89, 95, 36, 65, 255]]
        collection._collection_store("cpm", {"cards": _cl_rows, "money": 4321})
        _shopb = shopdoors._init_body("Init", "cpm")
        _cp = re.search(rb"/CP=(\d+)", _shopb)
        _m = re.search(rb"/M=(\d+)", _shopb)
        _want_cl = cardtables.card_level(_cl_rows)
        if not _cp or not _m:
            common._say("FAIL: the shop body needs both /M= and /CP=, got %r" % (_shopb,))
            ok = False
        else:
            if int(_cp.group(1)) != _want_cl:
                common._say("FAIL: /CP= must carry the CARD LEVEL (%d) -- it lands in "
                     "struct +0x00, the VS. COM opponent gate. Got %r"
                     % (_want_cl, _shopb)); ok = False
            if _cp.group(1) == _m.group(1) and _m.group(1) != b"0":
                common._say("FAIL: /CP= must not carry the wallet -- money is /M= "
                     "(struct +0xC8). Got %r" % (_shopb,)); ok = False

        # --- @GameExit= must be ANSWERED or the client hangs on Loading ------
        # Reported live 2026-08-20T13:31Z: stalled on the loading screen after a
        # clean quit. 0x8416C drains (0x41, 0x0E) immediately before sending
        # `@GameExit=` on (0x41, 0x0D), so it is waiting for request+1.
        _ex = dispatch.handle_line(b"41000D00@GameExit=", member_id=None)
        if not _ex or not _ex.startswith(b"41000E00"):
            common._say("FAIL: @GameExit= must be answered on (0x41, 0x0E) -- the "
                 "sender drains exactly that slot first. Got %r" % (_ex,))
            ok = False
        if not _ex or b"@GameEA=" not in _ex:
            common._say("FAIL: the @GameExit= answer is named `@GameEA` (0x8462D) and "
                 "needs the `=` delimiter, got %r" % (_ex,)); ok = False
        _exv = re.search(rb"/Exit=(-?\d+)", _ex or b"")
        if not _exv or int(_exv.group(1)) <= 0:
            common._say("FAIL: /Exit= must be > 0 -- 0x8465B tests it before clearing "
                 "the global at 0x298200. Got %r" % (_ex,)); ok = False

        # --- a match must NOT start while the owner is still configuring ----
        # WARNING: Measured 2026-08-20T12:43Z: two seats were reached while the host
        # was mid-dialog, the wake-up went out, and the host answered @GameNG=
        # -- stranding the guest on a black screen. See `_TABLE_CONFIRMED`.
        pushqueue._PUSHES.clear(); matchmaking._MATCH_ACCEPTS.clear(); seating._TABLE_CONFIRMED.clear(); tmroom._CONFIRMED.clear()
        seating._SEATED["#TM0R001"] = {1: [(mid, 1), (mid + 1, 2)]}
        matchmaking._announce_match(tmroom, "#TM0R001", 1, [(mid, 1), (mid + 1, 2)])
        if pushqueue._PUSHES:
            common._say("FAIL: no match may be announced before the owner confirms -- "
                 "a wake-up into an open dialog is answered @GameNG=. Got %r"
                 % (pushqueue._PUSHES,)); ok = False
        # ...and 0x24 (@Save=, the preset) must NOT count as confirmation: the
        # client sends it repeatedly while the dialog is still open.
        tablesettings._note_table_settings(b"@Save=/Tbl=1/Game=1@Tet=/tl=0@Tab=/in=0",
                             mid, 0x24, b"UE7QN1N9G")
        # ...and `/Tbl=` must NOT be read as a table number.
        # WARNING: Measured on prod 2026-08-20T19:41:47Z: a real save made AT TABLE 2
        # carried `/Tbl=0`, the hint beat the peer, and the settings were filed
        # against table 0 -- which does not exist:
        #
        #     table -> UNRESOLVED [peer 0xd0e4bcbb92 indexes to 2 but /Tbl= says
        #                          0 -- TAKING /Tbl=, ...]
        #     ...table 0 is not in the shipped fixture -- nothing to update
        #
        # `0x87410` takes `/Tbl=` and `/Game=` as PAGE selectors; the peer
        # is the table. Asserted on the ARITHMETIC and on the CALL SITE, because
        # by this point in the fixture no room peer is registered and a
        # behavioural check would pass for the wrong reason (it would answer
        # None either way -- which is exactly how a green test can hide this).
        _room_peer = 0xf0e4bcbb91                  # UKXDBA266, off the wire
        if peers.table_index_for_peer(b"UE7QN1N9N", _room_peer, None)[0] != 2:
            common._say("FAIL: UE7QN1N9N (0xd0e4bcbb92) is table 2 against room "
                 "%#x" % (_room_peer,)); ok = False
        if peers.table_index_for_peer(b"UE7QN1N9N", _room_peer, 0)[0] != 0:
            common._say("FAIL: this test is inert -- a `/Tbl=` hint is supposed to win "
                 "when one is passed, which is why the call site must not pass "
                 "one for @Save=."); ok = False
        _src = inspect.getsource(tablesettings._note_table_settings)
        if "table_index_for_peer(peer_nick, room, None, chan=chan)" not in _src:
            common._say("FAIL: _note_table_settings must pass NO /Tbl= hint -- it is "
                 "the page selector, not a table number (0x87410)."); ok = False

        # --- a table's rules must OUTLIVE THIS PROCESS -------------------
        # WARNING: They used to live only in `_TABLE_SETTINGS`, so a restarted
        # container answered "no rules" for every table until somebody
        # reconfigured one. The dialog sites that hard-code "seed from the
        # table's rules" then find the info record unpopulated, `0x873A0`
        # fails its `+0x30 == 2` gate, and `0x6BC61` shows a message instead
        # of opening the dialog.
        _rules = {"bm": 166, "du": 0, "st": 1, "cb": 1, "ca": 1, "gs": 1,
                  "tl": 3, "in": 0, "lu": 99999, "ll": 0, "au": 0, "al": 0,
                  "co": 0, "pa": 0, "Tbl": 0, "Game": 1}
        tablesettings._table_rules_put("#TM0R001", 2, _rules)
        tablesettings._TABLE_SETTINGS.clear()                 # <- the restart
        _back = tablesettings._table_rules_get("#TM0R001", 2)
        if not _back:
            common._say("FAIL: a table's rules must survive an empty process cache -- "
                 "they are what code 0x13 is built from."); ok = False
        elif _back.get("tl") != 3 or _back.get("lu") != 99999:
            common._say("FAIL: the recovered rules must be the ones stored, got %r"
                 % (_back,)); ok = False
        # ...and the PAGE SELECTORS are not rules; storing them would put
        # `/Tbl=` and `/Game=` into a 0x13 body that has no such fields.
        elif "Tbl" in _back or "Game" in _back:
            common._say("FAIL: /Tbl= and /Game= are page selectors and must not be "
                 "stored as table rules. Got %r" % (sorted(_back),)); ok = False
        tablesettings._TABLE_SETTINGS.clear()
        matchmaking._announce_match(tmroom, "#TM0R001", 1, [(mid, 1), (mid + 1, 2)])
        if pushqueue._PUSHES:
            common._say("FAIL: @Save= (0x24) is the preset write, not Confirm -- it "
                 "must not start a match. Got %r" % (pushqueue._PUSHES,)); ok = False
        # ...but the Confirm (0x14) must, and on a table that filled up while
        # the dialog was open it must start it THERE, since @GameEN= is gone.
        seating._TABLE_CONFIRMED.add(("#TM0R001", 1))
        matchmaking._announce_match(tmroom, "#TM0R001", 1, [(mid, 1), (mid + 1, 2)])
        if not pushqueue._PUSHES:
            common._say("FAIL: once the owner has confirmed, a full table must start")
            ok = False
        pushqueue._PUSHES.clear(); matchmaking._MATCH_ACCEPTS.clear(); seating._TABLE_CONFIRMED.clear(); tmroom._CONFIRMED.clear()
        seating._SEATED.clear()

        # --- the PRESET write: ONE HALF, and the dwords stay dwords --------
        # WARNING: The failure this guards is worse than the bug it fixes. `@Save=`
        # carries all fourteen fields but only the SENDING PAGE filled its half
        # in; storing both halves would blank the other page every time. See
        # `table_preset_fields`, and `logs/authserv.log` 2026-08-18T06:45:57Z
        # for a real `/Game=1` whose whole `@Tab=` half was zeros.
        if tmsave is not None:
            _rules = (b"@Save=/Tbl=0/Game=1"
                      b"@Tet=/bm=0/du=0/st=1/cb=1/ca=1/gs=1/tl=3"
                      b"@Tab=/in=0/lu=0/ll=0/au=0/al=0/co=0/pa=0")
            _ch, _half = tablesettings.table_preset_fields(_rules)
            _tab_offs = {tmsave.TABLE_PRESET_BY_NAME[n][0]
                         for n in tmsave.TABLE_PRESET_TAB}
            if _tab_offs & set(_ch):
                common._say("FAIL: a /Game=1 (rules page) @Save= must store ONLY the "
                     "@Tet= half -- its @Tab= zeros are untouched globals, not "
                     "the player's choices. Got %r" % (sorted(_ch),)); ok = False
            if _ch.get(0x0D0) != (1, 3):
                common._say("FAIL: /tl=3 belongs at save +0x0D0 as one byte "
                     "(0x14DB44 reads it with `mov byte`). Got %r"
                     % (_ch.get(0x0D0),)); ok = False

            _restr = (b"@Save=/Tbl=1/Game=0"
                      b"@Tet=/bm=0/du=0/st=0/cb=0/ca=0/gs=0/tl=0"
                      b"@Tab=/in=0/lu=99999/ll=0/au=300/al=100/co=0/pa=0")
            _ch2, _half2 = tablesettings.table_preset_fields(_restr)
            _tet_offs = {tmsave.TABLE_PRESET_BY_NAME[n][0]
                         for n in tmsave.TABLE_PRESET_TET}
            if _tet_offs & set(_ch2):
                common._say("FAIL: a /Tbl=1 (restrictions page) @Save= must store ONLY "
                     "the @Tab= half. Got %r" % (sorted(_ch2),)); ok = False
            # WARNING: THE WIDTH IS THE WHOLE POINT for these four. `au=300` truncated
            # to a byte is 44, and a 0..44 rank band refuses everybody the same
            # way 0..0 did -- which is the measured refusal, re-manufactured.
            for _n, _want in ((b"au", 300), (b"al", 100), (b"lu", 99999)):
                _o, _w = tmsave.TABLE_PRESET_BY_NAME[_n]
                if _ch2.get(_o) != (4, _want):
                    common._say("FAIL: /%s=%d must be stored as a DWORD at +0x%03X. "
                         "Got %r" % (_n.decode(), _want, _o, _ch2.get(_o)))
                    ok = False

            # ...and a message naming NEITHER page is not a preset write.
            _ch3, _half3 = tablesettings.table_preset_fields(
                b"@Tet=/tl=0@Tab=/in=0")
            if _ch3 or _half3:
                common._say("FAIL: an @Save= with /Tbl=0 and /Game=0 names no page -- "
                     "0x87410 would have built the bare 0x14 pair instead. "
                     "Got %r" % (_ch3,)); ok = False

            # The factory header must carry what the client's own Default sends,
            # and the DWORD fields must be dwords.
            # WARNING: `au`/`al` WERE ASSERTED AT 300/100 HERE AND THAT WAS WRONG --
            # carried over from a code 0x14 Confirm, i.e. the TABLE tier. Page 2's
            # Default on the PRESET tier sends `lu=99999` and every other
            # restriction ZERO. See `tmsave.DEFAULT_TABLE_PRESET_RETRACTED`; do
            # not put the band back without settling which tier owns it.
            _minted = bytearray(tmsave.build([]))
            for _n, _want in ((b"lu", 99999), (b"au", 0), (b"al", 0)):
                _o, _w = tmsave.TABLE_PRESET_BY_NAME[_n]
                _got = struct.unpack_from("<I", _minted, _o)[0]
                if _got != _want:
                    common._say("FAIL: a minted save must carry /%s=%d at +0x%03X "
                         "(the client's own Default). Got %d"
                         % (_n.decode(), _want, _o, _got)); ok = False

        # --- the PIPE format itself, which is what 0xAB080 indexes ---------
        # WARNING: We emitted `/ID=0/ID=1` for weeks of session time. `0xAB080` finds
        # `name=` then steps past `index` copies of the separator at 0x51B2B78,
        # which is `|`; the value ends at the next `/` or `@`. A repeated KEY
        # gives occurrence >= 1 nothing to step to, and NONE of the call sites
        # checks the return before storing -- hence NOT_FOUND ids, squares for
        # names, and rules read from stack garbage (the auto-selecting timer).
        _pb = matchstart._vsgame_body([(mid, 1), (mid + 1, 2)], 0, [4, 5, 6, 7, 8, 9, 10])
        if b"/ID=0/ID=" in _pb or b"/R=4/R=" in _pb:
            common._say("FAIL: repeated KEYS -- values must be `|`-separated under ONE "
                 "key (0xAB15A steps over 0x51B2B78 = '|'). Got %r" % (_pb,))
            ok = False
        if selftest_tables._pipe_vals(_pb, b"/R=") != [b"4", b"5", b"6", b"7", b"8", b"9", b"10"]:
            common._say("FAIL: the seven rules must survive as a pipe list in order, "
                 "got %r" % (selftest_tables._pipe_vals(_pb, b"/R="),)); ok = False
        # The value must STOP at the next key, not swallow it.
        if selftest_tables._pipe_vals(_pb, b"/N=") != [b"2"]:
            common._say("FAIL: a value ends at the next `/` or `@` (0x51B3950), got %r"
                 % (selftest_tables._pipe_vals(_pb, b"/N="),)); ok = False

        # --- the recipient leads its own copy, whatever seat it holds ------
        # WARNING: Three runs, one rule (see `_vsgame_body`): the client matched its
        # id ONLY at occurrence 0, and answered ID=NOT_FOUND whenever its id was
        # later in the list -- including when the list carried the real content
        # ids. `/GID=` is NOT what `/ID=` is compared against: that is the global
        # at 0x294BD4 (`GAME-ID`, which reads 0), while `gameID = 1000001602`
        # comes from 0x2B52A0/A4, the pair feeding `/GID=`. Different variables.
        matchstart._note_game_id(mid, b"@Init=/NN=X/CN=Y/GID=000000003B9ACB2E/L=1")
        if matchstart._game_id_of(mid) != 1000000302:
            common._say("FAIL: /GID= is hex ASCII and must decode, got %r"
                 % (matchstart._game_id_of(mid),)); ok = False
        _seatsG = [(mid, 1), (mid + 1, 2)]
        _self = b"%d" % common._env_int("POL_TM_VSGAME_SELFID", 0)
        for _me in (0, 1):
            _bd = matchstart._vsgame_body(_seatsG, _me, [0] * 7)
            _ids = selftest_tables._pipe_vals(_bd, b"/ID=")
            if _ids[0] != _self:
                common._say("FAIL: the recipient at table seat %d must still be /ID= "
                     "occurrence 0 in its own copy -- got %r" % (_me, _ids))
                ok = False
            if _self in _ids[1:]:
                common._say("FAIL: no peer may carry the recipient's own id, or the "
                     "match stops on the wrong player -- got %r" % (_ids,))
                ok = False
        # ...and the reordering itself: the recipient's own NAME leads its own
        # copy. Asserted with names this test sets, so it cannot pass on
        # ambient state -- an earlier version did, and a negative run that
        # should have failed came back green.
        tmroom.note_name(mid, "AAAHOST")
        tmroom.note_name(mid + 1, "ZZZGUEST")
        _n0 = [_aad60(v.decode()) for v in
               selftest_tables._pipe_vals(matchstart._vsgame_body(_seatsG, 0, [0] * 7), b"/CN=")]
        _n1 = [_aad60(v.decode()) for v in
               selftest_tables._pipe_vals(matchstart._vsgame_body(_seatsG, 1, [0] * 7), b"/CN=")]
        if _n0[:2] != [b"AAAHOST", b"ZZZGUEST"]:
            common._say("FAIL: seat 0's copy must lead with its own name, got %r"
                 % (_n0,)); ok = False
        if _n1[:2] != [b"ZZZGUEST", b"AAAHOST"]:
            common._say("FAIL: seat 1's copy must ALSO lead with its own name -- the "
                 "client only ever matches itself at occurrence 0. Got %r"
                 % (_n1,)); ok = False
        matchstart._GAME_IDS.clear()

        # --- an unconfigured table must serve the CLIENT'S defaults, not 0 ----
        # Measured off the wire 2026-08-20; see TET_DEFAULTS. Zeros are not
        # merely un-default -- the wire reading measured au=0/al=0 as always refused.
        if ruleset._rule_seven(None) != [0, 0, 1, 1, 1, 1, 3]:
            common._say("FAIL: a table nobody has configured must serve the client's "
                 "own defaults, got %r" % (ruleset._rule_seven(None),)); ok = False
        # ...and a table that HAS been configured still wins.
        if ruleset._rule_seven({"tl": 9}) != [0, 0, 1, 1, 1, 1, 9]:
            common._say("FAIL: stored settings must override the defaults, got %r"
                 % (ruleset._rule_seven({"tl": 9}),)); ok = False
        # @VsGameInit/@WatchInfo use the PARSER's slot order, tl at occ 4 (the
        # match engine's timer slot [0xE3]), NOT @Tet's occ 6 -- the PvP 0:30 bug.
        if ruleset._vsgame_rule_seven(None) != [0, 1, 1, 1, 3, 0, 1]:
            common._say("FAIL: @VsGameInit defaults must put tl=3 at occ 4 (Special "
                 "Tile On, 3:00), got %r" % (ruleset._vsgame_rule_seven(None),)); ok = False
        if ruleset._vsgame_rule_seven({"tl": 9})[4] != 9:
            common._say("FAIL: the owner's timer must land at /R= occ 4 for @VsGameInit,"
                 " got %r" % (ruleset._vsgame_rule_seven({"tl": 9}),)); ok = False

        # --- @CardSelect=: the vector must be TRUE ----------------------------
        # The 2026-08-20T11:04Z fault: answering `/Ans=1` for every seat on the
        # FIRST player's pick told the host both had chosen; it went on, and the
        # guest arrived to a game that had moved and could not start.
        _seats2 = [(mid, 1), (mid + 1, 2)]
        _one = matchstart._cardselect_body(_seats2, {pushqueue._push_key(mid)}, 7, 0)
        if _one != b"43000700@CardSelect=/Ans=1|0/Ok=0":
            common._say("FAIL: with only seat 0 chosen the vector must be "
                 "/Ans=1/Ans=0, got %r" % (_one,)); ok = False
        # WARNING: AND IT IS RECIPIENT-FIRST. The player who just picked must read
        # `1` at occurrence 0 -- telling it `0` is what made P2's client
        # auto-select (measured 11:47:33Z, `/Ans=0/Ans=1` to the picker).
        _mineone = matchstart._cardselect_body(_seats2, {pushqueue._push_key(mid + 1)}, 7, 1)
        if _mineone != b"43000700@CardSelect=/Ans=1|0/Ok=0":
            common._say("FAIL: the picker must see its OWN readiness at occurrence 0, "
                 "got %r" % (_mineone,)); ok = False
        _both = matchstart._cardselect_body(_seats2, {pushqueue._push_key(mid), pushqueue._push_key(mid + 1)}, 7, 0)
        if _both != b"43000700@CardSelect=/Ans=1|1/Ok=1":
            common._say("FAIL: with both chosen the vector must be /Ans=1/Ans=1, "
                 "got %r" % (_both,)); ok = False
        # WARNING: /Ok= IS THE GO SIGNAL. 0x1031BC returns `/Ok= + 1`, and the
        # card-select STATE (0x17, arm 0xC282E) tests `cmp eax, 2` -- so only
        # `/Ok=1` advances it. 0 while anyone is still choosing keeps the scene
        # parked, which is exactly what it is for.
        if b"/Ok=1" not in _both:
            common._say("FAIL: with everyone chosen /Ok= must be 1 -- 0xC283A tests "
                 "`cmp eax, 2` and 0x1031BC returns /Ok=+1, so /Ok=0 parks the "
                 "scene for ever. Got %r" % (_both,)); ok = False
        if b"/Ok=0" not in _one:
            common._say("FAIL: while someone is still choosing /Ok= must be 0, got %r"
                 % (_one,)); ok = False

        # --- AN EMPTY ROSTER IS NOT "EVERYONE HAS CHOSEN" ---------------------
        # 2026-08-25T03:01:05Z: pol-git-sync recreated login/authsess while a
        # 2-player match was in CARD SELECT. 8s later the fresh process took
        # member 16's `@CardSelect=` with `seats == []`, and because `others`
        # filters OVER `seats` it came out vacuously empty -- "1 of 2 have
        # chosen" and then "...dealing: @StartData= to member 16". `deal_to =
        # seats or [(member_id, 0)]` dealt the sender a ONE-SIDED game; the
        # other player was never dealt in (this band never resends) and the
        # table's observer stayed on "players are selecting their cards".
        matchstart._CARD_READY.clear(); boardrules._MATCH_TURN.clear(); pushqueue._PUSHES.clear()
        boardrules._MATCH_HANDS.clear()
        _orph = dispatch.handle_line(b"43000700@CardSelect=/C=5"
                            b"@N0=/D=42|43|1|20|18|7|4|255"
                            b"@N1=/D=47|38|1|28|11|7|3|255"
                            b"@N2=/D=58|56|1|56|45|14|7|2"
                            b"@N3=/D=59|45|1|45|56|14|7|2"
                            b"@N4=/D=87|60|2|60|87|21|12|4",
                            member_id="orphanmatch", peer_nick=b"UTESTPEER")
        if boardrules._MATCH_TURN:
            common._say("FAIL: a @CardSelect= for a match with NO ROSTER (no seats, "
                 "not a COM game) must NOT deal -- it created %d turn "
                 "entry/entries, i.e. a one-sided game"
                 % (len(boardrules._MATCH_TURN),)); ok = False
        if any(b"@StartData=" in e[1] for e in pushqueue._PUSHES.get(pushqueue._push_key("orphanmatch"), ())):
            common._say("FAIL: a rosterless @CardSelect= must not queue @StartData= -- "
                 "that is the one-sided deal reaching the wire"); ok = False
        # ...and a VS. COM game, which legitimately has NO seats, must still
        # deal. This is the case `deal_to`'s solo fallback was written for, and
        # the gate above must not take it with it.
        matchstart._CARD_READY.clear(); boardrules._MATCH_TURN.clear(); pushqueue._PUSHES.clear()
        dispatch.handle_line(b"43000603@ComGame=/Rule=100|0|1|1|1|0|0|0/Com=1",
                    member_id="orphancom", peer_nick=b"UTESTPEER")
        pushqueue._PUSHES.pop(pushqueue._push_key("orphancom"), None)
        dispatch.handle_line(b"43000700@CardSelect=/C=5"
                    b"@N0=/D=42|43|1|20|18|7|4|255"
                    b"@N1=/D=47|38|1|28|11|7|3|255"
                    b"@N2=/D=58|56|1|56|45|14|7|2"
                    b"@N3=/D=59|45|1|45|56|14|7|2"
                    b"@N4=/D=87|60|2|60|87|21|12|4",
                    member_id="orphancom", peer_nick=b"UTESTPEER")
        if not boardrules._MATCH_TURN:
            common._say("FAIL: a VS. COM game has no seats BY CONSTRUCTION and must "
                 "still deal -- the rosterless gate must key on com_n, not on "
                 "`seats` alone"); ok = False

        # --- THE DEPLOY GATE MUST SEE A CARD-SELECT MATCH ---------------------
        # `count` was `len(_MATCH_TURN)`, which is not created until the deal,
        # so a match in card select reported 0 live matches and pol-git-sync /
        # pol-stale-check recreated login+authsess straight through it. That is
        # the restart above.
        import live_sessions
        _lmp = webwatch.LIVE_SERVICE
        matchstart._CARD_READY.clear(); boardrules._MATCH_TURN.clear(); matchmaking._MATCH_STARTED.clear()
        webwatch._live_matches_write()
        try:
            if live_sessions.read_marker(_lmp).get("count") != 0:
                common._say("FAIL: with no match anywhere the live marker must "
                     "report 0"); ok = False
        except Exception as _e:
            common._say("FAIL: the live marker was not written (%s)" % (_e,)); ok = False
        # A match that has STARTED but not yet dealt -- the exact blind spot.
        matchmaking._MATCH_STARTED[("#TM0RTEST", 4)] = True
        webwatch._live_matches_write()
        try:
            if live_sessions.read_marker(_lmp).get("count") != 1:
                common._say("FAIL: a match that has STARTED but not yet DEALT must "
                     "count as live -- this is the card-select blind spot that "
                     "let a deploy restart eat a match on 2026-08-25"); ok = False
        except Exception as _e:
            common._say("FAIL: the live marker was not written (%s)" % (_e,)); ok = False
        matchmaking._MATCH_STARTED.clear(); matchstart._CARD_READY.clear(); boardrules._MATCH_TURN.clear()
        webwatch._live_matches_write()
        # ==== THE BATTLE, DRIVEN THROUGH THE REAL HANDLER =================
        #
        # WARNING: THROUGH `handle_line`, NOT THROUGH `_apply_placement`. The hand
        # compaction check in this file's history did its `del` on a local list
        # and passed green with the real fix removed; a battle test that calls
        # the resolver directly would do the same thing, because the whole
        # question is whether a PLACEMENT reaches it.
        boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); boardrules._MATCH_BOARD.clear()
        matchstart._TURN_RAND.clear(); pushqueue._PUSHES.clear()
        matchmaking._remember_match("#TM0R001", 1, _seatsD)
        for _m, _v in _seatsD:
            matchmaking._MATCH_PEER[pushqueue._push_key(_m)] = b"UE7QN1N9G"
        # Two cards that MUST fight: tile 5 points EAST (bit 2 = 4), tile 6
        # points WEST (bit 6 = 64), so each has an arrow into the other.
        # Fields: id|attack|type|pdef|mdef|power|ARROWS|flag.
        _east, _west = b"100|50|0|10|10|0|4|0", b"101|50|0|10|10|0|64|0"
        # ...and one that cannot: tile 6 pointing NORTH is a free flip.
        _north = b"102|50|0|10|10|0|1|0"

        def _play(seat, turn, tile, rows):
            """One player's whole turn: deal them `rows`, then place slot 0."""
            boardrules._MATCH_HANDS.setdefault(("#TM0R001", 1), {})[
                pushqueue._push_key(_seatsD[seat][0])] = [bytearray(r) and r for r in rows]
            return dispatch.handle_line(protocol._turn_code(protocol.PUTDATA_CMD, turn)
                               + b"@PutData=/P=0/H=0/F=%d" % tile,
                               peer=b"auth-band", peer_nick=b"UE7QN1N9G",
                               member_id=_seatsD[seat][0])

        def _battles():
            return [e[1] for v in pushqueue._PUSHES.values() for e in v
                    if b"@BattleData=" in e[1]]

        _play(0, 0, 5, [_east])
        if _battles():
            common._say("FAIL: the first card on an empty board fights nobody -- got "
                 "%r" % (_battles(),)); ok = False
        if (boardrules._MATCH_BOARD.get(("#TM0R001", 1)) or {}).get(5) is None:
            common._say("FAIL: a relayed placement must be REMEMBERED -- without a "
                 "board model nothing can tell a battle from a flip"); ok = False
        pushqueue._PUSHES.clear()
        _play(1, 1, 6, [_west])
        _bd = _battles()
        # One battle, and it must reach BOTH seats: measured 2026-08-20T17:34Z,
        # placer and watcher alike printed -BattleStart:8 and parked in
        # 0x101A10(0xC).
        if len(_bd) != 2 or len(set(_bd)) != 1:
            common._say("FAIL: a contested placement owes EVERY seat the same "
                 "@BattleData on (0x43, %d) -- got %r"
                 % (protocol.BATTLEDATA_CMD, _bd)); ok = False
        elif not _bd[0].startswith(b"43000C01"):
            # 0x103C0D compares the third envelope byte against [obj+0x18D] and
            # bails with `Errot!!Turn%d!=%d`. Turn 1 is the turn it was played
            # on -- the client does not increment until GamePlayEnd.
            #
            # WARNING: THE HEADER IS A LITERAL ON PURPOSE. Written as
            # `_turn_code(BATTLEDATA_CMD, 1)` this assertion compares the
            # builder against ITSELF and passes with the turn byte hardcoded to
            # zero -- checked, it did. Code 0x43, command 0x0C, turn 0x01.
            common._say("FAIL: @BattleData must be 43 00 0C <turn> -- code 0x43, "
                 "command %d, and the turn it was FOUGHT on. Got %r"
                 % (protocol.BATTLEDATA_CMD, _bd[0][:8])); ok = False
        else:
            _a = selftest_tables._pipe_vals(_bd[0], b"/A=")
            _d = selftest_tables._pipe_vals(_bd[0], b"/D=")
            _r = selftest_tables._pipe_vals(_bd[0], b"/R=")
            if len(_a) != 5 or len(_d) != 5:
                common._say("FAIL: @BattleData is FIVE values a side -- tile, raw "
                     "stat, roll, selector, multiplier -- got /A=%r /D=%r"
                     % (_a, _d)); ok = False
            if len(_r) != 8:
                common._say("FAIL: /R= is exactly eight (0x103E52 `cmp esi, 8`), got "
                     "%r" % (_r,)); ok = False
            if (_a[:1], _d[:1]) != ([b"6"], [b"5"]):
                common._say("FAIL: occ0 is the TILE, attacker first -- the card just "
                     "placed attacks. Got /A=%r /D=%r" % (_a[:1], _d[:1]))
                ok = False
            if _a[3:] != [b"1", b"1"] or _d[3:] != [b"4", b"1"]:
                common._say("FAIL: a type-P attacker selects ATTACK (1) against "
                     "PHYSICAL defence (4), and a card with no ability has "
                     "multiplier 1 -- got /A=%r /D=%r" % (_a[3:], _d[3:]))
                ok = False
            if _a[1] != b"50" or _d[1] != b"10":
                common._say("FAIL: the raw stat is the SELECTED stat -- attack 50 vs "
                     "phys def 10, got %r / %r" % (_a[1], _d[1])); ok = False
            if not 0 <= int(_a[2]) <= 50 or not 0 <= int(_d[2]) <= 10:
                common._say("FAIL: a roll is bounded by its own raw stat, got %r / %r"
                     % (_a[2], _d[2])); ok = False
            # WARNING: AND THE SERVER'S BOARD MUST AGREE WITH THE NUMBERS IT SENT.
            # This is the one assertion that catches a desync: the clients do
            # not take our word for who won, they compute it themselves from
            # these two rolls (0xD34D0, the higher one takes the tile), so a
            # board that resolved it differently is two players looking at
            # different games with nothing on the wire to say so.
            _own = boardrules._MATCH_BOARD.get(("#TM0R001", 1)) or {}
            _hi = int(_a[2]) > int(_d[2])
            _want = (5, 1) if _hi else (6, 0)
            if (_own.get(_want[0]) or tmbattle.Card(_east, None)).owner != _want[1]:
                common._say("FAIL: /A= rolled %s and /D= rolled %s, so tile %d belongs "
                     "to seat %d -- the client works that out for itself from "
                     "the same two numbers (0xD34D0). Our board says %r"
                     % (_a[2], _d[2], _want[0], _want[1],
                        {t: c.owner for t, c in sorted(_own.items())}))
                ok = False
        # The board must have moved: one of the two tiles changed hands, and
        # every tile is owned by somebody.
        _sc = scoring._board_scores("#TM0R001", 1, 2)
        if sum(_sc) != 2 or _sc not in ([2, 0], [0, 2], [1, 1]):
            common._say("FAIL: two placements make two owned tiles, got %r" % (_sc,))
            ok = False
        if _sc == [1, 1]:
            common._say("FAIL: a battle always changes a tile -- 0xD34D0 has no "
                 "outcome that leaves both cards where they were (a DRAW "
                 "re-fights). Got %r" % (_sc,)); ok = False

        # ...AND THE SAME BATTLE THE OTHER WAY UP. The fixture above has a
        # 50-attack card against a 10-defence one, so the attacker wins on
        # essentially every roll -- which means it cannot tell a correct
        # resolver from one that always says ATTACKER. (Checked: it does not.)
        # This one is the mirror: attack 1 into physical defence 100.
        _weak, _wall = b"103|1|0|1|1|0|4|0", b"104|1|0|100|100|0|64|0"
        for _round in range(6):
            boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); boardrules._MATCH_BOARD.clear()
            matchstart._TURN_RAND.clear(); pushqueue._PUSHES.clear()
            matchmaking._remember_match("#TM0R001", 1, _seatsD)
            for _m, _v in _seatsD:
                matchmaking._MATCH_PEER[pushqueue._push_key(_m)] = b"UE7QN1N9G"
            _play(1, 0, 6, [_wall])
            pushqueue._PUSHES.clear()
            _play(0, 1, 5, [_weak])
            _bd2 = _battles()
            if not _bd2:
                common._say("FAIL: attack 1 into a wall is still a BATTLE -- both "
                     "cards point at each other, and the arrows are what "
                     "decide that, not the stats"); ok = False
                break
            _a2 = selftest_tables._pipe_vals(_bd2[0], b"/A=")
            _d2 = selftest_tables._pipe_vals(_bd2[0], b"/D=")
            if _a2[1] != b"1" or _d2[1] != b"100":
                common._say("FAIL: the raw stats here are attack 1 vs phys def 100, "
                     "got %r / %r" % (_a2[1], _d2[1])); ok = False
                break
            _own2 = boardrules._MATCH_BOARD.get(("#TM0R001", 1)) or {}
            _hi2 = int(_a2[2]) > int(_d2[2])
            _t2, _s2 = (6, 0) if _hi2 else (5, 1)
            if (_own2.get(_t2) or tmbattle.Card(_weak, None)).owner != _s2:
                common._say("FAIL: /A= rolled %s and /D= rolled %s, so tile %d belongs "
                     "to seat %d -- the losing side is the one that changes "
                     "hands. Our board says %r"
                     % (_a2[2], _d2[2], _t2, _s2,
                        {t: c.owner for t, c in sorted(_own2.items())}))
                ok = False
                break

        # A FREE FLIP IS NOT A BATTLE. The same geometry with the second card
        # pointing away takes the tile with no message at all -- and a server
        # that sent @BattleData here would put the client into a battle scene
        # its own arrow engine never entered.
        boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); boardrules._MATCH_BOARD.clear()
        matchstart._TURN_RAND.clear(); pushqueue._PUSHES.clear()
        matchmaking._remember_match("#TM0R001", 1, _seatsD)
        for _m, _v in _seatsD:
            matchmaking._MATCH_PEER[pushqueue._push_key(_m)] = b"UE7QN1N9G"
        _play(0, 0, 5, [_east])
        pushqueue._PUSHES.clear()
        _play(1, 1, 6, [_north])
        if _battles():
            common._say("FAIL: a card that does NOT point back is taken with no "
                 "battle (0xD3862 writes flag 2, not 1) -- got %r"
                 % (_battles(),)); ok = False
        if scoring._board_scores("#TM0R001", 1, 2) != [1, 1]:
            common._say("FAIL: tile 5 points east at tile 6, and tile 6 points north "
                 "-- neither takes the other, so the score is 1/1. Got %r"
                 % (scoring._board_scores("#TM0R001", 1, 2),)); ok = False
        # ...but the placer's own arrow DOES flip: put the north card down
        # first and then attack it from tile 5 with an east arrow.
        boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); boardrules._MATCH_BOARD.clear()
        matchstart._TURN_RAND.clear(); pushqueue._PUSHES.clear()
        matchmaking._remember_match("#TM0R001", 1, _seatsD)
        for _m, _v in _seatsD:
            matchmaking._MATCH_PEER[pushqueue._push_key(_m)] = b"UE7QN1N9G"
        _play(1, 0, 6, [_north])
        pushqueue._PUSHES.clear()
        _play(0, 1, 5, [_east])
        if _battles():
            common._say("FAIL: attacking a card that does not point back is a FLIP")
            ok = False
        if scoring._board_scores("#TM0R001", 1, 2) != [2, 0]:
            common._say("FAIL: a free flip hands the tile to the placer -- seat 0 "
                 "should hold both, got %r"
                 % (scoring._board_scores("#TM0R001", 1, 2),)); ok = False

        # --- LUCKY CARDS: the tally, and the Prize Center's own opener ------
        #
        # The board is built by hand rather than played, because `_play` rolls
        # dice and this has to assert an exact take. `placer` is what makes a
        # take legible: tile 6 was PLAYED by seat 1 and is OWNED by seat 0.
        boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); boardrules._MATCH_BOARD.clear()
        matchstart._TURN_RAND.clear(); pushqueue._PUSHES.clear()
        matchmaking._remember_match("#TM0R001", 1, _seatsD)
        _bd = boardrules._MATCH_BOARD.setdefault(("#TM0R001", 1), {})
        _bd[5] = tmbattle.Card(_east, 0)              # seat 0 played and kept
        _bd[6] = tmbattle.Card(_north, 1)             # seat 1 played it...
        _bd[6].owner = 0                              # ...and seat 0 took it
        _tk = scoring._board_takes("#TM0R001", 1, 2)
        if _tk != [[102], []]:
            common._say("FAIL: a TAKE is owner != placer -- seat 0 took card 102 off "
                 "seat 1 and seat 1 took nothing. Got %r" % (_tk,)); ok = False
        if any(c.placer != p for c, p in ((_bd[5], 0), (_bd[6], 1))):
            common._say("FAIL: `placer` must not move when `owner` does -- it is the "
                 "only thing that can tell a take from a play"); ok = False

        if tmprize is None:
            common._say("FAIL: tmprize did not import -- the Prize Center cannot be "
                 "served and no lucky card can be counted"); ok = False
        else:
            import shutil as _shutil
            import tempfile as _tempfile
            _root = _tempfile.mkdtemp(prefix="tm-selftest-prize-")
            _envsave = {k: os.environ.get(k) for k in
                        ("POL_RESOURCE_DIR", "POL_TM_LUCKY_POOL",
                         "POL_TM_CV_INIT")}
            try:
                os.environ["POL_RESOURCE_DIR"] = _root
                # Force the week's draw to a pool that CONTAINS the taken card,
                # so the tally is deterministic without waiting for a Sunday.
                os.environ["POL_TM_LUCKY_POOL"] = "102,105,106"
                scoring._tally_lucky("#TM0R001", 1, _seatsD)
                _mid0 = _seatsD[0][0]
                if tmprize.pending_lucky(_mid0) != 10:
                    common._say("FAIL: taking one of the week's lucky cards is worth "
                         "one card's points, got %r"
                         % (tmprize.pending_lucky(_mid0),)); ok = False
                if tmprize.pending_lucky(_seatsD[1][0]) != 0:
                    common._say("FAIL: the player who LOST a lucky card is owed "
                         "nothing"); ok = False

                # ...and the door hands over `@ExcInit`, not the card shop's
                # `@Init`. THIS is the assertion that would have caught the
                # Check Out bug a year earlier.
                os.environ["POL_TM_CV_INIT"] = "excinit"
                _cv = dispatch._handle_line(protocol.encode_code(protocol.MSG_CVREQ)
                                   + b"@CvReq=/NN=0000000000000000/HID=0"
                                     b"/Dm=0/Vol=0/CN=", member_id=_mid0)
                _cv = _cv if isinstance(_cv, list) else [_cv]
                if len(_cv) != 2 or b"@CvEnter=" not in _cv[0]:
                    common._say("FAIL: the Prize Center needs BOTH the endpoint "
                         "handshake and a (0xB2, 0x13) push, got %r"
                         % (_cv,)); ok = False
                elif b"@ExcInit=" not in _cv[1]:
                    common._say("FAIL: WARNING: the PRIZE CENTER's arm is @ExcInit (rva "
                         "0x1054EE), not the CARD SHOP's @Init -- that is the "
                         "same mistake the Check Out door made. Got %r"
                         % (_cv[1],)); ok = False
                elif _cv[1].count(b"/PP=") != 1 or _cv[1].count(b"/LC=") != 1:
                    common._say("FAIL: /PP= and /LC= are PIPE-separated single fields "
                         "-- repeated keys make every indexed read return the "
                         "first value. Got %r" % (_cv[1],)); ok = False
                # The award was PAID by that message, so a second visit in the
                # same week must offer nothing -- announcing twice pays twice.
                if tmprize.pending_lucky(_mid0) != 0:
                    common._say("FAIL: opening the Prize Center PAYS the pending "
                         "award; the same week must not still be owed"); ok = False
                _cv2 = dispatch._handle_line(protocol.encode_code(protocol.MSG_CVREQ)
                                    + b"@CvReq=/NN=0000000000000000/HID=0"
                                      b"/Dm=0/Vol=0/CN=", member_id=_mid0)
                _pp2 = (_cv2[1] if isinstance(_cv2, list) else _cv2)
                _pp2 = _pp2.split(b"/PP=")[1].split(b"/")[0].split(b"|")
                if [int(x) for x in _pp2] != [10, 0, 0, 0]:
                    common._say("FAIL: the SECOND visit shows the credited balance "
                         "and no pending award -- got %r" % (_pp2,)); ok = False
                # And `off` really is off, so the old behaviour is reachable.
                os.environ["POL_TM_CV_INIT"] = "off"
                _cv3 = dispatch._handle_line(protocol.encode_code(protocol.MSG_CVREQ)
                                    + b"@CvReq=/NN=0000000000000000/HID=0"
                                      b"/Dm=0/Vol=0/CN=", member_id=_mid0)
                if len(_cv3 if isinstance(_cv3, list) else [_cv3]) != 1:
                    common._say("FAIL: POL_TM_CV_INIT=off must send the handshake "
                         "alone"); ok = False
            finally:
                for k, v in _envsave.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                _shutil.rmtree(_root, ignore_errors=True)

        boardrules._MATCH_HANDS.clear(); boardrules._MATCH_TURN.clear(); boardrules._MATCH_BOARD.clear()
        matchstart._TURN_RAND.clear(); pushqueue._PUSHES.clear()

    except Exception as exc:
        import traceback as _tb
        common._say("FAIL: in-game selftest raised %r -- %s" % (exc, _tb.format_exc()))
        ok = False
    finally:
        (tmroom._KEY, tmroom._OWNER[0]) = saved[0], saved[1]
        for d, v in ((tmroom._TABLES, saved[2]), (tmroom._PEERS, saved[3]),
                     (tmroom._RECORDS, saved[4]), (tmroom._ROOMS_SEQ, saved[5]),
                     (seating._SEATED, saved_seated), (pushqueue._PUSHES, saved_pushes),
                     (matchmaking._MATCH_ROSTER, saved_roster),
                     (matchmaking._MATCH_ACCEPTS, saved_accepts),
                     (matchstart._CARD_READY, saved_ready), (matchmaking._MATCH_PEER, saved_peer),
                     (boardrules._MATCH_HANDS, saved_hands), (boardrules._MATCH_TURN, saved_turn),
                     (boardrules._MATCH_BOARD, saved_board), (matchstart._TURN_RAND, saved_rand)):
            d.clear(); d.update(v)
        seating._TABLE_CONFIRMED.clear(); tmroom._CONFIRMED.clear(); seating._TABLE_CONFIRMED.update(saved_conf)
        tmroom._SHARED.forget()
    return ok
