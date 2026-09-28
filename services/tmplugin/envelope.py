"""The game envelope (class G on the auth band): notice, which hands each line to the game or
answers it.
"""
import os
import re
import titles
import tetramaster
from . import chat, corenames


# ---------------------------------------------------------------------------
# the game envelope (class G on the auth band)
# ---------------------------------------------------------------------------
def notice(cls, payload, text, target, nick, srv, sess, tag=b"TM0"):
    """Tetra Master's own text format on the game envelope. Same
    envelope/class as Janhourou's binary channel ('G'), but the body is
    `<8 hex code><command>` -- see tetramaster.handle_line. The shared
    POLpro classes (P/R/A/L) are the core's: PASS hands them back.
    """
    if cls in (b"P", b"R", b"A", b"L"):
        return titles.PASS
    # *** A WHISPER IS DELIVERED, NOT ANSWERED. *** Measured live
    # 2026-08-21T01:45:38Z: a whisper is this same game envelope addressed
    # to the TARGET member's NICK --
    #     NOTICE USJ49D490 :GTM0G42000801@Chat=/La=1/Dt=#CHAT#\tUWAH
    # -- code 0x42 msgid 8, header shop byte 01 (the byte the panel widget
    # maps to display mode 2, the whisper presentation; preserved because
    # the fill never touches the header). It used to fall through to
    # tetramaster.handle_line and die as "captured, silent" -- never
    # delivered, no error to the sender. Same empty-name gate as room chat,
    # so the same fill; and the
    # sender gets an echo for the same measured reason (this client does
    # not self-render its own line). One session per side, first that
    # accepts: a member can hold room-band AND table-band sockets, and the
    # client's message store is process-global, so sending to every
    # session would ingest the line twice. POL_TM_CHAT_WHISPER=0 restores
    # the old swallowing (bisection only).
    whisper = None
    if os.environ.get("POL_TM_CHAT_WHISPER", "1") == "1":
        whisper = chat._tm_chat_fill_name(text, None, nick,
                                     member_id=corenames._session_get("member_id"))
    if whisper is not None:
        wprefix = b":" + nick + b"!~x@" + corenames._irc_host(srv) + b" NOTICE "
        delivered = echoed = 0
        for ts in corenames.PRESENCE.sessions_by_nick(target):
            if ts.send([wprefix + target + b" :" + whisper]):
                delivered = 1
                break
        for ts in corenames.PRESENCE.sessions_by_nick(nick):
            if ts.send([wprefix + nick + b" :" + whisper]):
                echoed = 1
                break
        corenames.log("authserv",
            f"  TM whisper {nick.decode('latin1')} -> "
            f"{target.decode('latin1')}: delivered={delivered} "
            f"echoed={echoed}"
            + ("" if delivered else " -- TARGET NOT ONLINE, dropped"))
        return None
    # *** THE TRADE NEGOTIATION IS DELIVERED, NOT ANSWERED -- like the
    # whisper above. *** Measured live 2026-08-22T04:44:59Z, the first
    # player trade ever initiated against this server:
    #     NOTICE UA4XX8PKP :GTM0G21000000@Tr=/NN=AB12CD31DC3F63B6/HID=0/...
    #     NOTICE UA4XX8PKP :GTM0G23000000@TrCan=          (63 s later)
    # -- code 0x21 `@Tr=` addressed to the TARGET PLAYER'S NICK (not the
    # room service peer), body carrying only the SENDER's identity. The
    # target client polls codes 0x21/0x25/0x23/0x2A/0x2C every frame in
    # the lobby scene (its standing trade ear),
    # so a verbatim forward lands in its message store and the
    # 0x21 handler at TM.dll 0x089B02 consumes it. Both fell through to
    # handle_line and died "captured, silent" -- the invite never reached
    # the other player.
    #
    # Verbatim, and NO ECHO -- deliberately unlike chat: the sender's own
    # client polls the same codes, so an echoed `@Tr=` would pop a trade
    # invite FROM THEMSELVES on the initiator's screen. `@TrAns=` (0x22)
    # and `@TrCanAck=` (0x2A) ride the same relay on the return leg,
    # addressed to the initiator's nick. POL_TM_TRADE_RELAY=0 restores the
    # old swallowing (bisection only).
    # VERIFIED: THE TRADE IS PEER-TO-PEER, and the SERVE-A-LIST model is RETRACTED
    # (static RE 2026-08-22, tm-trade-negotiation.md). The trade board is a
    # SCENE (TM.dll tick 0x15590) both clients enter after the accept, and
    # it runs its OWN handshake on codes the server must RELAY, not author:
    #   * @TrID= / @TrEnt=  (code 0xA0) -- state 1 sends these to the
    #     partner; state 3 consumes the relayed @TrID. NOT in the old key
    #     set, so they fell through to handle_line and DIED -- a definite
    #     blocker no matter which scene runs.
    #   * @Card=/@Data=/@Quit= on code 0xA2, msgid 25/26/27 -- the live
    #     card-list / selection / decide exchange. The client addresses
    #     these to the SESSION PEER ([0x5242958], set by the @Init(24) the
    #     accept serve authors below), which folds to the PARTNER's login
    #     nick, so they arrive here addressed to the partner exactly like
    #     @Tr=. They are 0xA2-family and share the @Card=/@Data= strings
    #     with the auction, so they are matched by CODE+MSGID, not string.
    # All relayed verbatim, framed from the sender, NO echo -- same law as
    # @Tr= (the sender polls the same codes; an echo self-invites).
    # VERIFIED:VERIFIED: `@TrEnt=` / `@TrID=` (code 0xA0) ARE A REQUEST TO US, NOT PEER
    # TRAFFIC -- measured 2026-08-24T16:38:33Z, the FIRST TIME this project
    # has ever seen either on the wire:
    #
    #   NOTICE UKJ7LOE8G :GTM0GA0000000@TrEnt=
    #        /NN=AB12CD1EEEAE9C3B|0000000000000000/HID=0|0/Dm=0|0/Vol=0|0
    #        -> TARGET NOT ONLINE, dropped
    #
    # `UKJ7LOE8G` is a SERVICE nick -- the same one `@Init=` is addressed to
    # -- so relaying it peer-to-peer drops it every time, which is what that
    # log line is. The client is asking the server to admit it to the trade
    # session, and the trade board (TM.dll tick 0x15590) then PARKS IN STATE
    # 2 polling code 0xA1 for the answer. That park is the "Now Loading"
    # screen seen in live testing: no error, `err=0x00`, the board simply
    # waiting on a reply we have never sent.
    #
    # `@TrEntAns`'s only appearance in the image is the PARSE site at
    # 0x8A9BE -- the client never builds it, so it is server-authored by
    # construction.
    #
    # WARNING: `/Ans=` IS A GRANT CODE, NOT AN IDENTITY. The reading this block
    # used to carry ("`0x8A9DF` reads `/Ans=` and stores the result as a
    # 64-bit at `[0x5242958]`") is RETRACTED -- it conflated two different
    # slots of the same stack frame, and it is why the board sat in state 2
    # all evening with `code=0xA1 -> FOUND` in its own store. Read the frame
    # (`0x8A950`, F = esp after the prologue):
    #
    #   0x8A972  record  = F+0x28            <- 0x825A0(out, 0xA1, -1)
    #   0x8A976  F+0x14  = 0                 <- the return slot, preset 0
    #   0x8A9BE  0xAA920(record, "@TrEntAns", &F+0x18)
    #   0x8A9DF  0xAB080("/Ans=", &F+0x18, 0, &F+0x14)   <- out is F+0x14
    #   0x8A9E9  [0x5242958] = F+0x30 / F+0x34 = **record+0x8/+0xC**
    #   0x8AA23  edi = F+0x14 ; ... ; return edi
    #
    # So `[0x5242958]` comes off the RECORD's own sender stamp -- i.e. from
    # how the NOTICE is FRAMED, not from anything we write in the body --
    # and `/Ans=` is the FUNCTION'S RETURN VALUE. `0xAB080` ends at 0xAB3F4
    # with `call 0x1E2C30` (atoi) and `mov [out], eax`, negated on a leading
    # '-'. It is a DECIMAL int parser.
    #
    # `atoi("AB12CD31DC3F63B6") == 0`. That is the whole bug: we served a
    # 16-hex-digit POL-ID into a field the client atoi's, got 0, and 0 is
    # exactly the value that keeps the board where it was.
    #
    # The board's use of the return (tick 0x15590 state 2, 0x15915):
    #
    #     edi = 0x8A950()
    #     edi >  0          -> [0x52281FC]=0xF, [+0x164]=0, jmp 0x16308
    #                          and 0x16308 is `inc [esi+0x1BC]` -- STATE 3
    #     edi == -256       -> error dialog 0x19D
    #     edi <  0          -> 0x194030(edi), the server-error message table
    #     edi == 0          -> 0x195260(scene, esi, 0, 0xBA); that returns 0,
    #                          `je 0x1630E` skips the increment -> PARK
    #
    # The same shape, with the same `jle` guard, is how the client reads
    # every other `/Ans=` in the image -- `@CardEnt` at 0x8BA50 and 0x8BEB9
    # both `cmp eax,0 / jle <skip>` before adopting the session destination.
    # A small POSITIVE integer means GRANTED; <= 0 means it is not.
    #
    # KEY: AND THE TEN "RESOURCE BITS" WERE NEVER THE GATE. `[load+0x160]` is
    # set at 0x12EFD5 -- `ebp = 1 << i ; or [esi+0x160], ebp` -- inside the
    # loading widget's own tick 0x12EBE0, and its guard at 0x12EFBC is
    # `percent >= 100 && frame == 3`. The bits are a CONSEQUENCE of the
    # progress float reaching 100, not a precondition for it. Progress is
    # clamped to 80.0f (0x158A4) for as long as sub-state `[board+0x1A0]`
    # is 0, and nothing in state 2 ever moves that sub-state -- only states
    # 3 and 9 do (0x15A9F, 0x15DC6). `bits=0x000/0x3FF` was the symptom of
    # a board stuck one state earlier, not a missing resource load.
    _TRADE_KEYS = (b"@Tr=", b"@TrAns=", b"@TrCan=", b"@TrCanAck=",
                   b"@TrID=", b"@TrEnt=")
    if (b"@TrEnt=" in payload or b"@TrID=" in payload)                 and os.environ.get("POL_TM_TRENT_ANS", "1") == "1":
        try:
            _me = corenames._session_get("member_id")
            _pm = None
            try:
                # BOTH SIDES of the binding -- a one-sided lookup returned
                # None live at 01:52:03Z and cost the partner its grant.
                _pm = tetramaster.trade_partner_of(_me)
            except Exception:
                pass
            # VERIFIED: A GRANT CODE. Both id spaces were tried and both parked
            # the board, for the same reason -- neither is a decimal number:
            #
            #   2026-08-24T16:52:25Z  /Ans=000000E13883D826   guid_of
            #   2026-08-24T21:39:21Z  /Ans=AB12CD31DC3F63B6   pol_id_of
            #                                                 atoi -> 0 both
            #
            # The second one is the run that logged `store lookup #226037:
            # code=0xA1 -> FOUND` and STILL did not move: the record landed,
            # the name matched, and the field parsed to the one value that
            # means "not granted". `POL_TM_TRENT_ANS_VALUE` overrides it
            # without a rebuild; anything <= 0 is a refusal, and a negative
            # is looked up in the client's server-error table (0x194030).
            try:
                _ans = (os.environ.get("POL_TM_TRENT_ANS_VALUE", "1")
                        .strip().encode("latin1")) or b"1"
                int(_ans)          # decimal or the client reads it as 0
            except Exception:
                _ans = b"1"
            _peer_id = 1 if _pm is not None else 0
            # WARNING: RETURN THE LINE, DO NOT SEND IT. This is the whole bug,
            # and probe_lookup settled it: the client polled code 0xA1
            # **2,168 times, every one "not found"** while `store_count=1`.
            # Three send variants (idle-drain framing, relay framing, two id
            # spaces) all reported `sent=True` and none ever reached the
            # store -- because a TM0 SERVICE answer does not travel that way.
            #
            # The proven path is a few hundred lines below, and it is how
            # `@TeachDVAns`, `@InitAns` and `<CS>` have always worked:
            #
            #     out.append(NoPad(_game_notice_line(
            #         b"G" + tag + b"G" + r, target, nick, srv)))
            #     return out
            #
            # i.e. the reply is RETURNED, and `_auth_channel_loop` sends it
            # on the socket the request arrived on -- the "answered with N
            # line(s)" line in the log. An out-of-band
            # `PRESENCE.sessions_by_nick(...).send()` is the PUSH path, which
            # is for unprompted traffic to a peer, not for answering a
            # service request. The framing was right the first time; the
            # delivery mechanism never was.
            _body = b"A1000000@TrEntAns=/Ans=" + _ans
            # VERIFIED: AND THE STATE-3 RECORD, `(0xA2, 24) @Init` -- MEASURED LIVE
            # 2026-08-24T23:19:58Z: with /Ans=1 the board left state 2 on
            # tick #372 and parked in state 3, which is exactly what state
            # 3 polls for. It rides the SAME two paths as the grant (reply
            # + partner push) because it is wanted one tick later and a
            # store record keeps until something consumes it.
            #
            # The arm, read at 0x1064CA (`0x101A10(n)` tries (0x43,n), then
            # (0xB2,n) at 0x1051BB, then (0xA2,n) at 0x10648C; n=24 is arm 0
            # of the 0xA2 table and prints `---->Recv=TRADEINIT`):
            #
            #   0xAA920(rec, "@Init")            required, else bail
            #   0xAB080("/ID=")   -> low byte -> [sess+0x31]   THE CHANNEL
            #   sprintf("@No%d", [sess+0x19B])   <- the recipient's OWN index
            #   0xAA920(rec, "@No<idx>")         required, else bail
            #     0xAB080("/RA=") -> [sess+4]  dword
            #     0xAB080("/M=")  -> [sess+0]  word
            #     0xAB080("/CP=")
            #     0xAB080("/S=")  -> [sess+0xEC] word
            #   0x10B6B0(rec) = rec+0x8/+0xC -> [sess+0x28/0x2C]  (self-stamp)
            #
            # We serve BOTH `@No0=` and `@No1=` because `[sess+0x19B]` is the
            # recipient's own seat and we do not get to see it; extra
            # elements are inert (0xAA920 is a container lookup by name).
            # Zeros are the invents-nothing default -- a trade has no rank,
            # money or board yet.
            #
            # PARTIAL: `/ID=0` IS THE ONE GUESS. `/ID=`'s low byte becomes
            # `[sess+0x31]`, and EVERY later 0xA2 arm gates on
            # `0x10B6C0(rec) == [sess+0x31]` (0x10668D, 0x1066B0, ...) where
            # `0x10B6C0` is `rec+0x3D0`. Both sides of that comparison
            # initialise to 0 -- the session at 0x101196, the record at
            # 0xA8359 -- so 0 is the self-consistent choice. **If it is
            # wrong the failure is specific and visible: `---->Recv=
            # TRADEINIT` appears and the board advances, and then 25/26/27
            # are silently dropped at 0x106CBC.** That symptom means read
            # what the store stamps into rec+0x3D0 (writers 0x826AD /
            # 0x827A7 / 0x828AB) and put THAT in `/ID=`.
            _init_on = os.environ.get("POL_TM_TRADE_INIT24", "1") == "1"
            _no = os.environ.get("POL_TM_TRADE_INIT24_NO",
                                 "/RA=0/M=0/CP=0/S=0").encode("latin1")
            _init_body = (b"A2001800@Init=/ID="
                          + os.environ.get("POL_TM_TRADE_INIT24_ID",
                                           "0").strip().encode("latin1")
                          + b"@No0=" + _no + b"@No1=" + _no)
            corenames.log("authserv",
                "  TM trade: answering %s with (0xA1) @TrEntAns=/Ans=%s "
                "(from member %r, partner %r) AS A REPLY on this socket "
                "-- %s"
                % (b"@TrID=" if b"@TrID=" in payload else b"@TrEnt=",
                   _ans.decode(), _me, _pm,
                   "watch for the board to leave state 2" if _peer_id else
                   "WARNING: PARTNER UNKNOWN -- the grant still stands, but the "
                   "partner gets no copy and will park in state 2"))
            if _init_on:
                corenames.log("authserv",
                    "  TM trade: + (0xA2,24) %s -- the state-3 record. "
                    "Watch the client's own trace for "
                    "'---->Recv=TRADEINIT', then the board probe for "
                    "state=0x4" % _init_body.decode("latin1"))
            # KEY: AND THE PARTNER NEEDS ONE TOO -- IT NEVER ASKS.
            # State 1 branches on the role: index 0 SENDS @TrID=/@TrEnt=,
            # index 1 SKIPS. Both then park in state 2 polling code 0xA1.
            # So answering only the requester leaves the other client
            # waiting for ever on a message it is not designed to request --
            # which is exactly what the second test machine's screen has been
            # showing. The static fix order said "to BOTH" and this is what
            # that meant.
            #
            # Measured: only 127.0.0.1 ever sends 0xA0, and the other
            # machine's store polled code 0xA1 4,924 times, all misses,
            # because we had sent it nothing. That machine's store DOES
            # take pushes -- `code=0x2C -> FOUND` is our own go flag -- so
            # the partner's copy goes out on the same synchronous
            # freshest-session path the go flag uses.
            #
            # WARNING: The "id space MIRRORED, each side is told the OTHER side's
            # POL-ID" note that used to live here went with the retracted
            # reading above: `/Ans=` is not an identity at all, so there is
            # nothing to mirror. Both copies carry the SAME grant code.
            # `[0x5242958]` is set from the record's own sender stamp
            # (`record+0x8`), i.e. from how this NOTICE is FRAMED -- so if
            # the destination ever needs to change, change `target` in the
            # `_game_notice_line` call, not the body.
            try:
                if _pm is not None:
                    _pbody = b"A1000000@TrEntAns=/Ans=" + _ans
                    # The partner's SESSIONS, freshest first -- and its
                    # nick comes off the session rather than a lookup
                    # table, so the line is addressed to whatever that
                    # connection actually calls itself.
                    _pses = sorted(
                        corenames.PRESENCE.sessions_for(int(_pm)),
                        key=lambda x: (x.in_room_recently(),
                                       getattr(x, "last_heard", 0)),
                        reverse=True)
                    _pnick = getattr(_pses[0], "nick", None) if _pses else None
                    if _pnick:
                        _ppad = (b" " if os.environ.get(
                            "POL_GAME_NOTICE_PAD") == "space" else b"")
                        _psent = False
                        # The grant FIRST, then the state-3 record, on the
                        # one session that takes them -- order matters only
                        # in that the board consumes them one state apart.
                        _pbodies = ([_pbody, _init_body] if _init_on
                                    else [_pbody])
                        for _ps in _pses:
                            _plines = [
                                corenames.NoPad(corenames._game_notice_line(
                                    b"GTM0G" + _b, target,
                                    getattr(_ps, "nick", _pnick), srv))
                                for _b in _pbodies]
                            if _ps.send(_plines, pad_override=_ppad):
                                _psent = True
                                break
                        corenames.log("authserv",
                            "  TM trade: PARTNER copy of (0xA1) "
                            "@TrEntAns=/Ans=%s%s pushed to %s (member %r) "
                            "sent=%s -- index 1 never sends @TrEnt but "
                            "parks in state 2 all the same"
                            % (_ans.decode(),
                               " + (0xA2,24) @Init" if _init_on else "",
                               _pnick.decode("latin1", "replace"),
                               _pm, _psent))
                    else:
                        corenames.log("authserv",
                            "  TM trade: PARTNER copy NOT sent -- no nick "
                            "for member %r" % (_pm,))
            except Exception as _pe:
                corenames.log("authserv",
                    f"  TM trade: partner @TrEntAns copy failed: {_pe!r}")
            return [corenames.NoPad(corenames._game_notice_line(b"GTM0G" + _b, target,
                                            nick, srv))
                    for _b in (([_body, _init_body] if _init_on
                                else [_body]))]
        except Exception as e:
            corenames.log("authserv", f"  TM trade @TrEnt answer failed: {e!r}")
        return None
    _trade_hit = any(k in payload for k in _TRADE_KEYS)
    # WARNING: INITIALISE OUTSIDE THE BRANCH. The `_TRADE_KEYS` hit above skips the
    # `if not _trade_hit` block entirely, so a flag set only in there is
    # UNDEFINED on the `@Tr=` path -- i.e. a NameError on the invite, the
    # one message that has always worked. Caught before shipping; the classic
    # shape of a switch that does not reach every caller.
    _trade_a2 = False
    _rpm = None
    if not _trade_hit and os.environ.get("POL_TM_TRADE_RELAY", "1") == "1":
        # Code+msgid match for the 0xA2 negotiation (25 TRADEQUIT / 26
        # TRADELIST / 27 TRADESELECT). payload is cls+ebody; the ebody
        # carries the 8-hex header decode_ebody reads.
        try:
            _code, _ = tetramaster.decode_ebody(payload[1:])
        except Exception:
            _code = None
        # 25 (TRADEQUIT) and 27 (TRADESELECT) always; 26 (TRADELIST)
        # behind POL_TM_TRADE_RELAY26. The old blanket exclusion of 26
        # rested on the "a SECOND (0xA2,26) record crashes" theory, which
        # was RETRACTED (2026-08-23: the first authored 20-card record
        # crashes; 08-22's 11-18-card records did not -- the crash tracks
        # LOGGED LINE LENGTH, see note_trade_accept). Relaying the
        # clients' OWN lists is the protocol end-state: the native builder
        # chunks at <=15 cards/message, under the suspected logger
        # threshold by construction. Default OFF only so the length A/B
        # (short authored list) runs single-variable; flip to 1 for the
        # relay-model run and set POL_TM_TRADE_CARD=0 with it.
        #
        # VERIFIED: 26 IS ON BY DEFAULT NOW (2026-08-24T23:32Z). Both clients
        # reached card selection and sent their OWN lists, and we dropped
        # every one of them:
        #
        #   m6  A2001A00@Card=/P=0/E=1/S=0/C=1@N0=/D=8|9|0|9|5|26
        #   m3  A2001A00@Card=/P=1/E=1/S=0/C=1@N0=/D=57|65|1|35|45|6
        #   m3  A2001A00@Card=/P=1/E=0                      <- terminator
        #
        # That is the native builder's own shape, measured, and it settles
        # the /E=//S=//C= argument: one @N<i>= section per card, /C= the
        # count in THIS message, /E= a more-follow flag (0 terminates).
        # `/P=` is the sender's own SEAT (m6 sent 0, m3 sent 1) and is
        # global, not per-recipient -- so relay it VERBATIM, never rewrite.
        #
        # WARNING: THE KNOB IS GONE ON PURPOSE, AND IT HAD TO BE. Changing this
        # module's default from "0" to "1" did NOTHING on prod:
        # `docker-compose.prod.yml` line 129 pins
        # `POL_TM_TRADE_RELAY26: "${POL_TM_TRADE_RELAY26:-0}"`, so the
        # container env carries a literal 0 and `os.environ.get(k, "1")`
        # never sees its own default. Verified in the RUNNING container,
        # not in the file: `docker inspect ... .Config.Env` -> RELAY26=0.
        # And a deploy pipeline that ignores docker-compose*.yml would
        # ship nothing for a compose edit without a hand-recreate.
        # Two defaults for one setting, the compose one winning silently --
        # the same compiled-vs-deployed-defaults shape seen elsewhere.
        #
        # The knob only ever existed to keep the authored-list length A/B
        # single-variable. That A/B is over and the authored serve is gone;
        # relaying each client's OWN list is the protocol, so there is no
        # longer a question for a switch to answer.
        #
        # KEY: AND THE SET IS THE CLIENT'S OWN DISPATCH TABLE, not a guess.
        # 0x1064BB bounds `n-24` at 0x10 and indexes the byte table at
        # 0x5096E14, which selects an arm from 0x5096DF0:
        #
        #   24  0x1064CA  @Init        TRADEINIT   (we author this one)
        #   25  0x106645  @Quit        TRADEQUIT
        #   26  0x1067B5  @Card        TRADELIST
        #   27  0x106A7A  @Data        TRADESELECT
        #   28  0x106B42  @Data        TRADEDECIDE   <- the CONFIRM
        #   30  0x106BFF  @Quit        TRADECLOSE
        #   39  0x1066E2  @Dead
        #   40  0x106759  @DataError
        #   29, 31..38    -> 0x106CBC, the bail; not real messages
        #
        # The old allowlist stopped at 27, so both clients could pick cards
        # and SEE each other's picks -- 26 and 27 relayed -- and then had no
        # way to agree, because 28 was dropped on the floor. Live report,
        # 2026-08-24T23:52Z: "I have cards selected on both sides, and I
        # don't see the option to actually do the trade." 27 `@Data=` is the
        # 30-slot selection state (`/P=1/M=1/D=1|0|0|...`) and it was
        # flowing; 28 is the decision that follows it.
        _relay_msgids = (24, 25, 26, 27, 28, 30, 39, 40)

        if _code is not None and (_code & 0xFF) == 0xA2 \
                and ((_code >> 16) & 0xFF) in _relay_msgids:
            _trade_hit = True
            _trade_a2 = True
    if os.environ.get("POL_TM_TRADE_RELAY", "1") == "1" and _trade_hit:
        # WARNING: FRESHEST SESSION FIRST, measured 2026-08-22T05:02:59Z: the first
        # relayed @Tr= reported delivered=1 and the target client showed
        # NOTHING -- the write went to one of the member's several RESUMED
        # zombie sessions (three resumes + a ghost re-attach in the half
        # hour before), whose dead socket buffers bytes without erroring.
        # `alive` only turns False when a write fails, so "first that
        # accepts" is exactly the wrong order. The room-band heartbeat
        # (<DR> every 1-3 s) is the proof of life; sort by it.
        # KEY: THE TRADE BOARD DOES NOT ADDRESS THE PARTNER -- IT ADDRESSES
        # THE SERVICE, and the whisper relay below can never deliver that.
        # Measured 2026-08-24T23:32-23:33Z, board live in card selection:
        #
        #   NOTICE UKJ7LOE8G :GTM0GA2001A00@Card=/P=0/E=1/S=0/C=1@N0=...
        #   NOTICE UKJ7LOE8G :GTM0GA2001900@Quit=/P=1
        #                     ^ the SERVICE nick -> candidates=0, dropped
        #
        # Why: `[0x5242958]` is the board's send destination, and 0x8A950
        # sets it from the `@TrEntAns` RECORD's sender stamp (record+0x8) --
        # which is how WE framed that NOTICE, i.e. `target`. So the board
        # talks back to whoever we answered as, by construction.
        #
        # And the return leg has to keep that framing. Every 0xA2 arm gates
        # TWICE before it will look at a record (0x106660):
        #
        #   0x10B6B0(rec) = rec+0x8/+0xC  ==  [sess+0x28/0x2C]   sender
        #   0x10B6C0(rec) = rec+0x3D0     ==  [sess+0x31]        channel
        #
        # and `[sess+0x28/0x2C]` was self-stamped from OUR `@Init` record at
        # 0x1064F3. So a relayed card list must arrive framed from the SAME
        # nick `@Init` came from -- `target` -- or the partner drops it
        # silently at 0x106CBC. Framing it from the sending PLAYER (what the
        # whisper relay does, and correct for `@Tr=`) fails that gate.
        _a2_to = None
        if _trade_a2:
            try:
                _rm = corenames._session_get("member_id")
                _rpm = tetramaster.trade_partner_of(_rm)
                if _rpm is not None:
                    _a2_to = sorted(
                        corenames.PRESENCE.sessions_for(int(_rpm)),
                        key=lambda s: (s.in_room_recently(),
                                       getattr(s, "last_heard", 0)),
                        reverse=True)
            except Exception as _re:
                corenames.log("authserv",
                    f"  TM trade: 0xA2 partner lookup failed: {_re!r}")
        if _a2_to:
            # from `target` (the service we answered as), to the partner.
            delivered = 0
            chosen = b"-"
            for ts in _a2_to:
                _tn = getattr(ts, "nick", None)
                if not _tn:
                    continue
                if ts.send([corenames.NoPad(corenames._game_notice_line(text, target, _tn,
                                                    srv))]):
                    delivered = 1
                    chosen = ts.peer_ip
                    break
            corenames.log("authserv",
                f"  TM trade 0xA2 -> PARTNER member {_rpm}: "
                f"{tetramaster.describe_line(payload[1:])} "
                f"delivered={delivered} via={chosen.decode('latin1')} "
                f"sessions={len(_a2_to)} -- framed from {target!r} so the "
                f"partner's [sess+0x28] sender gate passes"
                + ("" if delivered else " -- NOT DELIVERED"))
            # CAPTURE BOTH HALVES AS THEY GO PAST. The offer lists (26) and
            # the selection bitmaps (27) are everything needed to apply the
            # swap server-side, and we are already relaying them, so nothing
            # extra has to be asked for. See `apply_trade_swap`.
            try:
                _mid = (_code >> 16) & 0xFF
                if _mid == 26:
                    tetramaster.note_trade_offer(_rm, text)
                elif _mid == 27:
                    tetramaster.note_trade_select(_rm, text)
            except Exception as _pe2:
                corenames.log("authserv",
                    f"  TM trade: capture failed: {_pe2!r}")
            # KEY: THE COMMIT IS OURS TO SEND. `(0xA2,28) @Data=/M=3` is the
            # only message in the trade the CLIENT CANNOT BUILD: a scan for
            # a `push 0x1C` + `push 0xA2` builder finds nothing, and the arm
            # itself (0x106B42) proves the shape -- on `/M= == 3` it checks
            # **both** `[sess+0x19D] == 2` (my mode) and `[sess+0x1CD] == 2`
            # (the partner's, set from their msgid-27 `/M=`) before calling
            # 0x106E30, the commit. A self-sent message would not need to
            # verify its own sender's mode. So it is server-authored, like
            # `@TrEntAns` and `@Init` -- we are the trade service.
            #
            # Measured 2026-08-24T23:57Z+, both clients live in the board:
            #
            #     6x @Data=/P=0/M=1     selection changes
            #     3x @Data=/P=0/M=2     READY
            #     6x @Data=/P=1/M=1
            #     3x @Data=/P=1/M=2     READY
            #
            # Both sides at mode 2, nothing else on the wire but @Pong, and
            # the trade sat there -- the tester had ticked the checkbox by
            # their name and hit confirm on both. Arm 28 needs only the
            # `@Data` element, the two gates and `/M=`, so the whole record
            # is `A2001C00@Data=/M=3`. Sent to BOTH, framed from `target`
            # exactly like the relay above, and ONCE per readiness edge.
            if ((_code >> 16) & 0xFF) == 27 and _rpm is not None:
                try:
                    _m = re.search(rb"/M=(\d+)", text)
                    _mode = int(_m.group(1)) if _m else -1
                    _tk = tetramaster._push_key(_rm)
                    _pk = tetramaster._push_key(_rpm)
                    # setdefault, NOT `.get(...) or {}` -- the latter
                    # mutates a throwaway dict when an entry is missing, so
                    # readiness would never persist and the commit would
                    # silently never fire. A silent no-op is the worst
                    # failure mode this file has.
                    _me_st = tetramaster._TRADE_PENDING.setdefault(_tk, {})
                    _pa_st = tetramaster._TRADE_PENDING.setdefault(_pk, {})
                    # Readiness is an EDGE, both ways: mode 2 arms, anything
                    # else disarms, so backing out of a confirm and
                    # re-confirming works and we never double-commit.
                    _me_st["ready"] = (_mode == 2)
                    if _mode != 2:
                        _me_st.pop("committed", None)
                        _pa_st.pop("committed", None)
                    if (_mode == 2 and _pa_st.get("ready")
                            and not _me_st.get("committed")):
                        _me_st["committed"] = True
                        _pa_st["committed"] = True
                        # WARNING: `GTM0G` IS NOT OPTIONAL. The first cut of this
                        # sent the bare record and the client never saw it:
                        # arm 28 prints `---->Recv=TRADEDECIDE` BEFORE it
                        # parses `/M=`, and no TRADEDECIDE appeared in the
                        # trace, so it was thrown out at the class/framing
                        # gate rather than at the mode check. Every other
                        # send in this file carries the prefix -- the relay
                        # passes `text`, which already has it, and the
                        # @TrEntAns/@Init path writes `b"GTM0G" + body`.
                        _cbody = b"GTM0GA2001C00@Data=/M=3"
                        _cpad = (b" " if os.environ.get(
                            "POL_GAME_NOTICE_PAD") == "space" else b"")
                        _cgot = []
                        for _cm in (_rm, _rpm):
                            _sent = False
                            for _cs in sorted(
                                    corenames.PRESENCE.sessions_for(int(_cm)),
                                    key=lambda s: (s.in_room_recently(),
                                                   getattr(s, "last_heard",
                                                           0)),
                                    reverse=True):
                                _cn = getattr(_cs, "nick", None)
                                if not _cn:
                                    continue
                                if _cs.send([corenames.NoPad(corenames._game_notice_line(
                                        _cbody, target, _cn, srv))],
                                        pad_override=_cpad):
                                    _sent = True
                                    break
                            _cgot.append("m%s=%s" % (_cm, _sent))
                        corenames.log("authserv",
                            "  TM trade: BOTH SIDES READY (/M=2) -- sent "
                            "(0xA2,28) @Data=/M=3, the commit the client "
                            "cannot build, to %s. Watch for the cards to "
                            "actually change hands (arm 0x106B42 -> "
                            "0x106E30)." % ", ".join(_cgot))
                except Exception as _ce:
                    corenames.log("authserv",
                        f"  TM trade: commit trigger failed: {_ce!r}")
            # PARTIAL: AND THE CLIENTS ANSWER THE DECIDE. Measured 00:52Z: after
            # `(0xA2,28) @Data=/M=3` landed (`---->Recv=TRADEDECIDE` in the
            # client's own trace, so it passed BOTH gates), each client sent
            #
            #     GTM0GA2001C00@Ans=/P=0/M=2
            #     GTM0GA2001C00@Ans=/P=1/M=2
            #
            # addressed to the SERVICE. Arm 28 only matches `@Data`, so
            # relaying these to the partner is a no-op -- they are for US.
            # `@Ans=` is built at 0x10904D by the same trade-send dispatcher
            # as `@Data=`, carrying `/P=` from `[sess+0x19B]` and `/M=`.
            #
            # VERIFIED:VERIFIED: CONFIRMED LIVE 2026-08-25T01:02:12Z -- THE TRADE
            # COMPLETED. Arm 28 has exactly two branches: `/M=3` (gated on
            # both modes == 2) and `/M=4` at 0x106BE9, which has no gates and
            # makes 0x101A10(28) return 1 for the pump's drain at 0x19E41.
            # The full handshake, measured:
            #
            #   BOTH SIDES READY (/M=2)    -> (0xA2,28) @Data=/M=3
            #   both clients  @Ans=/P=n/M=2
            #   BOTH SIDES ANSWERED        -> (0xA2,28) @Data=/M=4
            #   -> `---->Recv=TRADEDECIDE` twice, board leaves state 8
            #
            # So the decide is a TWO-PHASE exchange and the service drives
            # both halves. POL_TM_TRADE_ANS4=0 disables.
            if (((_code >> 16) & 0xFF) == 28 and b"@Ans=" in text
                    and _rpm is not None
                    and os.environ.get("POL_TM_TRADE_ANS4", "1") == "1"):
                try:
                    _am = re.search(rb"/M=(\d+)", text)
                    _amode = int(_am.group(1)) if _am else -1
                    _ak = tetramaster._push_key(_rm)
                    _apk = tetramaster._push_key(_rpm)
                    _a_me = tetramaster._TRADE_PENDING.setdefault(_ak, {})
                    _a_pa = tetramaster._TRADE_PENDING.setdefault(_apk, {})
                    _a_me["ans"] = (_amode == 2)
                    if (_amode == 2 and _a_pa.get("ans")
                            and not _a_me.get("done")):
                        _a_me["done"] = True
                        _a_pa["done"] = True
                        _fbody = b"GTM0GA2001C00@Data=/M=4"
                        _fpad = (b" " if os.environ.get(
                            "POL_GAME_NOTICE_PAD") == "space" else b"")
                        _fgot = []
                        for _fm in (_rm, _rpm):
                            _fs_ok = False
                            for _fs in sorted(
                                    corenames.PRESENCE.sessions_for(int(_fm)),
                                    key=lambda s: (s.in_room_recently(),
                                                   getattr(s, "last_heard",
                                                           0)),
                                    reverse=True):
                                _fn = getattr(_fs, "nick", None)
                                if not _fn:
                                    continue
                                if _fs.send([corenames.NoPad(corenames._game_notice_line(
                                        _fbody, target, _fn, srv))],
                                        pad_override=_fpad):
                                    _fs_ok = True
                                    break
                            _fgot.append("m%s=%s" % (_fm, _fs_ok))
                        corenames.log("authserv",
                            "  TM trade: BOTH SIDES ANSWERED (@Ans=/M=2) "
                            "-- sent (0xA2,28) @Data=/M=4, phase 2 of the "
                            "decide exchange, to %s. CONFIRMED LIVE "
                            "2026-08-25T01:02Z -- the trade completes."
                            % ", ".join(_fgot))
                        # KEY: AND NOW ACTUALLY MOVE THE CARDS. Collections
                        # are server-authoritative, so without this the
                        # swap lives only on the two clients and is gone at
                        # the next launch -- live report, 01:1xZ, having
                        # completed a trade minutes earlier: "can confirm
                        # the trade did not persist". All-or-nothing; see
                        # apply_trade_swap for the refusal cases.
                        _rep = tetramaster.apply_trade_swap(_rm, _rpm)
                        corenames.log("authserv",
                            "  TM trade: COLLECTIONS -- %s" % _rep)
                        # KEY: AND CLOSE THE SESSION. Board state 10 polls
                        # `0x101A10(0x1E)` = msgid 30 TRADECLOSE, and on a
                        # hit it sets sub-state 1 (progress 80 -> 101, so
                        # the loading screen finishes) and advances 10 -> 11
                        # (0x15F34/0x15F3A). Without it the board parks at
                        # `state=0xA pct=80` -- live report, 02:16Z: "got to
                        # finishing the trade but now still at the now
                        # loading when completing". Same clamp-at-80 park as
                        # the original state-2 bug, one state from the end.
                        #
                        # Arm 30 (0x106BFF) wants `@Quit` + the usual two
                        # gates, then parses `/P=` and `/B=`, calls
                        # 0x101530 (the player-data recalc) and clears
                        # `[sess+0xEA]` -- the teardown. `/P=` is the
                        # recipient's OWN seat, which is how the client uses
                        # /P= in every record it sends (@Card, @Data, @Ans);
                        # we captured it from those. `/B=` lands in a byte
                        # at [0x524648E]; 0 is the invents-nothing default.
                        # POL_TM_TRADE_CLOSE=0 disables, POL_TM_TRADE_CLOSE_B
                        # overrides /B= without a rebuild.
                        if os.environ.get("POL_TM_TRADE_CLOSE", "1") == "1":
                            _bv = os.environ.get(
                                "POL_TM_TRADE_CLOSE_B", "0").strip()
                            _cgot2 = []
                            for _qm2 in (_rm, _rpm):
                                _seat = (tetramaster._trade_state(_qm2)
                                         .get("seat"))
                                _qbody = (b"GTM0GA2001E00@Quit=/P=%d/B=%s"
                                          % (int(_seat or 0),
                                             _bv.encode("latin1")))
                                _ok2 = False
                                for _qs in sorted(
                                        corenames.PRESENCE.sessions_for(int(_qm2)),
                                        key=lambda s: (
                                            s.in_room_recently(),
                                            getattr(s, "last_heard", 0)),
                                        reverse=True):
                                    _qn = getattr(_qs, "nick", None)
                                    if not _qn:
                                        continue
                                    if _qs.send([corenames.NoPad(corenames._game_notice_line(
                                            _qbody, target, _qn, srv))],
                                            pad_override=_fpad):
                                        _ok2 = True
                                        break
                                _cgot2.append("m%s(/P=%s)=%s"
                                              % (_qm2, _seat, _ok2))
                            corenames.log("authserv",
                                "  TM trade: CLOSE -- sent (0xA2,30) "
                                "@Quit= to %s; board state 10 polls msgid "
                                "30 and only then finishes its loading"
                                % ", ".join(_cgot2))
                except Exception as _ae:
                    corenames.log("authserv",
                        f"  TM trade: @Ans follow-up failed: {_ae!r}")
        else:
            tprefix = b":" + nick + b"!~x@" + corenames._irc_host(srv) + b" NOTICE "
            cand = sorted(corenames.PRESENCE.sessions_by_nick(target),
                          key=lambda s: (s.in_room_recently(),
                                         getattr(s, "last_heard", 0)),
                          reverse=True)
            delivered = 0
            chosen = b"-"
            for ts in cand:
                if ts.send([tprefix + target + b" :" + text]):
                    delivered = 1
                    chosen = ts.peer_ip
                    break
            corenames.log("authserv",
                f"  TM trade relay {nick.decode('latin1')} -> "
                f"{target.decode('latin1')}: "
                f"{tetramaster.describe_line(payload[1:])} "
                f"delivered={delivered} via={chosen.decode('latin1')} "
                f"candidates={len(cand)}"
                + ("" if delivered else " -- TARGET NOT ONLINE, dropped"))
        # KEY: A `(0xA2,25) @Quit=` IS THE END OF THE TRADE, AND UNTIL NOW
        # NOBODY TOLD THE SERVER. Live report, 2026-08-24T23:33Z: "even though
        # we had a graceful trade quit, our lobby status hasn't changed from
        # 'busy' -- I'd still have to restart TM to do another trade." The
        # quit was addressed to the service nick and dropped (see above), so
        # `_TRADE_PENDING` kept the pair armed and the busy mark stood. Now
        # that we actually see it, disarm both sides.
        # BOTH `@Quit` arms end it: 25 TRADEQUIT and 30 TRADECLOSE.
        if _trade_a2 and ((_code >> 16) & 0xFF) in (25, 30):
            try:
                _qm = corenames._session_get("member_id")
                _qpm = tetramaster.trade_partner_of(_qm)
                tetramaster.note_trade_cancel(
                    *[x for x in (_qm, _qpm) if x is not None])
                corenames.log("authserv",
                    "  TM trade: (0xA2,25) @Quit= -- trade ENDED, "
                    "disarmed members %r and %r" % (_qm, _qpm))
            except Exception as _qe:
                corenames.log("authserv",
                    f"  TM trade: @Quit disarm failed: {_qe!r}")
        # ARM THE TRADE-SESSION HANDSHAKE (static RE 2026-08-22, the
        # serve-a-list model RETRACTED). On an accepted @TrAns=/Ans=1 both
        # clients enter the trade board scene (TM.dll 0x15590) and run the
        # @TrID/@TrEnt (0xA0) -> @TrEntAns (0xA1) -> @Init (0xA2,24)
        # handshake before any card exchange. note_trade_accept now queues
        # @TrEntAns + @Init framed from the PARTNER (states 2/3), NOT the
        # old (0xA2,1)+(0xA2,26) list. The card lists are RELAYED, never
        # authored (each client sends its own via the relay above).
        #
        # WARNING: THE PRIME REMAINING SUSPECT for the "immediate error after
        # accept": scene 0x15590 STATE 0 walks the client's table array
        # (0x521bb70) for a row whose status byte +0x28 == 'D' and jumps to
        # the error state (0x12c) if NONE is found -- BEFORE state 1 sends
        # @TrID. So the decisive measurement for the next run is simply:
        # does EITHER client emit an inbound @TrID= (code 0xA0)? If YES,
        # state 0 passed and @TrEntAns/@Init are the next links to verify.
        # If NO @TrID ever arrives, state 0 (the 'D' table) is the blocker
        # and the handshake serve is moot -- solve the table status next.
        # POL_TM_TRADE_SERVE=0 disables the authored side entirely.
        # KEY: THE VS-COM-SHAPED PATH, RESTORED (2026-08-24, live report: "we
        # were able to get it a bit farther when I first started on this and
        # it was using the same sort of approach as VS COM").
        #
        # It was removed by `20bd5087` on the strength of a STATIC trace --
        # "the 0x2C flag sets a byte read nowhere, the (0x41,7) wake is never
        # polled" -- and that overruled two LIVE measurements which are still
        # the best evidence we have:
        #
        #  * `08409060`, ~26,000 client store lookups off the shim log: the
        #    client polls exactly two groups, the trade family
        #    **`0x2C` 0x2A 0x25 0x23 0x21** (15,927x) and the game family
        #    `0x41` `0x1A` (10,521x). 0x2C is polled constantly, and it is
        #    the ONE code in that ear with no client-side send site -- i.e.
        #    server-push only, which is what a go-signal looks like.
        #  * `1c54cd08`, 05:30Z: after the 0x2C flag landed **the accepter's
        #    scene ADVANCED** to polling (0x41,7) + 0x1A -- the same bare
        #    wake-up + @GameOwner pair the VS reservation flow waits on.
        #    (0x41,7)'s handler (0x8477F) parses NOTHING and drains (0x41,9)
        #    and (0x41,0x10) behind it: arrival alone is the event, the shape
        #    of "stop waiting, it is happening".
        #
        # That "scene advanced" is the furthest this has ever got, and every
        # model since has gone backwards from it. Three months of static
        # reading has since falsified its own successors -- the game band
        # (watchdog flat at 0 across three runs), the seat ('D' row; seating
        # both players changed nothing) -- so the static objection that
        # retired this one no longer outranks the measurement.
        #
        # WARNING: THE 0x25 PROBE IS NOT RESTORED. It was a vocabulary-harvest
        # device, never a fix, and 20bd5087's one durable observation is that
        # an unmatchable 0x25 body gets injected into the card list as a
        # garbage entry. No reason to carry that risk into a fix attempt.
        #
        # WARNING: MIXED EVIDENCE ON THE WAKE, kept honest: `f03a913c` reverted the
        # (0x41,7) push because at 15:20Z it was delivered and sat UNREAD --
        # that run's accepter polled only the ear codes. The 14:14Z run did
        # poll it. That inconsistency was blamed on a leftover SEAT, and the
        # seat model is now dead, so the real cause is unexplained. Hence its
        # own switch: POL_TM_TRADE_WAKE=0 leaves the go flag alone.
        #
        # POL_TM_TRADE_GO=0 disables both.
        if delivered and b"@TrAns=" in payload and b"/Ans=1" in payload \
                and os.environ.get("POL_TM_TRADE_GO", "1") == "1":
            try:
                _both = {corenames._session_get("member_id"), corenames._sess_member_id(cand[0])}
                _wake = os.environ.get("POL_TM_TRADE_WAKE", "1") == "1"
                # WARNING: THE GO FLAG MUST GO OUT **SYNCHRONOUSLY**, AND THE
                # FIRST ATTEMPT AT THIS PROVED IT (2026-08-24T14:17Z).
                # Queued via `_queue_push` it rode the idle drain and was
                # filed by the client **4.0 s after the accept**
                # (14:17:10.81 accept -> 14:17:14.82 push), while the
                # client's own probe_file order shows the trade already
                # over by then:
                #
                #     FILED 21000000@Tr=        the invite
                #     pgate = 0xFEAFDC3F        the partner gate SET
                #     pgate = 0x00000000        ZEROED -- the trade is done
                #     FILED 2C000100@TrGo=      ...the flag lands HERE
                #
                # `POL_TM_IDLE_MIN_S` is 4 s and the scene dies in about one
                # frame, so the queue can never win that race -- it is the
                # same "+14 s vs a scene that errors in seconds" trap the
                # card-list serve already fell into. The one mechanism
                # MEASURED to reach the client's store promptly is the
                # idle-drain framing sent on the freshest session at accept
                # time, which is what the board serve below uses. Use it.
                #
                # Both directions: `nick` sent the @TrAns (the accepter),
                # `target` is the initiator it answers, and each copy is
                # framed FROM the other one -- the id key that decides which
                # object the client thinks the message is about.
                _pad = (b" " if os.environ.get("POL_GAME_NOTICE_PAD")
                        == "space" else b"")

                def _sync_send(_to, _from, _body, _what):
                    try:
                        _c = sorted(corenames.PRESENCE.sessions_by_nick(_to),
                                    key=lambda s: (s.in_room_recently(),
                                                   getattr(s, "last_heard", 0)),
                                    reverse=True)
                        _l = corenames.NoPad(corenames._game_notice_line(
                            b"GTM0G" + _body, _from, _to, srv))
                        for _s in _c:
                            if _s.send([_l], pad_override=_pad):
                                return True
                    except Exception as _e:
                        corenames.log("authserv", f"  TM trade {_what} send failed: {_e}")
                    return False

                # WARNING: RETRACTED, AND THIS IS THE STUCK "BUSY". The note here
                # said "the handler (0x08CC7F) sets `go` when the MSGID is
                # non-zero". 0x08CC7F is not a go handler -- it is the only
                # consumer of code 0x2C in the whole image (swept: one call
                # site), and it lives inside 0x8CC40, which is the client's
                # **am-I-busy predicate**:
                #
                #   0x8CC73  push 0x2c ; 0x825A0(out, 0x2C, -1)
                #   0xA8A63  jne  0xa8a74          found -> busy
                #   0xA8A65  push 0x11             block[11] = 17 = 'R'
                #   0xA8A79  mov  al, [0x524648E]  otherwise, this byte
                #
                # block[11] is the 12th letter of the member record's
                # 16-letter status block (base 0x5245F8B, so block[11] =
                # 0x5245F96, setter 0xA7B70). Measured on BOTH players all
                # evening: 'A' -> 'R' the moment a trade starts, and never
                # back, which is the live report's "both users still show as
                # being busy" and why only a TM restart -- which clears the
                # store -- ever fixed it.
                #
                # The block base is nailed by two independent constants: the
                # reset at 0xA7DF0 writes 'I' to 0x5245F98 and 'B' to
                # 0x5245F9A, which are block[13] and block[15] in every
                # observed string, and block[0] is the NAME LENGTH ('D'=3
                # for a 3-letter name, 'L'=11 for an 11-letter one).
                #
                # So the flag's ONLY effect on the client is to make the
                # player report itself busy. It does not open the board and
                # it advances nothing -- the board comes up through the
                # handshake we now serve in full (@TrEntAns -> @Init ->
                # ... -> the (0xA2,30) close). It is scaffolding from before
                # that was understood, and it is actively harmful, so it is
                # OFF by default. POL_TM_TRADE_GOFLAG=1 restores it for an
                # A/B; POL_TM_TRADE_GO=0 still disables the wake as well.
                _go_a = _go_i = False
                if os.environ.get("POL_TM_TRADE_GOFLAG", "0") == "1":
                    _go_a = _sync_send(nick, target,
                                       b"2C000100@TrGo=/P=0", "go flag")
                    _go_i = _sync_send(target, nick,
                                       b"2C000100@TrGo=/P=0", "go flag")
                # THE WAKE STAYS QUEUED, and that is deliberate rather than
                # an oversight. The 05:30Z measurement is that the scene
                # advanced to POLLING (0x41,7) only AFTER the flag landed --
                # so the wake only means anything if the flag worked, and if
                # it worked there is a live scene a few seconds later to
                # receive it. Sending both in one breath would also violate
                # the stagger law `_queue_push` documents.
                if _wake:
                    for _m in _both:
                        if _m is not None:
                            tetramaster._queue_push(
                                _m, b"41000700@GameStart=",
                                why="trade accepted: the (0x41,7) wake the "
                                    "advanced scene was measured polling")
                corenames.log("authserv",
                    "  TM trade accept -- 0x2C go flag: accepter=%s "
                    "initiator=%s (OFF by default -- its only consumer is "
                    "the client's am-I-busy predicate 0x8CC40)%s"
                    % (_go_a, _go_i,
                       "; (0x41,7) wake queued behind it" if _wake
                       else "; wake OFF"))
            except Exception as e:
                corenames.log("authserv", f"  TM trade go-push failed: {e}")
        if delivered and b"@TrAns=" in payload and b"/Ans=1" in payload:
            try:
                # (member, partner member, partner nick) both ways round:
                # `nick` sent this @TrAns (the accepter), `target` is who
                # it answers (the initiator). The partner nick is the
                # SOURCE the session pushes ride -- see note_trade_accept.
                _acc = corenames._session_get("member_id")
                _ini = corenames._sess_member_id(cand[0])
                # Role-aware (2026-08-24): the accepter's scene polls
                # (0xA2,1)+list; the initiator's 25/26 waiter wants the
                # list ONLY (an init would clog its ring unconsumed).
                tetramaster.note_trade_accept((_acc, _ini, target, "acc"),
                                              (_ini, _acc, nick, "ini"))
                # SERVE THE ACCEPTER NOW, VIA ITS FRESHEST SESSION, WITH
                # THE IDLE-DRAIN'S EXACT MECHANICS (2026-08-24). History
                # of this block, one failure per model: the idle-push
                # queue delivered at +14s (scene errors in seconds); the
                # in-this-reply serve was transmitted on the live @TrAns
                # socket and the client NEVER FILED it (zero store
                # FOUNDs, the purge popped nothing -- 01:49Z run). The
                # ONE path that has measurably FILED a served (0xA2,26)
                # is the idle drain's chat_sess.send of a
                # NoPad(_game_notice_line(...)) with the game-notice pad
                # override (00:40:46Z, consumed by the 26-waiter). Same
                # bytes, same framing -- just synchronously at accept
                # time, on the freshest session (the relay's zombie
                # lesson, 28c8d7fc). Re-serves stay as backup.
                # WARNING: THIS SERVE HAS ITS OWN COPY OF THE SWITCH, AND IT WAS
                # MISSING. `POL_TM_TRADE_SERVE` was defaulted OFF in
                # tetramaster.py, but the three gates there cover
                # note_trade_accept / the re-serve tick / the @Init serve --
                # NOT this synchronous accept-time send, which calls
                # `_trade_entry_lines` directly. So the 14:17Z run still put
                # three (0xA2,*) lines on the wire ("3 line(s) sent to the
                # accepter NOW" in the log) while the commit message said the
                # authored serve was off, and the run tested two changes
                # instead of one. A switch that does not reach every caller
                # is not a switch.
                _served = 0
                _serve_on = os.environ.get("POL_TM_TRADE_SERVE", "0") == "1"
                try:
                    _acand = [] if not _serve_on else sorted(
                        corenames.PRESENCE.sessions_by_nick(nick),
                        key=lambda s: (s.in_room_recently(),
                                       getattr(s, "last_heard", 0)),
                        reverse=True)
                    _pad = (b" " if os.environ.get("POL_GAME_NOTICE_PAD")
                            == "space" else b"")
                    for _why, _line in ([] if not _serve_on
                                        else tetramaster._trade_entry_lines(
                                            _acc, _ini, "acc")):
                        _nline = corenames.NoPad(corenames._game_notice_line(
                            b"GTM0G" + _line, target, nick, srv))
                        for _ts in _acand:
                            if _ts.send([_nline], pad_override=_pad):
                                _served += 1
                                break
                except Exception as e:
                    corenames.log("authserv",
                        f"  TM trade accept-time serve failed: {e}")
                corenames.log("authserv",
                    ("  TM trade accept -- board serves armed for both "
                     "sides; %d line(s) sent to the accepter NOW "
                     "(idle-drain framing, freshest session), initiator "
                     "via the <DR>-clocked re-serves." % _served)
                    if _serve_on else
                    "  TM trade accept -- authored (0xA2,*) board serve is "
                    "OFF (POL_TM_TRADE_SERVE=1 restores it); the go flag is "
                    "the accept path now")
            except Exception as e:
                corenames.log("authserv", f"  TM trade serve arm failed: {e}")
        # A cancel (either direction) disarms it -- the board is gone.
        elif delivered and (b"@TrCan=" in payload
                            or b"@TrCanAck=" in payload):
            try:
                tetramaster.note_trade_cancel(
                    corenames._session_get("member_id"), corenames._sess_member_id(cand[0]))
            except Exception:
                pass
        return None
    # Tetra Master's own text format. Same envelope/class as Janhourou's
    # binary channel ('G'), but the body is `<8 hex code><command>` -- see
    # tetramaster.handle_line. Answering @TeachDV clears the "retrieving
    # default data" timeout; the rest is captured, not invented.
    if os.environ.get("POL_TM0", "1") != "1":
        corenames.log("authserv", f"  TM0 disabled -- {tetramaster.describe_line(payload[1:])}")
        return None
    try:
        # VERIFIED: MEMBER AND PEER ON EVERY TM0 LINE. Two clients' logs have to
        # be readable as ONE conversation -- without the member id, an
        # interleaved capture of two players is unattributable, and every
        # question in this subsystem so far has been "which of them did
        # that". The peer nick is here too because it IS the table id
        # (`tetramaster.table_index_for_peer`).
        _tm_member = corenames._session_get("member_id")
        corenames.log("authserv", f"  TM0[m{_tm_member} {target!r}] <- "
                        f"{tetramaster.describe_line(payload[1:])}")
        # `target` is the peer NICK the client addressed -- the TM0 service
        # peer. tetramaster derives `/Shm=` (the @Init destination id) from
        # it; see the banner above `_teach_shm`. Nothing else uses it.
        # THE CLIENT'S OWN ID. `@Init=/NN=` carries the guid it calls
        # itself by, and it arrives on EVERY session -- unlike class-L
        # `<PC>`, which the roster also learns from but which is not
        # guaranteed. See `tmroom.note_guid`.
        # (An `@Init=/NN=` guid capture lived here and was REMOVED: it is
        # the 64-bit id, not the lobby-band one the member record needs.
        # See the `<PC>` note in `_roster_note`.)
        # `member_id` lets tetramaster PERSIST what the client tells us --
        # the guild from `@Init=/GLD=` lands in that member's save at
        # +0x3B, which is the byte the guild screen's gate reads.
        reply = tetramaster.handle_line(payload[1:], peer="auth-band",
                                        peer_nick=target,
                                        member_id=corenames._session_get("member_id"))
    except Exception as e:
        corenames.log("authserv", f"  tetramaster raised on {payload[:80]!r}: {e} -- silent")
        return None
    # VERIFIED: DERIVE THE CLIENT'S ID KEY FROM THIS CONNECTION (2026-08-22). Both
    # halves are already here and nowhere else: `nick` is the client's login
    # nick (whose base-36 fold is the wire form of its own id) and
    # `tmroom.pol_id_of` is the SAME id in app form, as the client sent it in
    # `@Init=/NN=`. `K` is what makes a published table id round-trip -- see
    # `tmroom.CLIENT_KEY_DEFAULT`. Cheap, idempotent, and it LOGS a
    # disagreement with the compiled default instead of hiding one.
    try:
        import tmroom as _tmroom
        _self = _tmroom.pol_id_of(_tm_member)
        if _self:
            _tmroom.learn_client_key(_self, tetramaster.peer_guid(nick))
    except Exception:
        pass
    if not reply:
        # WARNING: SAY WHAT WAS DROPPED. This line used to name neither the code
        # nor the body, so a client stuck polling an unanswered slot looked
        # identical to one idling -- which is exactly what it cost on
        # 2026-09-07T02:05Z: the post-game "Change Settings" screen polled
        # every 15 s and the log could only say that SOMETHING went
        # unanswered. A bodyless poll decodes to no `@Cmd=` at all, so the
        # repr of the raw payload is the only thing that identifies it.
        corenames.log("authserv", "  TM0: no answer for this command -- captured, "
                        "silent: %r" % (bytes(payload[:64]),))
        return None
    # handle_line may answer with SEVERAL E-bodies: the card shop needs its
    # @ShEnter reply followed by an UNSOLICITED SHOPINIT push, because the
    # client sends nothing more once it has the endpoint. A bare bytes reply
    # stays a single line, so every other caller is unaffected.
    replies = reply if isinstance(reply, (list, tuple)) else [reply]
    out = []
    for r in replies:
        # Per-item, not one try around the loop: handle_line has already
        # POPPED any pending pushes into `replies`, so an encode raise here
        # used to kill the whole connection with those bodies in hand --
        # and this band never resends. One malformed body now costs itself,
        # with a traceback, not the reply and every push behind it.
        try:
            corenames.log("authserv", f"  TM0[m{_tm_member} {target!r}] -> "
                            f"{tetramaster.describe_line(r)}")
            out.append(corenames.NoPad(corenames._game_notice_line(b"G" + tag + b"G" + r,
                                               target, nick, srv)))
        except Exception:
            import traceback as _tb
            corenames.log("authserv", f"  TM0 encode of {r[:80]!r} raised -- skipped:\n"
                            f"{_tb.format_exc()}")
    return out
