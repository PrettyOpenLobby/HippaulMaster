#!/usr/bin/env python3
"""tm_watch_browser.py -- the Tetra Master WATCHING page in a real headless
Chrome (render-web-ui-before-shipping), fed by the REAL publisher: the
matches are played through tetramaster's own placement resolver, which
writes the tables file the board reads -- so the page is tested against the
exact shape authsess will write, not a hand-made copy.

    python tools/tm_watch_browser.py [--shots DIR]

Fails on any JavaScript error, on art or a face that never loads, on a move
that does not animate (the card in, the battle numbers, the flip), on a
wrong board after it, on a missing GAME START / WIN, or on the list, the
"not shown" answer and the phone layout. LOOK at the screenshots.
"""
import argparse
import json
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="tm-watch-browser-")
os.makedirs(os.path.join(TMP, "resources"), exist_ok=True)
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.environ["POL_TM_ROSTER_KEY"] = "tm:test:%s:roster" % os.path.basename(TMP)
os.environ["POL_TM_WATCH_KEY"] = "tm:test:%s:tables-live" % os.path.basename(TMP)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))

from fe_panel_browser import Browser, free_port   # noqa: E402
import tmstore                                    # noqa: E402
tmstore.Snapshot(tmstore.roster_key()).write(
    {"names": {"101": "Lex", "102": "Corvin", "201": "periwinks"}})
import tetramaster as tm                          # noqa: E402
import tm_cardprm                                 # noqa: E402

E, W, N, S = 1 << 2, 1 << 6, 1 << 0, 1 << 4


def row(cid, atk, arrows):
    return b"%d|%d|0|20|20|0|%d|0" % (cid, atk, arrows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=tempfile.mkdtemp(prefix="tm-watch-shots-"))
    o = ap.parse_args(argv)
    os.makedirs(o.shots, exist_ok=True)
    fails = []

    def check(name, ok, detail=""):
        print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                                "  " + str(detail) if detail and not ok else ""), flush=True)
        if not ok:
            fails.append(name)

    # a 2-player match at room 1 table 3, one card already down
    chan, idx = "#TM0R001", 3
    seats = [(101, b"x"), (102, b"y")]
    tm._MATCH_ROSTER[101] = (chan, idx, seats)
    tm._TABLE_SETTINGS[(chan, idx)] = {"in": 0}
    tm._MATCH_STARTED[(chan, idx)] = {101, 102}
    tm._MATCH_HANDS[(chan, idx)] = {tm._push_key(101): [row(1, 1, 0)] * 4,
                                    tm._push_key(102): [row(1, 1, 0)] * 5}
    tm._MATCH_TURN[(chan, idx)] = {"turn": 0, "active": 0, "n": 2}
    tm._MATCH_OBJECTS[(chan, idx)] = [3] + [0] * 15       # an unplayed ATTACK-UP tile on 0
    tm._live_matches_write()
    tm._apply_placement(chan, idx, 2, 0, 5, row(65, 90, E | S), rnd=random.Random(1))
    tm._MATCH_TURN[(chan, idx)].update(turn=1, active=1)
    tm._watch_publish(force=True)

    import boardtm
    import polboards
    boardtm.WATCH_STALE_S = 3600.0
    port = free_port()
    args = polboards.build_parser().parse_args(["--tm-port", str(port), "--tm-url",
                                                "http://127.0.0.1:%d" % port])
    srv = polboards.serve(boardtm, args, port)
    base = "http://127.0.0.1:%d/" % port
    art_ok = ("[...document.querySelectorAll('#stage img')].every("
              "i => i.complete && i.naturalWidth > 0)")
    b = Browser(width=1400, height=1000)
    try:
        b.goto(base + "watch#1-3", settle=3.0)
        check("the page loads the match and the game's own 4x4 board",
              b.js("S && S.id") == "1-3" and b.js("$('#boardbg').src.endsWith('board16.png')")
              and b.js(art_ok), b.js("S && S.id"))
        check("the faces loaded", b.js("document.fonts.check('700 13px \"TMText\"')"
                                       " && document.fonts.check('24px \"TMTitle\"')"))
        check("the card on the board: Bahamut in the first player's colour, on tile 5",
              b.js("Object.keys(CARDS).join()") == "5"
              and b.js("CARDS[5].base.src.endsWith('base_b.png')")
              and b.js("CARDS[5].el.querySelector('.art').src.endsWith('card_065.png')"))
        check("both players' panels: names, scores, hands as card backs, the turn",
              b.js("[...document.querySelectorAll('#panels .t.b')].map(e => e.textContent).join()")
              == "Lex,Corvin" and b.js("document.querySelectorAll('#panels .backs img').length") == 9
              and "Turn 2 of 10" in b.js("$('#info').textContent"),
              [b.js("[...document.querySelectorAll('#panels .t.b')].map(e => e.textContent).join()"),
               b.js("document.querySelectorAll('#panels .backs img').length"),
               b.js("$('#info').textContent")])
        check("the pointer is at the player whose turn it is, and their plate pulses",
              b.js("[...document.querySelectorAll('#panels img')].some(i => i.src.endsWith('hand.png'))")
              and b.js("document.querySelectorAll('#panels img.plhl').length") == 1)
        stats = b.js("[...CARDS[5].el.querySelectorAll('img.st')].map(i => i.src.split('/').pop()).join()")
        check("the card face's stat line: attack 90 -> 9, P, pdef 20 -> 2, mdef 20 -> 2 (tens digit)",
              stats == "st_9.png,st_p.png,st_2.png,st_2.png", stats)
        check("its arrows are the game's own tips ON the card (east + south)",
              b.js("[...CARDS[5].el.querySelectorAll('img.ca')].map(i => i.src.split('/').pop()).sort().join()")
              == "ca2.png,ca4.png" and b.js("CARDS[5].el.querySelectorAll('polygon').length") == 0)
        check("an unplayed special tile shows its icon (sword = attack up)",
              b.js("[...document.querySelectorAll('#layer .spicon')].map(i => i.src.split('/').pop()).join()")
              == "sp_1.png")
        info = b.js("CARDS[5].el.dispatchEvent(new MouseEvent('mouseenter')); "
                    "$('#cinfo') ? $('#cinfo').textContent + '|' + $('#cinfo').querySelectorAll('img.st').length : ''")
        check("hovering a card shows it large with its name and stat line",
              (info or "").startswith(tm_cardprm.name(65)) and info.endswith("|4"),
              [info, tm_cardprm.name(65)])
        b.screenshot(os.path.join(o.shots, "1-board.png"))
        b.js("CARDS[5].el.dispatchEvent(new MouseEvent('mouseleave'))")

        # seat 1 attacks it from tile 6 (pointing west): a battle
        tm._MATCH_HANDS[(chan, idx)][tm._push_key(102)].pop()
        b.js("window.SEEN1 = {num: 0, hit: 0}; new MutationObserver(() => {"
             " SEEN1.num = Math.max(SEEN1.num, document.querySelectorAll('#fx .num').length);"
             " if ([...document.querySelectorAll('#fx img.spr')].some(i => /fx_hit/.test(i.src))) SEEN1.hit = 1;"
             "}).observe($('#fx'), {childList: true, subtree: true, attributes: true}); 1")
        tm._apply_placement(chan, idx, 2, 1, 6, row(100, 80, W), rnd=random.Random(7))
        tm._MATCH_TURN[(chan, idx)].update(turn=2, active=0)
        tm._watch_publish(force=True)
        st = tmstore.kv.get_json(tmstore.watch_key())["tables"]["1-3"]["state"]
        battle = st["steps"][-1]["battles"][0]
        b.pump(1.9)
        b.screenshot(os.path.join(o.shots, "2-battle.png"))
        seen1 = json.loads(b.js("JSON.stringify(SEEN1)"))
        check("the new card drops in and its battle is on: both numbers up, the burst between",
              b.js("Object.keys(CARDS).sort().join()") == "5,6" and seen1 == {"num": 2, "hit": 1},
              [b.js("Object.keys(CARDS).sort().join()"), seen1])
        b.pump(6.0)
        want = st["board"]
        check("after the battle the board is the game's: every owner matches",
              b.js("Object.entries(CARDS).map(([t, k]) => t + ':' + k.c.owner).sort().join()")
              == ",".join(sorted("%s:%s" % (t, c["owner"]) for t, c in want.items()))
              and b.js("document.querySelectorAll('#fx .num').length") == 0,
              [b.js("Object.entries(CARDS).map(([t, k]) => t + ':' + k.c.owner).sort().join()"), want, battle])
        loser = battle["def"] if battle["win"] == "att" else battle["att"]
        check("...the loser shows the winner's colour",
              b.js("CARDS[%d].base.src.endsWith('base_%s.png')"
                   % (loser, "r" if battle["win"] == "att" else "b")), battle)
        b.screenshot(os.path.join(o.shots, "3-after.png"))

        tm._MATCH_TURN[(chan, idx)]["result_sent"] = True
        tm._watch_publish(force=True)
        b.pump(2.2)
        check("the game ends: WIN over the winner, or DRAW",
              b.js("[...document.querySelectorAll('#fx img')].some(i => /win_face|draw/.test(i.src))")
              and "Game over" in b.js("$('#info').textContent"))
        b.screenshot(os.path.join(o.shots, "4-win.png"))

        # a fresh 3-player VS. COM game on the 5x5 board: GAME START
        pk = tm._push_key(201)
        tm._COM_GAME[pk] = {"n": 3, "coms": [4, 9], "table": ("#TM0R002", 7), "member": 201}
        tm._COM_AT[("#TM0R002", 7)] = pk
        tm._TABLE_SETTINGS[("#TM0R002", 7)] = {"in": 2}
        tm._MATCH_HANDS[(None, pk)] = {pk: [row(1, 1, 0)] * 5,
                                       ("com", 1): [row(1, 1, 0)] * 5, ("com", 2): [row(1, 1, 0)] * 5}
        tm._MATCH_TURN[(None, pk)] = {"turn": 0, "active": 2, "n": 3}
        tm._watch_publish(force=True)
        time.sleep(0.6)                  # past boardtm's half-second tables cache
        b.goto(base + "watch?x=1#2-7", settle=1.2)
        import urllib.request
        try:
            raw = urllib.request.urlopen(base + "watch.json?t=2-7", timeout=5).read()[:600]
        except Exception as e:                   # noqa: BLE001
            raw = "fetch failed: %r" % (e,)
        check("GAME START as a game begins",
              b.js("[...document.querySelectorAll('#fx img')].some(i => i.src.endsWith('gs_game.png'))"),
              [b.js("$('#msg') && $('#msg').textContent"), b.errors, raw])
        b.screenshot(os.path.join(o.shots, "5-start.png"))
        b.pump(2.6)
        reel = b.js("[...document.querySelectorAll('#fx .rl img')].map(i => i.src.split('/').pop()).filter(s => /^reel/.test(s)).join()")
        check("...then WHO GOES FIRST: the game's reel (3 players: the 3a..3i frames)",
              (reel or "").startswith("reel3"), reel)
        b.screenshot(os.path.join(o.shots, "5b-roulette.png"))
        tm._apply_placement(None, pk, 3, 2, 12, row(148, 60, 0xFF), rnd=random.Random(3))
        tm._watch_publish(force=True)
        b.pump(5.0)                      # the first move waits for the reel to finish
        check("three seats on the 5x5 board, the machines marked COM",
              b.js("$('#boardbg').src.endsWith('board25.png')")
              and b.js("document.querySelectorAll('#panels .t.b').length") == 3
              and b.js("[...document.querySelectorAll('#panels .lab')].filter(e => e.textContent === 'COM').length") == 2
              and b.js("Object.keys(CARDS).join()") == "12",
              [b.js("document.querySelectorAll('#panels .t.b').length"), b.js("Object.keys(CARDS).join()")])
        check("...the card's arrows drawn from its mask (all eight)",
              b.js("CARDS[12] ? CARDS[12].el.querySelectorAll('img.ca').length : -1") == 8,
              b.errors)
        names = b.js("[...document.querySelectorAll('#panels .t.b')].map(e => e.textContent).join()")
        check("...the machines by their own names and faces (PlPrm.BIN 4 and 9, gW080)",
              names == "periwinks,Bogart the Gladiator,Akbar the Hunter"
              and b.js("[...document.querySelectorAll('#panels img.face')].map(i => i.src.split('/').pop()).join()")
              == "face_04.png,face_09.png", names)
        b.screenshot(os.path.join(o.shots, "6-3p.png"))

        b.goto(base + "watch", settle=2.0)
        check("/watch alone lists the matches being played",
              b.js("document.querySelectorAll('#list li a').length") == 2
              and "Lex vs Corvin" in b.js("$('#list').textContent"), b.js("$('#list') && $('#list').textContent"))
        b.screenshot(os.path.join(o.shots, "7-list.png"))
        b.goto(base + "watch?y=1#9-9", settle=2.0)
        check("a match that is not shown says so, with the way to the list",
              "not being shown" in b.js("$('#msg').textContent"))
        b.goto(base, settle=3.0)
        check("the rankings page offers 'Watch live (2)'",
              b.js("!E.live.hidden && E.live.textContent") == "Watch live (2)"
              and b.js("E.live.getAttribute('href')") == "watch", b.js("E.live && E.live.textContent"))
        b.screenshot(os.path.join(o.shots, "8-rankings-link.png"))
        b.call("Emulation.setDeviceMetricsOverride", width=420, height=900, deviceScaleFactor=2,
               mobile=True)
        b.goto(base + "watch?m=1#1-3", settle=3.0)
        check("a phone shows the match with no sideways scroll",
              b.js("S && S.id") == "1-3" and b.js("document.documentElement.scrollWidth <= innerWidth + 1"))
        b.screenshot(os.path.join(o.shots, "9-phone.png"))
        # --- hand-made states for what a quick test game will not reliably deal
        b.call("Emulation.clearDeviceMetricsOverride")
        def put(tables):
            tmstore.kv.set_json(tmstore.watch_key(), {"stamp": time.time(), "tables": tables})
            time.sleep(0.6)                              # past boardtm's half-second cache

        def card(cid, owner, arrows=0, **kw):
            d = {"id": cid, "owner": owner, "placer": owner, "arrows": arrows, "atk": 150,
                 "type": 1, "pdef": 45, "mdef": 5, "ability": 0, "mod": 0}
            d.update(kw)
            return d
        P2 = [{"name": "Lex", "com": False, "hand": 3, "score": 1, "ci": None, "vote": None},
              {"name": "COM 4", "com": True, "hand": 3, "score": 1, "ci": 4, "vote": None}]
        base_st = {"id": "4-1", "room": 4, "table": 1, "n": 2, "tiles": 16, "com": True,
                   "phase": "play", "turn": 3, "active": 0, "limit": 10, "players": P2,
                   "board": {"5": card(10, 0, E), "6": {"block": 7, "owner": None, "placer": None,
                                                        "arrows": 255},
                             # a ROTATING block at phase 1: its arrow (the ray's
                             # way) at ((1-4)&7)*45 = 225 deg
                             "10": {"block": 9, "owner": None, "placer": None, "arrows": 2,
                                    "phase": 1}},
                   "objects": [0] * 16, "steps": [], "winner": None, "began": time.time()}
        put({"4-1": {"watchable": True, "state": base_st}})
        b.goto(base + "watch?s=1#4-1", settle=2.5)
        stats = b.js("[...CARDS[5].el.querySelectorAll('img.st')].map(i => i.src.split('/').pop()).join()")
        check("150 shows the STAR, type 1 is M, 45 -> 4, 5 -> 0",
              stats == "st_star.png,st_m.png,st_4.png,st_0.png", stats)
        rot = b.js("CARDS[10] && CARDS[10].rotArrow ? CARDS[10].rotArrow.style.transform + '|' + "
                   "CARDS[10].el.querySelectorAll('img.rotglow').length : ''")
        check("a rotating block: its arrow turned to where the ray fires (225 deg), the orb's glow",
              rot == "rotate(225deg)|1", rot)
        # a battle against the CHANCE BLOCK on 6: triggered, not damaged
        step1 = {"seq": 1, "t": time.time(), "turn": 3, "seat": 0, "tile": 2,
                 "card": card(20, 0, S), "lost": False,
                 "battles": [{"att": 2, "def": 6, "a_raw": 90, "a_roll": 60, "a_sel": 0,
                              "d_raw": 50, "d_roll": 10, "d_sel": 0, "win": "att"}],
                 "changed": [], "effects": [{"kind": "power_up", "tile": 6, "tiles": [2, 5]}],
                 "owners": {"2": 0, "5": 0}}
        st2 = dict(base_st, steps=[step1], turn=4, active=1,
                   board={"2": card(20, 0, S, mod=10), "5": card(10, 0, E, mod=10),
                          "10": dict(base_st["board"]["10"], phase=2)})
        # record the most highlights / numbers the effects layer ever held
        b.js("window.SEEN = {hl: 0, num: 0, fx: {}}; new MutationObserver(() => {"
             " SEEN.hl = Math.max(SEEN.hl, document.querySelectorAll('#fx .hl').length);"
             " SEEN.num = Math.max(SEEN.num, document.querySelectorAll('#fx .num').length);"
             " document.querySelectorAll('#fx img.spr, #fx img.bolt').forEach(i => {"
             "  const n = (i.getAttribute('src') || '').split('/').pop().replace(/_\\d+\\.png$/, '');"
             "  if (n) SEEN.fx[n] = 1; });"
             "}).observe($('#fx'), {childList: true, subtree: true, attributes: true}); 1")
        put({"4-1": {"watchable": True, "state": st2}})
        b.pump(9.0)
        seen = json.loads(b.js("JSON.stringify(SEEN)"))
        check("a battle against a chance block shows NO numbers (it is struck: the burst, no highlight)",
              seen["hl"] == 0 and seen["num"] == 0 and "fx_hit" in seen["fx"], seen)
        check("...power up plays the game's own: smoke, the girl, creatures on the cards",
              {"fx_smoke", "fx_girl", "fx_up"} <= set(seen["fx"]), seen["fx"])
        check("...and the turn after, the rotating block's arrow has turned +45 deg clockwise",
              b.js("CARDS[10].rotAngle") == 270 and "270deg" in b.js("CARDS[10].rotArrow.style.transform"),
              b.js("CARDS[10].rotArrow.style.transform"))
        check("...the chance block is gone and power up tints the stat line",
              b.js("Object.keys(CARDS).sort().join()") == "10,2,5"
              and b.js("CARDS[5].el.querySelectorAll('img.st.up').length") == 3,
              b.js("Object.keys(CARDS).sort().join()"))
        b.js("SEEN.fx = {}; effect({kind: 'ray', tile: 5, to: 2, tiles: [2]}, GEN); 1")
        b.pump(3.5)
        seen = json.loads(b.js("JSON.stringify(SEEN)"))
        check("a rotating block's ray: a burst, the bolt, and nothing left after",
              {"fx_burst", "fx_bolt"} <= set(seen["fx"]) and b.js("$('#fx').children.length") == 0,
              [seen["fx"], b.js("$('#fx').children.length")])
        # a real battle, and the match goes away WHILE its numbers are up
        step2 = {"seq": 2, "t": time.time(), "turn": 4, "seat": 1, "tile": 1,
                 "card": card(30, 1, S), "lost": False,
                 "battles": [{"att": 1, "def": 5, "a_raw": 80, "a_roll": 40, "a_sel": 0,
                              "d_raw": 60, "d_roll": 50, "d_sel": 0, "win": "def"}],
                 "changed": [], "effects": [], "owners": {"1": 0, "2": 0, "5": 0}}
        st3 = dict(st2, steps=[step1, step2], turn=5, active=0,
                   board=dict(st2["board"], **{"1": card(30, 0, S)}))
        put({"4-1": {"watchable": True, "state": st3}})
        b.pump(1.3)
        up = b.js("document.querySelectorAll('#fx .num').length")
        put({})
        b.pump(2.6)
        check("the match ending mid-battle leaves NO numbers or highlights behind",
              up == 2 and b.js("$('#fx').children.length") == 0
              and "has ended" in b.js("$('#msg').textContent")
              and b.js("Object.keys(CARDS).length") > 0,
              [up, b.js("$('#fx').children.length"), b.js("$('#msg').textContent")])
        b.screenshot(os.path.join(o.shots, "10-ended.png"))
        # the "Play again?" panel
        P3 = [dict(P2[0], vote=0, score=6), dict(P2[1], vote=1, score=4)]
        over = dict(base_st, id="4-2", table=2, phase="over", winner=0, players=P3,
                    board={"5": card(10, 0, E)})
        put({"4-2": {"watchable": True, "state": over}})
        b.goto(base + "watch?r=1#4-2", settle=1.0)
        b.pump(5.5)                      # the WIN banner first: 32 in, held to 120, 32 out (~3.1 s)
        res = b.js("$('#result') ? $('#result').textContent : ''")
        check("game over: the result and who is deciding whether to play again",
              "Lex wins" in res and "Lex: Deciding..." in res
              and "Bogart the Gladiator: Playing again" in res, res)
        b.screenshot(os.path.join(o.shots, "11-result.png"))
        again = dict(over, phase="select", winner=None, board={}, steps=[],
                     players=[dict(p, vote=None, score=0) for p in P2])
        put({"4-2": {"watchable": True, "state": again}})
        b.pump(1.8)
        check("...a yes starts the rematch: the result goes, the cards are being chosen",
              "Rematch!" in b.js("$('#msg').textContent") and not b.js("!!$('#result')"),
              b.js("$('#msg').textContent"))
        check("no JavaScript errors", not b.errors, b.errors)
    finally:
        b.close()
        srv.shutdown()
    print("screenshots in %s" % o.shots)
    if fails:
        raise SystemExit("[tm_watch_browser] %d FAILED: %s" % (len(fails), ", ".join(fails)))
    print("[tm_watch_browser] OK")


if __name__ == "__main__":
    main()
