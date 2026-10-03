"""The entry point for every TM0 line: handle_line and the _handle_line command switch."""
import os
import random
import tmbattle
import re
import time
from tmcup import event_info, event_phase, event_window
from .deps import tmauction, tmprize
from . import (
    boardrules, cardshop, cardtables, careerstats, collection, common, matchend, matchmaking,
    matchstart, opener, peers, placement, pots, protocol, purse, pushqueue, rematch,
    reservation, roomchat, ruleset, savefile, scoring, seating, shopdoors, tableaudit, tablerow,
    tablesettings, tournament, trade, turns, vscom, watchers, webwatch,
)


def handle_line(body, peer="-", peer_nick=None, member_id=None):
    """Decode one TM0 E-body and return the reply, with any PENDING PUSH behind it.

    VERIFIED: THE PUSH IS WHY THIS WRAPPER EXISTS. `@MuchMake` is server-initiated
    (the command table: `@MuchMakeAns=` is an ANSWER, the only inverted command in the family)
    and this band has no spontaneous send -- every byte we emit is a reply to a
    line the client sent. So a push RIDES one, exactly as `@GameQA` rides the
    `@GameM=` reply and SHOPINIT rides `@ShEnter`. The client drains its message
    store by (code, msgid), not by request/response pairing, which is what makes
    that legal rather than a trick.

    WARNING: `@Pong=` IS THE CARRIER OF LAST RESORT, and it is a good one: the client
    sends one every ~2 s (76 in one measured session) and we answer it with
    silence today, so a match can be announced within two seconds of becoming
    ready without inventing a channel.
    """
    reply = _handle_line(body, peer, peer_nick, member_id)
    pushes = pushqueue._pending_pushes(member_id, peer_nick)
    if not pushes:
        return reply
    out = ([] if reply is None
           else list(reply) if isinstance(reply, (list, tuple)) else [reply])
    return out + pushes


def _handle_line(body, peer="-", peer_nick=None, member_id=None):
    """Decode one TM0 E-body and return the reply E-body, or None to stay silent.

    `body` is the payload with the leading 'G' class char stripped, mirroring
    janhourou.handle_line. Silence is deliberate for anything we cannot yet
    speak: a logged unknown is a lead, a wrong answer is noise.

    `peer_nick` is the NICK the client addressed its NOTICE to -- the TM0 service
    peer. It is what `/Shm=` is derived from; see `_teach_shm`. Optional, so the
    selftest and any other caller keep working without it.
    """
    code, cmd = protocol.decode_ebody(body)

    if b"@Pong=" in cmd:
        # THE RESERVATION KEEP-ALIVE. A @Pong addressed to a TABLE's own peer
        # is the client saying "I still hold this table" (see the _SEAT_ALIVE
        # banner -- measured live 2026-08-22, and its SILENCE is the only wire
        # expression of Cancel Reservation). Stamp it, then let the expiry
        # sweep reap whoever has gone quiet.
        if member_id is not None:
            _hb_chan, _hb_idx = matchmaking._peer_table(peer_nick)
            if _hb_chan:
                # ...stamped against THE TABLE THIS BEAT NAMED. See _alive_key.
                matchmaking._SEAT_ALIVE[matchmaking._alive_key(_hb_chan, member_id,
                                       _hb_idx)] = time.time()
        matchmaking.expire_silent_seats()
        # ...and the same silence frees a VS. COM table whose player died
        # without an `@GameExit=`. See `expire_com_tables`.
        vscom.expire_com_tables()
        # VERIFIED: RE-FEED THE RESERVATION LIST every heartbeat for a SEATED player.
        # RE 2026-08-21: the client's reservation list (+0x108/+0x110)
        # and the "table members" sidebar are fed ONLY by a (code 0x41, msgid
        # 0xC) `@GameML` that the reservation branch 0xA940C->0x83B80 polls
        # every frame while +0x41c is armed; the store holds few entries so a
        # one-shot reply is evicted before that branch runs. `@Pong=` arrives
        # ~every 2s on the game band and we answered it with SILENCE, which
        # makes it the perfect carrier: a msgid-0xC reply rides it ALONE (no
        # competing msgid-4 to be grabbed first), lands in the store, and the
        # branch copies it -- filling the sidebar and letting the Start-Game
        # gate see the reserved players. A looker (not seated) gets None, so
        # their slot stays empty and Reserve stays available.
        # `POL_TM_RESV_PONG=0` restores the old silence.
        out = []
        # EVENT SHOP item re-feed: if this member just opened the event shop, its
        # item list (@Data + @Card) is PACED onto the @Pong heartbeat -- the door
        # batch does not take (measured: item count stayed 0). Push here, after the
        # scene has armed its item wait, for the configured number of heartbeats.
        # TRADE-BOARD RE-SERVE: while a trade accept is armed, re-push the
        # entry lines on the heartbeat -- the client's one-shot @TrAns purge
        # (measured 2026-08-24, [tmlog]: RecvCLEAR pops (0xA2,*)) eats any
        # copy delivered before it; the first post-purge copy sticks. Queued
        # via _queue_push, so it rides this very reply through the wrapper.
        trade._trade_reserve_tick(member_id)
        left = shopdoors._EVENTSHOP_PENDING.get(member_id, 0)
        if left > 0:
            shopdoors._EVENTSHOP_PENDING[member_id] = left - 1
            if left - 1 <= 0:
                shopdoors._EVENTSHOP_PENDING.pop(member_id, None)
            items = shopdoors._eventshop_item_lines(member_id)
            if items:
                out.extend(items)
                common._say("tm: member %s -- event shop items pushed on @Pong (%d push(es) "
                     "left); watch the shim [tmevent] EVENTSHOP itemE field" % (member_id, left - 1))
        if os.environ.get("POL_TM_RESV_PONG", "1") == "1":
            resv = reservation._reservation_reply(member_id)
            if resv:
                out.extend(resv if isinstance(resv, list) else [resv])
        return out or None

    if b"@TeachDV=" in cmd:
        # The opener. Teach the client its default-data version so the
        # "retrieving default data" wait resolves. See the module header.
        if not opener._teach_enabled():
            return None
        # HOLD THE REPLY. Measured 2026-08-15 from a live pol.exe minidump: the PC
        # client LOGS our answer (`sqMg:recv(...)<E2000000@TeachDVAns/D=1/V=1/...>`)
        # and then leaves every parsed field at its sentinel -- LN D/V still 255,
        # AC/RK/Shm still 0 -- so the message was consumed by the message layer and
        # the handler at TM.dll 0x053c134d never applied it. Same signature as the
        # POLpro race in responders.py: the client marks the operation pending
        # AFTER its send returns, and the result handler drops anything that lands
        # while the state is still 0. We were answering in ~3 ms.
        #
        # `delay=<ms>` in the teach control file, re-read per line, so this is
        # tunable to zero without a restart once the shape is known.
        opener._teach_delay()
        d, v = opener._teach_d(), opener._teach_v()
        return protocol.encode_code(protocol.MSG_TEACHDVANS) + opener._teachdvans_body(d, v, peer_nick)

    if b"@Init=" in cmd:
        # THE STEP AFTER THE MANIFEST, and the one that was timing out.
        #
        # Sequence, measured live 2026-08-15T00:10 (authserv.log + lobby.log):
        #   we answer @TeachDVAns  ->  the client fetches b/g/TM0IML  ->  two
        #   seconds later it sends `@Init=/NN=<u64 hex>/HID=0/Tm0=0/Tm1=0/Dm=0/
        #   Vol=0/CN=<content id, hex ASCII>/GID=<content id>/L=0`  ->  we were
        #   silent  ->  it timed out. `@Init=` appears exactly ONCE in the whole
        #   of authserv.log, in the first run where the manifest carried a
        #   NON-ZERO entry u64 -- runs with count=1 but a zero u64 never sent it.
        #   So the manifest chain (count -> flag -> u64) is what unlocks this.
        #
        # Reply shape, read off the parser at TMaster.pex 0x003a9240:
        #   * 0x0029a880(msg, buf, 129, 0) -- the message must present code
        #     **129** (0x81), the usual request+1 (128 -> 129, as 225 -> 226);
        #   * 0x0029c920(buf, "@InitAns", ...) -- the body must contain
        #     `@InitAns`;
        #   * 0x0029ce50(buf, "/Ans=", 0, &out) -- an integer field `/Ans=`,
        #     which the function RETURNS directly.
        # Its caller (0x003b7448 in the default-data state machine, state at
        # object+0x160) does `blez` on that return, so **/Ans= must be > 0** to
        # continue. 1 is the least-committal positive answer; the knob exists
        # because the value may turn out to mean something (a session/table id)
        # rather than a plain OK.
        if not opener._init_enabled():
            return None
        # PERSIST THE GUILD. The second `@Init=` of a launch carries `/GLD=<n>`
        # -- the choice the player just confirmed -- and it is the ONLY command
        # that can (the getter feeding /GLD= has three callers, the two
        # @Init senders and the gate). Write it to save +0x3B and the guild
        # screen stops drawing, because 0x14C866 reads exactly that byte.
        # `/GLD=0` means "not chosen yet", so it is never written back.
        # VERIFIED: THE PLAYER'S GAME-ID, AND THEY HAND IT TO US THEMSELVES.
        # See `_GAME_IDS`: `/GID=` here is exactly what `@VsGameInit`'s `/ID=`
        # list is matched against, so capturing it once at login is all the
        # board ever needs.
        matchstart._note_game_id(member_id, cmd)
        # ...AND THE **POL-ID**, WHICH IS A DIFFERENT ID AND THE ONE THE CHAT
        # ROSTER IS KEYED BY. `@Init=/NN=` is formatted from TM.dll's
        # [0x5242918]/[0x524291C]; 0x14BC50 copies that pair to
        # [0x5245308]/[0x524530C]; and the chat sidebar compares every roster
        # row's id against those two and skips the match as "me" (0x0AFE3).
        # So the row a client recognises as itself is the one carrying exactly
        # the characters it sent here -- which is why `note_pol_id` stores the
        # string, not a parsed integer. See `_chat_roster_body`.
        #
        # WARNING: An earlier capture of this same field lived in `responders.py` and
        # was REMOVED, correctly: it was being fed to `tmroom.note_guid`, whose
        # value is the lobby-band guid+slot the member RECORD's +0x00 carries,
        # and this is not that id. Both are true of the same person at once.
        matchstart._note_pol_id(member_id, cmd)
        # A FRESH @Init IS A FRESH SESSION -- void any seat this member still
        # holds from a dead one. See `void_all_seats`: this is the session-death
        # release that a crash + fast re-login slips past the retire guard.
        if os.environ.get("POL_TM_INIT_VOIDS_SEATS", "1") == "1":
            try:
                tableaudit.void_all_seats(member_id, "fresh @Init")
            except Exception as exc:
                common._say("tm:   @Init seat void failed (%r)" % (exc,))
        if savefile._guild_persist_enabled():
            m = re.search(rb"/GLD=(\d+)", cmd)
            guild = int(m.group(1)) if m else 0
            if guild and member_id is not None:
                if savefile._save_patch(member_id, savefile.SAVE_OFF_GUILD, guild):
                    common._say("tm: member %s guild -> %d (save +0x%02X)"
                          % (member_id, guild, savefile.SAVE_OFF_GUILD))
        # The `=` after the name is the PC container-parser's key delimiter --
        # see the banner above `_teachdvans_body`. `/Ans=` is unchanged, and the
        # PS2's strstr of `@InitAns` still matches.
        ans = protocol.encode_code(protocol.MSG_INITANS) + (b"@InitAns=/Ans=%d" % opener._init_ans())
        # A PENDING TRADE IS SERVED HERE, in the same batch: a trade game-band
        # connection's whole life is handshake -> ~15s idle -> client close,
        # so the @InitAns reply is the ONE carrier guaranteed to ride that
        # socket while the board it feeds still exists. See note_trade_accept.
        serve = trade._trade_pending_serve(member_id)
        return [ans] + serve if serve else ans

    if b"@Chat=" in cmd:
        # WHO IS IN THIS CHAT? -- msgid 1, asked twenty times in one measured
        # session and answered zero. See the `MSG_CHAT` banner: the msgid is the
        # arm, `1` is the request and `4` is the member list it is waiting for.
        #
        # Every other msgid stays silent HERE and says so, because the other two
        # arms are not ours to answer: msgid 8 is a chat LINE, and lines are
        # relayed as a room NOTICE (a `#`-target, which `_game_notice_reply`
        # hands straight back), never through this dispatcher.
        # `code` is None for a body with no 8-hex header, and a headerless
        # `@Chat=` is not the request -- the sender at 0x07F540 always stamps
        # one. Fold it to "some other arm" rather than raising.
        msgid = ((code or 0) >> 16) & 0xFF
        if msgid != roomchat.CHAT_MSGID_WHO:
            common._say("tm: member %s sent @Chat= msgid 0x%02X -- not the roster "
                 "request (msgid %d); captured, silent"
                 % (member_id, msgid, roomchat.CHAT_MSGID_WHO))
            return None
        if not roomchat._chat_roster_enabled():
            return None
        return roomchat._chat_roster_body(member_id)

    if b"@Opt=" in cmd:
        # THE OPTION WRITE-BACK. The client has no save-write API, so this is how
        # a setting the player changed ever reaches storage -- see the banner
        # above `OPT_FIELD_OFFSETS`. Two things happen here and they are
        # independent: we PERSIST the twelve bytes, and we ANSWER, because the
        # sending scene blocks on `@OptAns` and an unanswered `@Opt=` is the same
        # 75-second dead wait that `@Quit=` was.
        if not savefile._opt_enabled():
            return None
        if savefile._opt_persist_enabled():
            moved = savefile._save_patch_many(member_id, savefile._opt_fields(cmd))
            for off, (old, new) in sorted(moved.items()):
                common._say("tm: member %s save +0x%03X %d -> %d%s"
                      % (member_id, off, old, new,
                         "  (skip handle linking)" if off == savefile.SAVE_OFF_HNSS else ""))
        return protocol.encode_code(protocol.MSG_OPTANS) + (b"@OptAns=/Ans=%d" % savefile._opt_ans())

    if b"@ShReq=" in cmd:
        shopdoors._PRIZE_DOOR.discard(member_id)
        # Player Data -> Cards. One field, `/EN=`, and it must be > 0; the
        # client then takes OUR message's sender id as the card-shop endpoint.
        # See the banner above `_sh_enabled`.
        if not cardshop._sh_enabled():
            return None
        lines = [protocol.encode_code(cardshop._sh_code()) + (b"@ShEnter=/EN=%d" % cardshop._sh_en())]
        # RESTORE THE COLLECTION, if we are asked to. See the banner above
        # `_COLLECTION_SUFFIX`: recording is free and always on, giving the
        # cards BACK is the unproven half, so it is opt-in.
        if collection._card_restore_enabled():
            saved = collection._collection_cards(member_id)
            if saved:
                common._say("tm: member %s restoring %d saved card(s)"
                      % (member_id, len(saved)))
                lines.append(cardshop._card_body(protocol.encode_code(protocol.MSG_SHOPCARD), saved))
        # ...and PUSH the shop's opening message straight after it. The client
        # sends nothing more once it has the endpoint -- it drops into
        # `0x101A10(0x13)` and waits -- so SHOPINIT has to be unsolicited. The
        # store is a queue, so arriving before the scene starts waiting is fine;
        # the record simply sits there until the matcher picks it up.
        #
        # WHICH DOOR is decided here rather than below, because it now picks the
        # INIT BODY as well as the AUCMONEY amount -- sending the card shop's
        # `@Init=` to Check Out is what ran the pack-name setup on a screen with
        # no packs. See the banner above `_INIT_ARM_FIELDS`.
        msgid = (code >> 16) & 0xFF
        checkout = msgid == shopdoors.SHREQ_MSGID_CHECKOUT
        arm = shopdoors._checkout_init_arm() if checkout else "Init"
        amount = shopdoors._checkout_money_for(member_id) if checkout else None
        if cardshop._shopinit_enabled():
            if checkout:
                common._say("tm: member %s @ShReq= msgid 0x%02X = auction Check Out -- "
                      "SHOPINIT body is @%s= (POL_TM_CHECKOUT_INIT). Read the "
                      "client's own '---->Recv=' line to see which arm it ran; "
                      "@Init= is the CARD SHOP arm and is what printed "
                      "Now1stPackName='s Pack on this screen."
                      % (member_id, msgid, arm))
            lines.append(protocol.encode_code(protocol.MSG_SHOPINIT)
                         + shopdoors._init_body(arm, member_id, amount))
        # ...AND THE WALLET, BUT ONLY AT THE CARD SHOP. `AUCMONEY` is the only
        # message that carries money; SHOPINIT parses `/M=` and discards it. See
        # `_aucmoney_line`. It has to follow SHOPINIT, because SHOPINIT is what
        # sets the shop number this message's gate compares against.
        #
        # msgid 0x01 is the auction Check Out door, where this same `/M=` is
        # announced as the proceeds of a card sale -- see the banner above
        # `SHREQ_MSGID_CHECKOUT` for the two live measurements that establish it.
        if checkout and shopdoors._aucmoney_shop_only():
            # The reproduction path for the measurement, NOT a fallback: this is
            # what hung the load. See the banner above `SHREQ_MSGID_CHECKOUT`.
            common._say("tm: member %s @ShReq= msgid 0x%02X = Check Out -- WITHHOLDING "
                  "AUCMONEY (POL_TM_AUCMONEY_SHOP_ONLY=1). This is measured to "
                  "hang the screen before it renders; unset it to serve /M=%d."
                  % (member_id, msgid, shopdoors._checkout_money()))
        else:
            if checkout:
                common._say("tm: member %s @ShReq= msgid 0x%02X = auction Check Out -- "
                      "serving /M=%d (proceeds awaiting collection), NOT the "
                      "wallet (%d). The wallet here is what announced a card "
                      "sale that never happened."
                      % (member_id, msgid, amount, purse.money_of(member_id)))
            money_line = shopdoors._aucmoney_line(member_id, amount)
            if money_line:
                lines.append(money_line)
            if checkout:
                # AND THE CARDS. The Check Out scene waits on TWO commands --
                # 0x20 for the money and 0x21 for the cards (TM.dll 0x13498B and
                # 0x134F97) -- so serving only AUCMONEY answers half the screen.
                # After AUCMONEY because that is the order the scene's own
                # `Recv=` trace shows, and because SHOPINIT sets the shop number
                # both messages' gates compare against.
                # SETTLE FIRST. The sweep runs off the class-A path, and
                # Check Out arrives on class G -- so a player who opens Check
                # Out without touching the auction list would be told they are
                # owed nothing by a store that had never been brought up to
                # date. Late import: tetramaster is imported BY tmtitle.
                try:
                    import tmtitle as _r
                    _r._auction_sweep()
                except Exception as _e:                      # pragma: no cover
                    common._say("tm: member %s Check Out -- sweep failed (%s); "
                          "delivering whatever is already pending"
                          % (member_id, _e))
                # WARNING: RE-READ THE MONEY. `amount` was computed before this block,
                # so it predates the sweep -- which is exactly when a sale gets
                # credited. Measured: a seller credited 20 was served /M=0 in
                # the same reply that queued their cards correctly.
                amount = shopdoors._checkout_money_for(member_id)
                if money_line and amount:
                    lines[lines.index(money_line)] = shopdoors._aucmoney_line(
                        member_id, amount)
                # *** THE FOUR-SECTION STREAM (default). The scene runs four
                # sub-scenes -- money A, cards A, cards B, money B -- each
                # consuming its own 0x20/0x21 record from the store queue, and
                # it has NO base UI: an unanswered section is a black screen.
                # NO @Dead, EVER: 0x27 is the scene's error channel -- an
                # answered @Dead in any state raises dialog 221 ("Failed to
                # connect to server.", TRM-37221) and parks the scene in
                # state 0x80. The exit is the client's own @Quit= -> our
                # @EQuit=/D=0 on msgid 23. Full machine: the MSG_AUCDEAD
                # banner, `_checkout_stream_enabled`.
                # OK: RENDER CONFIRMED LIVE 2026-08-21T03:13:42Z (m3): the
                # sweep-returned card rode cards A and the screen said the
                # card "was returned (for being unsold)" -- so CARDS A WEARS
                # THE 'RETURNED CARD' LABEL. WARNING: Pending cards carry no
                # won-vs-returned kind yet, so a WON card would ALSO render
                # as "returned" here; if won cards belong in cards B, the
                # pending store needs a kind field first. Exit path (money B
                # consumed after @EQuit) not yet exercised live.
                if shopdoors._checkout_stream_enabled():
                    # WARNING: STRIP EVERY 0x20 LINE, NOT `money_line` BY IDENTITY.
                    # The re-read block above REPLACES the object in `lines`
                    # when the sweep just credited money, so remove(money_line)
                    # raises ValueError -- which ate the ENTIRE door reply on
                    # the first real settlement (17:16:34Z, a tester's 'Payment:
                    # 100' visit: "list.remove(x): x not in list -- silent",
                    # client timed out with the credit safely still pending).
                    lines = [l for l in lines
                             if not (l[:2] == b"B2" and l[4:6] == b"20")]
                    stream = shopdoors._checkout_stream_lines(member_id, amount)
                    lines.extend(stream)
                    # WARNING: THE REMAINING SECTIONS PACE ON TIME, NOT ON @Get.
                    # Measured 17:28Z (m9, the first WON-card delivery): the
                    # NON-EMPTY cards path never sends @Get -- the pacer only
                    # fires on the quiet/empty branch (state 6 sends 0x15 only
                    # when [subobj+0x14C]==1; the add-with-UI branch clears
                    # it) -- so every @Get-gated release starved and the
                    # client timed out in cards B's wait. The real constraint
                    # The door batch (money A + cards A = returned + terminator)
                    # is in `lines`; cards B (won) rides @Get #1 and money B +
                    # @EQuit ride @Get #2 (see _checkout_pull_lines). Read
                    # pending for the log and the clear.
                    pend = shopdoors._pending_of(member_id)
                    refund = int(pend.get("refund") or 0)
                    common._say("tm: member %s Check Out -- money A /M=%d (proceeds) + "
                          "cards A (returned %r) + cards B (won %r, on @Get #1) "
                          "+ money B /M=%d (bid refund, on @Get #2); @EQuit on "
                          "@Get #2. Clearing pending; recover from this line if "
                          "the client never took them."
                          % (member_id, amount, pend["cards"], pend.get("won"),
                             refund))
                    if tmauction is not None and (pend["cards"] or pend.get("won")
                                                  or amount or refund):
                        _collected = [tmauction.card_row_from_ii(ii)
                                      for ii in (pend["cards"]
                                                 + (pend.get("won") or []))]
                        try:
                            tmauction.clear_pending(
                                member_id, money=bool(amount), cards=True,
                                won=True, refund=bool(refund))
                        except OSError as e:
                            common._say("tm: member %s Check Out -- CANNOT clear "
                                  "the pending store (%s). The next visit "
                                  "will re-deliver; fix the store first."
                                  % (member_id, e))
                        else:
                            # PERSIST THE CARDS SERVER-SIDE, after a SUCCESSFUL
                            # clear (so a re-delivery cannot also re-add them --
                            # duplicates compound silently). The client adds each
                            # collected card to ITS OWN deck, but our collection
                            # authors the save the client reloads, so without
                            # this a won/returned card is confiscated on the next
                            # sync -- the wallet-credit trap, one field over.
                            if _collected:
                                collection._collection_add_cards(member_id, _collected)
                            # CREDIT THE SERVER-SIDE WALLET by BOTH money
                            # sections. Each adds its /M= to the CLIENT's wallet
                            # ([0x2B64A8] += /M=) -- money A the sale proceeds
                            # (Collecting Payment), money B the bid refund --
                            # so without the mirror the next wallet sync
                            # (/M=money_of()) confiscates them (found 17:22Z:
                            # the tester collected 100 on screen while money_of read
                            # the old balance). WARNING: money B's wallet credit is
                            # NOT yet confirmed live -- it may be delivery-only.
                            _credit = (amount or 0) + refund
                            if _credit:
                                purse._set_money(member_id,
                                           purse.money_of(member_id) + _credit,
                                           "auction collected (proceeds %s + "
                                           "refund %s)" % (amount or 0, refund))
                    probes = shopdoors._checkout_probe_lines()
                    if probes:
                        lines.extend(probes)
                    return lines
                # --- LEGACY SHAPE (POL_TM_CHECKOUT_STREAM=0) below. ---
                # WARNING: 2026-08-21: THE "FALSIFIED" VERDICT BELOW IS ITSELF
                # RETRACTED. authserv.log shows the 01:5x test served /M=0 (the
                # env never reached the container) WITH AUCCARDS+@Dead still on
                # (fa4fbe60 deployed ~01:45Z, after the 01:34/01:37 runs) -- it
                # reproduced nothing. The 08-19 render was @Init + /M=777; no
                # run since has matched it. Kept verbatim below as history.
                # WARNING: OFF BY DEFAULT -- THESE TWO REGRESSED A WORKING SCREEN.
                # Check Out RENDERED on 2026-08-19 with nothing but @EcmInit and
                # AUCMONEY(/M=777): "You sold your card! ... Payment: 777".
                # Tonight AUCCARDS and @Dead were added and every attempt since
                # has hung, including one carrying a real /M=20.
                #
                # The suspect is @Dead's own arm (0x105DB3), which does
                # `mov byte [esi+0xea], 0` -- it CLEARS a flag. If that byte is
                # what tells the scene it has something to draw, sending @Dead
                # unconditionally kills the screen, and the working run never
                # got one.
                #
                # WARNING: AND THAT THEORY IS FALSIFIED -- TESTED 2026-08-21T01:5xZ.
                # POL_TM_CHECKOUT_M=777 was set on prod, reproducing the 08-19
                # run exactly (same value, same @EcmInit + AUCMONEY, these two
                # gated OFF) and the screen STILL did not render. So AUCCARDS
                # and @Dead are NOT what broke Check Out, and neither is /M=0:
                # something else changed between 08-19 and 08-20.
                #
                # They stay off anyway -- unproven, not guilty -- so the wire
                # matches the last shape known to have worked. Turning them on
                # is a bisect step (=1 AUCCARDS, =2 adds @Dead), not a fix.
                #
                # WARNING: WHERE TO START NEXT, and it is NOT here: diff what else
                # moved in the Check Out path between 08-19 and 08-20 --
                # @EcmInit's own body, _checkout_init_arm, the probe lines, the
                # shop byte. Three deploys were spent adding messages to a
                # screen whose problem was never the messages, and the 777 test
                # is what finally proved that. Get `Recv=` tracing on a client
                # before spending another.
                _cards_mode = 0
                try:
                    _cards_mode = int(os.environ.get("POL_TM_AUCCARDS", "0"), 0)
                except ValueError:
                    _cards_mode = 0
                cards_line = shopdoors._auccards_line(member_id) if _cards_mode else None
                if cards_line:
                    lines.append(cards_line)
                    if _cards_mode >= 2:
                        lines.append(shopdoors._aucdead_line())
                    # WARNING: CLEAR ON SEND, AND LOG THE CONTENTS. There is no ack --
                    # this arm stores its payload and sets a flag, it does not
                    # reply -- so "delivered" is unobservable and one of two
                    # risks has to be taken. Not clearing re-sends the same
                    # cards on EVERY Check Out visit, and the client ADDS them
                    # to the collection each time: an item dupe that compounds
                    # silently. Clearing risks losing one delivery if the
                    # message never lands.
                    #
                    # Dupe is the worse failure, so we clear -- and the full
                    # payload goes in the log so a lost one is RECOVERABLE by
                    # hand rather than gone. Same reasoning as the sweep: prefer
                    # the failure a human can see and undo.
                    pend = shopdoors._pending_of(member_id)
                    if not pend["cards"] and not amount:
                        common._say("tm: member %s Check Out -- nothing owed; serving "
                              "the EMPTY AUCCARDS (/E=0) so the scene's 0x21 "
                              "wait is answered rather than left hanging."
                              % (member_id,))
                    else:
                        common._say("tm: member %s Check Out -- serving AUCCARDS %r "
                              "and AUCMONEY /M=%d, then clearing. Recover from "
                              "this line if the client never took them."
                              % (member_id, pend["cards"], amount))
                        if tmauction is not None:
                            try:
                                tmauction.clear_pending(
                                    member_id, money=bool(amount), cards=True)
                            except OSError as e:
                                common._say("tm: member %s Check Out -- CANNOT clear "
                                      "the pending store (%s). The next visit "
                                      "will re-deliver these cards; fix the "
                                      "store before the player opens it again."
                                      % (member_id, e))
        if checkout:
            # ...AND POKE THE DRAIN LOOP. Last, so nothing above it changes, and
            # only on this door. See the banner above `_CHECKOUT_PROBE_DEFAULT`.
            probes = shopdoors._checkout_probe_lines()
            if probes:
                common._say("tm: member %s Check Out -- PROBING the drain loop with "
                      "%s. Read the client trace for `store lookup ... FOUND` "
                      "and the `find(\"@Key\")` lines that follow it: those name "
                      "the arm's vocabulary. POL_TM_CHECKOUT_PROBE= to stop."
                      % (member_id,
                         ", ".join("0x%02X msgid %d" % (c, m)
                                   for c, m in shopdoors._checkout_probe_codes())))
                lines.extend(probes)
        return lines

    if b"@Sell=" in cmd:
        # THE SALE. `@Sell=/C=<n>@<i>=/D=<card>` -- the player sold cards at the
        # card shop. Fire-and-forget: there is no `@ESell` in TM.dll, so the
        # client has already priced it, credited itself and moved on, and the
        # only thing that can go wrong here is our record drifting from its.
        # See `_sell_cards`, and the price banner above it.
        return cardshop._sell_cards(member_id, cmd)

    if b"@Erase=" in cmd:
        # VERIFIED: THE DISCARD. Captured 2026-08-20T15:40:38Z, the first one ever seen:
        #
        #     code=178 msgid=0x10 sh=1  @Erase=/Mode=1/C=1@0=/D=201|69|0|55|56|16
        #
        # so it arrives on **(0xB2, 0x10)** with a `/Mode=` and then the same
        # `/C=<n>@<i>=/D=<card>` list `@Sell=` uses. FIRE-AND-FORGET: the sender
        # at 0x1080E4 does NOT drain a reply slot first (compare `@GameExit=`,
        # whose 0x8416C drains (0x41, 0x0E) before sending), so nothing is
        # waiting on us -- but the card must still leave our collection, or the
        # next `_collection_to_save` writes it back and the discard undoes
        # itself on the following load.
        #
        # WARNING: `/Mode=` is UNREAD. The one capture carried `Mode=1`; the client has
        # `@Erase=` senders on codes 0x43 and 0xA2 as well, so other modes and
        # other screens very likely exist. Log it rather than branch on a value
        # seen exactly once.
        return cardshop._erase_cards(member_id, cmd)

    if b"@Buy=" in cmd:
        # THE CARD SHOP PURCHASE. `/No=` is the pack index the player picked.
        if not cardshop._shopbuy_enabled():
            return None
        # VERIFIED: AND IT COSTS SOMETHING NOW. See the banner above `_pack_price`:
        # `/No=` indexes `PackPrm.BIN` directly, validated against a tester's
        # own purchases (`/No=3` -> 9000, `/No=0` -> the free Pauper's Pack).
        # The client debits ITSELF, so this only has to agree with it; a wrong
        # debit desyncs the wallet and `_collection_to_save` then writes our
        # number into the save the client loads.
        _no = re.search(rb"/No=(\d+)", cmd or b"")
        _pack_no = _no.group(1) if _no else None
        # KEY: THE PRIZE CENTER EXCHANGE (2026-09-25). Behind `@CvReq=` a
        # `@Buy=` spends PRIZE POINTS on one of PackPrm records 45..74 (+0x04
        # cost in points, +0x20 the card) -- it must never touch gold, which
        # is what this arm did before (tm-buy-price-depends-on-the-door).
        # `/No=` is taken as the record (45..74) or as the prize index (0..29):
        # which one TM.dll sends was never captured, and the two ranges do not
        # overlap. The card rides the pack path's single-card fallback.
        # POL_TM_PRIZE_EXCHANGE=0 restores the old (gold) behaviour.
        if (member_id in shopdoors._PRIZE_DOOR and tmprize is not None
                and common._env_int("POL_TM_PRIZE_EXCHANGE", 1)):
            _n = int(_pack_no) if _pack_no is not None else -1
            _rec = _n if 45 <= _n < 75 else (45 + _n if 0 <= _n < 30 else None)
            _pts = cardtables._pack_price(_rec) if _rec is not None else None
            if _pts is None:
                common._say("tm: member %s Prize Center @Buy= /No=%s names no prize "
                     "-- silent" % (member_id, _n))
                return None
            _left = tmprize.spend(member_id, int(_pts), say=common._say)
            if _left is None:
                common._say("tm:   WARNING: member %s cannot afford prize record %d (%d "
                     "points) -- no card; the client should not have asked"
                     % (member_id, _rec, _pts))
                return None
            common._say("tm: VERIFIED: member %s exchanged %d prize point(s) for prize "
                 "record %d (%d left)" % (member_id, _pts, _rec, _left))
            return cardshop._shopbuy_body(body[:8], member_id, b"%d" % _rec)
        if common._env_int("POL_TM_BUY_DEBIT", 1):
            _cost = cardtables._pack_price(_pack_no)
            if _cost is None:
                common._say("tm: member %s @Buy= %s -- NO PRICE for that pack index; "
                     "not debiting (this will desync if the client charged)"
                     % (member_id, cmd[:40]))
            elif _cost and member_id is not None:
                _bal = purse.money_of(member_id)
                if _bal < _cost:
                    # WARNING: DO NOT DEBIT WHAT WE CANNOT PRICE. This used to be an
                    # unconditional `max(0, bal - cost)`, and the clamp ATE A
                    # WHOLE BALANCE: 2026-08-25T04:25:54Z, member 3 held 39980,
                    # bought pack /No=4 which our table prices at 40000, and
                    # went to **0** -- 39980 gold destroyed by a 20-gold
                    # shortfall. The warning below was already printed, one
                    # line AFTER the write it was warning about.
                    #
                    # And the client disagreed with us in the same breath: it
                    # went on to hand over the pack (3 cards ACQUIRED at
                    # 04:26:03), then took a second one at /No=3 while our
                    # balance said 0. A client that could not afford a pack
                    # does not deliver it -- it prices and decides for itself,
                    # which this file has said since `_sell_price`. So when our
                    # number says "cannot afford" and the client proceeds
                    # anyway, the number that is wrong is OURS.
                    #
                    # Refusing to move money is the only safe arm. A missed
                    # debit leaves us a bit rich and self-corrects at the next
                    # measured price; a clamp to zero is unrecoverable and
                    # `@ComGameInit=/M=` then ASSIGNS that zero into the live
                    # wallet, destroying the client's own correct figure too.
                    common._say("tm:   WARNING: member %s @Buy= /No=%s costs %d and we think "
                         "they hold %d -- NOT DEBITING. Our price or our "
                         "balance is wrong (the client prices and decides for "
                         "itself); a clamp here would destroy %d gold to cover "
                         "a %d shortfall. POL_TM_BUY_DEBIT=0 disables debiting "
                         "entirely."
                         % (member_id, _no.group(1).decode(), _cost, _bal,
                            _bal, _cost - _bal))
                else:
                    purse._set_money(member_id, max(0, _bal - _cost),
                               "pack /No=%s" % _no.group(1).decode())
            else:
                common._say("tm: member %s bought pack /No=%s -- FREE (%s)"
                     % (member_id, _no.group(1).decode() if _no else "?",
                        "the Pauper's Pack" if _no and _no.group(1) == b"0"
                        else "price 0 in PackPrm"))
        # Echo the request's own header: it already carries code 0xB2, msgid
        # 0x14 and the shop number, which is exactly what the handler's two
        # equality gates compare against.
        return cardshop._shopbuy_body(body[:8], member_id, _pack_no)

    if b"@Get=" in cmd:
        # THE ACQUIRE. `@Get=/No=1` on (0xB2, 0x15) is the client TELLING us it
        # took a card -- it has already moved staging slot 0 into its collection
        # and zeroed the rest of the pack before the message goes out (TM.dll
        # 0x17961D..0x179657), so there is nothing to answer and answering is
        # not what makes it work. What it is good for is TRUTH: this is the only
        # moment we can know a grant became an acquisition.
        if not collection._record_on_grant():
            collection._collection_commit_offer(member_id)
        # ...AND ON THE CHECK OUT DOOR IT IS THE SECTION PACER. A cards
        # sub-scene sends it as it completes (state 6, 0x13538C), and the
        # NEXT section's record must arrive only now -- pre-queued same-msgid
        # records are drained together (measured 03:29Z). See _CHECKOUT_PULL.
        pull = shopdoors._checkout_pull_lines(member_id, peer_nick)
        if pull:
            common._say("tm: member %s Check Out @Get= -- releasing %s (the section "
                  "pacer; pre-queuing is measured to fail)"
                  % (member_id,
                     " + ".join(l[8:l.index(b"=", 8) + 1].decode("ascii",
                                "replace") for l in pull)))
            # WARNING: NO closing @Dead here either -- the 04:37Z double-@Dead
            # model is retracted (see the MSG_AUCDEAD banner): 0x133BEB is
            # state 0x0D's ABORT poll, and a @Dead there raises dialog 221
            # ("Failed to connect to server.") instead of any closing
            # notice. The exit is the client's own @Quit= -> @EQuit=/D=0.
            return pull
        return None

    if b"@Decks=" in cmd:
        # THE CLIENT REPORTING ITS DECK, unprompted. The static read established msgid 0x22
        # has no arm, so this wants RECORDING, not answering -- and it is the
        # only place the player's own arrangement of their cards is ever stated.
        collection._collection_set_deck(member_id, cmd, sh=((code or 0) >> 24) & 0xFF)
        return None

    if b"@CvReq=" in cmd:
        # RANKINGS -> PRIZE CENTER. See the banner above `_cv_en`.
        if not cardshop._cv_enabled():
            return None
        if member_id is not None:
            shopdoors._PRIZE_DOOR.add(member_id)
        lines = [protocol.encode_code(protocol.MSG_CVENTER) + (b"@CvEnter=/EN=%d" % cardshop._cv_en())]
        # ...AND THE SAME UNSOLICITED (0xB2, 0x13) PUSH THE SHOP GETS. Measured
        # live 2026-08-16: `@CvEnter=/EN=1` alone is ACCEPTED -- the client takes
        # the endpoint and goes quiet, sending only `@Pong` -- and then hangs,
        # because the scene's poll (TM.dll rva 0x8B410) has two accept paths and
        # only one of them reports completion:
        #
        #   0x8B43F  lookup (0xB2, 0x13)  -> sender id, and [esp+0xc] = 1
        #   0x8B47E  lookup code 0x71     -> parse @CvEnter + /EN=, sender id,
        #                                    but [esp+0xc] is left at 0
        #   0x8B520  mov edi, [esp+0xc]   <- the return value
        #
        # So `@CvEnter` hands the endpoint over and SHOPINIT is what actually
        # advances the scene. The first path never reads the body -- it takes
        # only the sender id -- so ANY (0xB2, 0x13) satisfies it.
        #
        # WARNING: ...AND FOR A YEAR THAT WAS THE CARD SHOP'S `@Init=`, WHICH IS THE
        # SAME MISTAKE THE AUCTION CHECK OUT DOOR MADE. msgid 0x13 dispatches on
        # the BODY COMMAND (see `_INIT_ARM_FIELDS`): `@Init` is the card shop,
        # and the Prize Center's own arm is **`@ExcInit`** (rva 0x1054EE) -- the
        # EXCHANGE, 賞品交換所, whose `/PP=` carries the point balance and the
        # three pending awards and whose `/LC=` carries the week's lucky cards.
        # Sending `@Init` here initialises the exchange as a card shop, which is
        # exactly what printed `Now1stPackName='s Pack` on the Check Out screen.
        #
        # `POL_TM_CV_INIT=excinit|init|off` picks, per request, so the wrong one
        # can be reproduced on demand -- the client's own `---->Recv=` line is
        # what arbitrates, not this comment.
        arm = os.environ.get("POL_TM_CV_INIT", "excinit").strip().lower()
        if arm == "off":
            pass
        elif arm == "einit":
            # EVENT-SHOP EXPERIMENT (2026-08-22). The orphaned Event Shop SCENE
            # has no reachable entry, BUT @EInit -- the event shop's opener -- rides
            # the SAME (0xB2, 0x13) SHOPINIT slot as @ExcInit and is parsed by the
            # SAME shop scene (arm 0x10563A), told apart only by the body name. So
            # feeding @EInit down the REACHABLE Prize Center door should flip that
            # scene into its event-shop variant with no client patch. @EInit is
            # @Init with /RT= in place of /SP=/SN=; structurally
            # it is the card shop's opener (implemented, safe) + one field, so it
            # cannot hit @EcmInit's /C= stack-garbage loop. Fields ini-tunable for
            # live bisection. WARNING: UNMEASURED -- watch the client's ---->Recv= line.
            n  = common._env_int("POL_TM_SHOP_N", 1)
            rt = common._env_int("POL_TM_EINIT_RT", 1)
            # /M= is the member's OWN balance -- see `_eventshop_item_lines`.
            # A hardcoded 10000 here would announce a wallet nobody earned.
            # /CP= is the CARD LEVEL slot (struct +0x00, stored at
            # 0x1056D8 in this arm), not a spare zero -- see `card_level`.
            body = (b"@EInit=/N=%d/RA=0/M=%d/CP=%d/S=1/RT=%d"
                    % (n, purse.money_of(member_id), cardtables.card_level_of(member_id), rt))
            _stat = os.environ.get("POL_TM_EINIT_STAT")
            if _stat is not None:
                try:
                    body += b"/Stat=%d" % int(_stat, 0)
                except ValueError:
                    pass
            common._say("tm: member %s -- Prize Center door sending @EInit (EVENT SHOP "
                 "variant experiment): %s" % (member_id, body.decode("latin-1")))
            lines.append(protocol.encode_code(protocol.MSG_SHOPINIT) + body)
            # @EInit opens the scene but it stays "Now Loading" until its item list
            # (money @Data 0xB2/0x20 + cards @Card 0xB2/0x21) arrives. MEASURED
            # via the in-process @EInit object hook 2026-08-22: pushed in the SAME
            # door batch, the items DO NOT take -- the captured object showed item
            # count esi+0xf0 == 0 -- because the shop-family scene drains the entry
            # batch before it arms its item wait (the exact "sections can't be
            # pre-queued" rule the auction Check Out hit). So PACE them: push on the
            # member's next @Pong(s), after the scene has armed, the same carrier the
            # reservation re-feed uses. POL_TM_EINIT_PACE=0 = old door-batch push
            # (for A/B); POL_TM_EINIT_PONGS = how many heartbeats to (re-)push on.
            if os.environ.get("POL_TM_EINIT_PACE", "1") == "1":
                shopdoors._EVENTSHOP_PENDING[member_id] = common._env_int("POL_TM_EINIT_PONGS", 2)
                common._say("tm: member %s -- event shop items PACED onto the next %d "
                     "@Pong(s) (door-batch push does not take -- item count stayed 0)"
                     % (member_id, shopdoors._EVENTSHOP_PENDING[member_id]))
            else:
                lines.extend(shopdoors._eventshop_item_lines(member_id))
        elif arm == "excinit" and tmprize is not None:
            body = tmprize.excinit_body(
                member_id, shop_no=common._env_int("POL_TM_SHOP_N", 1), say=common._say)
            common._say("tm: member %s opened the PRIZE CENTER -- %s"
                 % (member_id, body.decode("latin-1")))
            lines.append(protocol.encode_code(protocol.MSG_SHOPINIT) + body)
            # `excinit_body` runs `announce()`, which PAYS -- so the balance
            # this member holds has just moved. Carry it into the save's
            # `Prize Points Acquired` while we are the ones who changed it.
            careerstats._save_sync_stats(member_id)
        elif cardshop._shopinit_enabled():
            if arm == "excinit":                             # pragma: no cover
                common._say("tm: POL_TM_CV_INIT=excinit but tmprize did not import -- "
                      "falling back to the CARD SHOP's @Init=, which is the "
                      "wrong arm for this door")
            lines.append(protocol.encode_code(protocol.MSG_SHOPINIT)
                         + shopdoors._shopinit_body(member_id))
        return lines

    if b"@CardReq=" in cmd and tournament._event_test_member(member_id) \
            and event_phase()[0] in ("over", "closed"):
        return tournament._event_prize_lines(member_id)

    if b"@CardReq=" in cmd:
        # The card-service endpoint handshake. See the banner above MSG_CARDREQ.
        if not cardshop._cardent_enabled():
            return None
        return protocol.encode_code(protocol.MSG_CARDENT) + (b"@CardEnt=/Ans=%d" % cardshop._cardent_ans())

    if b"@EventTimeReq=" in cmd:
        # THE EVENT COUNTDOWN. Both event pumps block here. See the banner above
        # `_eventtime_enabled` for why -1/-1 is the right default.
        if not tournament._eventtime_enabled():
            return None
        start, end, now = tournament._eventtime_fields()
        if start == -1 and end == -1 and tournament._event_test_member(member_id):
            # A tournament TEST member (POL_TM_EVENT_ZONE_MEMBERS) is in an
            # event we declare for them: Start == -1 with End != -1 is "running"
            # (PS2 gate 0x3E4D30), and Now is
            # the seconds left.
            # Time Left is End - Now (PC 0x8C340), so End carries the seconds
            # and Now is 0; End=1/Now=7200 drew a blank box (negative time).
            _ph, _ws, _we = event_phase()
            _t = time.time()
            # THE ANSWER IS THE PHASE. Pending: Start != -1
            # and Time Left = End - Now counts down to the start; running:
            # Start = -1, End = length, Now = elapsed; over/closed: -1/-1 (a
            # first entry shows "The tournament is over.", a re-entry goes to
            # the results, which the Close push below releases).
            if _ph == "pending":
                start, end, now = int(_ws - _t), int(_ws - _t), 0
            elif _ph == "running":
                start, end, now = -1, int(_we - _ws), int(_t - _ws)
            else:
                start, end, now = -1, -1, 0
            tournament.note_seen(member_id, _ph)
            # ...AND LET THEM IN. The event loader (PC ctor 0x968D0) waits in
            # state 6, "Entering room..." at 90%, for `@EventEn` (code 0xD0,
            # reader 0x8CDF0; /Ans= must be > 0), after the <DE>/<DS> room
            # entry the polpro template already answers. Live 2026-09-26 it sat
            # there. Only @EInitReq= / @CardReq= purge 0xD0, so an early push
            # keeps.
            common._say("tm: member %s @EventTimeReq -- event %s (Start=%d End=%d "
                 "Now=%d); + @EventEn=/Ans=1" % (member_id, _ph, start, end, now))
            # The ticker: (0xD7) @ETelop, reader 0x8C790; a page shows once its
            # /ID= has every page in and /Lp= >= 1.
            telop = os.environ.get("POL_TM_EVENT_TELOP") or str(
                event_info().get("ticker")
                or "Welcome to the %s!" % event_info().get("name", "tournament"))
            lines = [protocol.encode_code(protocol.MSG_EVENTTIMEANS)
                     + (b"@EventTimeReqCheck=/Start=%d/End=%d/Now=%d"
                        % (start, end, now))]
            if tournament.returning_from_game(member_id):
                # Back from a tournament game, the client does not read
                # @EventEn (2026-10-03 narration log: unread until the next
                # full entry), and an unread one in its receive store is half
                # of why the next seating failed (see the @Ready= answer).
                common._say("tm:   ...member %s is back from a tournament game -- "
                            "no @EventEn" % (member_id,))
            else:
                lines.append(protocol.encode_code(0xD0) + b"@EventEn=/Ans=1")
            if _ph == "closed":
                # Results are final: the results pump waits for this, 60 s,
                # then "Tallying tournament results". Only (0xD1,5) is drained
                # by the request, so this survives.
                lines.append(tournament._event_time_push("close"))
            if telop and _ph in ("pending", "running"):
                # /Pg= is ONE field, `index|total`: /Pg=0/Pg=1 read the total
                # as 0 and the page never committed (section 9, extra). Queued
                # a few replies on, so it lands once the ticker exists.
                # /St= is HEX, like every TM text field (/CN= "Lex" = 4c6578):
                # plain ASCII drew as kana junk on the PS2 and the PC client
                # died at the same point twice (live 2026-09-26). The live
                # page set (_ticker_pages), queued a few replies on so it lands
                # once the ticker exists.
                for _i, _b in enumerate(tournament._ticker_bodies(tournament._ticker_pages())):
                    pushqueue._queue_push(member_id, _b, "the tournament ticker",
                                after=common._env_int("POL_TM_EVENT_TELOP_GAP", 3) + _i)
            return lines
        return (protocol.encode_code(protocol.MSG_EVENTTIMEANS)
                + (b"@EventTimeReqCheck=/Start=%d/End=%d/Now=%d" % (start, end, now)))

    if b"@EInitReq=" in cmd:
        # THE EVENT SHOP'S CARD DOOR -- card machine state 3. See the banner
        # above `_einitent_enabled`; this cannot fire before the scene opens.
        if not tournament._einitent_enabled():
            return None
        card, ans = tournament._einitent_fields()
        ent = (protocol.encode_code(protocol.MSG_EINITENT)
               + (b"@EInitEnt=/Card=%d/Ans=%d" % (card, ans)))
        if not tournament._event_test_member(member_id):
            common._say("tm: member %s @EInitReq -- answering /Card=%d/Ans=%d"
                 % (member_id, card, ans))
            return ent
        # VERIFIED: LIVE 2026-09-26: this is the EVENT JOIN, not the Event Shop. The
        # client sends it to the PTL 'E' row after entering an event room
        # (PC 0x761F0), and after /Ans=1 it waits for
        # a code-0xB2 message that returns 1 -- unanswered, it times out. Route
        # A: on msgid 0x17, `@Event=` then `@EQuit=/D=0/Stat=0`, same sender,
        # header shop byte == /N=, builds the event main scene directly.
        # UNMEASURED past the join.
        # SE'S FIRST-ENTRY FLOW: a (0xB2, 0x13) @Init= (the card shop's SHOPINIT
        # body, arm 0x105201) opens the EVENT CARD SCENE (PC ctor 0xFD0D0, PS2
        # CQMEventCard): the player picks five from their collection, the client
        # uploads (0xB2, 7) @CardSelect=/C=5@N0=/D=.. and waits for (0xB2, 7)
        # @Select=/A= from this same sender with shop byte == /N=, then goes on
        # to the tournament room. Once per
        # event window; later entries keep that deck and skip the scene.
        # OFF BY DEFAULT (POL_TM_EVENT_DECK_PICK=1 turns it on for everyone,
        # POL_TM_EVENT_DECK_PICK_MEMBERS=<id>,<id> for listed members): the first
        # live test (PS2, 2026-09-26) sent the @Init with shop byte 1, which the
        # client ignores (see below); fixed, not yet live-proved.
        try:
            import tmeventstate
            _picked = tmeventstate.deck(event_window()[0], member_id)
        except Exception:
            _picked = []
        if tournament._event_deck_pick_on(member_id) and len(_picked) != 5:
            # THE SHOP BYTE MUST NOT BE 1. The @Init arm returns -1 for shop
            # byte 1 and 1 otherwise, and only "otherwise" also sets the
            # network flag [obj+0xEA] that makes the scene upload its pick (PC
            # 0x510539F / 0x51053AD, the byte read by 0x50A9780(6) into +0x3D0;
            # PS2 0x40B53C..0x40B558 via 0x2E5D80 = +0x3C8 = header byte 3,
            # filled at 0x2CE814). The entry flow advances to the card scene only
            # on exactly 1 (PC 0x50FC91F), so the card shop's own /N=1 body
            # was ignored and the join timed out (the PS2 test, 2026-09-26).
            # /N= is kept equal: the scene checks the @Select reply's shop byte
            # against it ([obj+0x30]).
            _shop = common._env_int("POL_TM_EVENT_PICK_SHOP", 2)
            if _shop == 1:
                _shop = 2
            _init = re.sub(rb"/N=\d+", b"/N=%d" % _shop, shopdoors._shopinit_body(member_id), count=1)
            common._say("tm: member %s @EInitReq -- TEST event join, FIRST entry this "
                 "window: /Card=%d/Ans=%d, then (0xB2,0x13,shop %d) %s to open "
                 "the event card scene" % (member_id, card, ans, _shop,
                                           _init.decode("latin1")))
            return [ent, protocol.encode_code(protocol.MSG_SHOPINIT | ((_shop & 0xFF) << 24)) + _init]
        hdr = protocol.encode_code(0xB2 | (0x17 << 16) | (tournament._EVENT_JOIN_SHOP << 24))
        common._say("tm: member %s @EInitReq -- TEST event join: /Card=%d/Ans=%d, then "
             "@Event + @EQuit on (0xB2,0x17,shop %d)"
             % (member_id, card, ans, tournament._EVENT_JOIN_SHOP))
        return [ent,
                hdr + (b"@Event=/N=%d/RA=0/M=0/CP=0/S=1/RT=0" % tournament._EVENT_JOIN_SHOP),
                hdr + b"@EQuit=/D=0/Stat=0"]

    if b"@GameQT=" in cmd:
        # CONFIRM, on the table settings screen. See the banner above
        # `MSG_GAMEQA`: the client asks for this now, and without it `0x841E0`
        # returns 0 and the screen says Lobby.BIN 337, "Reservations cannot be
        # made with current table restrictions". Same value as the push.
        #
        # WARNING: TWO SENDERS, ONE NAME (static read 2026-08-22).
        # The BARE `@GameQT=` is the client's generic dialog-confirm --
        # rules-Confirm, a guest's rules-accept AND the tile-menu Cancel
        # Reservation are byte-identical (measured 08-21, confirmed static:
        # sender 0x83FC0 takes the table id as an argument but uses it only for
        # its local wait bookkeeping; nothing reaches the wire). Sender 0x84050
        # appends `/ID=<16-hex table id>` -- a table-scoped form fired from the
        # table-screen scene (sites 0x4BF68/0x4C01A; exact menu attribution
        # unpinned). Both senders drain and wait on (0x41, 6), so the reply is
        # the same @GameQA either way.
        #
        # Until 2026-08-22 BOTH forms were answered as confirm and nothing was
        # released -- which is the live report "cancel frees the table on my
        # screen and it comes back occupied": the ack satisfied the cancel's
        # waiter, the scene tore down and cleared the client-local reservation
        # global, and the next TD/PTL repainted the tile from the served row,
        # which still carried the seat. A `/ID=`-bearing `@GameQT=` now
        # RELEASES the sender's seat in their room (`release_seats` republishes
        # the freed row and hands off ownership; an emptied table drops its
        # confirmed flag in `_republish_table`). The bare form stays a pure
        # confirm -- distinguishing bare cancel from bare confirm needs the
        # scripted capture that has not been done; do not guess it here.
        # `POL_TM_GAMEQT_CANCEL=0` restores confirm-for-everything. Both arms
        # log the raw body, so a live session shows which form each UI action
        # sends.
        if not reservation._gameqt_enabled():
            return None
        qt = reservation._gameqa_qt()
        _idm = re.search(rb"/ID=([0-9A-Fa-f]{1,16})", cmd or b"")
        if _idm and common._env_int("POL_TM_GAMEQT_CANCEL", 1):
            wire_id = int(_idm.group(1), 16)
            chan = None
            try:
                import tmroom
                chan = tmroom.room_of(member_id)
            except Exception as exc:
                common._say("tm:   ...cannot place member %s in a room (%r)"
                     % (member_id, exc))
            common._say("tm: member %s @GameQT=/ID=%016X -- the table-scoped form: "
                 "releasing their seat%s (raw: %r)"
                 % (member_id, wire_id,
                    (" in %s" % chan) if chan
                    else " -- NO room record, nothing to release",
                    cmd[:96]))
            if chan:
                tableaudit.release_seats(member_id, chan)
            return protocol.encode_code(protocol.MSG_GAMEQA) + (b"@GameQA=/QT=%d" % qt)
        common._say("tm: member %s @GameQT= -- the bare dialog-confirm (confirm / "
             "accept / cancel are byte-identical); /QT=%d (raw: %r)"
             % (member_id, qt, cmd[:96]))
        return protocol.encode_code(protocol.MSG_GAMEQA) + (b"@GameQA=/QT=%d" % qt)

    if b"@GameENC=" in cmd:
        # *** CHECKED BEFORE `@GameEN=`, AND THAT ORDER IS LOAD-BEARING. ***
        # `b"@GameEN=" in cmd` is a SUBSTRING test and `@GameENC=` does not
        # contain it -- but only because of the `=`. The two literals sit 0x104
        # apart in TM.dll and the selftest below pins them apart; testing the
        # longer key first means a future edit to either key cannot silently
        # route the COM game into the reservation branch.
        if not vscom._gameeca_enabled():
            return None
        pushqueue._drop_stale_match_pushes(member_id, "a new VS. COM game (@GameENC=)")
        en = vscom._gameeca_en()
        common._say("tm: member %s @GameENC= -- VS. COM game request; answering "
             "@GameECA=/EN=%d (msgid 0x%02X). /EN= MUST be > 0: TM.dll 0x853AF "
             "parses it and 0 takes the -99 arm = Lobby.BIN 448."
             % (member_id, en, (vscom._gameeca_msgid() >> 16) & 0xFF))
        # WARNING: THE GAME PEER IS THE COM MATCH'S SESSION IDENTITY -- CAPTURE IT
        # HERE. A COM flow never sends `@GameOK=` (the PvP hook that fills
        # `_MATCH_PEER`), so every in-match push used to go out framed from the
        # recipient's OWN nick -- and the match object's session-pair gates
        # ([obj+0x28]/[0x2C], stored by the @ComGameInit arm 0x102D3C from the
        # DELIVERING record's sender u64) silently discard a message framed
        # from anyone else. That is where the dealt-but-dead @StartData went.
        # The peer this request rides is the peer the whole COM session lives
        # on (the client @Pongs it with /GM=), so pin pushes to it: right
        # socket AND right envelope in one.
        key = pushqueue._push_key(member_id)
        if key is not None:
            # A NEW GAME ENDS THE OLD ONE. A PvP roster still naming this
            # member is a match they are demonstrably not in any more (the COM-never-plays bug:
            # the crashed 3-player game that made every later COM game deal
            # and then never play). See `_match_abandoned`.
            if matchmaking._MATCH_ROSTER.get(key):
                tableaudit._match_abandoned(member_id, "a new VS. COM game (@GameENC=)")
            # A durable stake nothing in this process claims = a restart
            # orphaned it mid-match. Refund BEFORE the fresh game overwrites
            # the entry (the fourth restart casualty of 2026-08-22).
            pots._stake_recover(member_id, "new @GameENC=")
            vscom._COM_GAME[key] = {"n": None, "coms": [], "peer": peer_nick}
            if peer_nick:
                matchmaking._MATCH_PEER[key] = peer_nick
            # AND TELL THE ROOM. The peer this request rides IS the table
            # the player picked VS. COM on (`_peer_table` decodes it; measured
            # live 2026-08-25), and `/NN=` is the occupancy id the tile
            # renders. Without this the table stayed free for everyone else
            # and no Observe entry existed -- the reported bug.
            _enc_nn = re.search(rb"/NN=([0-9A-Fa-f]{1,16})", cmd or b"")
            vscom._bind_com_table(member_id, peer_nick,
                            int(_enc_nn.group(1), 16) if _enc_nn else None)
        # AND HAND THEM THE TABLE. The reply below is what lets the client
        # BUILD the COM scene; the `@MuchMake` pair rides later replies, never
        # this one, for the same reason `_queue_vsgameinit` does not ride the
        # `@GameWa=` that triggers it.
        vscom._queue_com_match(member_id, peer_nick)
        return protocol.encode_code(vscom._gameeca_msgid()) + (b"@GameECA=/EN=%d" % en)

    if b"@GameEN=" in cmd:
        # THE RESERVATION. See the banner above `MSG_GAMEEA`: the waiter reads
        # one field out of one message, and until now got neither. Checked
        # BEFORE `@GameM=` only for readability -- the two keys cannot collide,
        # and `@GameENC=` does not contain `@GameEN=` either.
        if not reservation._gameea_enabled():
            return None
        # SEAT THEM, AND TELL THE REST OF THE ROOM. This is the reported bug:
        # "when a game is reserved by one player, it doesn't show as reserved for
        # everyone else -- it still believes it's empty". `@GameEN=` arrives on
        # the TABLE's peer (measured 00:26:34Z on UE7QN1N9G, table 1), so it can
        # be placed, and `+0x0C` is the seated count every other client reads out
        # of `b/g/PTL`.
        #
        # WARNING: AND THIS DOES NOT SHOW ON ANYBODY'S SCREEN. Measured 2026-08-20
        # in `TM.dll.unpacked` after live testing reported "no really observable
        # changes at all": the room's table screen walks the records at 0x66D7A
        # and reads +0x00, +0x04, +0x14, block[0], block[4..5], block[14..29] and
        # block[48] -- and NOT +0x0C. A whole-image scan finds **one** reference
        # to +0x0C in the entire client, the auto-pick's ">= 8" refusal at
        # 0x7633A, and **zero** to +0x08 and +0x10.
        #
        # The seat count is kept anyway because it is TRUE and it does feed that
        # one gate, but the visible status is the STATE BYTE (+0x14) through the
        # jump table at 0x674C4, and WHO is sitting there is the two 64-bit ids
        # packed into block[14..29]. See the measurement in `tools/tmptl.py`.
        # Neither is written here: the state values are a 7-way vocabulary we
        # have only partly decoded, and the nibble order of those ids is not
        # pinned -- and an id fed to a consumer is worse than a blank tile.
        #
        # A FULL TABLE (or, opt-in, a repeat) IS REFUSED FIRST, before any seat
        # moves -- `_seat_at_table` would otherwise take this member off the
        # table they already hold elsewhere. See `_gameea_refusal`.
        refused = reservation._gameea_refusal(member_id, peer_nick)
        if refused is not None:
            common._say("tm: member %s @GameEN= REFUSED -- /EN=%d (%s); no seat moved"
                 % (member_id, refused,
                    "table full" if refused == reservation.GAMEEA_TABLE_FULL
                    else "already registered"))
            return protocol.encode_code(protocol.MSG_GAMEEA) + (b"@GameEA=/EN=%d" % refused)
        n = seating._seat_at_table(member_id, peer_nick, seated=True, cmd=cmd)
        # Start the heartbeat clock AT the reservation: the first table-peer
        # @Pong lands ~6 s later (measured), and a stampless seat would
        # otherwise ride the sweep's first-sight grace instead of the real TTL.
        _en_chan, _en_idx = matchmaking._peer_table(peer_nick)
        if _en_chan and member_id is not None:
            matchmaking._SEAT_ALIVE[matchmaking._alive_key(_en_chan, member_id,
                                   _en_idx)] = time.time()
        en = reservation._gameea_en() if n is None else max(n, reservation._gameea_en())
        common._say("tm: member %s @GameEN= -- the RESERVATION; /EN=%d%s"
             % (member_id, en,
                "" if n is None else " (%d at the table)" % n))
        return protocol.encode_code(protocol.MSG_GAMEEA) + (b"@GameEA=/EN=%d" % en)

    if b"@Break=" in cmd:
        # THE IN-MATCH LEAVE NOTICE. (0x43, msgid 39, `/ID=0`), sent from the
        # board scene's state 3 at 0x109B0C, measured on prod 2026-09-03 ~2 s
        # BEFORE the same client's `@GameExit=`. Nothing waits on a reply
        # (the sender's scene is on its way out), so none is sent -- but the
        # OTHER players' seats are what it is about: see `_seat_departed`.
        common._say("tm: member %s @Break= -- leaving the match (no reply is read; "
             "the empty seat is played by the server from here)" % (member_id,))
        turns._seat_departed(member_id, "@Break=")
        return None

    if b"@ExitCh=" in cmd or b"@Viewer=" in cmd or b"@Mode=" in cmd:
        # Fire-and-forget senders, read off TM.dll 2026-09-04: `@ExitCh=`
        # (code 0x20, fn 0x86460) builds, appends and calls the plain send
        # 0x82060 -- no store lookup, no wait; it precedes the IRC PART.
        # `@Viewer=` (0x41 msgid 0x11, fn 0x84F70) loops on 0x821D0, the SEND
        # completion, not a reply. `@Mode=` (0xB2, 0x1088AA) has never been
        # seen on a wire. Logged so they stop reading as "no answer".
        common._say("tm: member %s %s -- a fire-and-forget notice (no reply is "
             "polled for it)" % (member_id, cmd.split(b"=")[0].decode("latin1")))
        return None

    if b"@GameExit=" in cmd:
        # A PLAYER LEAVING A RUNNING MATCH: hand the seat to the server BEFORE
        # the roster bookkeeping below forgets who was playing.
        turns._seat_departed(member_id, "@GameExit=")
        # ...AND UNSEAT THEM. Without this the first reservation of a session
        # marks the table forever: the row is server-side state now, so somebody
        # has to take it back down, and a table that says 1 seated with nobody
        # there is worse than the bug this fixes.
        seating._seat_at_table(member_id, peer_nick, seated=False, cmd=cmd)
        # ...AND END THEIR MATCH, both kinds. The "match is RUNNING" signals
        # now gate the @Pong reservation re-feed (see `_reservation_reply` --
        # the ring-flood fix), so they must CLEAR on the room-return signal or
        # the re-feed stays suppressed at the table for ever, which is the
        # +0x108 bug reborn. `_reset_for_rematch` on the COM key is a no-op
        # for anyone not in a COM match.
        _gx_key = pushqueue._push_key(member_id)
        if _gx_key is not None:
            # A departing member stops being a watcher of anything.
            for _wset in watchers._MATCH_WATCHERS.values():
                _wset.pop(_gx_key, None)
            # ...AND GIVE THE TABLE BACK. A VS. COM game holds its tile at
            # Playing (`_COM_AT`); the room-return `@GameExit=` is what ends
            # it, exactly as it ends a PvP match below. The flag must
            # not outlive the game.
            _gx_cc, _gx_ci = vscom._com_table_of(member_id)
            if _gx_ci is not None:
                vscom._release_com_table(_gx_cc, _gx_ci, why="VS. COM over")
            rematch._reset_for_rematch(None, _gx_key)
            # ...AND REFUND AN UNSETTLED STAKE. The client's own abort path
            # (0xBC701) does `wallet += pot; pot = 0` when a COM game ends
            # without a result; mirror it or the save sync eats the stake.
            _gx_e = vscom._COM_GAME.get(_gx_key)
            if _gx_e and _gx_e.get("staked") and common._env_int("POL_TM_WAGER", 1):
                purse._set_money(member_id, purse.money_of(member_id) + _gx_e["staked"],
                           "VS. COM aborted -- stake refunded (mirror of "
                           "0xBC701)")
                _gx_e["staked"] = None
                pots._stake_clear(member_id)
            else:
                # ...and a stake only the COLLECTION remembers (the restart
                # wiped the match) is refunded here too.
                pots._stake_recover(member_id, "@GameExit=")
        _gx_c, _gx_i, _gx_s = matchmaking._match_of(member_id)
        if _gx_i is not None:
            _gx_run = matchmaking._MATCH_STARTED.get((_gx_c, _gx_i)) or set()
            _gx_run.discard(_gx_key)
            if not _gx_run:
                # THE LAST PLAYER IS BACK IN THE ROOM: the match is over and
                # "Playing" must not outlive it. Pop BOTH flags --
                # leaving _MATCH_BEGAN would hand a rematch-without-reconfirm
                # the old epoch and shrink its TTL hold -- then republish so
                # the tile falls back to whatever the seats justify (the
                # authored free state once everyone's @GameExit= has unseated
                # them via _seat_at_table above).
                matchmaking._MATCH_STARTED.pop((_gx_c, _gx_i), None)
                matchmaking._MATCH_BEGAN.pop((_gx_c, _gx_i), None)
                try:
                    import tmroom
                    tablerow._republish_table(tmroom, _gx_c, _gx_i,
                                     seating._seats_of(_gx_c).get(_gx_i) or [],
                                     why="match over, Playing withdrawn")
                except Exception as exc:
                    common._say("tm:   table %s not returned from Playing (%r)"
                         % (_gx_i, exc))
        # WARNING: AND ANSWER IT, OR THE CLIENT HANGS ON THE LOADING SCREEN. Reported live,
        # 2026-08-20T13:31Z: "framework is still stalled at the loading screen
        # when quitting" -- after a clean `@Quit=` exchange and `@GameExit=`,
        # with nothing further on the wire.
        #
        # `@GameExit=` follows this family's drain-before-you-wait idiom exactly,
        # and the sender shows the slot it will read:
        #
        #     0x8416C  push 0xe / push 0x41 / call 0x828E0   DRAIN (0x41, 0x0E)
        #     0x8417B  push 0xd / push 0x41
        #     0x8417F  "@GameExit=" / 0x84188 call 0xA97C0   SEND  (0x41, 0x0D)
        #
        # so the answer is request+1, and the waiter at 0x84601
        # (`0x827F0(buf, 0x41, ...)`) parses it at 0x8462D..0x8464F:
        #
        #     0xAA920(msg, "@GameEA")     the name -- the reservation reply's
        #                                 name, REUSED on a different msgid
        #     0xAB080("/Exit=", ..)       one int; 0x8465B tests `> 0` before
        #                                 clearing the global at 0x298200
        #
        # WARNING: The literal is `@GameEA` with no `=`; the wire needs the `=` like
        # every other key on this channel.
        if not common._env_int("POL_TM_GAMEEXIT_ANS", 1):
            return None
        ex = common._env_int("POL_TM_GAMEEXIT", 1)
        common._say("tm: member %s sent @GameExit= -- answering (0x41, 0x0E) "
             "@GameEA=/Exit=%d (0x8416C drains that slot immediately before "
             "sending, so it is waiting for it; 0x8465B wants > 0)"
             % (member_id, ex))
        return protocol.encode_code(matchmaking.GAMEEXIT_ANS_MSGID) + (b"@GameEA=/Exit=%d" % ex)

    if b"@GameM=" in cmd:
        # THE TABLE'S MEMBER LIST. See the banner above `MSG_GAMEML`.
        if not reservation._gameml_enabled():
            return None
        # WARNING: WE ANSWER TOO FAST, AND THIS CLIENT DROPS A REPLY THAT BEATS ITS
        # OWN BOOKKEEPING. Reported live 2026-08-20: Table Members renders EMPTY on a
        # table two people are standing at -- and the reply is demonstrably on
        # the wire and correct (`@GameML=/ID=../Num=2/MLID=..` with both rows,
        # authserv.log 02:49:39). The gap between the request and our answer was
        # **0.57 ms**.
        #
        # That is the `@TeachDVAns` race, one command over, and it is measured
        # on THIS client: "the client marks the operation pending AFTER its send
        # returns, and the result handler drops anything that lands while the
        # state is still 0. We were answering in ~3 ms." `_teach_delay` exists
        # for exactly that, and POL_POLPRO_DELAY_MS is the same fault a third
        # time on the lobby band.
        #
        # WARNING: THIS IS A HYPOTHESIS WITH A CHEAP TEST, NOT A MEASUREMENT. If the
        # pane is still empty with a delay in place, the delay is NOT the fault
        # and `POL_TM_GAMEML_DELAY_MS=0` puts it back rather than leaving an
        # unexplained pause in the path -- the same discipline `_teach_delay`
        # asks for.
        common._reply_delay("POL_TM_GAMEML_DELAY_MS", 1500)
        body = reservation._gameml_body(cmd, member_id, peer_nick)
        lines = [protocol.encode_code(protocol.MSG_GAMEML) + body]
        # WARNING: THE RESERVATION-SLOT COPY (msgid 0xC) ONLY WHEN THE REQUESTER IS
        # SEATED. @GameM= is the only request the client sends (log 2026-08-21:
        # it sends just @GameEN=/@GameM=/@GameQT=), so the reservation slot
        # (+0x108) fills from THIS response, on msgid 0xC. But a NON-SEATED
        # player sends @GameM= just to LOOK, and filling their +0x108 with the
        # seated players made their client think THEY were reserved and grey
        # Reserve ("viewing table members makes reservation impossible").
        # So: a SEATED requester gets the reservation list (their tile shows
        # pawns + Start Game -- the tester's "State A"); a looker gets only
        # the members pane and their Reserve stays available. POL_TM_GAMEML_RESV=0
        # disables.
        # WARNING: On the @GameM= reply frame the members flag +0x414 IS armed, so the
        # members arm (0xA918F) pops the FIRST matching (0x41,4) and the
        # reservation arm (0xA940C) sees nothing -- this copy cannot fill +0x108
        # on this frame. With POL_TM_RESV_MSGID=4 (default) it emits a SECOND
        # msgid-4 @GameML that lingers in the store for the next (non-View) frame,
        # where +0x414 is disarmed and the reservation arm can take it. The @Pong
        # re-feed is the primary carrier; this is belt-and-braces. See
        # _resv_gameml_code() for why 0xC never worked for the owner.
        if os.environ.get("POL_TM_GAMEML_RESV", "1") == "1" and body \
                and reservation._is_seated_here(member_id, peer_nick):
            lines.append(protocol.encode_code(protocol._resv_gameml_code()) + body)
        # ...and PUSH `@GameQA` behind it. See the banner above `MSG_GAMEQA`:
        # the reservation reads its verdict out of a message the client never
        # requests, so it has to arrive unsolicited. `@GameM=` is the one
        # table-context command the client does send, which makes it the
        # earliest moment we know the player is looking at a table.
        if reservation._gameqa_enabled():
            lines.append(protocol.encode_code(protocol.MSG_GAMEQA) + (b"@GameQA=/QT=%d" % reservation._gameqa_qt()))
        return lines

    if b"@Req=" in cmd and b"@ShReq=" not in cmd and b"@CvReq=" not in cmd:
        # THE LAST UNANSWERED MESSAGE IN THE CHAIN. See MSG_PLAYACK.
        # WARNING: The key test is anchored so `@ShReq=` (the card shop) and `@CvReq=`
        # (the Prize Center) cannot fall in here -- both CONTAIN "@Req=" as a
        # substring and both already have their own handlers above.
        if not common._env_int("POL_TM_PLAYACK", 1):
            return None
        en = common._env_int("POL_TM_PLAYACK_EN", 1)
        common._say("tm: member %s @Req= -- answering code 2 @PLAYACK=/EN=%d "
             "(0x86A7F needs > 0; 0 becomes error 0xFFFF7FFD)" % (member_id, en))
        # \U0001f534 ON THE PS2 THIS IS ALSO THE COM-BOARD TRIGGER, BECAUSE THE
        # CONSOLE'S ENTRY HANDSHAKE DOES NOT EXIST.
        #
        # MEASURED 2026-09-08. `_queue_com_match` (the only thing that ever
        # queues `@ComGameInit` into (0x43, 2)) hangs off the `@GameENC=`
        # handler -- and **`@GameENC`, `@GameECA` and `@CheckJoinTable` are all
        # ABSENT from `TMaster.pex`**, so that trigger can never fire on the
        # console. The PS2's VS. COM entry is
        #
        #     @Init= -> @InitAns=/Ans=1 ; <CR> profile -> <CS> ; @Req= -> @PLAYACK
        #
        # and then it polls (code 0x43, msgid 2) for `@ComGameInit` until it
        # gives up with **`-1-37338`** ("timed out while connecting to the
        # server", selector 338 at `0x0031a84c`, the only site that raises it).
        # The client-side names came out of the module: `0x003edbe0(obj, N)`
        # fetches (code 67, msgid N) and its msgid ladder maps 2 ->
        # `@ComGameInit` (`0x00489C58`) and 39 -> `@Dead` (`0x00489D18`).
        # Same build-split trap as `@EQuit`/`@Quit`.
        #
        # WARNING: THE GATE IS MATCH CONTEXT, NOT CLIENT KIND, because there is no
        # build tell in scope here (same problem `POL_TM_SHOPQUIT_PS2` has) and
        # the PC sends `@Req=` too -- in PvP, after `@GameReady=`. So fire ONLY
        # when this `@Req=` arrives with NO match context at all: no COM game
        # already set up (the PC's `@GameENC=` sets one), no PvP roster, and not
        # seated at a table. On the PC both COM and PvP fail at least one of
        # those, so this is a no-op there.
        #
        # WARNING: AND IT IS FAIL-SAFE ON PURPOSE. `@Req=` -> `@PLAYACK` ALREADY
        # WORKS on both builds; this trigger is additive, so anything it raises
        # (`_is_seated_here` does a bare `int(member_id)`, for one) must not be
        # allowed to take the working reply down with it.
        if common._env_int("POL_TM_REQ_COMGAMEINIT", 1) and member_id is not None:
            try:
                _rk = pushqueue._push_key(member_id)
                # A TOURNAMENT GAME'S @Req= IS NOT A COM ENTRY. Live 2026-09-26:
                # both event players' @Req= took this branch, and the COM board,
                # @Quit and @GameEA=/Exit=1 rode alongside @EventGameInit ->
                # "Could not start game".
                # NOR IS A SPECTATOR'S. 2026-10-03: a player walking up to watch
                # a live tournament game sent this same bare @Req= to that
                # table's peer, got a COM board + @GameEA=/Exit=1 queued, and
                # the @Data=/Watch= that followed found that fake COM game
                # instead of the match: they hung on loading.
                _wt = matchmaking._peer_table(peer_nick)
                _watching = _wt[1] is not None and _wt in boardrules._MATCH_TURN
                _bare = (_rk is not None and not vscom._COM_GAME.get(_rk)
                         and not matchmaking._MATCH_ROSTER.get(_rk)
                         and not tournament._in_event_match(member_id)
                         and not _watching
                         and not reservation._is_seated_here(member_id, peer_nick))
            except Exception as _exc:
                _bare, _rk = False, None
                common._say("tm: member %s @Req= -- COM-board gate skipped (%r); "
                     "the @PLAYACK reply below is unaffected"
                     % (member_id, _exc))
            if _bare:
                pots._stake_recover(member_id, "bare @Req= (PS2 VS. COM entry)")
                vscom._COM_GAME[_rk] = {"n": None, "coms": [], "peer": peer_nick}
                if peer_nick:
                    matchmaking._MATCH_PEER[_rk] = peer_nick
                common._say("tm: member %s bare @Req= with no match context -- treating "
                     "it as the PS2 VS. COM entry and queueing @ComGameInit into "
                     "(0x43, 2). @GameENC=/@GameECA=/@CheckJoinTable are absent "
                     "from TMaster.pex, so the PC trigger cannot fire here; "
                     "without this the client times out with -1-37338. "
                     "(POL_TM_REQ_COMGAMEINIT=0 disables)" % (member_id,))
                vscom._queue_com_match(member_id, peer_nick)
        return protocol.encode_code(matchmaking.MSG_PLAYACK) + (b"@PLAYACK=/EN=%d" % en)

    if b"@GameReady=" in cmd:
        # VERIFIED: THE OWNER-ACTION MATCH TRIGGER, captured live 2026-08-22. When the
        # host presses "Start Game" it walks @CheckJoinTable= -> PART -> this
        # @GameReady= (0x41 msgid 0x0A, no reply slot -- see the banner above
        # MSG_GAMEEA/GAMEWA) -> @Req= -> @GameOK=, and enters the match. The GUEST
        # meanwhile sits at the table polling @Pong, because it never received the
        # wake-up @GameStart= (0x41 msgid 7 = MATCH_41_MSGID) that starts its OWN
        # copy of that chain. This is the trigger the old headcount _announce_match
        # got wrong (it fired on SEAT COUNT before confirm, so the wake-up hit an
        # open rules dialog and the host answered @GameNG= -- see the 12:43 trace
        # by _TABLE_CONFIRMED). @GameReady= is post-confirm, post-PART: an
        # unambiguous "the host is going now", so wake the OTHER seated players.
        #
        # Delivered on their NEXT @Pong (after=lead), never appended to this reply
        # -- the wake-up must land on a frame AFTER the scene that polls (0x41,7)
        # exists (POL_TM_MATCH41_GAP / the _announce_match banner). The host is NOT
        # woken: it initiated on its own and is already past this scene.
        # POL_TM_GAMEREADY_WAKE=0 disables.
        if not common._env_int("POL_TM_GAMEREADY_WAKE", 1):
            return None
        chan, index, seats = matchmaking._match_of(member_id)
        if not seats:
            common._say("tm: member %s sent @GameReady= but no match roster is known -- "
                 "cannot wake the other players (was the seat path reached?)"
                 % (member_id,))
            return None
        # This member has now STARTED. Track it so we never wake a player who is
        # already going: without this, the guest's own @GameReady= (sent after it
        # is woken) would push @GameStart= back at the host, who is already in the
        # match -- a ping-pong, and a stray (0x41,7) into a scene that no longer
        # polls it. Wake only seated players who have NOT started.
        started = matchmaking._MATCH_STARTED.setdefault((chan, index), set())
        started.add(pushqueue._push_key(member_id))
        # The heartbeat-expiry match gate reads this: in-match players stop
        # ponging the table (the reservation is consumed client-side), so the
        # TTL must not reap a running game's seats. See `_MATCH_BEGAN`.
        matchmaking._MATCH_BEGAN.setdefault((chan, index), time.time())
        # ...AND TELL THE DEPLOY GATE NOW. The marker used to be first written
        # at `@CardSelect=`, so Start Game -> deal reported 0 live matches and
        # a restart in that window wiped the accept quorum (2026-09-03T22:57Z).
        webwatch._live_matches_write()
        # ...AND TELL THE ROOM. The row sat at recruiting (state 1) for the
        # whole match, so everyone else kept seeing Join on a table mid-game.
        # _match_running is True as of the add() above, so this republish
        # serves state 2 -- "Playing", with Observe on the menu. Idempotent on
        # the guests' later @GameReady= (note_table no-ops an identical row).
        try:
            import tmroom
            tablerow._republish_table(tmroom, chan, index,
                             seating._seats_of(chan).get(index) or list(seats),
                             why="match started, now Playing")
        except Exception as exc:
            common._say("tm:   table %s not flipped to Playing (%r) -- the room "
                 "keeps seeing the recruiting tile" % (index, exc))
        others = [(m, i) for (m, i) in seats
                  if pushqueue._push_key(m) != pushqueue._push_key(member_id)
                  and pushqueue._push_key(m) not in started]
        if not others:
            common._say("tm: member %s @GameReady= -- no un-started OTHER seated player "
                 "to wake (roster %r, started %r)"
                 % (member_id, [m for m, _ in seats], sorted(started)))
            return None
        body = protocol.encode_code(matchmaking.MATCH_41_MSGID) + os.environ.get(
            "POL_TM_MATCH41_BODY", "@GameStart=").encode("latin1", "replace")
        lead = common._env_int("POL_TM_MATCH41_GAP", 1)
        common._say("tm: member %s @GameReady= (host starting) -> waking %d other seated "
             "player(s) with @GameStart= (0x41 msgid 7) on their next @Pong: %s"
             % (member_id, len(others), ", ".join(str(m) for m, _ in others)))
        for mid, _ident in others:
            pushqueue._queue_push(mid, body, "match wake-up at table %d" % index, after=lead)
        return None

    if b"@GameOK=" in cmd or b"@GameNG=" in cmd:
        # THE TWO MESSAGES THE HOST AND GUEST SENT THE MOMENT (0x41,7) LANDED,
        # both of which we met with silence and both of which stalled. `@GameNG=`
        # is msgid 0x0F, so `request+1` puts its answer in the 0x10 slot the
        # client is already polling -- and that slot's waiter names it `@GameWa`
        # with `/Error=`. See GAMEWA_MSGID.
        if not common._env_int("POL_TM_GAMEWA", 1):
            return None
        err = common._env_int("POL_TM_GAMEWA_ERROR", 0)
        ok = b"@GameOK=" in cmd
        code = matchmaking.GAMEWA_OK_MSGID if ok else matchmaking.GAMEWA_NG_MSGID
        common._say("tm: member %s sent %s -- answering (0x41, %#x) @GameWa=/Error=%d"
             % (member_id, "@GameOK=" if ok else "@GameNG=",
                (code >> 16) & 0xFF, err))
        if ok and err == 0:
            webwatch._live_matches_write()          # the accept window is live time too
            # THE BAND, captured from the message itself. `@GameOK=` is sent by
            # the match scene, so the peer it arrives on is the peer that scene
            # is listening to -- and a board delivered anywhere else is unread
            # (see `_pending_pushes`).
            if peer_nick:
                matchmaking._MATCH_PEER[pushqueue._push_key(member_id)] = peer_nick
            # AND THIS IS WHERE THE BLACK SCREEN STARTS. `@GameWa=/Error=0` is
            # the last thing either client is told; the scene it then builds
            # polls `(0x43, 1)` and we have never answered it. Queued, not
            # appended -- see `_queue_vsgameinit` for why it must not ride this
            # reply. `@GameNG=` gets nothing: that player declined.
            vscom._queue_vsgameinit(member_id)
        return protocol.encode_code(code) + (b"@GameWa=/Error=%d" % err)

    if (b"@CardSelect=" in cmd and (code or 0) & 0xFF == 0xB2
            and tournament._event_test_member(member_id)):
        # THE EVENT CARD SCENE'S UPLOAD, not a board's pick: code 0xB2, shop
        # byte = the @Init's /N=. Its answer is (0xB2, 7) @Select=/A= with the
        # same shop byte (arm 0x1063FE parses /A= and drops it, returns 1).
        rows = boardrules._hands_from_cardselect(cmd)
        shop = ((code or 0) >> 24) & 0xFF
        if len(rows) == 5:
            try:
                import tmeventstate
                tmeventstate.set_deck(event_window()[0], member_id, rows)
            except Exception as exc:
                common._say("tm: WARNING: event deck for member %s NOT saved (%r)"
                     % (member_id, exc))
        # ...AND THEN LET THEM IN. The card scene closes on the @Select (its
        # state 10 polls msgid 7 first), but the entry helper behind it then
        # sits in sub-state 5 (RVA 0xFCCD8) polling (0xB2, 0x17) with NO
        # timeout: only an @EQuit from the same sender with the same shop byte
        # moves it on (sub-state 6 -> 4 -> phase 5 -> the event room). Without
        # it the first live pick (2026-10-03 01:58) left the player on
        # "loading" for good. Route A sends the same @EQuit after @Event.
        common._say("tm: member %s picked the TOURNAMENT DECK: %s -- answering "
             "(0xB2,7,shop %d) @Select=/A=0 + (0xB2,0x17) @EQuit to enter the room"
             % (member_id, ", ".join(r.split(b"|")[0].decode() for r in rows),
                shop))
        return [protocol.encode_code(0xB2 | (7 << 16) | (shop << 24)) + b"@Select=/A=0",
                protocol.encode_code(0xB2 | (0x17 << 16) | (shop << 24))
                + b"@EQuit=/D=0/Stat=0"]

    if b"@CardSelect=" in cmd:
        # VERIFIED: THE HOST IS ON THE BOARD AND THIS IS WHAT IT ASKS NEXT. Measured
        # live 2026-08-20T10:55:55Z, seconds after the first `@VsGameInit` was
        # read: `code=67 msgid=0x07 @CardSelect=/C=5@N0=/D=161|100|2|90|...`,
        # i.e. the player's five chosen cards. Unanswered, the screen sits on
        # "selecting" for ever (reported live, same minute).
        #
        # THE ANSWER, off arm 0x1030C9 -- and note it is gated like every other
        # in-match message on `[esi+0x28]/[esi+0x2c]`, the session pair that
        # `@VsGameInit` established, so this can only work after the board:
        #
        #   /Ans= once PER SEAT (occurrence i, i in 0..N-1), each written to
        #         `[esi + 0x19D + 48k]` where k = (i - me) mod N is the same
        #         display slot the board uses. `0x10317E` treats the value 1
        #         specially -- a seat whose byte becomes 1 having been 0 gets
        #         `[esi + 0x19F + 48k] = 5` -- which reads as "this player has
        #         confirmed", so 1 per seat is "everybody has chosen".
        #   /Ok=  occurrence 0. `0x1031B5` loads it and `0x1031BC` **increments**
        #         it into the result slot, and every caller tests `cmp eax, 1`.
        #         So **`/Ok=0` is the success value**, not 1.
        #
        # WARNING: NOT MEASURED. The widths and the arithmetic are read off the parser;
        # the VALUES are inferred from that `== 1` arm. `POL_TM_CARDSELECT=0`
        # restores the silence for an A/B, and `_ANS` / `_OK` sweep the two
        # numbers without a rebuild. The client says whether it read it:
        # `---->Recv=CARDSELECT` (0x103107) is in the traced sink.
        if not common._env_int("POL_TM_CARDSELECT", 1):
            common._say("tm: member %s sent @CardSelect= -- POL_TM_CARDSELECT=0, "
                 "staying silent" % (member_id,))
            return None
        msgid = ((code or 0) >> 16) & 0xFF
        chan, index, seats = matchmaking._match_of(member_id)
        # A LIVE COM BINDING BEATS A STALE LOBBY ROSTER -- see `_in_com_game`.
        if seats and vscom._in_com_game(member_id):
            common._say("tm:   ...member %s is in a VS. COM game but `_match_of` still "
                 "holds a %d-seat lobby roster from a reservation -- IGNORING "
                 "it. Left standing it deals no COM hand and pins the active "
                 "seat at 0 for ever." % (member_id, len(seats)))
            chan, index, seats = None, None, []
        # THE VS. COM PLAYER COUNT, AND THE MATCH KEY. `seats` is empty in a
        # COM game; the real N arrived on `@ComGame=/Com=...` (1 + one entry
        # per COM opponent -- 2 was hardcoded before that field was parsed).
        # And a COM match is keyed PER MEMBER: `_match_of` answers
        # (None, None) for it, so two members in simultaneous COM games would
        # share one board, one hand store and one turn counter.
        com_n = None if seats else vscom._com_n(member_id)
        if com_n:
            index = pushqueue._push_key(member_id)
        picked = re.search(rb"/C=(\d+)", cmd or b"")
        # THE HAND, AND THIS IS THE ONLY TIME WE SEE IT. `@PutData=` names a
        # card by hand index alone, so `@PutCard`'s `/D=` can only be built from
        # what was captured here. See `_MATCH_HANDS`.
        _hand = boardrules._hands_from_cardselect(cmd)
        if _hand:
            _hands_cs = boardrules._MATCH_HANDS.setdefault((chan, index), {})
            _hands_cs[pushqueue._push_key(member_id)] = _hand
            # ...AND THE DECK COPY, keyed by SEAT -- the win screen's take
            # list re-supplies every player's five (the play hands get popped
            # as played), and PvP needs it exactly like the COM path: a
            # decisive PvP game parked on the take polls without it.
            _seat_cs = next((i for i, (m, _v) in enumerate(seats)
                             if pushqueue._push_key(m) == pushqueue._push_key(member_id)), 0)
            _hands_cs[("deck", _seat_cs)] = list(_hand)
            common._say("tm:   ...member %s is holding %d card(s): %s"
                 % (member_id, len(_hand),
                    b", ".join(r.split(b"|")[0] for r in _hand).decode()))
        # WARNING: AND IT MUST BE TRUE. The first version answered `/Ans=1` for EVERY
        # seat on the first player's selection -- i.e. "everybody has chosen"
        # when only one had. Measured 2026-08-20T11:04Z: the host went on, the
        # guest (whose board was still 19 s away) arrived to a game that had
        # already moved and said it could not start. `/Ans=` occurrence i is
        # ABSOLUTE seat i (0x103150's `(N - me + i) % N` is only the DISPLAY
        # slot it writes to), so the vector has to say who has actually picked.
        ready = matchstart._CARD_READY.setdefault((chan, index), set())
        ready.add(pushqueue._push_key(member_id))
        # CARD SELECT IS PART OF THE MATCH as far as the deploy gate is
        # concerned -- stamp it here, not only at the deal. See
        # `_live_matches_write`.
        webwatch._live_matches_write()
        others = [m for m, _i in seats if pushqueue._push_key(m) not in ready]
        # WARNING: AN EMPTY ROSTER IS NOT "EVERYONE HAS CHOSEN". `others` is a filter
        # OVER `seats`, so it is VACUOUSLY empty when we hold no seats -- and
        # `not others` is the deal trigger. A PvP match whose roster this
        # process never saw therefore DEALS on the first `@CardSelect=` to
        # arrive, and `deal_to = seats or [(member_id, 0)]` deals it to the
        # SENDER ALONE.
        #
        # Measured 2026-08-25T03:01:13Z, 8s after authsess was recreated
        # mid-card-select: "member 16 @CardSelect= ... 1 of 2 have chosen"
        # followed immediately by "...dealing: (0x43, 8) @StartData= to member
        # 16" -- one seat dealt, `_MATCH_TURN` created, and member 3 left in
        # card select for ever because THIS BAND NEVER RESENDS (see below).
        # The restart did not merely lose the match, it MANUFACTURED a corrupt
        # one and published it to the deploy gate as live.
        #
        # A VS. COM game legitimately has no seats -- `com_n` is what says so,
        # and it is the only case `deal_to`'s solo fallback was written for.
        # `POL_TM_DEAL_ROSTER_GATE=0` restores the old vacuous behaviour.
        roster_known = bool(seats) or bool(com_n)
        if not common._env_int("POL_TM_DEAL_ROSTER_GATE", 1):
            roster_known = True
        n_eff = len(seats) or com_n or 2
        common._say("tm: member %s @CardSelect= (0x43, msgid %d) picked %s card(s) -- "
             "%d of %d have chosen%s. /Ok=0 is the success value (0x1031BC "
             "increments it). Watch for '---->Recv=CARDSELECT'."
             % (member_id, msgid, picked.group(1).decode() if picked else "?",
                len(ready), n_eff,
                "" if not others else "; still waiting on "
                + ", ".join(str(m) for m in others)))
        _mine = next((i for i, (m, _v) in enumerate(seats)
                      if pushqueue._push_key(m) == pushqueue._push_key(member_id)), 0)
        body_now = matchstart._cardselect_body(seats, ready, msgid, _mine, n_solo=com_n)
        # When the LAST player picks, everyone else is still holding the vector
        # from before and has no way to ask again -- this band never resends. So
        # the completed vector is pushed to them, the same shape as the board.
        if not others and not roster_known:
            common._say("tm: WARNING: member %s completed card select for a match this "
                 "process has NO ROSTER for (no seats, not a COM game) -- "
                 "REFUSING to deal. Dealing here would start a one-sided game "
                 "for the sender alone (deal_to's solo fallback) and strand "
                 "everyone else in card select. Almost always: login/authsess "
                 "was recreated mid-match -- check the container's uptime."
                 % (member_id,))
            # ...AND TELL THE CLIENT THE MATCH IS GONE, in the one message its
            # card-select scene reads for exactly that. Read off TM.dll
            # 2026-09-04: the scene at 0xD7280 polls (0x43, 7) for the picks
            # and then (0x43, 40) `@DataError`; on a hit (0xD7317..0xD737C)
            # it sends `@Quit=` (cmd 5, which the arm below answers), shows
            # message base+0x5DC and leaves (state 0x11). The arm 0x103481
            # parses `/No=` into a stack local and discards it. Without this
            # the client sat in card select until its own timeout -- the
            # "a fresh process cannot tell a client its match is gone" hole.
            # Card select is the ONLY match scene that polls cmd 40 (the
            # board scene does not), so this is sent here and nowhere else.
            if common._env_int("POL_TM_DATAERROR_ON_LOST_MATCH", 1):
                _no = common._env_int("POL_TM_DATAERROR_NO", 0)
                pushqueue._queue_push(member_id,
                            protocol._turn_code(protocol.DATAERROR_CMD, 0) + b"@DataError=/No=%d" % _no,
                            "the match is gone (@DataError -> the client quits)",
                            after=0)
                common._say("tm:   ...pushing (0x43, %d) @DataError=/No=%d so the "
                     "client leaves card select cleanly (it answers with "
                     "@Quit=). POL_TM_DATAERROR_ON_LOST_MATCH=0 disables."
                     % (protocol.DATAERROR_CMD, _no))
        if not others and roster_known and common._env_int("POL_TM_STARTDATA", 1):
            # THE DEAL. 0xC28C8 asks for command 8 the moment card select is
            # released, and nothing else will move the scene. Per recipient,
            # because `/S=` is in the recipient's own seat frame.
            # WHO LEADS. Retail spun the turn-order roulette to choose the
            # starting seat at RANDOM; we hardcoded seat 0, so the human led
            # every game (the roulette played but always landed on the player --
            # reported live 2026-09-02). The client is server-authoritative
            # here: `@StartData`'s `/S=` is consumed at TM.dll 0x10354E with NO
            # validation, no clamp and no "human first" branch (disassembled
            # 2026-09-02), so any seat in [0, N) is honoured. `/S=` and
            # `@TurnData`'s `/A=` both derive from this one `starter`, so drawing
            # it once keeps them consistent, and a COM seat winning the toss is
            # already handled by the `starter % n_eff != 0` COM-leads path below.
            # WARNING: UNIFORM IS AN INFERENCE, NOT A MEASUREMENT: the roulette is
            # cosmetic (it animates AFTER /S= is read), so the client holds no
            # record of retail's distribution. Uniform across the seats is the
            # least-committal randomisation and is client-consistent. Setting
            # `POL_TM_START_SEAT` still forces a fixed seat for repro/tests (=0
            # restores human-first if a COM-leads-from-turn-0 game misbehaves
            # live -- that path was never exercised while starter was pinned 0).
            if os.environ.get("POL_TM_START_SEAT") is not None:
                starter = common._env_int("POL_TM_START_SEAT", 0)
            else:
                starter = random.randrange(max(1, n_eff))
            # THE TURN COUNTER STARTS HERE. `@StartData`'s own third envelope
            # byte is stored as the turn (0x103536), and the client counts up
            # from it to `5 * N` before it prints `End` -- see `_turn_code`.
            first = common._env_int("POL_TM_FIRST_TURN", 0)
            boardrules._MATCH_TURN[(chan, index)] = {"turn": first, "active": starter,
                                          "n": n_eff, "starter": starter}
            webwatch._live_matches_write()
            # The WATCHER deal (@StartData + turn-0 @TurnData) is sent AFTER the
            # players' deal below, so it can share the players' turn-0 `rnd`.
            # See "DEAL THE WATCHERS TOO".
            if com_n:
                # A NEW COM GAME AT THE SAME KEY: the previous game's board
                # would leave its tiles occupied (PvP gets this cleanup in
                # `_remember_match`; a COM game has no board handshake).
                boardrules._MATCH_BOARD.pop((chan, index), None)
                boardrules._MATCH_OBJECTS.pop((chan, index), None)
                scoring._MATCH_COMBO.pop((chan, index), None)
                # ...AND DEAL THE COM HANDS. The server plays the COM
                # (measured -- see `_queue_com_turns`), so each COM seat
                # needs five cards it can put down. Deck per character from
                # `_com_deck_rows`; the mapping knob is documented there.
                _hands = boardrules._MATCH_HANDS.setdefault((chan, index), {})
                _coms = (vscom._COM_GAME.get(pushqueue._push_key(member_id)) or {}).get("coms") or []
                # ...AND KEEP THE ORIGINALS. The play hands get popped card by
                # card, but the WIN screen's take list (@TCList, measured
                # 16:49Z: the winner polls (0x43,26)+(0x43,35) and nothing
                # else) carries every player's FULL five -- see _tclist_body.
                if _hand:
                    _hands[("deck", 0)] = list(_hand)
                for _s in range(1, n_eff):
                    _ci = _coms[_s - 1] if _s - 1 < len(_coms) else _s
                    _hands[("com", _s)] = vscom._com_deck_rows(_ci)
                    _hands[("deck", _s)] = list(_hands[("com", _s)])
                    common._say("tm:   ...COM seat %d (character %s) holds: %s"
                         % (_s, _ci,
                            ", ".join(r.split(b"|")[0].decode()
                                      for r in _hands[("com", _s)])))
            # WARNING: FOURTH SEAT GATE, AND THE ONE THAT HELD THE DEAL. This loop
            # iterates `seats`, which is EMPTY in a VS. COM game -- so the body
            # never ran and @StartData was never dealt, leaving the scene parked
            # at 0xC28C8 polling command 8 for ever. Live 2026-08-22: /Ok=1
            # released card select exactly as measured, and then nothing came.
            # `_startdata_body` already falls back to N=2 when seats is empty,
            # which is the right frame for human + COM, so the deal itself
            # needed no change -- only somebody to deal it to.
            # See the sibling gates in `_push_muchmake`, `_queue_vsgameinit` and
            # `_cardselect_body`: SEAT COUNT IS THE FIRST THING TO CHECK when a
            # COM feature stalls.
            deal_to = seats or [(member_id, 0)]
            # THE BOARD, ROLLED ONCE FOR THE WHOLE MATCH and seeded into
            # `_MATCH_BOARD` before anybody is dealt -- see `_match_objects`.
            # It has to happen AFTER the COM path's `_MATCH_BOARD.pop` above
            # or the pop would take the blocks straight back out again.
            _codes = boardrules._match_objects(chan, index, n_eff, fresh=True)
            for mid in (m for m, _i in deal_to):
                seat = next((i for i, (m, _v) in enumerate(deal_to)
                             if pushqueue._push_key(m) == pushqueue._push_key(mid)), 0)
                sd = (protocol._turn_code(protocol.STARTDATA_CMD, first)
                      + matchstart._startdata_body(seats, seat, starter, n_solo=com_n,
                                        codes=_codes))
                common._say("tm:   ...dealing: (0x43, %d) @StartData= to member %s "
                     "(seat %d, /S=%d). Watch for '---->Recv=STARTDATA'."
                     % (protocol.STARTDATA_CMD, mid, seat, (starter - seat) % n_eff))
                # WARNING: IT MUST NOT SHARE A BATCH WITH THE CARD-SELECT ANSWER.
                # Measured 2026-08-20T14:19:12Z: both went out in ONE reply
                # ("answered with 2 line(s)"), (0x43, 8) flipped to **FOUND** --
                # so it was retrieved -- and `---->Recv=STARTDATA` never
                # printed. `@CardSelect=/Ok=1` is the message that CHANGES THE
                # STATE (0x17 -> 0x18 -> 0x19), and the deal rode along with the
                # thing that creates the state meant to read it. Same law as
                # `POL_TM_MATCH41_GAP` and `@MuchMake`'s two halves.
                pushqueue._queue_push(mid, sd, "the deal",
                            after=common._env_int("POL_TM_STARTDATA_GAP", 1),
                            peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
            # ...AND THE FIRST TURN, behind the deal. Both clients poll
            # command 9 the moment @StartData lands. One random table for the
            # whole match -- see `_TURN_RAND`.
            if common._env_int("POL_TM_TURNDATA", 1):
                rnd = matchstart._turn_rand((chan, index), fresh=True)
                if com_n and (starter % n_eff) != 0:
                    # THE COM LEADS: announcing turn 0 is not enough, the
                    # server has to PLAY it -- the chain owns both.
                    vscom._queue_com_turns(chan, index, member_id, n_eff,
                                     first, starter)
                    return body_now
                # WARNING: FIFTH SEAT GATE (found 2026-08-22, one loop below the
                # fourth): this iterated `seats`, so a VS. COM game got its
                # deal and then NO turn 0 -- the scene reads @StartData and
                # immediately polls command 9, for ever. `deal_to` is the
                # roster that already carries the solo fallback.
                for mid in (m for m, _i in deal_to):
                    seat = next((i for i, (m, _v) in enumerate(deal_to)
                                 if pushqueue._push_key(m) == pushqueue._push_key(mid)), 0)
                    # Turn 0: the board is empty, so the score /S= is all
                    # zeros (the client's own tally would write the same
                    # after the first placement).
                    _sc0 = ([0] * n_eff
                            if common._env_int("POL_TM_TURN_SCORES", 1) == 1
                            else None)
                    td = (protocol._turn_code(protocol.TURNDATA_CMD, first)
                          + matchstart._turndata_body(seats, seat, starter, rnd,
                                           scores=_sc0, n_solo=com_n))
                    common._say("tm:   ...turn %d: (0x43, %d) @TurnData= to member %s "
                         "(seat %d, /A=%d, RandTbl=%s). Watch for "
                         "'---->Recv=TURNINFO'."
                         % (first, protocol.TURNDATA_CMD, mid, seat,
                            (starter - seat) % n_eff,
                            ",".join(str(v) for v in rnd)))
                    pushqueue._queue_push(mid, td, "turn %d" % first,
                                after=common._env_int("POL_TM_TURNDATA_GAP", 1),
                                peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
                # WARNING: DEAL THE WATCHERS TOO -- the play scene a watcher's @WFCard
                # opens needs the SAME @StartData + turn-0 @TurnData a player
                # gets, or it has no turn-0 baseline: the client polls cmd 9
                # (@TurnData) before cmd 10 (@PutCard), so with only the NEXT
                # turn's @TurnData filed it advances to turn 1 and the first
                # @PutCard(0) mismatches (`Errot!!Turn` -> "invalid card data",
                # the observer quits -- measured shim-CASPC 2026-09-03). Sent in
                # the ABSOLUTE frame (seat 0; a watcher is never rotated) with
                # the players' own turn-0 `rnd`, framed from the table peer that
                # delivered @WatchInfo. They FILE until @WFCard opens the play
                # scene, which then consumes @StartData -> @TurnData(0) ->
                # @PutCard(0) -> @TurnData(1) ... in order.
                # `POL_TM_WATCH_STARTDATA=0` turns the watcher deal off.
                if watchers._watchers_of(chan, index) and common._env_int("POL_TM_WATCH_STARTDATA", 1):
                    # WARNING: THE BOARD RIDES ALONG (2026-09-25): without `codes`
                    # the watcher's deal had /F= all zeros -- no blocks, no
                    # special tiles -- so an observer drew an empty board and
                    # its replay of the first battle on a block went wrong.
                    _wsd = (protocol._turn_code(protocol.STARTDATA_CMD, first)
                            + matchstart._startdata_body(seats, 0, starter, n_solo=com_n,
                                              codes=_codes))
                    _wtd0 = (protocol._turn_code(protocol.TURNDATA_CMD, first)
                             + matchstart._turndata_body(seats, 0, starter, rnd,
                                              scores=([0] * n_eff
                                                      if common._env_int("POL_TM_TURN_SCORES", 1) == 1
                                                      else None),
                                              n_solo=com_n))
                    for _wmid, _wpeer in watchers._watchers_of(chan, index):
                        pushqueue._queue_push(_wmid, _wsd, "watcher: the deal (@StartData)",
                                    after=common._env_int("POL_TM_STARTDATA_GAP", 1),
                                    peer=_wpeer)
                        pushqueue._queue_push(_wmid, _wtd0,
                                    "watcher: turn 0 (deal baseline)",
                                    after=common._env_int("POL_TM_TURNDATA_GAP", 1),
                                    peer=_wpeer)
                    common._say("tm:   ...watcher deal: @StartData + turn-0 @TurnData "
                         "(absolute frame) to %d watcher(s) -- the play scene's "
                         "turn baseline" % len(watchers._watchers_of(chan, index)))
        if not others and common._env_int("POL_TM_CARDSELECT_BROADCAST", 1):
            for mid in (m for m, _i in seats if pushqueue._push_key(m) != pushqueue._push_key(member_id)):
                common._say("tm:   ...that completes it -- pushing the same "
                     "@CardSelect= vector to member %s" % (mid,))
                _seat = next((i for i, (m, _v) in enumerate(seats)
                              if pushqueue._push_key(m) == pushqueue._push_key(mid)), 0)
                pushqueue._queue_push(mid, matchstart._cardselect_body(seats, ready, msgid, _seat),
                            "card select complete",
                            after=common._env_int("POL_TM_CARDSELECT_GAP", 0),
                            peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
        return body_now

    if b"@Data=" in cmd and (code or 0) & 0xFF == protocol.IN_MATCH_CODE \
            and ((code or 0) >> 16) & 0xFF == protocol.WATCHINFO_CMD:
        # THE OBSERVER. Measured live 2026-08-22T20:07Z:
        # the client JOINs the table's own
        # channel and sends `@Data=/Watch=<n>` on (0x43, 37); the answer is
        # `@WatchInfo` on the same slot -- the arm 0x101B15's full state
        # snapshot, with the envelope sh selecting the mid-game form (sh=1
        # reads /H /T /S /A /F /CH; the exit state is sh+0xE). After it, the
        # watcher consumes the same in-match pushes the players get.
        if not common._env_int("POL_TM_WATCH", 1):
            return None
        _wchan, _widx = matchmaking._peer_table(peer_nick)
        if _widx is None:
            # The selftest (and any odd peer) fall back to the only PvP
            # match running, loudly.
            _cands = [k for k in boardrules._MATCH_TURN
                      if k[0] is not None or isinstance(k[1], int)]
            if len(_cands) == 1:
                _wchan, _widx = _cands[0]
                common._say("tm:   ...watch request peer unresolved -- falling back "
                     "to the only running match %r" % ((_wchan, _widx),))
        if _widx is None:
            common._say("tm: 🔭 member %s sent @Data=/Watch= but the peer %r "
                 "resolves to no table and no single match is running -- "
                 "silent" % (member_id, peer_nick))
            return None
        # THE STATE KEY, AND IT IS NOT ALWAYS THE TABLE. A PvP match is
        # keyed by (chan, index); a VS. COM match is keyed `(None, push_key)`
        # -- per member, because `_match_of` cannot place it (see the
        # `@CardSelect=` handler). `_COM_AT` is what turns the table the
        # observer clicked back into that key, and registering the watcher
        # UNDER it is what makes every COM `_watch_push` reach them unchanged.
        _wkey = (_wchan, _widx)
        _wcom = vscom._com_key_at(_wchan, _widx)
        # WARNING: A LIVE VS. COM GAME WINS OVER A STALE PvP ROSTER (2026-09-25).
        # `_MATCH_ROSTER` keeps a PvP match's seats after it ends, so a COM
        # game later played at the SAME table was answered with the old PvP
        # players and the watcher was registered under that dead key -- it
        # then received nothing (a bot vs COM
        # at table 1 after a PvP match there). The COM binding only exists
        # while its game runs, so when it is present it is the answer.
        _wseats = [] if _wcom is not None else next(
            (v[2] for v in matchmaking._MATCH_ROSTER.values()
             if v[0] == _wchan and v[1] == _widx), [])
        _wcoms = []
        if not _wseats and _wcom is not None:
            _wgot = vscom._COM_GAME.get(_wcom[1]) or {}
            _wme = _wgot.get("member")
            if _wme is None:
                common._say("tm: 🔭 member %s wants to watch the VS. COM game at %s "
                     "table %s but its binding names no member -- silent"
                     % (member_id, _wchan, _widx))
                return None
            _wkey = _wcom
            # The human is ABSOLUTE seat 0 in a COM game (the whole COM path
            # builds its bodies with me=0), so the roster is one real member
            # followed by one entry per chosen COM character.
            _wseats = [(_wme, 0)]
            _wcoms = list(_wgot.get("coms") or [])
        if not _wseats:
            common._say("tm: 🔭 member %s wants to watch %s table %s but no match "
                 "roster is there -- silent (the game may not have started)"
                 % (member_id, _wchan, _widx))
            return None
        _wn = len(_wseats) + len(_wcoms)
        # WARNING: NEVER SERVE `/N=` < 2, AND A COM GAME CAN ASK BEFORE ITS ROSTER
        # EXISTS. `/N=` is not a display field: arm 0x101B15 writes it to
        # [esi+0xE5] and then **[esi+0xE6] = N - 2** as the BOARD-SIZE index
        # into the tile-count table at 0x51C359E. N=1 indexes that table at
        # **-1** -- a garbage board size the whole snapshot is then built on,
        # and no later correction undoes the seeding.
        #
        # Measured live 2026-08-25: member 6 opened Observe on a COM table at
        # 04:08:39Z, BEFORE that game's `@ComGame=/Com=` had arrived (04:09:06Z)
        # -- so `_com_n` was still None, `_wcoms` was empty, and we answered
        # "1 player(s)". The client re-asked 66 s later and got a correct N=2
        # sh=1 snapshot on top of the poisoned one, then the live card stream,
        # and threw "invalid card data detected, exiting Tetra Master".
        #
        # Silence is the right answer: the observer demonstrably RE-ASKS, and a
        # watcher that arrives a beat early then gets a snapshot built on a real
        # roster. `POL_TM_WATCH_MIN_N=0` restores the old behaviour for an A/B.
        if _wn < max(2, common._env_int("POL_TM_WATCH_MIN_N", 2)):
            common._say("tm: 🔭 member %s wants to watch %s table %s but the roster is "
                 "only %d player(s) -- STAYING SILENT rather than seeding the "
                 "observer with /N=%d (arm 0x101B15 derives the board size as "
                 "N-2, so N<2 indexes the tile table out of range and poisons "
                 "the whole scene). %s"
                 % (member_id, _wchan, _widx, _wn, _wn,
                    "The COM roster (@ComGame=/Com=) has not arrived yet; the "
                    "client re-asks." if _wcom is not None
                    else "The match roster is incomplete."))
            return None
        _wnames = [matchstart._vsgame_player_name(m, i)
                   for i, (m, _v) in enumerate(_wseats)]
        _wnames += [matchstart._com_player_name(_ci, len(_wseats) + _k)
                    for _k, _ci in enumerate(_wcoms)]
        # @WatchInfo /R= parses on the @VsGameInit slots (not @Tet order).
        _wrules = ruleset._vsgame_rule_seven(tablesettings._table_rules_get(_wchan, _widx))
        _wst = boardrules._MATCH_TURN.get(_wkey) or {}
        _wboard = boardrules._MATCH_BOARD.get(_wkey) or {}
        _whands = boardrules._MATCH_HANDS.get(_wkey) or {}
        # VERIFIED: ALWAYS THE BOARD FORM (sh=1), even before the deal. Measured
        # 2026-09-03 (disasm of the watch scene): `@WatchInfo` sh=1 routes the
        # watch scene to PHASE 5 (deal-start, arm 0xC1329), which draws the
        # board and self-advances phase 5 -> 2 -> 3 (0xC1549 -> 0xC12CD) with NO
        # further network input -- so the observer renders the live board
        # exactly like a player, then fills in from the @PutCard/@TurnData
        # pushes. sh=0 routes to PHASE 1 (0xC0D37), which NEVER writes a phase
        # and so never advances -- THE "stuck on selecting cards" DEAD-END a
        # card-select joiner hit live (and the reason the deal-time sh=1
        # *refresh* is inert: by then the scene's super-state is already 4 and
        # the 0x0F state it lands on is outside the play machine's range). A
        # pre-deal join therefore gets an EMPTY board (turn 0, full 5-card
        # hands); a mid-game join gets the real board; a post-result join gets
        # the frozen final board -- all of them a board, none of them the
        # dead-end. `POL_TM_WATCH_SH0=1` restores the old pre-deal sh=0 form.
        _dealt = _wst.get("turn") is not None
        counts = []
        for m, _v in _wseats:
            counts.append(len(_whands.get(pushqueue._push_key(m)) or []) if _dealt else 5)
        # A COM seat's hand is held under ("com", seat) -- the server is the COM
        # player (see `_queue_com_turns`), so these counts are the real
        # remaining cards, not a guess.
        for _k in range(len(_wseats), _wn):
            counts.append(len(_whands.get(("com", _k)) or []) if _dealt else 5)
        if not _dealt and common._env_int("POL_TM_WATCH_SH0", 0):
            sh, mid_game = 0, None            # opt-in to the old dead-end form
        else:
            sh = 1
            mid_game = {"turn": _wst.get("turn") or 0,
                        "active": _wst.get("active") or 0,
                        "counts": counts, "board": _wboard,
                        "objects": boardrules._MATCH_OBJECTS.get(_wkey) or [],
                        "tiles": boardrules._board_tiles(_wn), "s": 0}
        # WARNING: THE SNAPSHOT SUPERSEDES THE QUEUE, SO CLEAR IT FIRST. Anything
        # already queued for this member is state from BEFORE the board we are
        # about to hand them -- up to and including a whole previous match a
        # still-registered watcher never drained. Delivered behind the
        # snapshot it reads as a card for a turn the client has passed:
        # `Errot!!Turn1!=0` -> "Invalid card data detected." See
        # `_watch_stream_purge` for the live trace.
        watchers._watch_stream_purge(member_id,
                            "observing %s table %s at turn %s"
                            % (_wchan, _widx,
                               (mid_game or {}).get("turn", "pre-game")))
        watchers._MATCH_WATCHERS.setdefault(_wkey, {})[
            pushqueue._push_key(member_id)] = (member_id, peer_nick)
        common._say("tm: 🔭 member %s OBSERVES %s table %s%s -- answering @WatchInfo "
             "sh=%d (%s), %d player(s), %d watcher(s) now. WARNING: the sh=1 /F=/CH= "
             "tile encoding is an EXPERIMENT -- watch the observer's screen."
             % (member_id, _wchan, _widx,
                " (a VS. COM game, key %r)" % (_wkey,) if _wcom else "", sh,
                "mid-game snapshot" if mid_game else "pre-game",
                _wn, len(watchers._MATCH_WATCHERS.get(_wkey) or {})))
        _wreply = (protocol._turn_code(protocol.WATCHINFO_CMD, sh)
                   + watchers._watchinfo_body(_wnames, _wrules, _wn, mid_game))
        if mid_game and common._env_int("POL_TM_WATCH_WFCARD", 1):
            # The board's card FACES ride behind as @WFCard, one per occupied
            # tile. FORMAT CORRECTED 2026-09-03 off arm 0x1020F4 (see
            # `_wfcard_body`): /T= is the TURN, the tile is an `@<tile>` marker,
            # /D= is the 6-value row -- the old `@WFCard=/T=<tile>/D=<8 vals>`
            # put the tile in the turn slot and named no tile at all.
            _wturn = int((_wst.get("turn") or 0))
            _wa = 1
            for _t in sorted(_wboard):
                if _wboard[_t].placer == tmbattle.OWNER_BLOCK:
                    continue      # a block has no card face (`@WFCard` /D=)
                pushqueue._queue_push(member_id,
                            protocol._turn_code(protocol.WFCARD_CMD, _wturn)
                            + watchers._wfcard_body(_t, _wboard[_t].row, _wturn),
                            "watcher: tile %d's card" % _t,
                            after=_wa, peer=peer_nick)
                _wa += 1
        return _wreply

    if b"@TurnData=" in cmd:
        # THE CLIENT'S OWN ACK. 0x103801: after `@TurnData` is read, the arm
        # tests `[esi+0x32]` and, when it is 0, calls the sender at 0x106E30 for
        # command 9 -- `@TurnData=/Ans=<own seat>` (builder 0x10794A). It is a
        # receipt, not a request.
        #
        # WARNING: NEVER ANSWER WITH A (0x43, 9) BODY. Command 9 is the slot the
        # client READS `@TurnData` from, so a turn-shaped reply would be
        # re-read as another announcement. But in an ack-gated COM game this
        # ack is also the RELEASE: it proves the client has entered the turn
        # (past the 0xC7642 GamePlayEnd purge that eats an early @PutCard --
        # the 3-player "invalid card data" crash), so the pending COM put
        # answers it directly on its own (0x43, 10) slot. See
        # `_queue_com_turns` / `_com_play_acked`.
        _t = ((code or 0) >> 24) & 0xFF
        common._say("tm: member %s acked turn %d (@TurnData=)" % (member_id, _t))
        webwatch._live_matches_write()
        if common._env_int("POL_TM_COM_ACK_GATE", 1):
            _chan, _index, _seats = matchmaking._match_of(member_id)
            if _seats and vscom._in_com_game(member_id):
                # WARNING: THE SWALLOWED ACK (2026-09-06T23:35:47Z). A stale
                # PvP roster made `_seats` truthy, `_cn` was derived from it
                # BEFORE the COM override, and the COM's ack-released put
                # never went out -- "no answer for this command -- captured,
                # silent", COM never plays. Same override as `@CardSelect=`.
                _chan, _index, _seats = None, None, []
            _cn = None if _seats else vscom._com_n(member_id)
            if _seats:
                # A DEPARTED SEAT'S TURN in a PvP match: this ack proves the
                # client is parked on (0x43, 10), so the server's put for
                # that seat answers it directly (see `_seat_departed`).
                _st = boardrules._MATCH_TURN.get((_chan, _index)) or {}
                _pending = _st.get("await_ack")
                _bseat = _st.get("bot_seat")
                if (_pending is not None and _bseat is not None
                        and 0 <= _bseat < len(_seats)
                        and turns._is_bot(_chan, _index, _seats[_bseat][0])):
                    if _pending != _t:
                        common._say("tm: WARNING: member %s acked turn %d but the departed "
                             "seat's pending turn is %d -- following the client"
                             % (member_id, _t, _pending))
                    _st.pop("await_ack", None)
                    _st.pop("bot_seat", None)
                    return turns._bot_move(_chan, _index, _t, _bseat,
                                     direct_to=member_id)
            if _cn:
                _index = pushqueue._push_key(member_id)
                _st = boardrules._MATCH_TURN.get((_chan, _index)) or {}
                _pending = _st.get("await_ack")
                _n = _st.get("n") or _cn
                if _pending is not None and _pending == _t:
                    _st.pop("await_ack", None)
                    return vscom._com_play_acked(_chan, _index, member_id, _n, _t,
                                           _st.get("active", _t % _n))
                if _pending is not None and _pending != _t:
                    # The CLIENT owns the counter (0xC76A2, incl. its cardless
                    # skip) -- follow it, loudly.
                    common._say("tm: WARNING: member %s acked turn %d but the pending COM "
                         "turn is %d -- following the client"
                         % (member_id, _t, _pending))
                    _st.pop("await_ack", None)
                    if _t % _n != 0:
                        return vscom._com_play_acked(_chan, _index, member_id, _n,
                                               _t, _t % _n)
        return None

    if b"@PutData=" in cmd:
        # VERIFIED: A CARD IS PLAYED. Builder 0x1079EE (send table index cmd-5 = 10),
        # so the client asks on the same slot it will read the answer from:
        #
        #     /P= -> byte [ebp+0x19B]  the sender's own seat, which is ALWAYS 0
        #            in its own copy of the board -- so it does not
        #            identify anybody and the actor comes from the connection.
        #     /H= -> byte [obj+0x1B2]  the hand slot
        #     /F= -> byte [obj+0x1B1]  the board tile
        #     sh  -> byte [obj+0x18D]  the TURN, which `@PutCard` must echo
        #
        # The answer is `@PutCard` on the same (0x43, 10), and 0xC6FC7 is the
        # `cmp eax, 1` that both players -- placer and watcher alike -- are
        # parked on. The watcher gets it as a push on its own band.
        if not common._env_int("POL_TM_PUTCARD", 1):
            common._say("tm: member %s sent @PutData= -- POL_TM_PUTCARD=0, staying "
                 "silent" % (member_id,))
            return None
        turn = ((code or 0) >> 24) & 0xFF
        chan, index, seats = matchmaking._match_of(member_id)
        # ...and the same stale-roster override as `@CardSelect=`, or the
        # placement lands under the PvP key the deal never used.
        if seats and vscom._in_com_game(member_id):
            chan, index, seats = None, None, []
        com_n = None if seats else vscom._com_n(member_id)
        if com_n:
            index = pushqueue._push_key(member_id)    # per-member COM match key
        n = len(seats) or com_n or 2
        actor = next((i for i, (m, _v) in enumerate(seats)
                      if pushqueue._push_key(m) == pushqueue._push_key(member_id)), 0)
        _h = re.search(rb"/H=(-?\d+)", cmd or b"")
        _f = re.search(rb"/F=(-?\d+)", cmd or b"")
        _p = re.search(rb"/P=(-?\d+)", cmd or b"")
        hand_idx = int(_h.group(1)) if _h else 0
        tile = int(_f.group(1)) if _f else 0
        # WARNING: VS. COM, UNMEASURED TERRITORY: in a COM game the human is seat 0
        # in its own (and only) frame, so `/P=` nonzero can only be the client
        # reporting a COM seat's move -- a message shape no capture has shown
        # yet. We hold no hand for a COM seat (only `@CardSelect=` fills
        # `_MATCH_HANDS`, and only the human sends it), so don't guess a card:
        # log the whole thing where the next reading will look.
        if com_n and _p and int(_p.group(1)) != 0:
            common._say("tm: WARNING: member %s @PutData= with /P=%s in a VS. COM game -- "
                 "the client is reporting a COM seat's move and we hold no "
                 "hand for it. NOT answered. Full body: %r. Decide from this "
                 "capture whether the client carries the card (/D=?) or "
                 "expects the server to play the COM."
                 % (member_id, _p.group(1).decode(), cmd))
            return None
        hand = (boardrules._MATCH_HANDS.get((chan, index)) or {}).get(pushqueue._push_key(member_id)) or []
        if not 0 <= hand_idx < len(hand):
            # WARNING: DO NOT INVENT A CARD. A wrong `/D=` is a different card on the
            # board for both players and nothing anywhere reports it; an id at
            # or above the client's own limit ([0x2FF4FC]) or a type >= 4 is
            # `Error:CardNum=%d` and the arm bails. Say what is missing instead.
            common._say("tm: WARNING: member %s played hand slot %d but we hold %d card(s) "
                 "for them -- no @PutCard sent. The hand is only ever learned "
                 "from @CardSelect= (see _MATCH_HANDS); if that message was "
                 "missed this match cannot be relayed."
                 % (member_id, hand_idx, len(hand)))
            return None
        row = hand[hand_idx]
        # WARNING: AND THE HAND COMPACTS. Measured off `0xC70F4`, the arm that runs
        # once a placement is applied:
        #
        #     edi = [obj+0x17A]                 the hand slot just played
        #     while edi < [0x2B657F + 48*p]:    the (already decremented) count
        #         copy slot[edi+1] -> slot[edi]      156 bytes, 0x2B65EC + 156*i
        #
        # so the five per-player slots at `0x2B65EC + 156*(player*5 + h)` are
        # LEFT-SHIFTED over the played one. `/H=` is therefore an index into the
        # REMAINING hand, and a server list that never shrinks answers the wrong
        # card from the second play onwards.
        #
        # Measured live 2026-08-20T16:36Z, and reported off the screen before it
        # was found ("it wound up putting a copy of the previous card"):
        # member 6 held 42,47,58,59,87 and played /H=0, /H=1, /H=1 -- three
        # different cards to the client, and this server sent 42, 47, **47**.
        del hand[hand_idx]
        st = boardrules._MATCH_TURN.setdefault((chan, index),
                                    {"turn": turn, "active": actor, "n": n})
        if turn != (st.get("turn") or 0):
            # Not fatal -- the CLIENT owns the counter between turns (0xC76A2)
            # and it is the authority. Follow it, and log the disagreement so a
            # desync is visible instead of silent.
            common._say("tm: WARNING: member %s is on turn %d, we had %d -- following the "
                 "client (0xC76A2 increments it; @PutCard must echo its value "
                 "or the arm prints Errot!!Turn)"
                 % (member_id, turn, st.get("turn")))
        st["turn"], st["active"], st["n"] = turn, actor, n
        webwatch._live_matches_write()
        common._say("tm: VERIFIED: member %s (seat %d) plays hand slot %d -- card %s -- onto "
             "tile %d, turn %d. Answering (0x43, %d) @PutCard= and pushing the "
             "same board to %d other player(s). Watch for "
             "'---->Recv=PUTCARD' then 'GamePlayPutCard=%d'."
             % (member_id, actor, hand_idx, row.split(b"|")[0].decode(), tile,
                turn, protocol.PUTCARD_CMD, n - 1, tile))
        for mid, _v in seats:
            if pushqueue._push_key(mid) == pushqueue._push_key(member_id):
                continue
            if turns._is_bot(chan, index, mid):
                continue                # a departed seat reads nothing
            seat = next((i for i, (m, _x) in enumerate(seats)
                         if pushqueue._push_key(m) == pushqueue._push_key(mid)), 0)
            pushqueue._queue_push(mid,
                        protocol._turn_code(protocol.PUTCARD_CMD, turn)
                        + boardrules._putcard_body(seats, seat, actor, hand_idx, tile, row),
                        "the card played on tile %d" % tile,
                        after=common._env_int("POL_TM_PUTCARD_GAP", 0),
                        peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
        # ...AND TO THE WATCHERS, in the absolute frame (an observer was
        # never rotated -- @WatchInfo's /CN= occurrences are absolute).
        watchers._watch_push(chan, index,
                    protocol._turn_code(protocol.PUTCARD_CMD, turn)
                    + boardrules._putcard_body(seats, 0, actor, hand_idx, tile, row,
                                    n_solo=com_n),
                    "the card on tile %d" % tile)
        # The one-human roster for the loops below: battles and the next turn
        # go to the placer too, and `seats` is empty in a VS. COM game.
        _roster = seats or [(member_id, 0)]
        # VERIFIED: AND NOW THE BOARD. The placement goes through `_apply_placement`,
        # which is the server BEING THE RESOLVER -- the one job 0xCEE51 leaves
        # it: the client's own resolver is skipped outright when the network
        # flag is set, so a contested placement waits on (0x43, 12) for ever
        # unless this runs.
        #
        # WARNING: THE BATTLE GOES TO EVERY SEAT INCLUDING THE PLACER. Both clients
        # run the same battle scene off the same `@PutCard` -- measured, both
        # printed the whole `-Battle*` sequence and both parked -- so this is
        # not a watcher-only push.
        # WARNING: THE RESOLVER IS NOW RESUMABLE. A placement that can attack more than
        # one defender STOPS here and waits for the player's `@BattleSelect=/B=`
        # (0x43, 11) -- the multi-defender path that HUNG live 2026-09-03 because
        # the server auto-resolved every battle in its own order and never
        # handled command 11. `_advance_placement` resolves the 0- and 1-battle
        # cases immediately (byte-identical to the old inline path) and finishes
        # the turn; on a >1 case it parks in `_PENDING_BATTLE` and the
        # `@BattleSelect=` arm resumes it. The next-turn announcement therefore
        # rides `_advance_placement`'s own `_finish()`, NOT this reply.
        if common._env_int("POL_TM_BATTLE", 1):
            if placement._begin_placement(chan, index, n, actor, tile, row, turn, seats,
                                com_n, member_id, _roster):
                placement._advance_placement(chan, index)
        elif common._env_int("POL_TM_TURNDATA", 1):
            # POL_TM_BATTLE=0: no board resolution, but the turn still advances
            # exactly as before (the deal/turn machinery is independent).
            if com_n:
                vscom._queue_com_turns(chan, index, member_id, n, turn + 1,
                                 (actor + 1) % n)
            else:
                turns._queue_next_turn(chan, index, seats, turn + 1, (actor + 1) % n,
                                 roster=_roster, n_solo=com_n)
        return protocol._turn_code(protocol.PUTCARD_CMD, turn) + boardrules._putcard_body(
            seats, actor, actor, hand_idx, tile, row, n_solo=com_n)

    if b"@BattleSelect=" in cmd:
        # VERIFIED: THE MULTI-DEFENDER TARGET CHOICE. When a placed card can attack more
        # than one back-pointing defender, the client's picker (scene state 3,
        # 0xCE558) sends ONE `@BattleSelect=/B=<tile>` (0x43, cmd 11) naming the
        # ABSOLUTE tile it chose, then parks in state 8 polling (0x43, 12) for
        # the `@BattleData` we push -- it does NOT expect an echo of this
        # message. Combos are client-driven: after each @BattleData it
        # re-contests and, if >1 targets still remain, sends another
        # @BattleSelect (a single remaining target it auto-resolves silently).
        # So we resolve the named tile, push its @BattleData, and let
        # `_advance_placement` re-check for the next pause or the turn's end.
        # Unhandled, this was a ~75 s dead wait -> the live hang.
        if not common._env_int("POL_TM_BATTLE", 1):
            common._say("tm: member %s sent @BattleSelect= -- POL_TM_BATTLE=0, silent"
                 % (member_id,))
            return None
        _b = re.search(rb"/B=(-?\d+)", cmd or b"")
        chosen = int(_b.group(1)) if _b else None
        chan, index, seats = matchmaking._match_of(member_id)
        if seats and vscom._in_com_game(member_id):
            chan, index, seats = None, None, []
        if not seats and vscom._com_n(member_id):
            index = pushqueue._push_key(member_id)
        if (chan, index) not in placement._PENDING_BATTLE:
            common._say("tm: member %s sent @BattleSelect=/B=%s but no placement is "
                 "awaiting a battle choice at %s table %s -- ignoring (stale, "
                 "already resolved, or a lost roster)"
                 % (member_id, chosen, chan, index))
            return None
        common._say("tm: VERIFIED: member %s chose battle target tile %s (@BattleSelect=/B=) "
             "-- resolving it and any single follow-ups; another >1 choice will "
             "park again." % (member_id, chosen))
        placement._advance_placement(chan, index, chosen=chosen)
        return None

    if b"@ComGame=" in cmd:
        # VERIFIED: VS. COM, AND IT IS A REQUEST/REPLY ON ONE SLOT -- NOT "server
        # initiated", and not a UI gate either. Both readings in
        # the earlier in-game notes come from matching the wrong literal:
        # TM.dll carries TWO, `@ComGameInit` at rva 0x234AD8 (the parser at
        # 0x102D2A, which is what that reading found) and **`@ComGame=` at rva
        # 0x234D10**, whose only xref is a BUILDER --
        #
        #     0x106F52  0xA97C0(buf, "@ComGame=", 0x43, esi, cmd & 0xFF, 0)
        #
        # so the client SENDS `@ComGame=` on code 0x43 with the command id in the
        # msgid slot, then blocks in `0x101A10(2)` waiting for the answer to come
        # back in that same slot. Section 11e was right all along.
        #
        # Answer where it asked: same code, same msgid. `0x101A10` looks the
        # message up by the id it was called with, so a reply parked anywhere
        # else is a reply nothing drains -- this file's oldest lesson.
        if not common._env_int("POL_TM_COMGAME", 1):
            common._say("tm: member %s sent @ComGame= -- POL_TM_COMGAME=0, staying "
                 "silent (the client will raise Lobby.BIN 428)" % (member_id,))
            return None
        msgid = ((code or 0) >> 16) & 0xFF
        # KEY: `/Com=` IS THE ROSTER, AND THIS IS THE ONLY MESSAGE THAT CARRIES
        # IT. The COM character-select screen sends (msgid 6, live 13:57:26Z)
        #
        #     @ComGame=/Rule=100|0|1|1|1|0|0|0/Com=1|2
        #
        # one pipe entry per chosen COM opponent (builder 0x107239: [obj+0xE5]
        # count over the per-entry table at [obj+0x1CA]). The player count of
        # the match is 1 + len(/Com=); discarding it was the N=2 hardcode that
        # answered a 3-player game with `/Ans=1|1` and `/L=0|0`. The character
        # INDEXES are kept too -- the client owns the COM decks (they are
        # client-side data), but whoever ends up playing the COM's turns will
        # need to know which characters were picked.
        _com_m = re.search(rb"/Com=([0-9|]+)", cmd or b"")
        if _com_m and pushqueue._push_key(member_id) is not None:
            coms = [int(v) for v in _com_m.group(1).split(b"|") if v]
            key = pushqueue._push_key(member_id)
            entry = vscom._COM_GAME.setdefault(
                key, {"n": None, "coms": [], "peer": peer_nick})
            entry["coms"], entry["n"] = coms, 1 + len(coms)
            if peer_nick:
                entry["peer"] = peer_nick
                matchmaking._MATCH_PEER[key] = peer_nick
            common._say("tm:   ...VS. COM roster: /Com=%s -> %d player(s) (1 human + "
                 "%d COM)." % ("|".join(str(c) for c in coms), entry["n"],
                               len(coms)))
        # VERIFIED: ECHO THE PLAYER'S OWN RULES. MEASURED BOTH WAYS 2026-08-25, and
        # this is the live report "the VS. COM settings I pick are ignored".
        #
        # The two messages use the SAME field order, and it is not `@Tet=`'s:
        #
        #   `@ComGame=/Rule=`  builder 0x106F87..0x107223, eight values --
        #       [+0xDC] (clamped 0..0x7D0), +0xE0, +0xE1, +0xE2, +0xE3, +0xE4,
        #       +0xD9, +0xE9
        #   `@ComGameInit=/R=` parser  0x102DE3..0x102ECB, SEVEN occurrences --
        #       0x102E05 -> [esi+0xDC], then +0xE0, +0xE1, +0xE2, +0xE3, +0xE4,
        #       and 0x102ECB -> [esi+0xD9]. There is no eighth: `+0xE9` is sent
        #       by the client and never read back.
        #
        # So the first seven positions line up exactly and the correct answer
        # is the client's own list, verbatim. What we were sending instead was
        # `_rule_seven`, which is the **`@Tet=`** order (bm, du, st, cb, ca,
        # gs, tl) -- a different mapping onto different slots. Live proof of
        # the damage, member 3 2026-08-25T04:09:06Z:
        #
        #     client  /Rule=135|1|1|1|3|0|0|0   -> DC=135 E0=1 E1=1 E2=1 E3=3
        #     we sent /R=135|0|1|1|1|1|3        -> DC=135 E0=0 E1=1 E2=1 E3=1
        #
        # every rule slot overwritten with a default the player did not pick.
        #
        # `_rule_seven` is untouched and still the fallback here: it also feeds
        # `@VsGameInit`, whose `/R=` lands on a DIFFERENT slot set
        # ([esi+0xE0..E4, E9, D9], no +0xDC), so this correction must not be
        # generalised to it without reading that parser too.
        # `POL_TM_COM_ECHO_RULES=0` restores the old defaults-only reply.
        rules = list(ruleset._rule_seven(None))
        _rl_all = re.search(rb"/Rule=(-?\d+(?:\|-?\d+)*)", cmd or b"")
        if _rl_all and common._env_int("POL_TM_COM_ECHO_RULES", 1):
            _picked = [int(v) for v in _rl_all.group(1).split(b"|")]
            if len(_picked) >= 7:
                rules = _picked[:7]
                common._say("tm:   ...VS. COM rules: echoing the player's own "
                     "/Rule= (%s) -- the first seven positions of /Rule= and "
                     "/R= are the same slots (0x106F87 builder, 0x102DE3 "
                     "parser)" % ",".join(str(v) for v in rules))
            else:
                common._say("tm: WARNING: member %s sent /Rule= with only %d value(s) -- "
                     "expected >= 7 (DC,E0..E4,D9). Serving defaults; capture "
                     "this body, the builder emits eight."
                     % (member_id, len(_picked)))
        # THE WAGER (the wager's money half, measured static 2026-08-22). /Rule=
        # occ 0 is the stake the player chose on the COM select screen, and
        # our /R= occ 0 lands in [obj+0xDC] = 0x52464BC -- THE POT: the match
        # build (0xEC1CA) does `wallet -= pot` (floored 0), the win path
        # doubles it (0x10BC54, capped 0x7D0 = 2000; 0x10CF82 clears the
        # double when the wallet cannot cover it) and the mode-1 settlement
        # arms (0xD88E5 / 0xD8B1D / 0x10F947...) do `wallet += pot; pot = 0`.
        # 0x10C67B is the "%d,%03d" prize print. Serving /R=0 is why every
        # win paid 0. The server mirrors the client's own arithmetic below
        # (stake in here, settle at the result, refund on @GameExit) so the
        # save sync agrees with the local wallet. POL_TM_WAGER=0 restores
        # /R=0 and no server-side movement. WARNING: Per-outcome client arithmetic
        # beyond the win-double is INFERRED -- the check is the client's own
        # wallet display matching `money_of` after each outcome.
        wager = 0
        if not pots._com_wager_enabled():
            common._say("tm:   ...VS. COM stake NOT taken: `/Rule=` occ 0 is not "
                 "measured to be the wager (see `_com_wager_enabled`) -- "
                 "POL_TM_WAGER_COM=1 to charge it anyway.")
        if pots._com_wager_enabled() and pushqueue._push_key(member_id) is not None:
            _rl = re.search(rb"/Rule=(-?\d+)", cmd or b"")
            cap = common._env_int("POL_TM_WAGER_CAP", 2000)
            wager = max(0, min(int(_rl.group(1)) if _rl else 0, cap))
            wager = min(wager, purse.money_of(member_id))   # 0x10CF82's own rule
            rules[0] = wager
        # ...AND KEEP THE PLAYER'S RULES FOR THE BOARD DEAL (`_com_rules_of`):
        # the echo only reaches the client's screen, and the deal is ours.
        # Only an echoed /Rule= is in COM order; the `_rule_seven` fallback
        # is @Tet order and would land on the wrong names.
        if _rl_all and common._env_int("POL_TM_COM_ECHO_RULES", 1)                 and pushqueue._push_key(member_id) is not None:
            _vals = [int(v) for v in _rl_all.group(1).split(b"|")]
            if len(_vals) >= len(ruleset.COM_RULE_KEYS):
                vscom._COM_GAME.setdefault(
                    pushqueue._push_key(member_id),
                    {"n": None, "coms": [], "peer": peer_nick})["rules"] =                     dict(zip((k.decode() for k in ruleset.COM_RULE_KEYS), _vals))
        common._say("tm: member %s @ComGame= (0x43, msgid %d) -- answering IN THAT SLOT "
             "with @ComGameInit=, /R=%s. Look for '---->Recv=COMGAMEINIT' in the "
             "client's own trace." % (member_id, msgid,
                                      ",".join(str(v) for v in rules)))
        _reply = (protocol.encode_code(protocol.IN_MATCH_CODE | (msgid << 16))
                  + vscom._comgame_body(member_id, rules))
        if wager > 0:
            # AFTER the body: /M= must carry the pre-stake balance -- the
            # client subtracts the pot itself at match build (0xEC1CA).
            _entry = vscom._COM_GAME.setdefault(
                pushqueue._push_key(member_id), {"n": None, "coms": [],
                                       "peer": peer_nick})
            _entry["staked"] = wager
            purse._set_money(member_id, purse.money_of(member_id) - wager,
                       "VS. COM stake in (/R=%d; the client subtracts the "
                       "same pot at 0xEC1CA)" % wager)
            pots._stake_persist(member_id, wager)
            # WARNING: THE POT STILL DOES NOT REACH THE CLIENT -- OPEN (#7). The client
            # reads the pot from [obj+0xDC], written only by the @ComGameInit
            # parser from the (0x43,2) board it consumes. A re-push of the
            # wager-bearing board into (0x43,2) was tried 2026-09-02 and did NOT
            # work: the client read @ComGameInit a second time but the win screen
            # still printed prize 0, so the pot was already latched (as 0) before
            # that read. The client does not re-poll (0x43,2) after committing the
            # wager. Cracking this needs a live memory probe on [obj+0xDC]
            # (0x52464BC) to see exactly when it is zeroed -- not another blind
            # push. Server settlement is correct regardless (the win pays out);
            # only the client's on-screen prize/pot is wrong.
        return _reply

    if b"@Rule=" in cmd:
        # THE TABLE-RULES REQUEST. Answer with the same 0x13 body the push
        # sends. The table is identified by the message's own u64 (0x86ED0 reads
        # it from the envelope, not from a field), so the answer goes back on the
        # connection it arrived on and needs no id of ours.
        if not common._env_int("POL_TM_TABLE_INFO", 1):
            return None
        idx = None
        try:
            import tmroom
            chan = tmroom.room_of(member_id) if member_id is not None else None
            if chan:
                idx, why = peers.table_index_for_peer(peer_nick, tmroom.room_peer(chan), chan=chan)
                common._say("tm: member %s @Rule= -- table %s [%s]"
                     % (member_id, idx if idx else "UNRESOLVED", why))
                fields = tablesettings._table_rules_get(chan, idx)
                if fields:
                    return protocol.encode_code(protocol.TABLE_INFO_CODE) + matchmaking.table_info_body(fields)
        except Exception as exc:
            common._say("tm:   @Rule= lookup failed (%r)" % (exc,))
        common._say("tm:   ...no stored settings for that table -- staying silent "
             "rather than inventing a ruleset the owner never chose")
        return None

    if b"@MuchMakeAns=" in cmd:
        # THE CLIENT ACCEPTING A MATCH. Nothing to answer (yet) -- what matters
        # is that it ARRIVED, because that is the proof the push landed and was
        # understood. Log it loudly; it is the measurement this whole path needs.
        m = re.search(rb"/Ans=(-?\d+)", cmd)
        common._say("tm: member %s ANSWERED THE MATCH PUSH -- @MuchMakeAns=/Ans=%s"
             % (member_id, m.group(1).decode() if m else "?"))
        _tn = re.search(rb"/TblNo=(\d+)", cmd)
        _tn = int(_tn.group(1)) if _tn else None
        _ans = int(m.group(1)) if m else 0
        if member_id in tournament._EVENT_MATCH:
            return tournament._event_match_answer(member_id, _ans, _tn)
        if tournament._event_test_member(member_id):
            return tournament.event_stale_answer(member_id, _ans, _tn)
        return None

    if b"@CheckJoinTable=" in cmd:
        # THE SECOND PLAYER TRYING TO SIT DOWN. Unanswered this is a dead wait,
        # which is the whole of "nothing else can be done". See MSG_CHECKJOIN.
        if os.environ.get("POL_TM_CHECKJOIN", "1") != "1":
            return None
        join = common._env_int("POL_TM_JOIN", 1) & 1
        mem = common._env_int("POL_TM_JOIN_MEM", 1) & 1
        common._say("tm: member %s @CheckJoinTable= (peer %s) -- answering /Join=%d"
             "/Mem=%d; the client packs these as Join | (Mem << 1)"
             % (member_id, peer_nick or b"-", join, mem))
        return (protocol.encode_code(protocol.MSG_CHECKJOINANS)
                + (b"@CheckJoinTable_Ans=/Join=%d/Mem=%d" % (join, mem)))

    if b"@Tab=" in cmd or b"@Tet=" in cmd:
        # THE TABLE SETTINGS, AND THE SERVER HAS NEVER LISTENED. The wire
        # reading found this on 2026-08-18 and it is still true: every one of these
        # met "no answer for this command -- captured, silent", which is the whole
        # of "the Save option does nothing" and half of "the restrictions are not
        # the defaults".
        #
        # Two carriers, both measured:
        #   code 0x24  `@Save=/Tbl=<n>/Game=<n>` + the pair -- the SAVE button,
        #              and the ONLY message that names a table.
        #   code 0x14  the bare pair -- sent on Confirm. NAMES NO TABLE, which is
        #              exactly why this is capture-and-log before it is anything
        #              else: `/Tbl=` is the one field that would let us publish
        #              the reservation, and the common path does not carry it.
        #
        # WARNING: NOTHING IS ANSWERED HERE ON PURPOSE. Whether `@Save=` has a reply
        # the client waits for is UNMEASURED, and inventing one is the habit this
        # subsystem has been burned by ten times. What this does is store the set
        # and say so, so the next reservation is evidence instead of a shrug.
        tablesettings._note_table_settings(cmd, member_id, code, peer_nick)
        # ...and if it is the PRESET write, store it. This is the half of the
        # settings work that was still open: `@Save=` is "save my custom default table
        # values", the client re-reads them from the SAVE at +0xB8..+0xF0 on the
        # next login (`0x14D9F0`), and we had never written a byte there.
        if (code or 0) & 0xFF == protocol.TABLE_SETTINGS_PRESET:
            tablesettings._persist_table_preset(cmd, member_id)
        # WARNING: `POL_TM_SAVE_ANS` -- THE SWEEP KNOB FOR THE UNMEASURED QUESTION ABOVE.
        #
        # DEFAULT 0, i.e. the silence this arm has always kept. Nothing about the
        # shipped behaviour changes; do not read this block as a decision that
        # `@Save=` has a reply. It does not invent one, it makes measuring
        # whether it needs one cost ONE ENV VAR instead of a code change and a
        # redeploy -- and this file already records what that costs: the VS. COM
        # session that "spun for 116 retries" was read only after exactly that
        # round trip (see the no-handler note in responders.py).
        #
        # 2026-08-26 completeness sweep: `@Tet=`/`@Save=` are the ONLY commands
        # in the whole measured window (`logs/authserv.log`, 2026-08-17..08-19)
        # that were unanswered then and are STILL unanswered now -- `@Get=`,
        # `@GameEN=`, `@Sell=`, `@GameQT=` have all since grown handlers, and
        # `@Pong=`'s silence is deliberate and correct (it is the push carrier).
        # That makes this the highest-value single measurement left outside the
        # match itself.
        #
        # THE PRECEDENT IS DIRECTLY BELOW: `@Ready=` sat unanswered too, hung the
        # win screen on the first COM game ever completed, and was released by a
        # well-formed echo in the same slot. If `@Save=` behaves the same way,
        # this knob is the one-line confirmation; if it does not, the silence was
        # right and that is worth knowing just as much.
        if common._env_int("POL_TM_SAVE_ANS", 0):
            common._say("tm: member %s @Tab=/@Tet=/@Save= (code 0x%02x) -- "
                 "POL_TM_SAVE_ANS=1, echoing the message back in the same slot. "
                 "WARNING: THIS SHAPE IS UNMEASURED. Watch for the Save/Confirm dialog "
                 "closing (success) or the client sitting on it anyway (the echo "
                 "is not what it wants)." % (member_id, (code or 0) & 0xFF))
            return body
        common._say("tm: member %s @Tab=/@Tet=/@Save= (code 0x%02x) -- stored, "
             "answering NOTHING (the shipped default). Set POL_TM_SAVE_ANS=1 to "
             "echo it back and find out whether this client is waiting on a "
             "reply; that question is still UNMEASURED."
             % (member_id, (code or 0) & 0xFF))
        return None

    if b"@Ready=" in cmd and b"@GameReady=" not in cmd \
            and (code or 0) & 0xFF == protocol.IN_MATCH_CODE:
        # WARNING: the guards matter: `@GameReady=` CONTAINS `@Ready=` (its own
        # handler sits earlier, but substring order must not be load-bearing
        # twice), and the READY arm exists only in the 0x43 table.
        # THE POST-RESULT HANDSHAKE -- THE WIN-SCREEN HANG. Measured live
        # 2026-08-22T14:51:56Z, the first COM game ever played to completion:
        # @ResultData was read, the win screen drew, and the client sent
        #
        #     code=67 msgid=0x0e sh=10  @Ready=/Ans=0
        #
        # and hung, because nothing here answered it. The receive arm
        # (0x1045E7, Recv=READY, command 14) is the simplest in the family:
        # name match, the session-pair gate, parse `/Go=`, result = 1
        # unconditionally (0x105174) -- so a well-formed echo with /Go=
        # present releases the scene. /Go='s consumer is not yet read;
        # 1 is the natural "go" and POL_TM_READY_GO sweeps it.
        if not common._env_int("POL_TM_READY", 1):
            common._say("tm: member %s sent @Ready= -- POL_TM_READY=0, staying "
                 "silent (the result screen will hang)" % (member_id,))
            return None
        go = common._env_int("POL_TM_READY_GO", 1)
        webwatch._live_matches_write()
        common._say("tm: member %s @Ready= (0x43, 14) -- the post-result handshake; "
             "answering /Go=%d in the same slot. Watch for '---->Recv=READY'."
             % (member_id, go))
        # THE PANEL EXISTS FROM HERE. Mark it, and if the other seat(s) have
        # already voted, hand this one the live status one reply behind this
        # answer -- see `_CONTINUE_READY`.
        _rc, _ri, _rs = matchmaking._match_of(member_id)
        if _rs and tournament._in_event_match(member_id):
            # A TOURNAMENT GAME ENDS WITHOUT A TAKE OR A QUESTION. The event
            # board goes GameDrawOver (0xBE707): the play-again panel in its
            # event form shows a loading bar that stops at 80% and polls
            # (0x43, 17) @Continue every frame with no timeout, moving on only
            # for shop byte >= 2 and its own /Ans= slot == 3 (0x10D98F; PS2
            # 0x348B04). No client @Continue= ever comes, so the server sends
            # the CONTINUE_FINAL form itself, from the table peer, one reply
            # after this; the client then leaves on its own (@GameExit= ->
            # @GameEA=/Exit=1, EnterRoom2, back to the tournament room).
            # Live 2026-09-26: both boards
            # sat at 80% after @Ready=/Go=1.
            _n = max(2, len(_rs))
            _cont = (protocol._turn_code(protocol.CONTINUE_CMD, 2)
                     + b"@Continue=/Ans=" + b"|".join([b"%d" % rematch.CONTINUE_FINAL] * _n)
                     + b"/B=" + b"|".join([b"0"] * _n))
            pushqueue._queue_push(member_id, _cont, "event game over -> back to the room",
                        after=common._env_int("POL_TM_EVENT_CONTINUE_GAP", 1),
                        peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(member_id)))
            common._say("tm:   ...member %s is in a TOURNAMENT game: no take, no panel "
                 "question; queued %s" % (member_id, _cont[8:].decode()))
            # NO /Go= ANSWER IN A TOURNAMENT GAME. The event board never reads
            # it (2026-10-03 narration log: filed after both games, looked up
            # 0 times; the board left on the @Continue alone). Unread, it sat
            # in the client's one receive store, and with it there the NEXT
            # seating never sent @CheckJoinTable: "Could not start game" after
            # every finished game, while a fresh client always started.
            return None
        if _rs and not vscom._in_com_game(member_id):
            # WARNING: A DECISIVE GAME SENDS @Ready= TWICE (measured 2026-09-07
            # 14:45Z): once off the result screen, once when the take scene
            # ends. Only the second one opens the panel. And the LOSER's second
            # comes SIX SECONDS after the take list, before the winner has
            # picked -- its scene ends on our /Go= answer and the winner sees
            # "Player has left the game" (41e). `POL_TM_READY_HOLD_LOSER=1`
            # HOLDS that answer until the winner's @GetSelect lands; the
            # release rides one reply behind the @GetSelect push, so the
            # loser's scene shows the taken card first.
            _st_r = boardrules._MATCH_TURN.get((_rc, _ri)) or {}
            _take_r = _st_r.get("take") or {}
            _k_r = pushqueue._push_key(member_id)
            _rn = _st_r.setdefault("ready_n", {}) if _st_r else {}
            _rn[_k_r] = int(_rn.get(_k_r) or 0) + 1
            _decisive = bool(_take_r)
            _pools_r = _take_r.get("pools") or []
            _losers_r = [s2 for s2 in range(len(_pools_r))
                         if s2 != _take_r.get("win") and _pools_r[s2]]
            _pending_r = (_decisive and
                          len(_take_r.get("picked") or []) < len(_losers_r))
            _tros_r = _take_r.get("roster") or []
            _my_r = next((i for i, m in enumerate(_tros_r)
                          if pushqueue._push_key(m) == _k_r), None)
            _is_winner = _decisive and _my_r == _take_r.get("win")
            _hold_mode = common._env_int("POL_TM_READY_HOLD_LOSER", 0)
            if _pending_r and not _is_winner and _rn[_k_r] >= 2 and _hold_mode:
                _take_r.setdefault("held_ready", {})[_k_r] = body[:8]
                if _hold_mode == 2:
                    # MODE 2 (measured 14:59Z that mode 1's held reply changes
                    # nothing: the scene ends before any reply): answer NOW
                    # with /Go=0 and push /Go=1 once the winner has picked --
                    # does the scene wait on the VALUE?
                    common._say("tm:   ...member %s's 2nd @Ready= (the take scene): "
                         "answering /Go=0 now, /Go=1 follows the winner's pick "
                         "(POL_TM_READY_HOLD_LOSER=2)" % (member_id,))
                    return body[:8] + b"@Ready=/Go=0"
                common._say("tm:   ...member %s's 2nd @Ready= (the take scene) is HELD "
                     "until the winner picks (POL_TM_READY_HOLD_LOSER=1)"
                     % (member_id,))
                return None
            if _decisive and _rn[_k_r] < 2:
                common._say("tm:   ...member %s's 1st @Ready= of a decisive game -- the "
                     "take scene is next, not the panel; not marking the "
                     "panel open" % (member_id,))
                return body[:8] + (b"@Ready=/Go=%d" % go)
            rematch._CONTINUE_READY.setdefault((_rc, _ri), set()).add(_k_r)
            _tl = rematch._MATCH_CONTINUE.get((_rc, _ri)) or {}
            _vt = _tl.get("votes") or {}
            _seats_r = _tl.get("seats") or _rs
            _ans_r = [_vt.get(pushqueue._push_key(m), rematch.CONTINUE_UNANSWERED)
                      for m, _v in _seats_r]
            _me_r = next((i for i, (m, _v) in enumerate(_seats_r)
                          if pushqueue._push_key(m) == pushqueue._push_key(member_id)), None)
            if (_me_r is not None and _ans_r[_me_r] == rematch.CONTINUE_UNANSWERED
                    and any(a != rematch.CONTINUE_UNANSWERED for a in _ans_r)
                    and common._env_int("POL_TM_CONTINUE_PARTIAL", 0)
                    and common._env_int("POL_TM_CONTINUE_READY_GATE", 1)):
                pushqueue._queue_push(member_id,
                            protocol._turn_code(protocol.CONTINUE_CMD,
                                       common._env_int("POL_TM_CONTINUE_SH", 2))
                            + rematch._continue_body(rematch._continue_frame(list(_ans_r), _me_r),
                                             common._env_int("POL_TM_CONTINUE_B", 0)),
                            "the @Continue live status",
                            after=common._env_int("POL_TM_CONTINUE_READY_GAP", 1),
                            peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(member_id)))
                common._say("tm:   ...the panel opens with a vote already in (%s): "
                     "pushing the live status one reply behind the @Ready "
                     "answer" % "|".join(str(v) for v in _ans_r))
        return body[:8] + (b"@Ready=/Go=%d" % go)

    if b"@GetSelect=" in cmd:
        # THE WINNER TAKES A CARD. Sender 0x107C6F: `@GetSelect=/H=<slot>`,
        # sh = the sender's own seat -- the winner naming which of the loser's
        # cards it takes. The receive arm (0x104E5B, Recv=GETSELECT, command
        # 15) parses `/E=` and `/H=` and stores /H='s byte into
        # [0x52463C4]+0x1B2 -- the same hand-slot global @PutData reads -- so
        # the answer is the confirmation the scene acts on. /E='s consumer is
        # unread; 0 as the least-committal value, POL_TM_GETSEL_E sweeps it.
        if not common._env_int("POL_TM_GETSELECT", 1):
            return None
        _h = re.search(rb"/H=(-?\d+)", cmd or b"")
        hsel = int(_h.group(1)) if _h else 0
        ev = common._env_int("POL_TM_GETSEL_E", 0)
        webwatch._live_matches_write()
        chan, index, seats = matchmaking._match_of(member_id)
        common._say("tm: member %s @GetSelect= (0x43, 15) picked hand slot %d -- "
             "echoing /E=%d/H=%d" % (member_id, hsel, ev, hsel))
        # The transfer half of the take: the pick is real now. The take state was
        # stashed by the result block (win/lose seats, every pool by slot,
        # the seat->member roster); the picked card joins the winner's
        # COLLECTION and -- when the losing seat is a REAL member (PvP) --
        # leaves the loser's. ONE PICK PER LOSING SEAT (reported live 2026-08-22,
        # first 3-player win: "it lets me take one card from each com" --
        # two @GetSelects, nothing on the wire naming the pool), consumed in
        # ascending seat order: the take screen walks the losers one panel
        # at a time, so pick k indexes losers[k]'s pool. In a COM game the
        # losing seat has no member behind it and nothing is removed.
        # POL_TM_TAKE_WIN=0 restores the display-only behaviour;
        # POL_TM_TAKE_PVP=0 keeps PvP out of the take flow entirely.
        _tkey = None
        if common._env_int("POL_TM_TAKE_WIN", 1):
            if not seats and vscom._com_n(member_id):
                _tkey = (chan, pushqueue._push_key(member_id))
            elif seats:
                _tkey = (chan, index)
        if _tkey is not None:
            _tst = boardrules._MATCH_TURN.get(_tkey) or {}
            _take = _tst.get("take")
            _tros = (_take or {}).get("roster") or []
            _my_seat = next((i for i, m in enumerate(_tros)
                             if pushqueue._push_key(m) == pushqueue._push_key(member_id)), 0)
            if _take and _take.get("win") == _my_seat:
                _pools = _take.get("pools") or []
                _losers = matchend._losers_from(_my_seat, _pools)
                _picked = _take.setdefault("picked", [])
                if len(_picked) < len(_losers):
                    _seatL = _losers[len(_picked)]
                    _pool = _pools[_seatL]
                    if 0 <= hsel < len(_pool):
                        _row = _pool[hsel]
                        _card = [int(v) for v in _row.split(b"|")]
                        _picked.append(_card[0])
                        collection._collection_add_cards(member_id, [_card])
                        _lmid = (_tros[_seatL]
                                 if _seatL < len(_tros) else None)
                        if _lmid is not None \
                                and pushqueue._push_key(_lmid) != pushqueue._push_key(member_id) \
                                and common._env_int("POL_TM_TAKE_LOSS", 1):
                            collection._collection_remove_card(_lmid, _card[0],
                                                    row=_row)
                        common._say("tm: VERIFIED: member %s TAKES card %d (slot %d of "
                             "seat %d's pool%s; pick %d of %d)"
                             % (member_id, _card[0], hsel, _seatL,
                                (" = member %s" % _lmid)
                                if _lmid is not None else " (COM)",
                                len(_picked), len(_losers)))
                    else:
                        common._say("tm: WARNING: member %s @GetSelect=/H=%d is outside "
                             "seat %d's %d-card pool -- nothing transferred"
                             % (member_id, hsel, _seatL, len(_pool)))
                else:
                    common._say("tm:   ...all %d picks already committed for this "
                         "match%s -- echo only"
                         % (len(_losers),
                            " (a PERFECT: the whole pool moved at the result)"
                            if _take.get("perfect") else ""))
            elif _take:
                common._say("tm:   ...the winner is seat %d, sender is seat %d -- "
                     "echo only" % (_take.get("win"), _my_seat))
        # In PvP the other seats watch the take on the same slot; a COM game
        # has nobody else to tell. A PERFECT take has no pick to watch: the
        # loser's client does not poll (0x43, 15), so a relayed echo would
        # only sit in its queue -- the stray @GetSelect is answered, not told.
        _perf_gs = False
        if seats:
            _perf_gs = bool(((boardrules._MATCH_TURN.get((chan, index)) or {})
                             .get("take") or {}).get("perfect"))
        for mid, _v in ([] if _perf_gs else seats):
            if pushqueue._push_key(mid) == pushqueue._push_key(member_id):
                continue
            pushqueue._queue_push(mid, body[:8] + (b"@GetSelect=/E=%d/H=%d" % (ev, hsel)),
                        "the winner's take",
                        after=common._env_int("POL_TM_GETSEL_GAP", 1),
                        peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
        # RELEASE the loser(s) whose take-scene @Ready= was held (see the
        # @Ready= arm): one reply behind the @GetSelect push, so their scene
        # renders the pick and then ends; their panel opens after that.
        if seats:
            _tk2 = (chan, index)
            _take2 = (boardrules._MATCH_TURN.get(_tk2) or {}).get("take") or {}
            _held = _take2.get("held_ready") or {}
            _pools2 = _take2.get("pools") or []
            _losers2 = [s2 for s2 in range(len(_pools2))
                        if s2 != _take2.get("win") and _pools2[s2]]
            _done2 = len(_take2.get("picked") or []) >= len(_losers2)
            if _held and (_done2 or not common._env_int("POL_TM_TAKE_WIN", 1)):
                for _hk, _hdr in list(_held.items()):
                    _hm = next((m for m, _v in seats if pushqueue._push_key(m) == _hk),
                               None)
                    if _hm is None:
                        continue
                    pushqueue._queue_push(_hm, _hdr + (b"@Ready=/Go=%d"
                                             % common._env_int("POL_TM_READY_GO", 1)),
                                "the held @Ready (the take is done)",
                                after=common._env_int("POL_TM_GETSEL_GAP", 1) + 1,
                                peer=matchmaking._MATCH_PEER.get(_hk))
                    rematch._CONTINUE_READY.setdefault(_tk2, set()).add(_hk)
                    common._say("tm:   ...releasing member %s's held @Ready= one reply "
                         "behind the @GetSelect push; its panel opens next"
                         % (_hm,))
                _take2["held_ready"] = {}
        return body[:8] + (b"@GetSelect=/E=%d/H=%d" % (ev, hsel))

    if b"@Continue=" in cmd:
        # WARNING: THE POST-GAME "PLAY AGAIN?" PANEL -- A QUORUM VOTE, AND THE ONE
        # MESSAGE IN THIS FAMILY WHOSE REPLY CAN BE PARSED AND STILL NOT COUNT.
        #
        # Read statically off arm 0x104EE1 on 2026-08-21.
        # NOTHING HERE HAS BEEN ON A WIRE. The client says in one line whether
        # it landed: `---->Recv=CONTINUE` (0x104F1A) plus `probe_lookup`'s
        # `(0x43, 17, n, found)`.
        #
        # What the client sends (builder 0x107CEC, through 0x106E30's table):
        #     /Ans=  its own answer      1 = play again, 2 = decline
        #     /Conf= 0, or 1 for the "...but change the settings first" button
        #            (that path then waits on command 4, `@ComList`) -- INFERRED
        #     /PR=   a persisted option, inferred to be the prize-ranking name
        #            display (0xCBB22/0xCBC05 write it beside the save byte
        #            0x294C86). We log it; we do not persist it yet.
        #     sh   = the sender's own seat, which is ALWAYS 0 in its own frame.
        #
        # WARNING: AND THAT LAST LINE IS THE TRAP. `0x104FAC` is the ONLY site in the
        # arm that sets 0x101A10's return value, and it fires only when the
        # reply's third envelope byte is non-zero:
        #
        #     if ([obj+0xEB] != 0) [esp+0x24] = 1
        #
        # So a reply with sh = 0 is parsed, applied, logged as
        # `---->Recv=CONTINUE`, and THEN reported as "no answer" -- the message
        # is gone from the store and the panel waits for ever. Answering this
        # the way `@Quit=` does, by echoing `body[:8]`, would echo the client's
        # own sh = 0 and swallow every reply. `_turn_code` puts `sh` in the
        # third byte; the assertion below refuses to send a zero.
        #
        # The contract, from the four waiters (0x10CA50 / 0x10D66A / 0x10D996 /
        # 0x10E07E):
        #     sh = 1     "the rematch is ON", and nothing else may use it --
        #                state 6 checks only the recipient's own slot before it
        #                re-deals (0x4000054 -> 0xC243E).
        #     sh >= 2    a status update: own slot 3 => leave (0x4000053), own
        #                slot 0/1 => redraw and keep waiting, own slot 2 =>
        #                ignored.
        # WARNING: A CLIENT THAT ANSWERED 2 LEAVES THE PANEL ONLY BY HAVING ITS OWN
        # SLOT RAISED TO 3 WITH sh >= 2. State 7 tests nothing else.
        if not common._env_int("POL_TM_CONTINUE", 1):
            common._say("tm: member %s sent @Continue= -- POL_TM_CONTINUE=0, staying "
                 "silent (the panel will sit on 'Thinking...')" % (member_id,))
            return None
        _a = re.search(rb"/Ans=(-?\d+)", cmd or b"")
        _c = re.search(rb"/Conf=(-?\d+)", cmd or b"")
        _p = re.search(rb"/PR=(-?\d+)", cmd or b"")
        ans = int(_a.group(1)) if _a else rematch.CONTINUE_UNANSWERED
        webwatch._live_matches_write()
        chan, index, seats = matchmaking._match_of(member_id)
        common._say("tm: member %s @Continue= /Ans=%d%s%s -- the post-game "
             "'Play again?' panel"
             % (member_id, ans,
                "" if not _c else " /Conf=%s" % _c.group(1).decode(),
                "" if not _p else " /PR=%s" % _p.group(1).decode()))
        if not seats:
            com_n2 = vscom._com_n(member_id)
            if com_n2:
                # THE EIGHTH SEAT GATE. A VS. COM game has no roster and this
                # branch stayed silent -- the "Play again?" panel would think
                # for ever. The COM seats' answers are OURS: the COM always
                # agrees, so the human's answer IS the vote. /Ans=1 -> sh=1,
                # everyone 1, rematch ON (the client re-picks cards; the deal
                # block re-deals the COM hands and pops the board). /Ans=2 ->
                # sh=2 with the human's own slot 3, which is the only test
                # state 7 makes before tearing the panel down.
                bval2 = common._env_int("POL_TM_CONTINUE_B", 0)
                ckey = (None, pushqueue._push_key(member_id))
                if ans == rematch.CONTINUE_AGAIN:
                    sh2 = 1
                    vals2 = [rematch.CONTINUE_AGAIN] * com_n2
                    rematch._reset_for_rematch(*ckey)
                    common._say("tm:   ...VS. COM: the COM always plays again -- "
                         "sh=1, rematch ON, per-match state reset")
                    # WARNING: AND /Conf=1 IS THE "CHANGE THE SETTINGS FIRST"
                    # BUTTON. That path waits on (0x43, 4) `@ComList`, which
                    # this server had never sent -- so the screen froze and the
                    # client went SILENT, because a slot poll is a local lookup
                    # and puts nothing on the wire. Measured three times live
                    # 2026-09-07 (Conf=1 froze, Conf=0 did not).
                    _conf = int(_c.group(1)) if _c else 0
                    if _conf == 1 and common._env_int("POL_TM_COMLIST", 1):
                        _cl = (protocol._turn_code(protocol.COMLIST_CMD,
                                          common._env_int("POL_TM_COMLIST_SH", 0))
                               + vscom._comlist_body(vscom._comlist_values()))
                        pushqueue._queue_push(member_id, _cl,
                                    "the COM opponent list (@ComList)",
                                    after=common._env_int("POL_TM_COMLIST_GAP", 1),
                                    peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(member_id)))
                        common._say("tm:   ...and /Conf=1 is CHANGE SETTINGS -- "
                             "queueing (0x43, %d) @ComList=/L=%d. Watch for "
                             "'---->Recv=COMLIST'. POL_TM_COMLIST=0 disables, "
                             "POL_TM_COMLIST_VALUES sweeps the row values "
                             "(their meaning is UNMEASURED)."
                             % (protocol.COMLIST_CMD, len(vscom._comlist_values())))
                else:
                    sh2 = common._env_int("POL_TM_CONTINUE_SH", 2)
                    vals2 = [rematch.CONTINUE_FINAL] + [rematch.CONTINUE_DECLINE] * (com_n2 - 1)
                    common._say("tm:   ...VS. COM: human declined -- own slot %d, "
                         "sh=%d tears the panel down"
                         % (rematch.CONTINUE_FINAL, sh2))
                return (protocol._turn_code(protocol.CONTINUE_CMD, sh2)
                        + rematch._continue_body(rematch._continue_frame(vals2, 0), bval2))
            # THE LIVE ROSTER IS GONE -- but a PENDING VOTE may still know it.
            # Measured live 2026-08-22T19:43Z: member 11 voted, a deploy
            # restart wiped _MATCH_ROSTER (and the other vote) 13s before
            # member 6's, and both clients parked on "Now loading" behind a
            # reply that never came. The vote tally now snapshots its seats,
            # so an expiry/roster race inside one process resumes here...
            for _k2, _t2 in list(rematch._MATCH_CONTINUE.items()):
                _snap = _t2.get("seats") if isinstance(_t2, dict) else None
                if _snap and any(pushqueue._push_key(m) == pushqueue._push_key(member_id)
                                 for m, _v in _snap):
                    chan, index = _k2
                    seats = list(_snap)
                    common._say("tm:   ...live roster gone, but a pending vote at "
                         "%r remembers this member -- resuming that quorum"
                         % (_k2,))
                    break
        if not seats:
            # ...and a vote NOTHING remembers (the restart case: both the
            # roster and the other player's vote died) gets the ORPHAN
            # teardown instead of a hang: sh=2 with a FULL-WIDTH all-3 list.
            # Slot 3 under sh>=2 is the one test state 7 makes, the rotation
            # is identity on a uniform list, and a LONGER-than-N list is safe
            # (the arm reads exactly its own N occurrences; the measured hazard was
            # a SHORT list). POL_TM_CONTINUE_ORPHAN=0 restores the silence.
            if not common._env_int("POL_TM_CONTINUE_ORPHAN", 1):
                common._say("tm:   ...no roster, no pending vote -- staying silent "
                     "(POL_TM_CONTINUE_ORPHAN=0)")
                return None
            common._say("tm:   ...no roster and no pending vote (a restart raced "
                 "the panel) -- answering the orphan teardown: sh=%d, "
                 "/Ans=3|3|3|3" % common._env_int("POL_TM_CONTINUE_SH", 2))
            return (protocol._turn_code(protocol.CONTINUE_CMD, common._env_int("POL_TM_CONTINUE_SH", 2))
                    + rematch._continue_body([rematch.CONTINUE_FINAL] * 4,
                                     common._env_int("POL_TM_CONTINUE_B", 0)))
        n = len(seats)
        me = next((i for i, (m, _v) in enumerate(seats)
                   if pushqueue._push_key(m) == pushqueue._push_key(member_id)), 0)
        key = pushqueue._push_key(member_id)
        tally = rematch._MATCH_CONTINUE.setdefault((chan, index), {})
        # The seats snapshot is what the resume path above searches; votes
        # moved under their own key so the snapshot cannot collide with a
        # member key.
        tally.setdefault("seats", [(m, v) for m, v in seats])
        votes = tally.setdefault("votes", {})
        # A departed seat can never answer: it declined when it left.
        for _bm, _bv in seats:
            if turns._is_bot(chan, index, _bm):
                votes[pushqueue._push_key(_bm)] = max(votes.get(pushqueue._push_key(_bm),
                                                      rematch.CONTINUE_UNANSWERED),
                                            rematch.CONTINUE_DECLINE)
        votes[key] = max(votes.get(key, rematch.CONTINUE_UNANSWERED), ans)
        answers = [votes.get(pushqueue._push_key(mid), rematch.CONTINUE_UNANSWERED)
                   for mid, _v in seats]
        waiting = [mid for (mid, _v), v in zip(seats, answers)
                   if v == rematch.CONTINUE_UNANSWERED]
        bval = common._env_int("POL_TM_CONTINUE_B", 0)
        if waiting:
            # VERIFIED: LIVE STATUS on the "Play again?" panel (reported live 2026-09-03:
            # "neither can see if the one has selected something"). Push each
            # STILL-WAITING seat an sh>=2 partial tally so its panel redraws
            # (0x10D996: own slot 0 => redraw and keep waiting) now showing who
            # has already answered. Two safety rules make this the RIGHT refresh,
            # not the "answer thrown away" hazard the old silence avoided:
            #   * only UNANSWERED seats are pushed -- a seat that answered 1 gets
            #     bounced back to the interactive panel by an sh>=2 update, so we
            #     never send one to an answered seat;
            #   * the VOTER gets NO reply (its own client already shows its own
            #     choice; a reply carrying its own slot would reset it too).
            # The quorum resolution below still sends everyone the final tally.
            # In 2-player this is exactly right (the lone other seat is always
            # the unanswered one). `POL_TM_CONTINUE_PARTIAL=0` restores silence.
            # WARNING: OFF BY DEFAULT since 2026-09-07T15:14Z. The sh>=2 partial
            # tally delivered to a panel that has NOT answered greys its Yes:
            # the winner's panel opened 15:14:02, the loser voted :07, the
            # status landed :08 and the winner's Yes greyed -- 41d exactly,
            # reproduced with every other cause removed (@GetAway to nobody,
            # the panel gated on the 2nd @Ready). The client has no way to
            # show "the other one chose" without that side effect.
            if common._env_int("POL_TM_CONTINUE_PARTIAL", 0):
                _psh = common._env_int("POL_TM_CONTINUE_SH", 2)
                _pushed = 0
                for i, (mid, _v) in enumerate(seats):
                    if i == me or answers[i] != rematch.CONTINUE_UNANSWERED:
                        continue
                    if (common._env_int("POL_TM_CONTINUE_READY_GATE", 1)
                            and pushqueue._push_key(mid) not in
                            (rematch._CONTINUE_READY.get((chan, index)) or set())):
                        common._say("tm:   ...seat %d (member %s) has not sent @Ready= "
                             "-- its panel does not exist yet; the live status "
                             "waits for it (read after the seat answers, it "
                             "bounces the panel -- the 04:37Z greyed Deck)"
                             % (i, mid))
                        continue
                    pushqueue._queue_push(mid,
                                protocol._turn_code(protocol.CONTINUE_CMD, _psh)
                                + rematch._continue_body(rematch._continue_frame(list(answers), i),
                                                 bval),
                                "the @Continue live status",
                                after=common._env_int("POL_TM_CONTINUE_GAP", 0),
                                peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
                    _pushed += 1
                common._say("tm:   ...%d of %d answered (%s); pushed live status to %d "
                     "waiting seat(s), holding the resolution -- still on %s"
                     % (n - len(waiting), n, "|".join(str(v) for v in answers),
                        _pushed, ", ".join(str(m) for m in waiting)))
            else:
                common._say("tm:   ...%d of %d have answered (%s); holding the reply "
                     "(POL_TM_CONTINUE_PARTIAL=0) -- still waiting on %s"
                     % (n - len(waiting), n,
                        "|".join(str(v) for v in answers),
                        ", ".join(str(m) for m in waiting)))
            return None
        if not waiting and all(v == rematch.CONTINUE_AGAIN for v in answers):
            sh = 1
            outcome = "EVERYONE said yes -- the rematch is ON"
            final = list(answers)
            rematch._reset_for_rematch(chan, index)
            common._say("tm:   ...and the per-match state is reset: a rematch never "
                 "sends @VsGameInit again, so nothing else would have cleared "
                 "the hands, the board or the turn counter.")
            # WARNING: ...AND NEITHER WOULD ANYTHING HAVE STAKED IT. A rematch sends
            # no @VsGameInit, so the accept-quorum stake above never runs and
            # every rematch used to be played for nothing while the client's
            # own "Play again?" panel showed a pot -- and, under `du`, a
            # DOUBLED one. The panel is the client half of this; see
            # `_double_up_pot` for the five instructions it is built on.
            # POL_TM_WAGER_REMATCH=0 restores the unstaked behaviour.
            if common._env_int("POL_TM_WAGER", 1)                     and common._env_int("POL_TM_WAGER_PVP", 1)                     and common._env_int("POL_TM_WAGER_REMATCH", 1):
                _rpot = pots._double_up_pot(chan, index, seats)
                if _rpot > 0:
                    pots._stake_pvp_pot(chan, index, seats, _rpot, "rematch pot")
                    common._say("tm:   ...the rematch is staked at %d a seat "
                         "(du=%s)"
                         % (_rpot,
                            (tablesettings._table_rules_get(chan, index) or {}).get(
                                "du", ruleset.TET_DEFAULTS["du"])))
        elif not waiting:
            sh = common._env_int("POL_TM_CONTINUE_SH", 2)
            outcome = ("somebody declined (%s) -- everyone leaves"
                       % "|".join(str(v) for v in answers))
            final = None                     # per recipient: own slot -> 3
        else:
            sh = common._env_int("POL_TM_CONTINUE_SH", 2)
            outcome = "a partial tally (%s) -- refresh only" % "|".join(
                str(v) for v in answers)
            final = list(answers)
        if not (sh & 0xFF):
            # THE ONE ASSERTION THIS PATH MUST CARRY. See the banner: sh = 0 is
            # parsed and then discarded, which looks in every log exactly like a
            # reply that worked.
            common._say("tm:   WARNING: REFUSING TO SEND @Continue= WITH sh=0 -- 0x104FAC "
                 "would parse it, apply it, and report 'no answer'. Fix "
                 "POL_TM_CONTINUE_SH.")
            return None
        common._say("tm:   ...%s: answering (0x43, %d) sh=%d. Watch for "
             "'---->Recv=CONTINUE' on every client."
             % (outcome, protocol.CONTINUE_CMD, sh))
        mine = None
        for i, (mid, _v) in enumerate(seats):
            vals = list(final) if final is not None else list(answers)
            if final is None:
                vals[i] = rematch.CONTINUE_FINAL     # only OUR slot 3 releases state 7
            body_i = (protocol._turn_code(protocol.CONTINUE_CMD, sh)
                      + rematch._continue_body(rematch._continue_frame(vals, i), bval))
            if i == me:
                mine = body_i
                continue
            pushqueue._queue_push(mid, body_i, "the @Continue tally",
                        after=common._env_int("POL_TM_CONTINUE_GAP", 0),
                        peer=matchmaking._MATCH_PEER.get(pushqueue._push_key(mid)))
        return mine

    if b"@Quit=" in cmd:
        shopdoors._PRIZE_DOOR.discard(member_id)
        # Leaving the card shop. Unanswered, this hangs the client and the
        # session dies ~75 s later -- see the banner above `CARDPRM_RECORDS`.
        if not cardshop._shopquit_enabled():
            return None
        # WARNING: BUT `@Quit=` IS SENT ON THREE DIFFERENT CODES AND THE ANSWER IS NOT
        # THE SAME NAME ON EACH. Caught live 2026-08-20T10:58:54Z, quitting a
        # MATCH from the host's end:
        #
        #     m3 <- code=67 msgid=0x05 sh=8  @Quit=/No=0
        #     m3 -> code=67 msgid=0x05 sh=8  @EQuit=/D=0      <- unreadable
        #
        # `@EQuit` is the CARD SHOP's name -- it belongs to 0xB2 command 23
        # (arm 0x105A97, `Recv=SHOPQUIT`). On code 0x43 the arm is **0x103039**,
        # which matches the literal `@Quit` and reads `/No=` and `/B=`; a body
        # named `@EQuit` fails its `0xAA920` name match, falls through to the
        # 0xB2 table -- whose commands start at 7, so msgid 5 is out of range --
        # and lands on the default arm, which returns 0. That is the exact input
        # to Lobby.BIN 428, "This command cannot be executed".
        #
        # Same message, same field, three codes: answer in the vocabulary of the
        # code it arrived on. `0x1030A3` sets the result to 1 unconditionally, so
        # the fields only have to be present and parseable.
        if (code or 0) & 0xFF == protocol.IN_MATCH_CODE:
            no = common._env_int("POL_TM_MATCHQUIT_NO", 0)
            b = common._env_int("POL_TM_MATCHQUIT_B", 0)
            common._say("tm: member %s @Quit= on code 0x43 -- that is the MATCH quit, "
                 "not the shop's: answering @Quit=/No=%d/B=%d (arm 0x103039 "
                 "matches the name `@Quit`; `@EQuit` is 0xB2's and would be "
                 "unreadable here)" % (member_id, no, b))
            return body[:8] + (b"@Quit=/No=%d/B=%d" % (no, b))
        # ...AND THE NAME ALSO DIFFERS BY CLIENT BUILD, not just by code.
        #
        # WARNING: `@EQuit` DOES NOT EXIST ANYWHERE IN THE PS2 MODULE. Grepped the
        # decrypted `TMaster.pex` (2026-09-08, load base 0x280000): `@Quit`,
        # `@Quit=` and `---->Recv=SHOPQUIT` are all present, `@EQuit` is not,
        # at any offset. The PS2's SHOPQUIT arm compares the body against
        # **`@Quit`** (`0x003f0fd0` -> `0x00489CB8`), passes the same two
        # equality gates the PC's does (sender id at s6+40, shop number at
        # s6+48), logs `Recv=SHOPQUIT` at `0x003f1010`, and then parses TWO
        # fields -- `/N=` (`0x003f1020`) and `/D=` (`0x003f103c`).
        #
        # So answering `@EQuit=/D=0` to a PS2 console is a name its dispatcher
        # can never match: the waiter never wakes and the shop screen hangs with
        # the client heartbeating `@Pong=/Cnt=N/SM=0` and asking for nothing.
        # Measured live 2026-09-08T19:46:22Z -- the exchange was otherwise
        # textbook (header echoed, `/D=0`), which is what made it look right.
        #
        # WARNING: POL_TM_SHOPQUIT_PS2 defaults to 1 because the PS2 is the build that
        # is played; set it to 0 to restore the PC vocabulary. There is no
        # client-kind signal in scope here to pick automatically -- `@Pong=`'s
        # `/SM=` vs `/RM=` is shop-vs-room STATE, not build -- and this module
        # already handles build splits with a knob (`_teachdvans` `ext`).
        shopdoors._CHECKOUT_PULL.pop(member_id, None)
        if os.environ.get("POL_TM_SHOPQUIT_PS2", "1") == "1":
            return body[:8] + (b"@Quit=/N=%d/D=%d" % (shopdoors._shop_n(), cardshop._shopquit_d()))
        return body[:8] + (b"@EQuit=/D=%d" % cardshop._shopquit_d())

    # Nothing above matched.
    #
    # WARNING: THIS ARM USED TO CLAIM the @Opt / @StartData / @TurnData / @BattleData
    # family was "not decoded yet". THAT WAS FALSE and ~17,000 lines stale: all
    # four are decoded and answered ABOVE (`_opt_body`, `_startdata_body`,
    # `_turndata_body`, `_battledata_body`, with the client's own dispatch arms
    # cited at each). A reviewer read this comment, plus the vestigial
    # frame()/parse()/build() stubs just below, and reported the entire TM match
    # protocol as unimplemented. Corrected 2026-08-26.
    #
    # As of 2026-09-04 every one of the client's 52 send keys has an arm above
    # (see the module docstring's COVERAGE list). Anything reaching here is a
    # key this server has never seen on a wire. Capture it; do not invent a
    # reply.
    return None


# --- stubs to be implemented from RE (kept as explicit NotImplemented so a caller
#     that reaches for them fails loudly instead of sending fabricated bytes) ---
def frame(sock):
    raise NotImplementedError("TM world framing not reversed yet (capture first)")


def parse(frame_bytes):
    raise NotImplementedError("TM world message set not reversed yet")


def build(msg):
    raise NotImplementedError("TM world message set not reversed yet")
