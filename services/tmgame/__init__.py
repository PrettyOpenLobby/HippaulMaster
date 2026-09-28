"""The Tetra Master game server, one module per concern.

    deps.py             Imports shared by the package's modules, and the optional ones (tmauction,
                        tmprize, tmroll, tmsave), which are None when the import fails.
    protocol.py         The TM0 game channel: message codes, the E-body header (encode_code,
                        decode_ebody) and the capture-log summaries.
    common.py           The flushed log line (_say) and the small helpers every module leans on:
                        integer knobs, reply delays.
    cardshop.py         The card shop: its door handshakes, packs and what a draw yields, the buy
                        reply, and selling or discarding cards.
    purse.py            A member's gil: the stored balance, opening an account, clamped writes.
    cardtables.py       The client's parameter tables read from services/tmdata: CardPrm, card
                        names, PackPrm, CardPri, and the card level.
    tournament.py       The event (tournament): the countdown, the matchmaker, the event hand,
                        scoring, prizes and the live ticker.
    shopdoors.py        The shop scene's @Init family: the card shop opener, the auction Check Out
                        door, the event shop list, the champion's pack.
    collection.py       A member's card collection file: loading, storing, granting, offers, deck
                        reports, deck slots and deck names.
    webwatch.py         The files the web board reads: the live-match marker (deploy gate) and the
                        watch file of matches in progress.
    pots.py             Stakes and pots: the durable stake record, the table pot, double-up and the
                        PvP escrow.
    trade.py            The card trade (0xA2): the trade board entry, offers, selections and the
                        swap between two collections.
    reservation.py      The reservation scene: @GameQA/@GameEA answers and refusals, and the @GameML
                        member and reservation lists.
    roomchat.py         Room chat (@Chat): the roster arm, its live toggle file and the serial it
                        carries.
    opener.py           The opener: @TeachDV default-data versions, the teach control file, @Init's
                        answer and the /Shm= peer id.
    peers.py            Peer nicks and ids: the peer guid fold, table and room classes, and which
                        table a peer nick names.
    savefile.py         Writing the player save (U/g/TM0DataFile): byte and field patches, the guild
                        byte and @Opt= profile fields.
    tablesettings.py    Table settings (@Tet=/@Tab=): parsing them, the owner's preset in the save,
                        and each table's rule set.
    seating.py          Who sits where: the seat lists per room, seating and unseating a member, and
                        the settings block fit.
    matchmaking.py      From seats to a match: match codes, table info pushes, @MuchMake, the
                        roster, the accept quorum and the seat heartbeat.
    pushqueue.py        The push queue: server-initiated E-bodies waiting to ride a later reply,
                        idle pushes and requeues.
    matchstart.py       Starting a game: card select, player names and game ids, @VsGameInit,
                        @StartData and @TurnData.
    ruleset.py          The seven /R= rule values: table defaults, the VS. game and VS. COM field
                        orders.
    vscom.py            Playing the computer: COM tables and decks (PlPrm), COM turns,
                        @ComList/@ComGameInit, @GameECA and the COM match.
    boardrules.py       The board: per-match hands, turn and board state, @PutCard/@BattleData
                        bodies, blocks, chance and special tiles, combos.
    placement.py        Playing a card: apply a placement, park it at a multi-target choice, and
                        resume it.
    scoring.py          Scoring a finished board: combo and mission counters, tile counts, perfect
                        wins, takes and lucky cards.
    matchend.py         The end of a match: take lists (@TCList), fractional places, result codes,
                        prize credit and @ResultData.
    watchers.py         Watching a match in the client: the watcher list, the watch stream and its
                        purges, @WFCard and @WatchInfo.
    careerstats.py      Career statistics: streaks, Elo, the per-match counters, card growth and
                        syncing them into the save.
    rematch.py          Play again? (@Continue): the answer ladder, the tally and the reset for a
                        rematch.
    turns.py            Turn flow: rosters, bots taking a departed seat, and announcing the next
                        turn.
    tableaudit.py       Checking and healing a room's tables: the audit, reconcile, and releasing
                        seats when members leave.
    tablerow.py         A table's published row: owner and owner changes, occupancy, the settings
                        block and republishing.
    dispatch.py         The entry point for every TM0 line: handle_line and the _handle_line command
                        switch.
    selftest_tables.py  Selftests: packs, seating, the heartbeat, the table audit and reconcile,
                        @GameML, peers and chat.
    selftest_ingame.py  Selftest: the code-0x43 in-match family, end to end.
    selftest_match.py   Selftests: rematch, combos, deck names, card level, rotating and chance
                        blocks, replays, bots, watching.
    selftest_run.py     The selftest entry point (selftest) and the checks against the live captured
                        opener.

tetramaster.py (one directory up) is the name the rest of the tree
imports, and the compatibility facade over these modules.


Protocol notes
--------------
Tetra Master (PlayOnline content id 2) world server.

STATUS (2026-08-26): the TM wire protocol IS reversed and IS served. This module
answers the in-match family on code 0x43 (`@StartData`, `@TurnData`, `@PutCard`,
`@BattleData`, `@ResultData`, `@Continue`, ...), the card shop and auction on
0xB2, and the card trade on 0xA2. Matches have run end to end live (2026-08-20,
2026-08-22) and a trade has completed live (2026-08-25).

WARNING: THE COMMAND TABLE BELOW IS THE AUTHORITY, NOT THE PROSE. Search for
`cmd  name             Recv=` before believing any sentence in this file about
what is or is not decoded. Prose ages; that table is read off the client's own
dispatch arms. Two comment blocks in this file (this docstring's STATUS, and the
fall-through arm at the end of `_handle_line`) sat ~17,000 lines out of date and
caused a reviewer to report the whole match protocol as unimplemented.

COVERAGE (audited 2026-09-04 against the client's command-table map and its 52-key
send list): every client send has a handler or a measured reason for silence.

- `@BattleSelect=`   handled (`3ad0700a`), the multi-defender picker.
- `@Break=`          (0x43 msgid 39, `/ID=0`) the in-match LEAVE notice, sent ~2 s
                     before `@GameExit=`. No reply is polled; the server takes
                     over the empty seat (`_seat_departed`).
- `@ExitCh=` (0x20), `@Viewer=` (0x41 msgid 0x11), `@Mode=` (0xB2): fire-and-forget
                     senders, read off their functions -- no store lookup, no
                     wait. Logged, not answered. `@Save=` likewise (0x87410 is a
                     formatter; its callers send through 0x111750 and poll
                     nothing), so `POL_TM_SAVE_ANS=0` is the measured default.
- 0x43 `@Data=`      msgid 37 is the watch request (handled); no other msgid has
                     ever been seen on a wire.

PRODUCED IN ONE MEASURED CASE (read off the client 2026-09-04):

- `@DataError=/No=` (0x43 cmd 40): the card-select scene (0xD7280) polls it
  beside the picks and, on a hit, sends `@Quit=` and leaves (0xD7317..0xD737C;
  `/No=` is parsed and discarded). Sent when a `@CardSelect=` arrives for a
  match this process has no roster for (a restart ate it) -- the only scene
  that polls cmd 40 is card select, so it is sent there and nowhere else.

STILL UNPRODUCED -- messages the client can PARSE that this server never sends,
because no wire capture shows what triggers them (do not invent one):

- `@ComList` (0x43 cmd 2/4): `/L=` count into obj+0x18C, `/D=` per row into
  the table at 0x524B630 (stride 0x14). Polled by the result scene (0x10D5ED)
  in a branch that sends `@Continue` cmd 17 with a third argument and sets
  0x5246586=1 first -- a "play again against a different opponent" path the
  live VS. COM rematch (09-03) never took (it went straight to `@CardSelect=`).
- `@GameKick=/Kick=/FN=/QN=` (0x41): popped by the reservation poll 0xA92D5
  (0x843A0); carries the kicker's names. No client send requests a kick.
- 0xB2 `@Select=/A=` (cmd 7): shop-scoped (checks the header's shop byte);
  polled by 0xFD220 only while a match context exists. Trigger unmeasured.
- `@ZoneList`: a debug/test-command path (0x1154E0 prints "ans0 = %s"), not
  a server message.
- the EVENT family, mapped off the client's event loader: the
  loader reads `b/g/TM0EventDataList`, sends `@EventTimeReq=` (0xD1), polls
  `@EventTimeReqCheck`, then waits for `@EventEn=/Ans=` (0xD0); `@ETelop`
  (0xD7, `/ID= /Lp= /Pg= /St=`) is the ticker; 0xD8 is a presence flag; the
  game half (`@EventGameInit` / `@EventCard` / `@EventResult`) shares the
  match arms. The scene has NO client entry in the US retail build
  (posting its command is a measured no-op), so it stays
  unbuilt. THE EVENT THE WIKI DOCUMENTS IS BUILT INSTEAD: the champion's
  `rank_1 Pack` in the ordinary Card Shop -- `@Init=/SN=<name>` (arm
  0x105201 formats "%s's Pack" for PackPrm record 5) and the save header's
  +0x104 name slot, both fed by `tm-champion.json` from the Sunday publish.
  See `_champion_name`.
- (0xA2, 24) `@Init` the trade-board opener is served from responders.py.

The client's send vocabulary is CLOSED at 52 `@Key=` literals (read off the
binary, fully enumerated), so the lists above are finite and known -- not a search.

What we know
------------
- Tetra Master was an online-only card game; the client is the PlayOnline Viewer's
  TM content (content id 2, name "TetraMaster"; see contentlist.py / pol-games-menu).
- The world address is handed to the client by the LOBBY (pp000), which dials it
  directly by raw IP (the POL-0008 boundary). In our stack OUR lobby emits that
  handoff -> POL_WORLD_IP:POL_WORLD_PORT, so the client dials our world stub.
- Session crypto: OPEN. The lobby/world MAY reuse the login session key (K=0 in our
  token0 flow) or re-key. The harness defaults to K=0 and is overridable, exactly
  like the lobby stub -- confirm from the first captured world frames.

Where the protocol RE comes from (PC-first; PS2 ELF is static-only, no DNAS)
---------------------------------------------------------------------------
- PC: TM.dll is a separate x86 rebuild, ASProtect-packed (see janhourou-format /
  pol-games-menu). Dynamic capture via this harness is the near-term route.
- PS2: the TM logic is in the Viewer monolith `SLPS_202.00`
  (unpacked MIPS); *reading* it touches no DNAS. Cross-index
  the PS2 name map for the TM module/handlers. This is the cleanest
  static reference for the match/board/turn message set.

Vestigial scaffolding -- do NOT read it as a status signal
----------------------------------------------------------
`frame()` / `parse()` / `build()` near the end of this file raise
`NotImplementedError` and have **zero callers** (verified 2026-08-26: no
`.frame(`/`.parse(`/`.build(` call site exists anywhere in `pol-server/`). They
are leftovers from the original capture-harness design. The live path is
`_handle_line` plus the per-command builders above it. They are kept only so a
stray caller fails loudly instead of sending fabricated bytes -- their existence
is not evidence that anything is unreversed.
"""
