"""Selftests: rematch, combos, deck names, card level, rotating and chance blocks, replays, bots,
watching.
"""
import json
import os
import shutil
import tempfile
import tmbattle
import struct
import time
import tmstore
from .deps import tmsave
from . import (
    boardrules, cardtables, careerstats, collection, common, dispatch, matchmaking, matchstart,
    placement, protocol, pushqueue, rematch, savefile, scoring, seating, selftest_run,
    shopdoors, tableaudit, tablerow, turns, vscom, watchers,
)


def _selftest_continue():
    """`@Continue=` -- the quorum reply, against the three rules that decide it.

    Not "does it look right": each assertion is a branch in arm 0x104EE1 or in
    the panel that polls it, and each has a failure mode that is invisible on
    the wire.
    """
    ok = True
    seats = [(101, 1), (102, 2)]
    chan, index = "#TMSELF", 1
    saved = dict(rematch._MATCH_CONTINUE), dict(matchmaking._MATCH_ROSTER), dict(pushqueue._PUSHES)
    try:
        rematch._MATCH_CONTINUE.clear(); matchmaking._MATCH_ROSTER.clear(); pushqueue._PUSHES.clear()
        for mid, _v in seats:
            matchmaking._MATCH_ROSTER[pushqueue._push_key(mid)] = (chan, index, seats)

        # Cases 0/0b exercise the LIVE STATUS, which is OFF by default since
        # 15:14Z (it greys the unanswered seat's Yes); pin it under its knob.
        _saved_partial = os.environ.get("POL_TM_CONTINUE_PARTIAL")
        os.environ["POL_TM_CONTINUE_PARTIAL"] = "1"
        # 0. THE LIVE STATUS WAITS FOR THE PANEL (2026-09-07T04:37Z, the Deck's
        #    greyed panel): a vote before the other seat's @Ready pushes it
        #    NOTHING; that seat's @Ready then gets the status once, one reply
        #    behind, framed recipient-first.
        rematch._CONTINUE_READY.clear()
        dispatch.handle_line(protocol._turn_code(protocol.CONTINUE_CMD, 0) + b"@Continue=/Ans=1/PR=0/Conf=0",
                    member_id=101)
        if any(b"@Continue=" in e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(102)) or [])):
            common._say("FAIL: no live status may be pushed to a seat whose @Ready= has "
                 "not arrived (read after it answers, it bounces the panel)")
            ok = False
        _rdy = dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id=102)
        _rdyl = _rdy if isinstance(_rdy, list) else ([_rdy] if _rdy else [])
        if any(b"@Continue=" in b for b in _rdyl):
            common._say("FAIL: the live status must not ride the @Ready= answer itself "
                 "(the panel is built from that reply)"); ok = False
        _q102 = [e for e in (pushqueue._PUSHES.get(pushqueue._push_key(102)) or [])
                 if b"@Continue=" in e[1]]
        if len(_q102) != 1 or b"/Ans=0|1" not in _q102[0][1]:
            common._say("FAIL: after @Ready= the pending vote's status is queued once for "
                 "the NEXT reply, recipient-first (/Ans=0|1); got %r"
                 % ([(e[0], e[1]) for e in _q102],)); ok = False
        rematch._MATCH_CONTINUE.clear(); pushqueue._PUSHES.clear(); rematch._CONTINUE_READY.clear()

        # 0b. A DECISIVE GAME: the 1st @Ready= does not open the panel, the
        #     loser's 2nd is HELD during the take and released one reply behind
        #     the @GetSelect push, and the winner's 2nd opens its panel.
        import tempfile as _tf
        _saved_res = os.environ.get("POL_RESOURCE_DIR")
        _saved_hold = os.environ.get("POL_TM_READY_HOLD_LOSER")
        _saved_turn = dict(boardrules._MATCH_TURN)
        try:
            os.environ["POL_RESOURCE_DIR"] = _tf.mkdtemp(prefix="tm-rdy-")
            os.environ["POL_TM_READY_HOLD_LOSER"] = "1"
            _row = b"220|55|0|78|80|18|7|255"
            boardrules._MATCH_TURN[(chan, index)] = {
                "turn": 9, "active": 1, "n": 2,
                "take": {"win": 0, "lose": 1, "pools": [[_row], [_row]],
                         "roster": [101, 102], "picked": []}}
            dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id=101)   # winner #1
            dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id=102)   # loser #1
            _r2 = dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id=102)  # loser #2
            if _r2 is not None:
                common._say("FAIL: the loser's 2nd @Ready= must be HELD during the take, "
                     "got %r" % (_r2,)); ok = False
            if pushqueue._push_key(101) in (rematch._CONTINUE_READY.get((chan, index)) or set()):
                common._say("FAIL: the winner's 1st @Ready= must not open its panel (it "
                     "is going into the take scene)"); ok = False
            dispatch.handle_line(b"43000F00@GetSelect=/H=0", member_id=101)
            _q102 = [e for e in (pushqueue._PUSHES.get(pushqueue._push_key(102)) or [])
                     if b"@Ready=/Go=" in e[1]]
            if len(_q102) != 1 or _q102[0][0] < 1:
                common._say("FAIL: the winner's pick must release the loser's held "
                     "@Ready= one reply behind the @GetSelect push, got %r"
                     % ([(e[0], e[1]) for e in _q102],)); ok = False
            dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id=101)   # winner #2
            if pushqueue._push_key(101) not in (rematch._CONTINUE_READY.get((chan, index)) or set()):
                common._say("FAIL: the winner's 2nd @Ready= (after the take) opens its "
                     "panel"); ok = False
        finally:
            boardrules._MATCH_TURN.clear(); boardrules._MATCH_TURN.update(_saved_turn)
            for _k, _v in (("POL_RESOURCE_DIR", _saved_res),
                           ("POL_TM_READY_HOLD_LOSER", _saved_hold)):
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v
        rematch._MATCH_CONTINUE.clear(); pushqueue._PUSHES.clear(); rematch._CONTINUE_READY.clear()
        if _saved_partial is None:
            os.environ.pop("POL_TM_CONTINUE_PARTIAL", None)
        else:
            os.environ["POL_TM_CONTINUE_PARTIAL"] = _saved_partial
        # ...and OFF (the default): a vote pushes the other seat NOTHING.
        dispatch.handle_line(b"43000E0A@Ready=/Ans=0", member_id=102)
        dispatch.handle_line(protocol._turn_code(protocol.CONTINUE_CMD, 0) + b"@Continue=/Ans=1/PR=0/Conf=0",
                    member_id=101)
        if any(b"@Continue=" in e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(102)) or [])):
            common._say("FAIL: with the live status OFF (default) a vote must push the "
                 "other seat nothing -- the sh>=2 update greys its Yes (41d)")
            ok = False
        rematch._MATCH_CONTINUE.clear(); pushqueue._PUSHES.clear(); rematch._CONTINUE_READY.clear()

        # 1. THE THIRD ENVELOPE BYTE IS NEVER 0. 0x104FAC is the only site that
        #    sets 0x101A10's return value and it tests exactly this.
        for who, ans in ((101, 1), (102, 1)):
            out = dispatch.handle_line(protocol._turn_code(protocol.CONTINUE_CMD, 0)
                              + b"@Continue=/Ans=%d/PR=0/Conf=0" % ans,
                              member_id=who)
        # The reply may be a LIST: seat 102's vote drains the live-status partial
        # tally we pushed it on 101's vote, alongside the resolution. Find the
        # resolution body (the yes/yes tally, /Ans=1|1) among whatever came back.
        _outl = out if isinstance(out, list) else ([out] if out else [])
        _res = next((b for b in _outl if b"@Continue=" in b and b"/Ans=1|1" in b),
                    None)
        if not _outl:
            common._say("FAIL: a completed yes/yes vote produced no reply at all")
            ok = False
        elif _res is None:
            common._say("FAIL: the resolution tally (/Ans=1|1) was not in the reply, "
                 "got %r" % (_outl,)); ok = False
        else:
            sh = (struct.unpack("<I", bytes.fromhex(_res[:8].decode()))[0] >> 24) & 0xFF
            if sh == 0:
                common._say("FAIL: @Continue= answered with sh=0 -- 0x104FAC parses "
                     "that and then reports 'no answer'"); ok = False
            elif sh != 1:
                common._say("FAIL: everyone said yes, so sh must be 1 (state 6's only "
                     "route to the re-deal); got %d" % sh); ok = False

        # 2. THE LIST IS ALWAYS N LONG. 0x104F71 stores without checking the
        #    parse return, so a short list maxes stack garbage into the records.
        rematch._MATCH_CONTINUE.clear(); pushqueue._PUSHES.clear()
        dispatch.handle_line(protocol._turn_code(protocol.CONTINUE_CMD, 0) + b"@Continue=/Ans=1",
                    member_id=101)
        out = dispatch.handle_line(protocol._turn_code(protocol.CONTINUE_CMD, 0) + b"@Continue=/Ans=2",
                          member_id=102)
        # The decliner's reply may be a LIST (its live-status partial from vote 1
        # drains alongside the resolution); the resolution is the sh=2 teardown
        # whose own slot 0 is CONTINUE_FINAL (3).
        _outl = out if isinstance(out, list) else ([out] if out else [])
        out = next((b for b in _outl if b"@Continue=" in b
                    and b.split(b"/Ans=")[1].split(b"/B=")[0].split(b"|")[0]
                    == b"%d" % rematch.CONTINUE_FINAL), None)
        if out is None or out.count(b"|") < len(seats) - 1:
            common._say("FAIL: the reply must carry one /Ans= per player; got %r"
                 % (_outl,)); ok = False

        # 3. A DECLINER LEAVES ONLY VIA ITS OWN SLOT == 3 (state 7, 0x10D9AD),
        #    and the recipient's own slot is occurrence 0.
        if out is not None:
            vals = out.split(b"/Ans=")[1].split(b"/B=")[0].split(b"|")
            if vals[0] != b"%d" % rematch.CONTINUE_FINAL:
                common._say("FAIL: the decliner's own slot must be raised to %d or it "
                     "sits on 'Play again?' for ever; got %r"
                     % (rematch.CONTINUE_FINAL, vals)); ok = False
            other = pushqueue._PUSHES.get(pushqueue._push_key(101), [])
            if not other:
                common._say("FAIL: the other player was never told the vote closed")
                ok = False
            else:
                ovals = (other[-1][1].split(b"/Ans=")[1]
                         .split(b"/B=")[0].split(b"|"))
                if ovals[0] != b"%d" % rematch.CONTINUE_FINAL:
                    common._say("FAIL: every recipient's OWN slot carries the 3, not "
                         "just the decliner's; got %r" % (ovals,)); ok = False

        # 4. A REMATCH RESETS THE GAME STATE -- it never sends @VsGameInit, so
        #    nothing else would.
        rematch._MATCH_CONTINUE.clear(); pushqueue._PUSHES.clear()
        boardrules._MATCH_HANDS[(chan, index)] = {"stale": [b"1|1|1|1|1|1|1|1"]}
        matchstart._CARD_READY[(chan, index)] = {pushqueue._push_key(101), pushqueue._push_key(102)}
        for who in (101, 102):
            dispatch.handle_line(protocol._turn_code(protocol.CONTINUE_CMD, 0) + b"@Continue=/Ans=1",
                        member_id=who)
        if (chan, index) in boardrules._MATCH_HANDS or (chan, index) in matchstart._CARD_READY:
            common._say("FAIL: the rematch left the previous game's hands/readiness "
                 "behind -- the next @CardSelect= would deal on one player")
            ok = False
    finally:
        rematch._MATCH_CONTINUE.clear(); rematch._MATCH_CONTINUE.update(saved[0])
        matchmaking._MATCH_ROSTER.clear(); matchmaking._MATCH_ROSTER.update(saved[1])
        pushqueue._PUSHES.clear(); pushqueue._PUSHES.update(saved[2])
        boardrules._MATCH_HANDS.pop((chan, index), None)
        matchstart._CARD_READY.pop((chan, index), None)
        boardrules._MATCH_TURN.pop((chan, index), None)
        boardrules._MATCH_BOARD.pop((chan, index), None)
        boardrules._MATCH_OBJECTS.pop((chan, index), None)
        scoring._MATCH_COMBO.pop((chan, index), None)
        matchstart._TURN_RAND.pop((chan, index), None)
    return ok


def _selftest_loss_combo():
    """The loss-side combo, pinned to the MEASURED game (2026-08-22T21:14Z).

    The fixture is the live board verbatim: tiles 8/9/10 the COM's, tile 4 the
    human's other card, and the human places tile 5 (card 10, arrows 0x7E)
    into three back-pointing enemies. The engine fights tile 10 (last by
    index, exactly as it did live), the placer LOSES, and the client's
    measured behaviour is the assertion: the placed card flips to the
    defender AND tile 4 -- reached by the beaten card's W arrow -- is
    captured with it (`board3p.log` 17:14:13). The old arm left tile 4 alone,
    which was THE board divergence.
    """
    ok = True
    key = ("#TMSELFTEST", 99)
    saved_board = dict(boardrules._MATCH_BOARD)
    try:
        boardrules._MATCH_BOARD.clear()
        boardrules._MATCH_BOARD[key] = {
            4: tmbattle.Card([21, 5, 0, 5, 5, 0, 0x03, 0], 0),
            8: tmbattle.Card([12, 5, 0, 5, 5, 0, 0x02, 0], 1),
            9: tmbattle.Card([58, 5, 0, 5, 5, 0, 0x07, 0], 1),
            10: tmbattle.Card([3, 90, 0, 90, 90, 0, 0x82, 0], 1),
        }

        class _Seq(object):
            # resolve() draws exactly four randrange(0x8000) values (two per
            # side); 0,0 rolls the 1-stat attacker to 0 and 0x4000,0x4000 the
            # 90-stat defender to 4, so the verdict is DEFENDER by arithmetic,
            # not by luck.
            def __init__(self, vals):
                self.vals = list(vals)

            def randrange(self, bound):
                return self.vals.pop(0) % bound

        battles, _marks = placement._apply_placement(
            key[0], key[1], 2, 0, 5, b"10|1|0|1|1|0|126|0",
            rnd=_Seq([0, 0, 0x4000, 0x4000]))
        board = boardrules._MATCH_BOARD.get(key) or {}
        if len(battles) != 1 or battles[0][1] != 10:
            common._say("FAIL: the engine must fight exactly the live game's battle "
                 "-- tile 10, last by index -- got %r"
                 % ([(b[0], b[1], b[3]) for b in battles],))
            ok = False
        elif battles[0][3] != tmbattle.DEFENDER:
            common._say("FAIL: the rigged rolls must lose the battle for the placer, "
                 "got verdict %r" % (battles[0][3],))
            ok = False
        if board.get(5) is None or board[5].owner != 1:
            common._say("FAIL: the beaten placed card must flip to the defender "
                 "(0xCFAB4)")
            ok = False
        if board.get(4) is None or board[4].owner != 1:
            common._say("FAIL: THE LOSS COMBO -- tile 4, reached by the beaten "
                 "card's W arrow, must be captured by the winner (measured "
                 "live 2026-08-22T21:14Z; the old stop-dead arm was the "
                 "board divergence)")
            ok = False
        for t in (8, 9, 10):
            if board.get(t) is not None and board[t].owner != 1:
                common._say("FAIL: tile %d was already the winner's and must not "
                     "move" % t)
                ok = False
    finally:
        boardrules._MATCH_BOARD.clear()
        boardrules._MATCH_BOARD.update(saved_board)
    return ok


def _selftest_deck_names():
    """The two 17-byte save strings are the DECK SET NAMES (2026-09-07).

    A stored `@DN1=` report lands on tab 1 (+0x104) with tab 2 zeroed; the
    base knob moves it; the champion's name must NOT be stamped into +0x104
    by default (a tester's tab read the champion's name) but still is under
    `POL_TM_CHAMPION_SAVE_SLOT=1`; and a stats sync clears a stale stamp --
    the repair path for the saves written on 2026-09-06.
    """
    import tempfile
    ok = True
    m = "dnames"
    env_keys = ("POL_RESOURCE_DIR", "POL_TM_DECK_NAME_BASE",
                "POL_TM_CHAMPION_SAVE_SLOT", "POL_TM_CHAMPION_NAME",
                "POL_TM_SAVE_WRITE", "POL_TM_DECK_NAMES", "POL_TM_DECK_SLOTS")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    if tmsave is None:
        return ok
    try:
        os.environ["POL_RESOURCE_DIR"] = tempfile.mkdtemp(prefix="tm-dn-")
        os.environ["POL_TM_SAVE_WRITE"] = "1"
        os.environ["POL_TM_CHAMPION_NAME"] = "laplacier"
        for k in ("POL_TM_DECK_NAME_BASE", "POL_TM_CHAMPION_SAVE_SLOT",
                  "POL_TM_DECK_NAMES"):
            os.environ.pop(k, None)
        path = savefile._save_file(m)

        def slots():
            b = open(path, "rb").read()
            return (bytes(b[0x104:0x115]).rstrip(b"\x00"),
                    bytes(b[0x115:0x126]).rstrip(b"\x00"))

        collection._collection_store(m, {"cards": [], "deck_names":
                              "@Decks=/C=0@DN1=/S=434F4F4C204445434B53"})
        if slots() != (b"COOL DECKS", b""):
            common._say("FAIL: a stored @DN1= name must land on deck tab 1 (+0x104) "
                 "with tab 2 zeroed, got %r" % (slots(),)); ok = False
        if b"laplacier" in open(path, "rb").read():
            common._say("FAIL: the champion's name must NOT be stamped into the save "
                 "by default -- it is the first deck tab (decided 2026-09-07)")
            ok = False
        os.environ["POL_TM_DECK_NAME_BASE"] = "0"
        collection._collection_to_save(m, [])
        if slots() != (b"", b"COOL DECKS"):
            common._say("FAIL: POL_TM_DECK_NAME_BASE=0 must move @DN1= to tab 2, got "
                 "%r" % (slots(),)); ok = False
        os.environ.pop("POL_TM_DECK_NAME_BASE", None)
        # The old behaviour, behind its knob (the positive control).
        os.environ["POL_TM_CHAMPION_SAVE_SLOT"] = "1"
        collection._collection_to_save(m, [])
        if slots()[0] != b"laplacier":
            common._say("FAIL: POL_TM_CHAMPION_SAVE_SLOT=1 must restore the champion "
                 "stamp, got %r" % (slots(),)); ok = False
        os.environ.pop("POL_TM_CHAMPION_SAVE_SLOT", None)
        # The repair: a stats sync alone (no collection change) clears it.
        moved = careerstats._save_sync_stats(m)
        if slots() != (b"COOL DECKS", b"") or 0x104 not in (moved or {}):
            common._say("FAIL: _save_sync_stats must put the deck names back over a "
                 "stale stamp (the 2026-09-06 repair), got %r moved=%r"
                 % (slots(), sorted((moved or {}).keys()))); ok = False
        # A 17+ byte name is cut to 16 so the NUL survives.
        collection._collection_store(m, {"cards": [], "deck_names":
                              "@Decks=/C=0@DN1=/S=" + "41" * 20 + "@DN2=/S=42"})
        b = open(path, "rb").read()
        if b[0x104:0x115] != b"A" * 16 + b"\x00" or slots()[1] != b"B":
            common._say("FAIL: a long name must be cut to 16 + NUL and @DN2= must be "
                 "tab 2, got %r" % (b[0x104:0x126],)); ok = False
        # THE DECK SLOTS. Cards stored with the old CardPrm column in field 8
        # must land in NO deck; a card-form report places them by its own
        # 7th value (absolute slot); a later report for another deck keeps
        # them; a re-report of the same deck replaces that deck only.
        collection._collection_store(m, {"cards": [[220, 55, 0, 78, 80, 18, 7, 2],
                                        [215, 90, 2, 100, 100, 23, 1, 3],
                                        [100, 10, 0, 10, 10, 5, 3, 0]]})

        def slot8():
            b = open(path, "rb").read()
            return [b[tmsave.CARDS_OFF + tmsave.REC * i + 8] for i in range(3)]

        if slot8() != [255, 255, 255]:
            common._say("FAIL: cards with no reported deck slot must save byte 8 = 255 "
                 "(the CardPrm column scattered them into decks), got %r"
                 % (slot8(),)); ok = False
        if collection._new_card_slot(2) != 255:
            common._say("FAIL: a granted card carries no deck slot"); ok = False
        collection._collection_set_deck(m, b"@Decks=/C=2@0=/D=220|55|0|78|80|7|3"
                                b"@1=/D=215|90|2|100|100|1|29", sh=1)
        if slot8() != [3, 29, 255]:
            common._say("FAIL: a card-form @Decks= report must place cards by its 7th "
                 "value (absolute slot), got %r" % (slot8(),)); ok = False
        collection._collection_set_deck(m, b"@Decks=/C=1@0=/D=220|55|0|78|80|7|1", sh=1)
        if slot8() != [1, 29, 255]:
            common._say("FAIL: re-reporting a card MOVES it (its old slot goes) and "
                 "leaves other decks alone, got %r" % (slot8(),)); ok = False
        # A second card joining the SAME deck must not evict the first (live
        # reports carry one card each).
        collection._collection_set_deck(m, b"@Decks=/C=1@0=/D=100|10|0|10|10|3|2", sh=1)
        if slot8() != [1, 29, 2]:
            common._say("FAIL: placing a second card in a deck must keep the first, "
                 "got %r" % (slot8(),)); ok = False
        collection._collection_set_deck(m, b"@Decks=/C=1@0=/D=100|10|0|10|10|3|255", sh=1)
        # A first map is SEEDED from the pre-slot verbatim report (03:05Z loss).
        collection._collection_store(m, {"cards": [[220, 55, 0, 78, 80, 18, 7, 2],
                                        [215, 90, 2, 100, 100, 23, 1, 3],
                                        [100, 10, 0, 10, 10, 5, 3, 0]],
                              "deck": "@Decks=/C=1@0=/D=100|10|0|10|10|3|4"})
        collection._collection_set_deck(m, b"@Decks=/C=1@0=/D=220|55|0|78|80|7|3", sh=1)
        if slot8() != [3, 255, 4]:
            common._say("FAIL: the first slot map must keep the pre-slot report's card "
                 "(slot 4) beside the new placement, got %r" % (slot8(),))
            ok = False
        collection._collection_set_deck(m, b"@Decks=/C=2@0=/D=220|55|0|78|80|7|3"
                                b"@1=/D=215|90|2|100|100|1|29", sh=1)
        collection._collection_set_deck(m, b"@Decks=/C=1@0=/D=100|10|0|10|10|3|255", sh=1)
        # A removal is the card reported with slot 255 (measured 03:17:51Z).
        collection._collection_set_deck(m, b"@Decks=/C=2@0=/D=220|55|0|78|80|7|255"
                                b"@1=/D=215|90|2|100|100|1|255", sh=1)
        if slot8() != [255, 255, 255]:
            common._say("FAIL: a card reported with slot 255 must leave its deck, got "
                 "%r" % (slot8(),)); ok = False
        os.environ["POL_TM_DECK_SLOTS"] = "0"
        if collection._new_card_slot(2) != 2:
            common._say("FAIL: POL_TM_DECK_SLOTS=0 restores the old column"); ok = False
        os.environ.pop("POL_TM_DECK_SLOTS", None)
        # A per-tab report MERGES: naming tab 2 must not lose tab 1 (the
        # 02:42Z TEST/RAWR loss).
        collection._collection_set_deck(m, b"@Decks=/C=0@DN1=/S=54455354")
        collection._collection_set_deck(m, b"@Decks=/C=0@DN2=/S=52415752")
        if slots() != (b"TEST", b"RAWR"):
            common._say("FAIL: a later @DN2= report must merge with the stored @DN1=, "
                 "got %r" % (slots(),)); ok = False
        os.environ["POL_TM_DECK_NAMES"] = "0"
        if collection._deck_name_fields(m):
            common._say("FAIL: POL_TM_DECK_NAMES=0 must author nothing"); ok = False
    except Exception as exc:
        import traceback
        common._say("FAIL: deck-names selftest raised %r\n%s"
             % (exc, traceback.format_exc())); ok = False
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ok


def _selftest_owner_name():
    """The @GameOwner handoff names the leaver, HEX-ENCODED (0xAAD60)."""
    import tmroom
    ok = True
    chan, index = "#TMOWNER", 1
    left, heir = 7101, 7102
    saved_push = dict(pushqueue._PUSHES)
    try:
        pushqueue._PUSHES.clear()
        tmroom.note_name(left, "DeckTestNew")
        tablerow._push_owner_change(tmroom, chan, index, left, [(heir, 0x22)])
        body = next((e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(heir)) or [])
                     if b"@GameOwner=" in e[1]), None)
        want = b"/CN=" + "DeckTestNew".encode("cp932").hex().upper().encode()
        if body is None or want not in body:
            common._say("FAIL: @GameOwner must carry the leaver's name as a hex :str "
                 "(0xAAD60 decodes /CN= by hex pairs; raw text drew a blank), "
                 "got %r" % (body,)); ok = False
        if body is not None and b"/CN=DeckTestNew" in body:
            common._say("FAIL: /CN= must not be raw text"); ok = False
    finally:
        pushqueue._PUSHES.clear(); pushqueue._PUSHES.update(saved_push)
    return ok


def _selftest_card_level():
    """THE CARD LEVEL, and that @ComGameInit no longer sends 0 (2026-09-08).

    The client's 0xB9040: the best grade of each DISTINCT card id, plus
    `type - 1` per row for types above 1, plus 5 per distinct arrow mask.
    """
    ok = True
    rows = [[89, 34, 0, 76, 32, 8, 20, 255],     # two copies, grades 8 and 13
            [89, 34, 0, 76, 32, 13, 20, 255],    # only the 13 counts
            [158, 97, 3, 89, 95, 36, 65, 255],   # type 3 -> +2, new mask -> +5
            [15, 14, 0, 10, 12, 3, 20, 255]]     # mask 20 already seen
    want = 13 + 36 + 3 + 2 + 5 * 2
    got = cardtables.card_level(rows)
    if got != want:
        common._say("FAIL: card_level must be the best grade per DISTINCT id + "
             "(type-1) per row + 5 per distinct arrow mask = %d, got %d"
             % (want, got)); ok = False
    if cardtables.card_level([[89, 34, 0, 76, 32, 8, 20, 255]] * 9) != 8 + 5:
        common._say("FAIL: duplicates of one card must not stack -- the dedupe is by "
             "CARD ID (0xB90FE)"); ok = False
    if cardtables.card_level([[99999, 1, 0, 1, 1, 200, 1, 255]]) != 0:
        common._say("FAIL: a card id past the CardPrm table is skipped whole "
             "(0xB90F6), grade and arrows included"); ok = False
    # ...and the message carries it. A 0 here LOCKS every VS. COM opponent
    # above the first threshold (PlPrm.BIN +0x02 vs struct +0x00).
    saved = os.environ.get("POL_TM_VSGAME_CP")
    saved_dir = os.environ.get("POL_RESOURCE_DIR")
    try:
        os.environ.pop("POL_TM_VSGAME_CP", None)
        import tempfile
        _d = tempfile.mkdtemp(prefix="tmcl")
        os.environ["POL_RESOURCE_DIR"] = _d
        collection._collection_store("clvl", {"cards": rows, "money": 500})
        body = vscom._comgame_body("clvl", [0] * 7)
        if b"/CP=%d" % want not in body:
            common._say("FAIL: @ComGameInit must carry the player's CARD LEVEL in "
                 "/CP= (it lands in struct +0x00, the opponent-unlock gate) "
                 "-- got %r" % (body,)); ok = False
        os.environ["POL_TM_VSGAME_CP"] = "7"
        if b"/CP=7" not in vscom._comgame_body("clvl", [0] * 7):
            common._say("FAIL: POL_TM_VSGAME_CP must still force a value"); ok = False
    finally:
        if saved is None:
            os.environ.pop("POL_TM_VSGAME_CP", None)
        else:
            os.environ["POL_TM_VSGAME_CP"] = saved
        if saved_dir is None:
            os.environ.pop("POL_RESOURCE_DIR", None)
        else:
            os.environ["POL_RESOURCE_DIR"] = saved_dir
    return ok



def _selftest_cardselect_leave():
    """A 2-player leave during card select tells the survivor (2026-09-07
    15:28Z): a (0x43, 40) @DataError is pushed to the remaining human and no
    synthetic deal happens."""
    ok = True
    chan, index = "#TMCSLEAVE", 1
    key = (chan, index)
    m0, m1 = "cslA", "cslB"
    seats = [(m0, 0x11), (m1, 0x22)]
    stores = (matchmaking._MATCH_ROSTER, boardrules._MATCH_TURN, boardrules._MATCH_HANDS, matchmaking._MATCH_STARTED,
              matchmaking._MATCH_BOTS, pushqueue._PUSHES, matchstart._CARD_READY, matchmaking._MATCH_PEER)
    saved = [(d, dict(d)) for d in stores]
    saved_synth = set(matchmaking._BOT_SYNTH)
    saved_mode = os.environ.get("POL_TM_CARDSELECT_LEAVE")
    try:
        for d in stores:
            d.clear()
        os.environ["POL_TM_CARDSELECT_LEAVE"] = "dataerror"   # opt-in since 09-07
        matchmaking._remember_match(chan, index, seats)
        matchmaking._MATCH_STARTED[key] = {pushqueue._push_key(m0), pushqueue._push_key(m1)}
        matchstart._CARD_READY[key] = {pushqueue._push_key(m0)}
        turns._seat_departed(m1, "selftest: quit at card select")
        q0 = [e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(m0)) or [])]
        if not any(b"@DataError=" in b for b in q0):
            common._say("FAIL: the last human must be pushed @DataError when the "
                 "other seat leaves during card select, got %r" % (q0,))
            ok = False
        if any(b"@StartData=" in b for b in q0) or key in boardrules._MATCH_TURN:
            common._say("FAIL: no synthetic deal may follow a 2-player card-select "
                 "leave (the survivor is told the match is gone)"); ok = False
        # `getaway`: the survivor is told by the client's own announcer
        # (@GetAway=/P=<abs seat>, cmd 35) AND the seat is dealt and played.
        for d in (boardrules._MATCH_TURN, boardrules._MATCH_HANDS, matchmaking._MATCH_BOTS, pushqueue._PUSHES, matchstart._CARD_READY):
            d.clear()
        matchmaking._BOT_SYNTH.clear()
        os.environ["POL_TM_CARDSELECT_LEAVE"] = "getaway"
        matchmaking._remember_match(chan, index, seats)
        matchmaking._MATCH_STARTED[key] = {pushqueue._push_key(m0), pushqueue._push_key(m1)}
        boardrules._MATCH_HANDS[key] = {pushqueue._push_key(m0): [b"1|5|0|6|5|2|26|255"] * 5,
                             ("deck", 0): [b"1|5|0|6|5|2|26|255"] * 5}
        matchstart._CARD_READY[key] = {pushqueue._push_key(m0)}
        turns._seat_departed(m1, "selftest: quit at card select (getaway)")
        q0 = [e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(m0)) or [])]
        if not any(b"@GetAway=/P=1" in b for b in q0):
            common._say("FAIL: getaway mode must push @GetAway=/P=<absolute seat> to "
                 "the survivor, got %r" % (q0,)); ok = False
        if any(b"@DataError=" in b for b in q0):
            common._say("FAIL: getaway mode must not also push @DataError"); ok = False
        if key not in matchmaking._BOT_SYNTH or not (boardrules._MATCH_HANDS.get(key) or {}).get(pushqueue._push_key(m1)):
            common._say("FAIL: getaway mode must still deal the departed seat a hand "
                 "(the client continues the match, timer off)"); ok = False
    finally:
        for d, v in saved:
            d.clear(); d.update(v)
        matchmaking._BOT_SYNTH.clear(); matchmaking._BOT_SYNTH.update(saved_synth)
        if saved_mode is None:
            os.environ.pop("POL_TM_CARDSELECT_LEAVE", None)
        else:
            os.environ["POL_TM_CARDSELECT_LEAVE"] = saved_mode
    return ok


def _selftest_rotating_turns():
    """THE ROTATING BLOCK TURNS (2026-09-07T14:23Z freeze).

    The frozen 2p game verbatim: a code-15 block on tile 6. Turn 1 the Deck's
    card 148 on tile 9 fights the block (wins; the ray fires SE at empty 11)
    then tile 12 (loses -> 9 is the human's). Turn 3 the Deck's card 24 on tile 3
    beats the block: the ray now fires SW at tile 9 and converts it to the
    Deck (both clients did; the static server fired E at empty 7). Turn 4
    the human's card 47 (N,NE) on tile 13 then points N at a Deck card that points
    back -- exactly ONE battle, which both clients found and the server did
    not. With POL_TM_ROTATING_STEP=0 the old behaviour returns and turn 4
    finds nothing (the freeze).
    """
    ok = True
    key = ("#TMSELFTEST", 96)

    class _Rig(object):
        def __init__(self, script):
            self.script, self.i = list(script), 0

        def randrange(self, bound):
            v = self.script[self.i % len(self.script)]
            self.i += 1
            return bound - 1 if v else 0

    saved_board, saved_turn = dict(boardrules._MATCH_BOARD), dict(boardrules._MATCH_TURN)
    saved_pend, saved_push = dict(placement._PENDING_BATTLE), dict(pushqueue._PUSHES)
    saved_step = os.environ.get("POL_TM_ROTATING_STEP")
    try:
        for step, want4 in (("1", [9]), ("0", [])):
            os.environ["POL_TM_ROTATING_STEP"] = step
            boardrules._MATCH_BOARD[key] = {6: tmbattle.object_card(15)}
            boardrules._MATCH_TURN[key] = {"turn": 0, "active": 1, "n": 2}
            # turn 0: the human (seat 1) card 58 on tile 12 -- no battle
            placement._apply_placement(key[0], key[1], 2, 1, 12,
                             b"58|56|1|56|45|14|7|2", rnd=_Rig([1, 1, 0, 0]))
            # turn 1: Deck (seat 0) card 148 on tile 9: beats the block, loses
            # to 12 -> 9 becomes the human's.
            boardrules._MATCH_TURN[key]["turn"] = 1
            # Live the Deck CHOSE the block first (@BattleSelect=/B=6), so this
            # turn rides the resumable resolver with that choice; the second
            # battle (vs 12) is then the single follow-up, lost.
            placement._PENDING_BATTLE.pop(key, None)
            placement._begin_placement(key[0], key[1], 2, 0, 9, b"148|10|0|10|10|3|114|255",
                             1, [], None, None, [],
                             rnd=_Rig([1, 1, 0, 0, 0, 0, 1, 1]))
            placement._advance_placement(key[0], key[1], chosen=6)
            if key in placement._PENDING_BATTLE or boardrules._MATCH_BOARD[key][9].owner != 1 \
                    or 12 not in boardrules._MATCH_BOARD[key] \
                    or boardrules._MATCH_BOARD[key][12].owner != 1:
                common._say("FAIL: [step %s] turn 1 (block chosen first, then 12 lost) "
                     "must hand tile 9 to seat 1; pending=%r 9 -> %r"
                     % (step, key in placement._PENDING_BATTLE,
                        boardrules._MATCH_BOARD[key][9].owner)); ok = False
            # turn 2: the human's card 35 on tile 0 -- quiet
            boardrules._MATCH_TURN[key]["turn"] = 2
            placement._apply_placement(key[0], key[1], 2, 1, 0, b"35|20|0|20|20|5|32|255",
                             rnd=_Rig([1, 1, 0, 0]))
            # turn 3: Deck card 24 on tile 3 beats the block -> the ray.
            boardrules._MATCH_TURN[key]["turn"] = 3
            placement._apply_placement(key[0], key[1], 2, 0, 3, b"24|18|1|6|10|2|96|2",
                             rnd=_Rig([1, 1, 0, 0]))
            own9 = boardrules._MATCH_BOARD[key][9].owner
            if (step == "1" and own9 != 0) or (step == "0" and own9 != 1):
                common._say("FAIL: [step %s] after turn 3 tile 9 must be seat %s's "
                     "(the ray fires SW on turn 3 when the block turns), got %r"
                     % (step, 0 if step == "1" else 1, own9)); ok = False
            # turn 4: the human's card 47 (N, NE) on tile 13.
            boardrules._MATCH_TURN[key]["turn"] = 4
            b4, _m = placement._apply_placement(key[0], key[1], 2, 1, 13,
                                      b"47|21|1|20|20|4|3|255",
                                      rnd=_Rig([1, 1, 0, 0]))
            if [b[1] for b in b4] != want4:
                common._say("FAIL: [step %s] turn 4 fought %r, expected %r -- %s"
                     % (step, [b[1] for b in b4], want4,
                        "THE FREEZE: both clients found the battle vs tile 9"
                        if step == "1" else
                        "the static block must reproduce the server's own log"))
                ok = False
    finally:
        boardrules._MATCH_BOARD.clear(); boardrules._MATCH_BOARD.update(saved_board)
        boardrules._MATCH_TURN.clear(); boardrules._MATCH_TURN.update(saved_turn)
        placement._PENDING_BATTLE.clear(); placement._PENDING_BATTLE.update(saved_pend)
        pushqueue._PUSHES.clear(); pushqueue._PUSHES.update(saved_push)
        if saved_step is None:
            os.environ.pop("POL_TM_ROTATING_STEP", None)
        else:
            os.environ["POL_TM_ROTATING_STEP"] = saved_step
    return ok


def _selftest_rotating_wraps():
    """THE ROTATING BLOCK'S RAY WRAPS (2026-09-26 VS. COM freeze, vs Akbar the
    Hunter).

    Deal /F=0|2|13|3|0|1|8|0|0|4|0|0|0|7|0|0: a code-13 block (phase 4) on
    tile 2. The board after turn 6 is rebuilt from the log's own map lines;
    turn 7 the human's card 47 (arrows 228, a_raw 32) on tile 7 fights the
    block and flips 10. Phase 4 + 7 = 3 fires NW: off the board for the old
    model ("at tile None"), but the client's ray table wraps, so it lands on
    tile 13 -- and with a_raw 32 it walks a second step to 8. Turn 8 the COM's
    card 35 (arrows 86) on tile 12 points E at 13, whose W arrow points back:
    ONE battle the client waits for. Knobs off, the live log comes back
    verbatim: nothing converted, turn 8 quiet (the freeze).
    """
    ok = True
    key = ("#TMSELFTEST", 97)

    class _Top(object):
        def randrange(self, bound):
            return bound - 1

    def _cd(cid, arrows, owner, ability=0):
        return tmbattle.Card([cid, 16, 0, 16, 16, 0, arrows, 0], owner,
                             ability=ability)

    saved_board, saved_turn = dict(boardrules._MATCH_BOARD), dict(boardrules._MATCH_TURN)
    saved_env = {k: os.environ.get(k) for k in
                 ("POL_TM_ROTATING_WRAP", "POL_TM_ROTATING_REACH")}
    try:
        for wrap, reach, want7, want8 in (
                ("1", "1", {13: 0, 8: 0, 10: 0}, [13]),
                ("1", "0", {13: 0, 8: 1, 10: 0}, [13]),
                ("0", "0", {13: 1, 8: 1, 10: 0}, [])):
            os.environ["POL_TM_ROTATING_WRAP"] = wrap
            os.environ["POL_TM_ROTATING_REACH"] = reach
            boardrules._MATCH_BOARD[key] = {
                1: tmbattle.object_card(2), 2: tmbattle.object_card(13),
                5: tmbattle.object_card(1),
                3: _cd(89, 56, 0, ability=1), 15: _cd(48, 84, 0),
                4: _cd(83, 78, 1), 8: _cd(45, 78, 1),
                9: _cd(27, 45, 1, ability=2), 10: _cd(136, 45, 1),
                13: _cd(80, 238, 1)}
            boardrules._MATCH_TURN[key] = {"turn": 7, "active": 0, "n": 2}
            b7, _m = placement._apply_placement(key[0], key[1], 2, 0, 7,
                                      b"47|32|0|20|20|0|228|255", rnd=_Top())
            got7 = {t: boardrules._MATCH_BOARD[key][t].owner for t in want7}
            if [b[1] for b in b7] != [2] or got7 != want7 \
                    or boardrules._MATCH_BOARD[key][2].owner != tmbattle.OWNER_BLOCK:
                common._say("FAIL: [wrap %s reach %s] turn 7 fought %r, owners %r, "
                     "expected [2] and %r" % (wrap, reach, [b[1] for b in b7],
                                              got7, want7)); ok = False
            boardrules._MATCH_TURN[key]["turn"] = 8
            b8, _m = placement._apply_placement(key[0], key[1], 2, 1, 12,
                                      b"35|20|0|20|20|0|86|255", rnd=_Top())
            if [b[1] for b in b8] != want8:
                common._say("FAIL: [wrap %s reach %s] turn 8 fought %r, expected %r -- %s"
                     % (wrap, reach, [b[1] for b in b8], want8,
                        "THE FREEZE: the client fights tile 13 here"
                        if want8 else "the old model must reproduce the live log"))
                ok = False
    finally:
        boardrules._MATCH_BOARD.clear(); boardrules._MATCH_BOARD.update(saved_board)
        boardrules._MATCH_TURN.clear(); boardrules._MATCH_TURN.update(saved_turn)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ok


def _selftest_colorshift_ends():
    """THE CODE-8 FREEZE, replayed (2026-09-24T22:53Z, 2P VS. COM).

    Board /F=0|0|1|0|0|0|8|0|0|0|0|8|0|5|0|4, /R=189|254|... (occ 1 = 254,
    so a code 8 hands its neighbours to the NON-attacker). Turn 0: card 90
    (arrows 255) on tile 7, targets 6 and 11, player picks 11 -- the block
    hands tile 7 to seat 1. Turn 1: the COM's card 46 (arrows 89) on tile 1
    points SE at tile 6. The client still has the block there and fights it.

    Knob on: turn 0 stops after 11, tile 6 stands, turn 1 fights tile 6.
    Knob off (the twin): turn 0 also consumes 6 and turn 1 fights nothing --
    the freeze as the live log showed it. The same turn 0 through `_apply_placement`
    (the COM's own path) must stop the same way.
    """
    ok = True
    key = ("#TMSELFTEST", 97)
    objs = [0, 0, 1, 0, 0, 0, 8, 0, 0, 0, 0, 8, 0, 5, 0, 4]
    row90 = b"90|18|1|42|31|8|255|255"
    row46 = b"46|18|1|20|31|6|89|255"

    class _Rig(object):
        def __init__(self, script):
            self.script, self.i = list(script), 0

        def randrange(self, bound):
            v = self.script[self.i % len(self.script)]
            self.i += 1
            return bound - 1 if v else 0

    def _deal():
        boardrules._MATCH_OBJECTS[key] = list(objs)
        boardrules._MATCH_BOARD[key] = {t: tmbattle.object_card(c)
                             for t, c in enumerate(objs)
                             if tmbattle.object_card(c) is not None}
        matchstart._TURN_RAND[key] = [189, 254, 187, 147, 119, 55, 8, 230]
        boardrules._MATCH_TURN[key] = {"turn": 0, "active": 0, "n": 2}
        placement._PENDING_BATTLE.pop(key, None)

    saved = [(d, dict(d)) for d in (boardrules._MATCH_BOARD, boardrules._MATCH_OBJECTS, matchstart._TURN_RAND,
                                    boardrules._MATCH_TURN, placement._PENDING_BATTLE, pushqueue._PUSHES)]
    saved_env = {k: os.environ.get(k)
                 for k in ("POL_TM_COLORSHIFT_ENDS", "POL_TM_TURNDATA")}
    try:
        os.environ["POL_TM_TURNDATA"] = "0"
        for knob in ("1", "0"):
            os.environ["POL_TM_COLORSHIFT_ENDS"] = knob
            _deal()
            placement._begin_placement(key[0], key[1], 2, 0, 7, row90, 0, [], None,
                             None, [], rnd=_Rig([1, 1, 0, 0]))
            placement._advance_placement(key[0], key[1], chosen=11)
            b = boardrules._MATCH_BOARD[key]
            if key in placement._PENDING_BATTLE or 11 in b or b[7].owner != (
                    1 if knob == "1" else 0):
                common._say("FAIL: [knob %s] turn 0 must consume 11 and leave tile 7 "
                     "with seat %s; board %r"
                     % (knob, 1 if knob == "1" else 0,
                        {t: c.owner for t, c in b.items()})); ok = False
            if (6 in b) != (knob == "1"):
                common._say("FAIL: [knob %s] chance block 6 must %s after turn 0"
                     % (knob, "STAND (the client never fought it)"
                        if knob == "1" else "be consumed (the old engine)"))
                ok = False
            boardrules._MATCH_TURN[key].update(turn=1, active=1)
            b1, _m = placement._apply_placement(key[0], key[1], 2, 1, 1, row46,
                                      rnd=_Rig([1, 1, 0, 0]))
            want = [6] if knob == "1" else []
            if [x[1] for x in b1] != want:
                common._say("FAIL: [knob %s] turn 1 (COM card 46 on tile 1) fought "
                     "%r, expected %r%s" % (knob, [x[1] for x in b1], want,
                     " -- THE FREEZE: the client fights the '?' on tile 6"
                     if knob == "1" else "")); ok = False
        # The COM's own resolver, same turn 0: next_defender picks 11 too.
        os.environ["POL_TM_COLORSHIFT_ENDS"] = "1"
        _deal()
        b0, _m = placement._apply_placement(key[0], key[1], 2, 0, 7, row90,
                                  rnd=_Rig([1, 1, 0, 0]))
        b = boardrules._MATCH_BOARD[key]
        if [x[1] for x in b0] != [11] or 6 not in b or b[7].owner != 1:
            common._say("FAIL: _apply_placement must stop after the block on 11 hands "
                 "tile 7 away; fought %r, board %r"
                 % ([x[1] for x in b0], {t: c.owner for t, c in b.items()}))
            ok = False
    finally:
        for d, v in saved:
            d.clear(); d.update(v)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ok


def _selftest_block_no_verdict():
    """THE 2026-09-26 VS. COM FREEZE, replayed (vs Flower Girl Natasha,
    2P). A LOST roll against a chance block is not a loss.

    /F=7|0|4|1|12|0|0|0|2|3|6|3|6|0|4|0, /R=180|234|145|114|21|220|57|73; the
    rolls are the live game's own `@BattleData` values. T2 the COM's card 4 on tile 5
    fights rotating 4 and chance 0 and "loses" to 0. Client: no verdict
    (0xCF68E), tile 5 stays seat 1's; T3 the human then loses tile 10 to
    seat 1, T4 the COM's card on 6 touches only its own cards, and T5 the
    human's card 5 on tile 9 has ONE target (12) -- auto-selected, no
    @BattleSelect. Knob off, the live log comes back: 5/10/6 go to owner 4 and
    T5 parks on [5, 6, 12], the freeze.
    """
    ok = True
    key = ("#TMSELFTEST", 96)
    objs = [7, 0, 4, 1, 12, 0, 0, 0, 2, 3, 6, 3, 6, 0, 4, 0]
    # (attacker id, defender id) -> the live /A= and /D= (raw, roll, sel, mult)
    rolls = {(9, 0x8006): ((6, 10, 1, 4), (1, 0, 4, 1)),
             (4, 0x8009): ((5, 1, 1, 1), (1, 0, 4, 1)),
             (4, 0x8007): ((5, 0, 1, 1), (1, 1, 4, 1)),
             (6, 4): ((8, 2, 1, 1), (13, 7, 8, 1)),
             (3, 6): ((5, 2, 1, 1), (4, 1, 8, 1)),
             (3, 4): ((5, 1, 1, 1), (13, 5, 8, 1)),
             (5, 0x8006): ((7, 5, 1, 1), (1, 0, 4, 1))}

    def _scripted(att, dfn, rnd, **_kw):
        a, d = rolls[(int(att["id"]), int(dfn["id"]))]
        return {"a_raw": a[0], "a_roll": a[1], "a_sel": a[2], "a_mult": a[3],
                "d_raw": d[0], "d_roll": d[1], "d_sel": d[2], "d_mult": d[3]}

    def _owners():
        return {t: c.owner for t, c in sorted(boardrules._MATCH_BOARD[key].items())}

    def _com(turn, tile, row):
        boardrules._MATCH_TURN[key] = {"turn": turn, "active": 1, "n": 2}
        return [b[1] for b in placement._apply_placement(key[0], key[1], 2, 1, tile,
                                               row)[0]]

    def _human(turn, tile, row):
        boardrules._MATCH_TURN[key] = {"turn": turn, "active": 0, "n": 2}
        placement._begin_placement(key[0], key[1], 2, 0, tile, row, turn, [], None,
                         None, [])
        return placement._advance_placement(key[0], key[1])

    saved = [(d, dict(d)) for d in (boardrules._MATCH_BOARD, boardrules._MATCH_OBJECTS, matchstart._TURN_RAND,
                                    boardrules._MATCH_TURN, placement._PENDING_BATTLE, pushqueue._PUSHES)]
    saved_env = {k: os.environ.get(k)
                 for k in ("POL_TM_BLOCK_NO_VERDICT", "POL_TM_TURNDATA")}
    saved_resolve = tmbattle.resolve
    try:
        os.environ["POL_TM_TURNDATA"] = "0"
        tmbattle.resolve = _scripted
        for knob in ("1", "0"):
            os.environ["POL_TM_BLOCK_NO_VERDICT"] = knob
            boardrules._MATCH_OBJECTS[key] = list(objs)
            boardrules._MATCH_BOARD[key] = {t: tmbattle.object_card(c)
                                 for t, c in enumerate(objs)
                                 if tmbattle.object_card(c) is not None}
            matchstart._TURN_RAND[key] = [180, 234, 145, 114, 21, 220, 57, 73]
            placement._PENDING_BATTLE.pop(key, None)
            _com(0, 15, b"9|6|0|6|2|1|84|255")
            _human(1, 11, b"9|6|0|6|2|1|126|255")
            f2 = _com(2, 5, b"4|5|0|2|3|1|200|255")
            _human(3, 10, b"6|8|1|6|4|1|130|255")
            f4 = _com(4, 6, b"3|5|1|2|3|1|84|255")
            o4 = _owners()
            done = _human(5, 9, b"5|7|0|6|5|1|39|255")
            o5 = _owners()
            if knob == "1":
                want4 = {3: 4, 4: 4, 5: 1, 6: 1, 8: 4, 10: 1, 11: 0, 12: 4,
                         15: 0}
                if f2 != [4, 0] or f4 != [] or o4 != want4:
                    common._say("FAIL: [knob 1] T2 fought %r (want [4, 0]), T4 fought "
                         "%r (want [] -- tiles 5 and 10 are the COM's own), "
                         "board after T4 %r, want %r" % (f2, f4, o4, want4))
                    ok = False
                if done is not True or key in placement._PENDING_BATTLE or 12 in o5 \
                        or any(o5.get(t) != 0 for t in (5, 6, 9, 10)):
                    common._say("FAIL: [knob 1] T5 must fight chance block 12 alone "
                         "(no @BattleSelect; the client auto-selects) and "
                         "flip 5/6/10; resolved=%r board %r" % (done, o5))
                    ok = False
            else:
                want4 = {3: 4, 4: 4, 5: 4, 6: 4, 8: 4, 10: 1, 11: 0, 12: 4,
                         15: 0}
                if f2 != [4, 0] or f4 != [10, 5] or o4 != want4:
                    common._say("FAIL: [knob 0] must reproduce the live log: T2 %r, T4 %r, "
                         "board %r, want [4, 0] / [10, 5] / %r"
                         % (f2, f4, o4, want4)); ok = False
                if done is not False or key not in placement._PENDING_BATTLE:
                    common._say("FAIL: [knob 0] T5 must park on [5, 6, 12] as the live game "
                         "did (got resolved=%r)" % (done,)); ok = False
    finally:
        tmbattle.resolve = saved_resolve
        for d, v in saved:
            d.clear(); d.update(v)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return ok


def _selftest_replay_3p():
    """THE 3-PLAYER FREEZE, replayed (2026-09-06T23:24-23:29Z).

    The fixture is the frozen game verbatim -- seats 0=member 18, 1=member 3,
    2=member 11, every `@PutCard /D=` row and every `@BattleData` verdict out
    of authserv.log -- driven through `_apply_placement` with the rolls rigged
    to the recorded outcomes. Two things are pinned:

      * `POL_TM_COMBO=placed` (the old engine) reproduces the server's own
        `board map after` lines to the tile and finds NO battle on turn 13 --
        the freeze, as the log shows it.
      * `defeated` (the default) diverges at turn 7 and finds exactly ONE
        battle on turn 13, tile 5 vs tile 11 -- which is what BOTH passive
        clients did (`-BattleCheck` -> `-Battle:Select=1` -> `-BattleStart:8`,
        then parked; shim-CASPC.log / shim-STEAMDECK.1.log). Tile 11 is seat
        1's from turn 8 on: the card taken on tile 7 (arrows N,NE,SE,S,SW)
        points SW at 11, and 11 was the loser's.
    """
    ok = True
    key = ("#TMSELFTEST", 97)
    A, D = tmbattle.ATTACKER, tmbattle.DEFENDER
    # (seat, tile, row, (verdict, defender tile) or None)
    game = [
        (2, 4, b"24|16|1|6|10|2|96|2", None),
        (0, 9, b"142|15|1|43|46|9|137|255", None),
        (1, 8, b"89|31|0|60|25|10|7|255", (A, 4)),
        (2, 2, b"26|16|1|5|9|2|40|255", None),
        (0, 3, b"89|31|0|60|25|10|7|255", (A, 4)),
        (1, 7, b"39|34|1|13|18|5|59|255", None),
        (2, 14, b"3|6|1|5|7|2|130|255", None),
        (0, 11, b"86|25|0|30|20|6|130|12", (A, 7)),
        (1, 12, b"59|45|1|45|56|13|163|255", (A, 7)),
        (2, 1, b"1|5|0|6|5|2|26|3", None),
        (0, 6, b"140|23|1|28|31|7|76|255", (D, 12)),
        (1, 0, b"86|29|0|32|11|6|40|255", None),
        (2, 10, b"11|9|1|8|14|3|8|255", None),
        (0, 5, b"46|28|1|15|30|6|104|255", None),      # the frozen turn
    ]
    # authserv.log 2026-09-06 23:24:48 .. 23:29:23, `board map after`:
    server_maps = [
        "4:2", "4:0 9:0", "4:1 8:1 9:1", "2:2 4:1 8:2 9:1",
        "2:2 3:0 4:0 8:2 9:1", "2:1 3:1 4:0 7:1 8:2 9:1",
        "2:1 3:1 4:0 7:1 8:2 9:1 14:2",
        "2:1 3:1 4:0 7:0 8:2 9:1 11:0 14:2",
        "2:1 3:1 4:0 7:1 8:1 9:1 11:0 12:1 14:2",
        "1:2 2:1 3:1 4:0 7:2 8:1 9:1 11:0 12:1 14:2",
        "1:2 2:1 3:1 4:0 6:1 7:1 8:1 9:1 11:0 12:1 14:2",
        "0:1 1:2 2:1 3:1 4:0 6:1 7:1 8:1 9:1 11:0 12:1 14:2",
        "0:1 1:2 2:1 3:1 4:0 6:1 7:1 8:1 9:1 10:2 11:0 12:1 14:2",
    ]
    # The clients' board (the defeated-card chain), turns 0..12:
    client_maps = list(server_maps[:7]) + [
        "2:0 3:0 4:0 7:0 8:2 9:1 11:0 14:2",                  # turn 7: 2,3 taken
        "2:1 3:1 4:0 7:1 8:1 9:1 11:1 12:1 14:2",             # turn 8: 2,3,11
        "1:2 2:1 3:1 4:0 7:2 8:1 9:1 11:1 12:1 14:2",
        "1:2 2:1 3:1 4:0 6:1 7:2 8:1 9:1 11:1 12:1 14:2",     # turn 10: 7 stays 2
        "0:1 1:2 2:1 3:1 4:0 6:1 7:2 8:1 9:1 11:1 12:1 14:2",
        "0:1 1:2 2:1 3:1 4:0 6:1 7:2 8:1 9:1 10:2 11:1 12:1 14:2",
    ]

    class _Rig(object):
        """resolve() draws two randrange(0x8000) per side: bound-1 for the
        side meant to win, 0 for the other (the stat moduli here never
        divide 0x7FFF, so the winner's roll is > 0)."""
        def __init__(self, script):
            self.script, self.i = list(script), 0

        def randrange(self, bound):
            v = self.script[self.i % len(self.script)]
            self.i += 1
            return bound - 1 if v else 0

    def _fmt(board):
        return " ".join("%d:%s" % (t, board[t].owner) for t in sorted(board))

    def _run(mode):
        saved_mode = os.environ.get("POL_TM_COMBO")
        os.environ["POL_TM_COMBO"] = mode
        try:
            boardrules._MATCH_BOARD[key] = {}
            out = []
            for turn, (seat, tile, row, v) in enumerate(game):
                rig = _Rig([0, 0, 1, 1] if v and v[0] == D else [1, 1, 0, 0])
                battles, _m = placement._apply_placement(key[0], key[1], 3, seat, tile,
                                               row, rnd=rig)
                out.append(([(b[1], b[3]) for b in battles],
                            _fmt(boardrules._MATCH_BOARD[key])))
            return out
        finally:
            if saved_mode is None:
                os.environ.pop("POL_TM_COMBO", None)
            else:
                os.environ["POL_TM_COMBO"] = saved_mode

    saved_board, saved_combo = dict(boardrules._MATCH_BOARD), dict(scoring._MATCH_COMBO)
    try:
        for mode, maps, want13 in (("placed", server_maps, []),
                                   ("defeated", client_maps, [11])):
            got = _run(mode)
            for turn, (seat, tile, row, v) in enumerate(game[:13]):
                fights, board = got[turn]
                want = [(v[1], v[0])] if v else []
                if fights != want:
                    common._say("FAIL: [%s] turn %d (seat %d, tile %d) fought %r, the "
                         "log had %r" % (mode, turn, seat, tile, fights, want))
                    ok = False
                if board != maps[turn]:
                    common._say("FAIL: [%s] board after turn %d is\n  %s\nexpected\n  %s"
                         % (mode, turn, board, maps[turn])); ok = False
            f13 = [t for t, _v in got[13][0]]
            if f13 != want13:
                common._say("FAIL: [%s] turn 13 (card 46, arrows 104, tile 5) fought "
                     "%r, expected %r -- %s"
                     % (mode, f13, want13,
                        "the old engine must reproduce the freeze (no battle "
                        "where every client found one)" if mode == "placed"
                        else "THE FREEZE: both clients ran -BattleCheck -> "
                        "-Battle:Select=1 on tile 5 vs tile 11 and parked for "
                        "a @BattleData the server never sent (2026-09-06T23:29Z)"))
                ok = False
    finally:
        boardrules._MATCH_BOARD.clear(); boardrules._MATCH_BOARD.update(saved_board)
        scoring._MATCH_COMBO.clear(); scoring._MATCH_COMBO.update(saved_combo)
    return ok


def _selftest_abandon_cleanup():
    """A CRASHED match must not poison the member's next game.

    Three shapes, each the live one: the abandon primitive seat by seat (the
    others keep the match and the departed seat is played by the server,
    the last human wipes it); the DROP path (`release_seats` freeing a seat
    under a running match retires the roster entry -- 23:30:45Z); and the
    reported case end to end: a member holding a stale 3-seat roster starts
    a VS. COM game, is dealt a COM hand, places a card, acks turn 1 and THE
    COM PLAYS. With `POL_TM_ABANDON_CLEANUP=0` the roster survives
    `@GameENC=` and this fails -- the positive control.
    """
    ok = True
    chan, index = "#TMABANDON", 1
    key = (chan, index)
    A, B, C = "abndA", "abndB", "abndC"
    seats = [(A, 0x11), (B, 0x22), (C, 0x33)]
    stores = (matchmaking._MATCH_ROSTER, boardrules._MATCH_TURN, boardrules._MATCH_BOARD, boardrules._MATCH_HANDS,
              matchmaking._MATCH_STARTED, matchmaking._MATCH_BEGAN, matchmaking._MATCH_BOTS, pushqueue._PUSHES, vscom._COM_GAME,
              vscom._COM_AT, matchmaking._MATCH_PEER, matchstart._CARD_READY, matchmaking._MATCH_ACCEPTS, matchstart._TURN_RAND,
              scoring._MATCH_COMBO, placement._PENDING_BATTLE, watchers._MATCH_WATCHERS, seating._SEATED)
    saved = [(d, dict(d)) for d in stores]
    saved_synth = set(matchmaking._BOT_SYNTH)
    saved_seat_env = os.environ.get("POL_TM_START_SEAT")

    def crashed_match():
        matchmaking._remember_match(chan, index, seats)
        matchmaking._MATCH_STARTED[key] = {pushqueue._push_key(m) for m, _v in seats}
        matchmaking._MATCH_BEGAN[key] = time.time()
        boardrules._MATCH_TURN[key] = {"turn": 13, "active": 0, "n": 3}
        boardrules._MATCH_BOARD[key] = {5: tmbattle.Card(b"46|28|1|15|30|6|104|255", 0)}
        boardrules._MATCH_HANDS[key] = {pushqueue._push_key(m): [b"1|5|0|6|5|2|26|3"]
                             for m, _v in seats}

    def gone(m):
        return matchmaking._match_of(m) == (None, None, [])

    try:
        for d in stores:
            d.clear()
        # --- 1. the primitive, seat by seat -------------------------------
        crashed_match()
        if not tableaudit._match_abandoned(A, "selftest: connection dropped"):
            common._say("FAIL: _match_abandoned must retire a held roster entry")
            ok = False
        if not gone(A):
            common._say("FAIL: the crashed member's roster entry must be retired "
                 "(_match_of(18) kept answering the dead 3-seat game)"); ok = False
        if matchmaking._match_of(B)[:2] != (chan, index) or matchmaking._match_of(C)[:2] != (chan, index):
            common._say("FAIL: the other players must keep the running match"); ok = False
        if not turns._is_bot(chan, index, A):
            common._say("FAIL: while others remain, the departed seat is played by the "
                 "server (the @Break=/@GameExit= arm)"); ok = False
        if key not in boardrules._MATCH_TURN:
            common._say("FAIL: the running match must survive the first departure")
            ok = False
        tableaudit._match_abandoned(B, "selftest: connection dropped")
        tableaudit._match_abandoned(C, "selftest: connection dropped")
        if (key in boardrules._MATCH_TURN or key in boardrules._MATCH_BOARD or key in matchmaking._MATCH_STARTED
                or key in matchmaking._MATCH_BEGAN or key in boardrules._MATCH_HANDS
                or any(v[:2] == key for v in matchmaking._MATCH_ROSTER.values())):
            common._say("FAIL: when the last human is gone the table's match state "
                 "must be wiped (turn/board/hands/started/began/roster)")
            ok = False
        # --- 2. the DROP path: a seat freed under a running match ---------
        crashed_match()
        seating._SEATED[chan] = {index: list(seats)}
        tableaudit.release_seats(A, chan)
        if not gone(A):
            common._say("FAIL: release_seats freeing a seat under the member's match "
                 "must retire their roster entry (the 23:30:45Z drop)"); ok = False
        if matchmaking._match_of(B)[:2] != (chan, index):
            common._say("FAIL: a released seat must not wipe the others' match")
            ok = False
        # --- 3. the reported case: stale roster -> VS. COM -> the COM plays -
        for d in stores:
            d.clear()
        crashed_match()                        # the crash was never processed
        os.environ["POL_TM_START_SEAT"] = "0"
        dispatch.handle_line(b"41001200@GameENC=/NN=AB12CD4A7D80FA66/HID=0/Dm=0/Vol=0"
                    b"/CN=/HN=4", member_id=A, peer_nick=b"UTESTPEER")
        if not gone(A):
            common._say("FAIL: @GameENC= must retire a stale PvP roster -- left "
                 "standing it is the COM-never-plays state"); ok = False
        # The live binding `_bind_com_table` makes from a decodable peer.
        _k = pushqueue._push_key(A)
        vscom._COM_GAME.setdefault(_k, {})["table"] = (chan, 2)
        vscom._COM_GAME[_k]["member"] = A
        vscom._COM_AT[(chan, 2)] = _k
        dispatch.handle_line(b"43000600@ComGame=/Rule=0|1|1|1|3|0|0|0/Com=9",
                    member_id=A, peer_nick=b"UTESTPEER")
        if vscom._com_n(A) != 2:
            common._say("FAIL: /Com=9 is one COM opponent -> 2 players, got %r"
                 % (vscom._com_n(A),)); ok = False
        dispatch.handle_line(b"43000700@CardSelect=/C=5"
                    b"@N0=/D=89|31|0|60|25|10|7|255"
                    b"@N1=/D=140|23|1|28|31|7|76|255"
                    b"@N2=/D=86|25|0|30|20|6|130|12"
                    b"@N3=/D=35|20|0|20|20|5|17|255"
                    b"@N4=/D=39|34|1|13|18|5|59|255",
                    member_id=A, peer_nick=b"UTESTPEER")
        ck = (None, _k)
        if len((boardrules._MATCH_HANDS.get(ck) or {}).get(("com", 1)) or []) != 5:
            common._say("FAIL: the COM must be dealt a hand after a crashed PvP "
                 "match, got %r" % ({k: len(v) for k, v in
                                     (boardrules._MATCH_HANDS.get(ck) or {}).items()},))
            ok = False
        _st = boardrules._MATCH_TURN.get(ck) or {}
        if _st.get("turn") != 0 or _st.get("active") != 0:
            common._say("FAIL: the COM game must deal turn 0 to the human (START_SEAT "
                 "0), got %r" % (_st,)); ok = False
        dispatch.handle_line(b"43000A00@PutData=/P=0/H=0/F=15", member_id=A,
                    peer_nick=b"UTESTPEER")
        if (boardrules._MATCH_TURN.get(ck) or {}).get("await_ack") != 1:
            common._say("FAIL: the human's put must announce COM turn 1 ack-gated, "
                 "got %r" % (boardrules._MATCH_TURN.get(ck),)); ok = False
        a1 = dispatch.handle_line(b"43000901@TurnData=/Ans=0", member_id=A,
                         peer_nick=b"UTESTPEER")
        a1l = a1 if isinstance(a1, list) else ([a1] if a1 else [])
        if not any(ln.startswith(b"43000A01") and b"@PutCard=/A=1" in ln
                   for ln in a1l):
            common._say("FAIL: THE COM MUST PLAY TURN 1 on the client's ack after an "
                 "abandoned PvP match (43000A01 @PutCard=/A=1), got %r -- the "
                 "23:35:47Z 'no answer for this command' shape" % (a1l,))
            ok = False
    except Exception as exc:
        import traceback
        common._say("FAIL: abandon-cleanup selftest raised %r\n%s"
             % (exc, traceback.format_exc())); ok = False
    finally:
        for d, v in saved:
            d.clear(); d.update(v)
        matchmaking._BOT_SYNTH.clear(); matchmaking._BOT_SYNTH.update(saved_synth)
        if saved_seat_env is None:
            os.environ.pop("POL_TM_START_SEAT", None)
        else:
            os.environ["POL_TM_START_SEAT"] = saved_seat_env
    return ok


def _selftest_battleselect():
    """The multi-defender `@BattleSelect` handshake (the 2026-09-03 live hang).

    A placement that can attack MORE THAN ONE back-pointing defender must PARK
    the resolver (client scene state 3 -- the picker -- sends `@BattleSelect=
    /B=<tile>` and waits), then resume on the chosen tile, auto-resolve any
    single follow-up, and only THEN finish the turn. A single- or zero-battle
    placement must never park.

    Board (4x4): the placer's card on tile 5 has E+S arrows into two enemies
    that both point back -- tile 6 (W arrow) and tile 9 (N arrow) -- so contest
    yields two BATTLE marks. Rigged rolls make the placer win both.
    """
    ok = True
    key = ("#TMBSELTEST", 7)
    m0, m1 = "bsel0", "bsel1"
    seats = [(m0, 0), (m1, 1)]
    saved_board = dict(boardrules._MATCH_BOARD)
    saved_turn = dict(boardrules._MATCH_TURN)
    saved_pend = dict(placement._PENDING_BATTLE)
    saved_push = {k: list(v) for k, v in pushqueue._PUSHES.items()}
    saved_rand = dict(matchstart._TURN_RAND)

    class _Win(object):
        # resolve() draws exactly four randrange(0x8000) values per battle (no
        # ability multiplier here, ability 0), attacker first then defender.
        # The pattern gives the attacker both HIGH draws and the defender both
        # ZERO, so the placer wins every battle by arithmetic, not luck. The
        # defender's stat is clamped to >= 1 (0xD334D), so a zeroed defender
        # would still ROLL and tie -- hence high-vs-zero, not stat tricks.
        def __init__(self):
            self.i = 0

        def randrange(self, bound):
            v = (0x7FFF, 0x7FFF, 0, 0)[self.i % 4]
            self.i += 1
            return v % bound if bound else 0

    try:
        boardrules._MATCH_BOARD.clear(); boardrules._MATCH_TURN.clear(); placement._PENDING_BATTLE.clear()
        pushqueue._PUSHES.clear(); matchstart._TURN_RAND.clear()
        boardrules._MATCH_BOARD[key] = {
            6: tmbattle.Card(b"2|0|0|0|0|0|64|0", 1),   # W arrow (bit6)
            9: tmbattle.Card(b"3|0|0|0|0|0|1|0", 1),    # N arrow (bit0)
        }
        boardrules._MATCH_TURN[key] = {"turn": 0, "active": 0, "n": 2}

        # Placed card: tile 5, E(bit2)+S(bit4) arrows = 0x14 = 20, strong.
        placed = placement._begin_placement(key[0], key[1], 2, 0, 5,
                                  b"1|15|0|0|0|0|20|0", 0, seats, None, m0,
                                  seats, rnd=_Win())
        if not placed:
            common._say("FAIL: @BattleSelect selftest could not place the card")
            return False
        paused = placement._advance_placement(key[0], key[1])
        if paused is not False:
            common._say("FAIL: a 2-defender placement must PARK for @BattleSelect, "
                 "not resolve itself (got %r)" % (paused,)); ok = False
        if key not in placement._PENDING_BATTLE:
            common._say("FAIL: a parked placement must be recorded in _PENDING_BATTLE")
            ok = False
        _bd0 = [e for e in (pushqueue._PUSHES.get(pushqueue._push_key(m1)) or [])
                if b"@BattleData=" in e[1]]
        if _bd0:
            common._say("FAIL: no @BattleData may be pushed while parked -- the client "
                 "has not chosen yet (got %d)" % len(_bd0)); ok = False

        # The player chooses tile 6. Winning it leaves tile 9 as the lone
        # remaining battle, which auto-resolves (no second @BattleSelect), then
        # the turn finishes.
        done = placement._advance_placement(key[0], key[1], chosen=6)
        if done is not True:
            common._say("FAIL: resolving the chosen battle (and its single follow-up) "
                 "must COMPLETE the placement, got %r" % (done,)); ok = False
        if key in placement._PENDING_BATTLE:
            common._say("FAIL: a completed placement must clear _PENDING_BATTLE")
            ok = False
        _bd = [e for e in (pushqueue._PUSHES.get(pushqueue._push_key(m1)) or [])
               if b"@BattleData=" in e[1]]
        if len(_bd) != 2:
            common._say("FAIL: two battles (chosen tile 6 + auto tile 9) must push two "
                 "@BattleData, got %d" % len(_bd)); ok = False
        board = boardrules._MATCH_BOARD.get(key) or {}
        if not all(board.get(t) is not None and board[t].owner == 0
                   for t in (5, 6, 9)):
            common._say("FAIL: the placer won both, so tiles 5/6/9 must all be seat 0, "
                 "got %r" % ({t: (board.get(t) and board[t].owner)
                              for t in (5, 6, 9)},)); ok = False
        # A @TurnData for the next turn must have been announced by _finish().
        _td = [e for e in (pushqueue._PUSHES.get(pushqueue._push_key(m1)) or [])
               if b"@TurnData=" in e[1]]
        if not _td:
            common._say("FAIL: finishing the placement must announce the next turn")
            ok = False

        # A ONE-defender placement must resolve without ever parking.
        placement._PENDING_BATTLE.clear(); pushqueue._PUSHES.clear()
        boardrules._MATCH_BOARD[key] = {9: tmbattle.Card(b"3|0|0|0|0|0|1|0", 1)}
        boardrules._MATCH_TURN[key] = {"turn": 2, "active": 0, "n": 2}
        placement._begin_placement(key[0], key[1], 2, 0, 5, b"1|15|0|0|0|0|16|0", 2,
                         seats, None, m0, seats, rnd=_Win())   # S arrow only
        if placement._advance_placement(key[0], key[1]) is not True:
            common._say("FAIL: a single-defender placement must resolve immediately, "
                 "never park"); ok = False
        if key in placement._PENDING_BATTLE:
            common._say("FAIL: a single-defender placement left a pending battle")
            ok = False
    finally:
        boardrules._MATCH_BOARD.clear(); boardrules._MATCH_BOARD.update(saved_board)
        boardrules._MATCH_TURN.clear(); boardrules._MATCH_TURN.update(saved_turn)
        placement._PENDING_BATTLE.clear(); placement._PENDING_BATTLE.update(saved_pend)
        pushqueue._PUSHES.clear(); pushqueue._PUSHES.update(saved_push)
        matchstart._TURN_RAND.clear(); matchstart._TURN_RAND.update(saved_rand)
    return ok


def _selftest_locked():
    """`selftest` with the board pinned -- see its docstring."""
    try:
        import tmroom
    except Exception:
        return selftest_run._selftest_all()                 # no roster module, no hazard
    saved = (tmroom._KEY, tmroom._OWNER[0],
             json.loads(json.dumps(tmroom._RULES)))
    tmpdir = tempfile.mkdtemp(prefix="tm-selftest-")
    try:
        tmroom._KEY = "tm:selftest:%s:roster" % os.path.basename(tmpdir)
        return selftest_run._selftest_all()
    finally:
        tmroom._KEY, tmroom._OWNER[0] = saved[0], saved[1]
        tmroom._RULES.clear(); tmroom._RULES.update(saved[2])
        tmroom._SHARED.forget()
        shutil.rmtree(tmpdir, ignore_errors=True)


def _selftest_accept_persist():
    """The accept quorum survives a process restart (2026-09-03T22:57Z prod).

    Two accepts with `_MATCH_ACCEPTS` wiped between them must still reach
    quorum; a persisted set for a DIFFERENT roster, or an old one, must not;
    and the deploy-gate marker must count a pending accept."""
    ok = True
    tmpdir = tempfile.mkdtemp(prefix="tm-accepts-")
    saved_env = os.environ.get("POL_DATA_DIR")
    saved_key = os.environ.get("POL_TM_ACCEPTS_KEY")
    saved = dict(matchmaking._MATCH_ACCEPTS)
    chan, index = "#TMACCTEST", 3
    seats = [("acc0", 0), ("acc1", 1)]
    try:
        os.environ["POL_DATA_DIR"] = tmpdir
        os.environ["POL_TM_ACCEPTS_KEY"] = "tm:selftest:%s:accepts" % os.path.basename(tmpdir)
        matchmaking._MATCH_ACCEPTS.clear()
        if matchmaking._note_accept(chan, index, "acc0", seats):
            common._say("FAIL: the first accept must HOLD the board"); ok = False
        try:
            with open(os.path.join(tmpdir, "tm-matches-live.json")) as f:
                cnt = int(json.load(f).get("count") or 0)
        except (OSError, ValueError):
            cnt = -1
        if cnt < 1:
            common._say("FAIL: a pending accept must count as a live match for the "
                 "deploy gate (count %r)" % cnt); ok = False
        matchmaking._MATCH_ACCEPTS.clear()                       # <- the restart
        got = matchmaking._note_accept(chan, index, "acc1", seats)
        if sorted(got) != ["acc0", "acc1"]:
            common._say("FAIL: the second accept after a restart must reach quorum "
                 "from the persisted set, got %r" % (got,)); ok = False
        if matchmaking._accepts_load().get(matchmaking._accepts_key(chan, index)):
            common._say("FAIL: quorum must clear the persisted entry"); ok = False
        matchmaking._MATCH_ACCEPTS.clear()
        matchmaking._note_accept(chan, index, "acc0", seats)
        matchmaking._MATCH_ACCEPTS.clear()
        if matchmaking._note_accept(chan, index, "acc1", [("acc1", 1), ("acc2", 2)]):
            common._say("FAIL: a persisted set for another roster must be ignored")
            ok = False
        matchmaking._MATCH_ACCEPTS.clear()
        matchmaking._note_accept(chan, index, "acc0", seats)
        data = matchmaking._accepts_load()
        data[matchmaking._accepts_key(chan, index)]["stamp"] = time.time() - 7200
        matchmaking._accepts_store(data)
        matchmaking._MATCH_ACCEPTS.clear()
        if matchmaking._note_accept(chan, index, "acc1", seats):
            common._say("FAIL: a stale persisted accept must be ignored"); ok = False
        matchmaking._MATCH_ACCEPTS.clear()
        matchmaking._note_accept(chan, index, "acc0", seats)
        matchmaking._remember_match(chan, index, seats)          # a fresh announcement
        if matchmaking._accepts_load().get(matchmaking._accepts_key(chan, index)):
            common._say("FAIL: a fresh announcement must forget the persisted set")
            ok = False
    finally:
        matchmaking._MATCH_ACCEPTS.clear(); matchmaking._MATCH_ACCEPTS.update(saved)
        tmstore.kv.delete(matchmaking._accepts_path())
        for name, value in (("POL_DATA_DIR", saved_env), ("POL_TM_ACCEPTS_KEY", saved_key)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(tmpdir, ignore_errors=True)
    if ok:
        common._say("selftest: accept quorum persists across a restart")
    return ok


def _selftest_bot_seat():
    """A player who leaves a running PvP match is replaced by the server.

    Mid-turn leave on the leaver's OWN turn plays at once; a later bot turn is
    ack-gated and answers the human's `@TurnData=` ack directly; the roster
    shrink from the leaver's unseat must not wipe the match; the departed
    seat pre-declines the rematch vote."""
    ok = True
    chan, index = "#TMBOTTEST", 2
    key = (chan, index)
    m0, m1 = "bot0", "bot1"
    seats = [(m0, 0), (m1, 1)]
    saved = (dict(matchmaking._MATCH_ROSTER), dict(boardrules._MATCH_TURN), dict(boardrules._MATCH_HANDS),
             dict(boardrules._MATCH_BOARD), dict(matchmaking._MATCH_STARTED), dict(matchmaking._MATCH_BOTS),
             {k: list(v) for k, v in pushqueue._PUSHES.items()}, dict(placement._PENDING_BATTLE),
             dict(rematch._MATCH_CONTINUE), set(matchmaking._BOT_SYNTH), dict(matchstart._TURN_RAND),
             dict(scoring._MATCH_COMBO))
    tmpdir = tempfile.mkdtemp(prefix="tm-bot-")
    saved_env = os.environ.get("POL_DATA_DIR")
    row = b"1|15|0|0|0|0|0|0"                       # no arrows: never fights

    def _puts(mid):
        return [e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(mid)) or [])]
    try:
        os.environ["POL_DATA_DIR"] = tmpdir
        for d in (matchmaking._MATCH_ROSTER, boardrules._MATCH_TURN, boardrules._MATCH_HANDS, boardrules._MATCH_BOARD,
                  matchmaking._MATCH_STARTED, matchmaking._MATCH_BOTS, pushqueue._PUSHES, placement._PENDING_BATTLE,
                  rematch._MATCH_CONTINUE, matchstart._TURN_RAND, scoring._MATCH_COMBO):
            d.clear()
        matchmaking._BOT_SYNTH.clear()
        matchmaking._remember_match(chan, index, seats)
        matchmaking._MATCH_STARTED[key] = {pushqueue._push_key(m0), pushqueue._push_key(m1)}
        boardrules._MATCH_HANDS[key] = {pushqueue._push_key(m0): [row] * 5, pushqueue._push_key(m1): [row] * 5,
                             ("deck", 0): [row] * 5, ("deck", 1): [row] * 5}
        boardrules._MATCH_TURN[key] = {"turn": 3, "active": 1, "n": 2}
        boardrules._MATCH_BOARD[key] = {}
        # -- leave on the leaver's own turn: the put goes out at once ---------
        turns._seat_departed(m1, "selftest")
        if not turns._is_bot(chan, index, m1):
            common._say("FAIL: the leaver must become a server-played seat"); ok = False
        p0 = _puts(m0)
        if not any(b"@PutCard=/A=1" in b for b in p0):
            common._say("FAIL: the human must be pushed the departed seat's @PutCard "
                 "(/A=1), got %r" % (p0,)); ok = False
        if not any(b"@TurnData=" in b for b in p0):
            common._say("FAIL: the departed seat's put must announce the next turn")
            ok = False
        if _puts(m1):
            common._say("FAIL: nothing may be pushed to the departed seat, got %r"
                 % (_puts(m1),)); ok = False
        if len(boardrules._MATCH_BOARD.get(key) or {}) != 1:
            common._say("FAIL: the bot's card must be on the board"); ok = False
        # -- the leaver's unseat republishes a shrunken roster ----------------
        matchmaking._remember_match(chan, index, [(m0, 0)])
        if not boardrules._MATCH_TURN.get(key) or not matchmaking._MATCH_BOTS.get(key):
            common._say("FAIL: unseating the leaver must not wipe the running match")
            ok = False
        # -- a later bot turn is ack-gated and answers the ack directly -------
        pushqueue._PUSHES.clear()
        turns._queue_next_turn(chan, index, seats, 5, 1, roster=seats)
        st = boardrules._MATCH_TURN.get(key) or {}
        if st.get("await_ack") != 5 or st.get("bot_seat") != 1:
            common._say("FAIL: a departed seat's turn must arm the ack gate, got %r"
                 % (st,)); ok = False
        if any(b"@PutCard=" in b for b in _puts(m0)):
            common._say("FAIL: the gated put must not go out before the ack"); ok = False
        reply = dispatch._handle_line(protocol._turn_code(protocol.TURNDATA_CMD, 5) + b"@TurnData=/Ans=0",
                             member_id=m0)
        if not reply or not reply.startswith(protocol._turn_code(protocol.PUTCARD_CMD, 5)) \
                or b"@PutCard=/A=1" not in reply:
            common._say("FAIL: the human's ack must be answered with the departed "
                 "seat's @PutCard on (0x43, 10), got %r" % (reply,)); ok = False
        if len(boardrules._MATCH_BOARD.get(key) or {}) != 2:
            common._say("FAIL: the second bot card must be on the board"); ok = False
        # -- the rematch vote: the departed seat has declined ------------------
        boardrules._MATCH_TURN[key]["result_sent"] = True
        creply = dispatch._handle_line(protocol._turn_code(protocol.CONTINUE_CMD, 0) + b"@Continue=/Ans=1",
                              member_id=m0)
        if not creply or b"@Continue=" not in creply:
            common._say("FAIL: with the departed seat pre-declined the vote must "
                 "resolve on the human's answer, got %r" % (creply,)); ok = False
        # -- a leaver DURING CARD SELECT gets a synthetic hand and the deal ---
        for d in (boardrules._MATCH_TURN, boardrules._MATCH_HANDS, boardrules._MATCH_BOARD, matchmaking._MATCH_BOTS,
                  pushqueue._PUSHES, rematch._MATCH_CONTINUE, matchstart._CARD_READY, matchstart._TURN_RAND):
            d.clear()
        matchmaking._BOT_SYNTH.clear()
        matchmaking._remember_match(chan, index, seats)
        matchmaking._MATCH_STARTED[key] = {pushqueue._push_key(m0), pushqueue._push_key(m1)}
        boardrules._MATCH_HANDS[key] = {pushqueue._push_key(m0): [row] * 5, ("deck", 0): [row] * 5}
        matchstart._CARD_READY[key] = {pushqueue._push_key(m0)}
        os.environ["POL_TM_CARDSELECT_LEAVE"] = "bot"    # the 3+-human path
        try:
            turns._seat_departed(m1, "selftest card select")
        finally:
            os.environ.pop("POL_TM_CARDSELECT_LEAVE", None)
        if key not in matchmaking._BOT_SYNTH:
            common._say("FAIL: a card-select leaver must be flagged synthetic"); ok = False
        if not (boardrules._MATCH_HANDS.get(key) or {}).get(pushqueue._push_key(m1)):
            common._say("FAIL: a card-select leaver must be dealt a hand"); ok = False
        if (boardrules._MATCH_TURN.get(key) or {}).get("turn") is None:
            common._say("FAIL: the synthetic pick must complete card select and DEAL")
            ok = False
        if not any(b"@StartData=" in b for b in _puts(m0)):
            common._say("FAIL: the human must be dealt (@StartData) after the "
                 "synthetic pick, got %r" % (_puts(m0),)); ok = False
        st = boardrules._MATCH_TURN.get(key) or {}
        if st.get("active") == 1 and st.get("await_ack") is None:
            common._say("FAIL: when the departed seat starts, its turn must be "
                 "ack-gated"); ok = False
    finally:
        for d, s in ((matchmaking._MATCH_ROSTER, saved[0]), (boardrules._MATCH_TURN, saved[1]),
                     (boardrules._MATCH_HANDS, saved[2]), (boardrules._MATCH_BOARD, saved[3]),
                     (matchmaking._MATCH_STARTED, saved[4]), (matchmaking._MATCH_BOTS, saved[5]),
                     (pushqueue._PUSHES, saved[6]), (placement._PENDING_BATTLE, saved[7]),
                     (rematch._MATCH_CONTINUE, saved[8]), (matchstart._TURN_RAND, saved[10]),
                     (scoring._MATCH_COMBO, saved[11])):
            d.clear(); d.update(s)
        matchmaking._BOT_SYNTH.clear(); matchmaking._BOT_SYNTH.update(saved[9])
        matchstart._CARD_READY.pop(key, None)
        if saved_env is None:
            os.environ.pop("POL_DATA_DIR", None)
        else:
            os.environ["POL_DATA_DIR"] = saved_env
        shutil.rmtree(tmpdir, ignore_errors=True)
    if ok:
        common._say("selftest: a departed seat is played by the server")
    return ok


def _selftest_dataerror():
    """A card select for a match this process has no roster for is answered
    with (0x43, 40) `@DataError=` -- the one message the card-select scene
    reads for "the match is gone" (it then sends `@Quit=` and leaves) -- and
    a roster-known pick is NOT."""
    ok = True
    saved_push = {k: list(v) for k, v in pushqueue._PUSHES.items()}
    saved_ready = dict(matchstart._CARD_READY)
    m = "lostmatch"
    try:
        pushqueue._PUSHES.pop(pushqueue._push_key(m), None)
        dispatch._handle_line(protocol.encode_code(protocol.IN_MATCH_CODE | (protocol.CARDSELECT_SLOT << 16))
                     + b"@CardSelect=/C=0", member_id=m)
        got = [e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(m)) or [])]
        want = protocol._turn_code(protocol.DATAERROR_CMD, 0) + b"@DataError=/No=0"
        if want not in got:
            common._say("FAIL: a no-roster card select must push %r, got %r"
                 % (want, got)); ok = False
        # Positive control: with a roster the pick must NOT be refused.
        pushqueue._PUSHES.pop(pushqueue._push_key(m), None)
        saved_roster = dict(matchmaking._MATCH_ROSTER)
        try:
            matchmaking._remember_match("#TMDETEST", 1, [(m, 0), ("lostmate", 1)])
            dispatch._handle_line(protocol.encode_code(protocol.IN_MATCH_CODE | (protocol.CARDSELECT_SLOT << 16))
                         + b"@CardSelect=/C=0", member_id=m)
            got = [e[1] for e in (pushqueue._PUSHES.get(pushqueue._push_key(m)) or [])]
            if any(b"@DataError=" in b for b in got):
                common._say("FAIL: a card select WITH a roster must not be refused")
                ok = False
        finally:
            matchmaking._MATCH_ROSTER.clear(); matchmaking._MATCH_ROSTER.update(saved_roster)
            matchstart._CARD_READY.pop(("#TMDETEST", 1), None)
    finally:
        pushqueue._PUSHES.clear(); pushqueue._PUSHES.update(saved_push)
        matchstart._CARD_READY.clear(); matchstart._CARD_READY.update(saved_ready)
    if ok:
        common._say("selftest: a lost match is told to the card-select scene")
    return ok


def _selftest_champion():
    """The rank_1 Pack is labelled after the champion: `/SN=` on the shop
    opener, absent without one, and a cp932 name never split mid-character or
    past 16 bytes. The save +0x104 write below exercises `write_field` only --
    that slot is the first DECK TAB and the rewrite path no longer stamps it
    (`_selftest_deck_names`)."""
    ok = True
    saved = os.environ.get("POL_TM_CHAMPION_NAME")
    try:
        os.environ["POL_TM_CHAMPION_NAME"] = ""
        if b"/SN=" in shopdoors._shopinit_body(1):
            common._say("FAIL: no champion, no /SN="); ok = False
        os.environ["POL_TM_CHAMPION_NAME"] = "Fox"
        body = shopdoors._shopinit_body(1)
        if b"/SN=Fox/" not in body and not body.endswith(b"/SN=Fox"):
            common._say("FAIL: the champion's name must ride @Init=/SN=, got %r"
                 % (body,)); ok = False
        if body.index(b"/SN=") > body.index(b"/PM=") if b"/PM=" in body else False:
            common._say("FAIL: /SN= must precede the trailing /PM="); ok = False
        os.environ["POL_TM_CHAMPION_NAME"] = "ABCDEFGHIJKLMNOPQRSTUV"
        if len(shopdoors._champion_name()) != 16:
            common._say("FAIL: the name must be cut to the client's 16 bytes, got %r"
                 % (shopdoors._champion_name(),)); ok = False
        os.environ["POL_TM_CHAMPION_NAME"] = "カス" * 5     # 10 kana = 20 bytes
        _n = shopdoors._champion_name()
        if len(_n) != 16 or len(_n) % 2:
            common._say("FAIL: a double-byte name must be cut on a character boundary, "
                 "got %d bytes" % len(_n)); ok = False
        if tmsave is not None:
            os.environ["POL_TM_CHAMPION_NAME"] = "Fox"
            buf = bytearray(tmsave.build([]))
            tmsave.write_field(buf, shopdoors.SAVE_OFF_CHAMPION, shopdoors.SAVE_CHAMPION_WIDTH,
                               shopdoors._champion_name())
            got = bytes(buf[shopdoors.SAVE_OFF_CHAMPION:shopdoors.SAVE_OFF_CHAMPION + shopdoors.SAVE_CHAMPION_WIDTH])
            if got != b"Fox".ljust(shopdoors.SAVE_CHAMPION_WIDTH, b"\x00"):
                common._say("FAIL: save +0x104 must carry the NUL-padded name, got %r"
                     % (got,)); ok = False
    finally:
        if saved is None:
            os.environ.pop("POL_TM_CHAMPION_NAME", None)
        else:
            os.environ["POL_TM_CHAMPION_NAME"] = saved
    if ok:
        common._say("selftest: the champion names the rank_1 Pack")
    return ok


def _selftest_watch_gap():
    """The observer purge-race gap, and its TWO-SIDED constraint (2026-09-06).

    A watcher's per-turn render (@PutCard/@BattleData/@WFCard) must be DELIVERED
    after the @TurnData that enters its own turn N and before the @TurnData that
    enters turn N+1. Land it before turn N's @TurnData and the client reads a
    card for a turn it has not entered (Errot!!Turn(N-1)!=N -- the original
    after=0 crash); land it after turn N+1's @TurnData and the client has already
    advanced (Errot!!Turn(N+1)!=N -- the crash after=2 CAUSED live).

    This SIMULATES the real drain sequence -- turn N's @TurnData is queued a
    cycle earlier than the render, turn N+1's a beat later -- and asserts the
    ORDER `_pending_pushes` actually hands the watcher, which is what an
    after-value assertion alone got wrong the first time.
    """
    ok = True
    saved_pushes = dict(pushqueue._PUSHES)
    saved_watchers = dict(watchers._MATCH_WATCHERS)
    saved_env = os.environ.get("POL_TM_WATCH_PUT_GAP")
    chan, index, wmid, wpeer = "#TMWATCHGAP", 1, 4242, b"WATCHERPEER"

    def _cmd_of(body):
        try:
            return (int(body[:8], 16) >> 8) & 0xFF
        except (ValueError, TypeError):
            return -1

    def _delivery_order():
        # Drain the watcher's queue reply-by-reply, collecting the cmd order.
        order = []
        for _ in range(12):
            for body in pushqueue._pending_pushes(wmid, wpeer):
                order.append(_cmd_of(body))
        return order

    def _run_one_turn():
        # The WORST case: the watcher has NOT polled since turn N's @TurnData was
        # queued, so all three of turn N's @TurnData, turn N's render, and turn
        # N+1's @TurnData are pending at once -- in that queue order (the order
        # the COM loop and the human-put handler emit them). Correct delivery
        # must be decided by LIST ORDER, which only happens when every one rides
        # the SAME `after`; a render that rides lower jumps its own turn, one
        # that rides higher falls behind the next turn. No intermediate drain,
        # precisely because a watcher's polls are decoupled from the cycle.
        pushqueue._PUSHES.clear()
        watchers._watch_push(chan, index,
                    protocol._turn_code(protocol.TURNDATA_CMD, 3) + b"@TurnData=/A=0",
                    "turn 3 (N)", after=1)
        watchers._watch_push(chan, index,
                    protocol._turn_code(protocol.PUTCARD_CMD, 3) + b"@PutCard=/A=0", "card N")
        watchers._watch_push(chan, index,
                    protocol._turn_code(protocol.BATTLEDATA_CMD, 3) + b"@BattleData=/A=0",
                    "battle N")
        watchers._watch_push(chan, index,
                    protocol._turn_code(protocol.TURNDATA_CMD, 4) + b"@TurnData=/A=1",
                    "turn 4 (N+1)", after=1)
        return _delivery_order()

    try:
        watchers._MATCH_WATCHERS.clear()
        watchers._MATCH_WATCHERS[(chan, index)] = {pushqueue._push_key(wmid): (wmid, wpeer)}

        # --- default gap: turn N's TD, then the render, then turn N+1's TD ---
        os.environ.pop("POL_TM_WATCH_PUT_GAP", None)
        order = _run_one_turn()
        # Expected: TD(N)=9, then PutCard=10 / Battle=12, then TD(N+1)=9.
        put_i = order.index(protocol.PUTCARD_CMD) if protocol.PUTCARD_CMD in order else -1
        bat_i = order.index(protocol.BATTLEDATA_CMD) if protocol.BATTLEDATA_CMD in order else -1
        td_first = order.index(protocol.TURNDATA_CMD) if protocol.TURNDATA_CMD in order else -1
        td_next = (order.index(protocol.TURNDATA_CMD, td_first + 1)
                   if order.count(protocol.TURNDATA_CMD) > 1 else -1)
        if td_first < 0 or td_next < 0 or put_i < 0 or bat_i < 0:
            common._say("FAIL: watcher did not receive both @TurnData plus the render "
                 "-- order %r" % (order,)); ok = False
        else:
            if not (td_first < put_i < td_next):
                common._say("FAIL: @PutCard must land AFTER turn N's @TurnData and "
                     "BEFORE turn N+1's -- order %r (td_N=%d put=%d td_N+1=%d)"
                     % (order, td_first, put_i, td_next)); ok = False
            if not (td_first < bat_i < td_next):
                common._say("FAIL: @BattleData must land inside turn N's window -- "
                     "order %r" % (order,)); ok = False

        # --- after=0 (the original crash) DOES overtake turn N's @TurnData ---
        # A positive control: the bug the fix ends must still be reproducible.
        os.environ["POL_TM_WATCH_PUT_GAP"] = "0"
        order0 = _run_one_turn()
        p0 = order0.index(protocol.PUTCARD_CMD) if protocol.PUTCARD_CMD in order0 else 99
        t0 = order0.index(protocol.TURNDATA_CMD) if protocol.TURNDATA_CMD in order0 else -1
        if not (p0 < t0):
            common._say("FAIL: the positive control is broken -- after=0 should race "
                 "the @PutCard AHEAD of turn N's @TurnData, order %r"
                 % (order0,)); ok = False
    finally:
        pushqueue._PUSHES.clear(); pushqueue._PUSHES.update(saved_pushes)
        watchers._MATCH_WATCHERS.clear(); watchers._MATCH_WATCHERS.update(saved_watchers)
        if saved_env is None:
            os.environ.pop("POL_TM_WATCH_PUT_GAP", None)
        else:
            os.environ["POL_TM_WATCH_PUT_GAP"] = saved_env
    return ok


def _selftest_watch_snapshot_purge():
    """A `@WatchInfo` snapshot must LEAVE NO per-turn body queued behind it.

    KEY: WITH A POSITIVE CONTROL, because an assertion whose fixture cannot
    violate it is not a test (the `/CP=` selftest sat green for nineteen days
    over the opposite behaviour). The fixture builds exactly the live backlog --
    a previous game's tail plus the new game's turn-0 bodies -- and the control
    re-runs it with `POL_TM_WATCH_SNAPSHOT_PURGE=0` and requires the stale put
    to STILL be delivered. If the purge stops working, the control is what says
    so.
    """
    ok = True
    saved_pushes = dict(pushqueue._PUSHES)
    saved_env = os.environ.get("POL_TM_WATCH_SNAPSHOT_PURGE")
    wmid, wpeer = 4243, b"SNAPPEER"

    def _cmd_of(body):
        try:
            return (int(body[:8], 16) >> 8) & 0xFF
        except (ValueError, TypeError):
            return -1

    def _load_backlog():
        # The 2026-09-09 shape: a stale tail the watcher never drained, then the
        # new game's turn-0 stream -- all of it queued BEFORE the handshake.
        pushqueue._PUSHES.clear()
        pushqueue._queue_push(wmid, protocol._turn_code(protocol.TURNDATA_CMD, 8) + b"@TurnData=/A=1",
                    "stale: prior game turn 8", peer=wpeer)
        pushqueue._queue_push(wmid, protocol._turn_code(protocol.RESULTDATA_CMD, 10) + b"@ResultData=/P=1",
                    "stale: prior game result", peer=wpeer)
        pushqueue._queue_push(wmid, protocol._turn_code(protocol.TURNDATA_CMD, 0) + b"@TurnData=/A=1",
                    "stale: new game turn 0", peer=wpeer)
        pushqueue._queue_push(wmid, protocol._turn_code(protocol.PUTCARD_CMD, 0) + b"@PutCard=/A=1/F=5",
                    "stale: THE turn-0 put that crashed it", peer=wpeer)
        pushqueue._queue_push(wmid, protocol._turn_code(protocol.BATTLEDATA_CMD, 0) + b"@BattleData=/A=5",
                    "stale: turn-0 battle", peer=wpeer)
        # NOT per-turn state: must SURVIVE the purge.
        pushqueue._queue_push(wmid, protocol._turn_code(5, 0) + b"@Quit=/No=0/B=0",
                    "not per-turn: @Quit", peer=wpeer)

    def _drain():
        order = []
        for _ in range(12):
            for body in pushqueue._pending_pushes(wmid, wpeer):
                order.append(_cmd_of(body))
        return order

    try:
        os.environ.pop("POL_TM_WATCH_SNAPSHOT_PURGE", None)
        _load_backlog()
        went = watchers._watch_stream_purge(wmid, "selftest")
        if went != 5:
            common._say("FAIL: the snapshot purge must drop the 5 per-turn bodies, "
                 "dropped %d" % went); ok = False
        order = _drain()
        for cmd, name in ((protocol.PUTCARD_CMD, "@PutCard"), (protocol.TURNDATA_CMD, "@TurnData"),
                          (protocol.BATTLEDATA_CMD, "@BattleData"),
                          (protocol.RESULTDATA_CMD, "@ResultData")):
            if cmd in order:
                common._say("FAIL: %s survived the @WatchInfo snapshot purge -- it "
                     "would reach the client behind the snapshot and be read "
                     "as a card for a turn it has passed (Errot!!Turn). "
                     "order %r" % (name, order)); ok = False
        if 5 not in order:
            common._say("FAIL: the purge ate a NON per-turn push (@Quit) -- it must "
                 "only drop what the snapshot restates, order %r"
                 % (order,)); ok = False

        # --- positive control: with the purge OFF the crash is reproducible ---
        os.environ["POL_TM_WATCH_SNAPSHOT_PURGE"] = "0"
        _load_backlog()
        if watchers._watch_stream_purge(wmid, "selftest control") != 0:
            common._say("FAIL: POL_TM_WATCH_SNAPSHOT_PURGE=0 must not purge"); ok = False
        order0 = _drain()
        if protocol.PUTCARD_CMD not in order0:
            common._say("FAIL: the positive control is broken -- with the purge off "
                 "the stale turn-0 @PutCard must still be delivered, order %r"
                 % (order0,)); ok = False
    finally:
        pushqueue._PUSHES.clear(); pushqueue._PUSHES.update(saved_pushes)
        if saved_env is None:
            os.environ.pop("POL_TM_WATCH_SNAPSHOT_PURGE", None)
        else:
            os.environ["POL_TM_WATCH_SNAPSHOT_PURGE"] = saved_env
    return ok
