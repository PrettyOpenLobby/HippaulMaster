#!/usr/bin/env python3
"""tm_watch_test.py -- the Tetra Master web watch document (tetramaster.py's
`_watch_*`, 2026-09-13), end to end through the REAL placement resolver.

    python tools/tm_watch_test.py

Pins the server owner's choices: only tables whose creator allows observing
(`@Tab=/in=` 0 or 2; 1 "Impossible" and password tables are listed without
state), HANDS HIDDEN (counts only -- no unplayed card appears anywhere in the
document), VS. COM games included; and the shape the board animates: one step per
placement with its battles (rolls), the tiles that changed hands (battle /
combo / flip) and a chance block's effect. And that it can never cost a match:
a process holding no match never writes, a failed write raises nothing, and
POL_TM_WATCH_KEY=0 writes nothing.

The document lives in polcore.kv (tmstore.watch_key); with POL_VALKEY_URL
unset that is this process's in-memory store, which is all a single-process
test needs.
"""
import json
import os
import random
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="tmwatch-")
os.makedirs(os.path.join(TMP, "resources"), exist_ok=True)
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
os.environ["POL_TM_ROSTER_KEY"] = "tm:test:%s:roster" % os.path.basename(TMP)
os.environ["POL_TM_WATCH_KEY"] = KEY = "tm:test:%s:tables-live" % os.path.basename(TMP)
os.environ["POL_TM_LIVE_MARKER"] = "1"
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
sys.path.insert(0, HERE)
import tm_testenv                                 # noqa: E402
tm_testenv.setup(need_core=False)

import tmstore                                    # noqa: E402
tmstore.Snapshot(tmstore.roster_key()).write(
    {"names": {"101": "Fox", "102": "Corvin", "201": "Maria",
               "301": "Perry", "302": "clem"}})

import tetramaster as tm                          # noqa: E402

CHECKS = []


def check(label, cond, detail=""):
    CHECKS.append(label)
    print("  %-72s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  " + str(detail)) if detail and not cond else ""), flush=True)
    if not cond:
        raise AssertionError(label)


def row(cid, atk, typ, pdef, mdef, arrows):
    """A card row as `@CardSelect=` carries it: id|atk|type|pdef|mdef|power|arrows|flag."""
    return b"%d|%d|%d|%d|%d|0|%d|0" % (cid, atk, typ, pdef, mdef, arrows)


def raw():
    return tmstore.kv.get(KEY)


def read():
    return json.loads(raw())


class _Broken:
    """A live store that is down: every call raises."""

    def __getattr__(self, name):
        def fail(*a, **k):
            raise ConnectionError("the live store is down")
        return fail


def reset():
    for d in (tm._MATCH_TURN, tm._MATCH_BOARD, tm._MATCH_OBJECTS, tm._MATCH_HANDS,
              tm._MATCH_STARTED, tm._MATCH_ROSTER, tm._COM_GAME, tm._COM_AT,
              tm._WATCH_GAMES, tm._WATCH_PENDING, tm._TABLE_SETTINGS):
        d.clear()
    tm._WATCH.update(t=0.0, body=None, owner=False, whined=False)


def main():
    E, W = 1 << 2, 1 << 6                       # arrow bits: east, west
    print("a process holding no match never writes")
    reset()
    tm._watch_touch()
    tm._watch_publish(force=True)
    check("no match here (the login container's case): nothing written, not the writer",
          raw() is None and not tm._WATCH["owner"])

    print("a player-vs-player match, through the real resolver")
    chan, idx = "#TM0R001", 3
    key = (chan, idx)
    seats = [(101, b"x"), (102, b"y")]
    tm._MATCH_ROSTER[101] = (chan, idx, seats)
    tm._MATCH_ROSTER[102] = (chan, idx, seats)
    tm._TABLE_SETTINGS[key] = {"in": 0}
    tm._MATCH_STARTED[key] = {101, 102}
    secret = row(222, 99, 1, 99, 99, 0xFF)          # still in a hand: never shown
    tm._MATCH_HANDS[key] = {tm._push_key(101): [row(10, 60, 0, 30, 30, E), secret],
                            tm._push_key(102): [row(20, 5, 0, 5, 5, W), secret]}
    tm._MATCH_TURN[key] = {"turn": 0, "active": 0, "n": 2}
    tm._live_matches_write()
    check("the first match message makes this process the writer",
          tm._WATCH["owner"] and raw() is not None)
    t = read()["tables"]
    check("the table is listed by <room>-<table>, watchable, dealt, no moves yet",
          set(t) == {"1-3"} and t["1-3"]["watchable"]
          and t["1-3"]["state"]["phase"] == "play" and t["1-3"]["state"]["steps"] == [], t)
    s = t["1-3"]["state"]
    check("players by name, their HAND COUNTS only, scores, a 4x4 board",
          [p["name"] for p in s["players"]] == ["Fox", "Corvin"]
          and [p["hand"] for p in s["players"]] == [2, 2]
          and s["tiles"] == 16 and s["limit"] == 10, s)
    # seat 0 plays card 10 (east arrow) on tile 5; seat 1 answers on tile 6
    # pointing west -- a battle, card 10 is far stronger
    tm._MATCH_HANDS[key][tm._push_key(101)].pop(0)
    tm._apply_placement(chan, idx, 2, 0, 5, row(10, 60, 0, 30, 30, E), rnd=random.Random(1))
    tm._MATCH_TURN[key].update(turn=1, active=1)
    tm._MATCH_HANDS[key][tm._push_key(102)].pop(0)
    tm._apply_placement(chan, idx, 2, 1, 6, row(20, 5, 0, 5, 5, W), rnd=random.Random(2))
    s = read()["tables"]["1-3"]["state"]
    st = s["steps"]
    check("one step per placement, in order", [x["seq"] for x in st] == [1, 2]
          and [(x["seat"], x["tile"]) for x in st] == [(0, 5), (1, 6)], st)
    b = st[1]["battles"]
    check("the battle as the players saw it: tiles, stats, rolls, the winner",
          len(b) == 1 and (b[0]["att"], b[0]["def"]) == (6, 5)
          and b[0]["win"] in ("att", "def") and 0 <= b[0]["a_roll"] <= b[0]["a_raw"]
          and 0 <= b[0]["d_roll"] <= b[0]["d_raw"], b)
    won = b[0]["win"] == "att"
    check("...and the board after it: who holds tiles 5 and 6, and the step says so",
          s["board"]["5"]["owner"] == (1 if won else 0)
          and s["board"]["6"]["owner"] == (1 if won else 0)
          and st[1]["lost"] == (not won)
          and (st[1]["changed"] == [{"tile": 5, "from": 0, "to": 1, "how": "battle"}]
               if won else st[1]["changed"] == []), (s["board"], st[1]))
    check("a card on the board is public: id, stats, arrows, owner and placer",
          s["board"]["5"]["id"] == 10 and s["board"]["5"]["arrows"] == E
          and s["board"]["5"]["placer"] == 0 and s["board"]["5"]["atk"] == 60)
    check("scores are tiles held", sum(p["score"] for p in s["players"]) == 2)
    blob = raw()
    check("HANDS HIDDEN: a card still in a hand appears nowhere in the document",
          '"id":222' not in blob and "222|" not in blob
          and [p["hand"] for p in s["players"]] == [1, 1])

    print("a free flip and a combo are told apart")
    key2 = ("#TM0R001", 4)
    tm._MATCH_ROSTER[103] = ("#TM0R001", 4, seats)
    tm._TABLE_SETTINGS[key2] = {"in": 2}          # "No Comments Allowed" = watchable
    tm._MATCH_TURN[key2] = {"turn": 0, "active": 0, "n": 2}
    tm._apply_placement("#TM0R001", 4, 2, 1, 1, row(30, 10, 0, 10, 10, 0), rnd=random.Random(3))
    tm._apply_placement("#TM0R001", 4, 2, 0, 0, row(31, 10, 0, 10, 10, E), rnd=random.Random(4))
    s2 = read()["tables"]["1-4"]
    check("'No Comments Allowed' (in=2) is watchable", s2["watchable"])
    flip = s2["state"]["steps"][-1]["changed"]
    check("an arrow into a card that points nowhere back: a FLIP, no battle",
          flip == [{"tile": 1, "from": 1, "to": 0, "how": "flip"}]
          and s2["state"]["steps"][-1]["battles"] == [], flip)

    print("a player-vs-player placement that PAUSES for the player's pick")
    # the resumable resolver: tile 5 (arrows east + west) can fight BOTH seat-1
    # cards, so it parks for @BattleSelect with the card already on the live
    # board -- and results written as each battle resolves
    key3 = ("#TM0R001", 5)
    seats3 = [(401, b"p"), (402, b"q")]
    tm._MATCH_ROSTER[401] = ("#TM0R001", 5, seats3)
    tm._MATCH_ROSTER[402] = ("#TM0R001", 5, seats3)
    tm._TABLE_SETTINGS[key3] = {"in": 0}
    tm._MATCH_TURN[key3] = {"turn": 2, "active": 0, "n": 2}
    tm._MATCH_BOARD[key3] = {4: tm.tmbattle.Card(row(60, 5, 0, 5, 5, E), 1),
                             6: tm.tmbattle.Card(row(61, 5, 0, 5, 5, W), 1)}
    os.environ["POL_TM_TURNDATA"] = "0"
    try:
        tm._watch_publish(force=True)
        shown = read()["tables"]["1-5"]["state"]["board"]
        tm._begin_placement("#TM0R001", 5, 2, 0, 5, row(62, 250, 0, 250, 250, E | W), 2,
                            seats3, 0, 401, seats3, rnd=random.Random(8))
        parked = tm._advance_placement("#TM0R001", 5)
        tm._watch_publish(force=True)
        mid = read()["tables"]["1-5"]["state"]
        check("while it waits for the pick, the board is the last move's: no new card yet",
              parked is False and mid["board"] == shown and "5" not in mid["board"]
              and mid["steps"] == [], (parked, mid["board"]))
        done = tm._advance_placement("#TM0R001", 5, chosen=4)
        s3 = read()["tables"]["1-5"]["state"]
        check("...and the move's step and its board arrive TOGETHER",
              done is True and len(s3["steps"]) == 1 and len(s3["steps"][0]["battles"]) == 2
              and s3["board"]["5"]["owner"] == 0
              and {s3["board"]["4"]["owner"], s3["board"]["6"]["owner"]} == {0}
              and s3["steps"][0]["owners"] == {"4": 0, "5": 0, "6": 0}, s3)
    finally:
        del os.environ["POL_TM_TURNDATA"]

    print("who may be shown")
    tm._TABLE_SETTINGS[key2] = {"in": 1}
    tm._watch_publish(force=True)
    e = read()["tables"]["1-4"]
    check("'Impossible' (in=1): listed WITHOUT state", e == {"watchable": False}, e)
    tm._TABLE_SETTINGS[key2] = {"in": 0, "pa": 1}
    tm._watch_publish(force=True)
    check("a password table: listed without state",
          read()["tables"]["1-4"] == {"watchable": False})
    tm._TABLE_SETTINGS[key2] = {"in": 0}
    os.environ["POL_TM_WEB_WATCH"] = "0"
    tm._watch_publish(force=True)
    check("POL_TM_WEB_WATCH=0: every table unwatchable",
          all(v == {"watchable": False} for v in read()["tables"].values()))
    del os.environ["POL_TM_WEB_WATCH"]

    print("a VS. COM game, and card select")
    pk = tm._push_key(201)
    tm._COM_GAME[pk] = {"n": 2, "coms": [5], "table": ("#TM0R002", 7), "member": 201}
    tm._COM_AT[("#TM0R002", 7)] = pk
    tm._MATCH_HANDS[(None, pk)] = {pk: [row(40, 1, 0, 1, 1, 0)] * 4,
                                   ("com", 1): [row(41, 1, 0, 1, 1, 0)] * 3}
    tm._MATCH_TURN[(None, pk)] = {"turn": 2, "active": 0, "n": 2}
    tm._apply_placement(None, pk, 2, 1, 9, row(41, 1, 0, 1, 1, 0), rnd=random.Random(5))
    tm._MATCH_STARTED[("#TM0R003", 1)] = {301}
    tm._MATCH_ROSTER[301] = ("#TM0R003", 1, [(301, b"a"), (302, b"b")])
    tm._watch_publish(force=True)
    t = read()["tables"]
    c = t["2-7"]["state"]
    check("a VS. COM game is listed at its real table, the machine named and marked",
          c["com"] and [p["name"] for p in c["players"]] == ["Maria", "COM 5"]
          and [p["com"] for p in c["players"]] == [False, True]
          and [p["hand"] for p in c["players"]] == [4, 3]
          and c["steps"][-1]["tile"] == 9, c)
    pk2 = tm._push_key(202)
    tm._COM_GAME[pk2] = {"n": 2, "coms": [], "table": ("#TM0R002", 8), "member": 202}
    tm._COM_AT[("#TM0R002", 8)] = pk2
    tm._TABLE_SETTINGS[("#TM0R002", 8)] = {"in": 0}
    tm._watch_publish(force=True)
    early = read()["tables"]["2-8"]["state"]["players"][1]
    check("a COM whose opponent is not chosen yet is just 'COM' (not record 1, Natasha)",
          early["name"] == "COM" and early["ci"] is None and early["com"], early)
    blk = tm.tmbattle.Card(row(0x8009, 0, 0, 0, 0, 1 << 2), 4)   # a rotating block dealt facing E
    ph = tm._watch_board({7: blk}, [0] * 16, turn=3)["7"].get("phase")
    check("a rotating block publishes its CURRENT phase: dealt E (2) + turn 3 = 5",
          ph == 5, ph)
    check("a table still in card select is listed, names and no board",
          t["3-1"]["state"]["phase"] == "select"
          and [p["name"] for p in t["3-1"]["state"]["players"]] == ["Perry", "clem"]
          and t["3-1"]["state"]["board"] == {})

    print("the end of a game, and a new one")
    tm._MATCH_TURN[key]["result_sent"] = True
    tm._watch_publish(force=True)
    s = read()["tables"]["1-3"]["state"]
    check("a finished game says so, with its winner (or -1 for a draw)",
          s["phase"] == "over" and s["winner"] in (0, 1, -1), s["winner"])
    tm._MATCH_TURN[key] = {"turn": 0, "active": 1, "n": 2}      # the rematch's deal
    tm._MATCH_BOARD.pop(key, None)
    tm._watch_publish(force=True)
    s = read()["tables"]["1-3"]["state"]
    check("a rematch (a fresh deal) starts a fresh list of moves",
          s["phase"] == "play" and s["steps"] == [] and s["board"] == {})
    tm._MATCH_TURN.pop(key, None)
    tm._MATCH_STARTED.pop(key, None)
    tm._watch_publish(force=True)
    check("a table whose game has gone is gone from the document", "1-3" not in read()["tables"])

    print("cadence")
    stamp = read()["stamp"]
    tm._watch_publish()
    tm._watch_publish(force=True)
    check("an unchanged document is not rewritten inside 5 s", read()["stamp"] == stamp)
    tm._WATCH["t"] = time.time() - tm.WATCH_EVERY_S - 1
    time.sleep(0.05)            # past the clock's tick: the in-memory store is instant
    tm._watch_publish()
    check("...and is rewritten every 5 s (the heartbeat the board trusts)",
          read()["stamp"] > stamp)

    print("it can never cost a match")
    live = tmstore.kv.default()
    tmstore.kv.reset(_Broken())
    try:
        tm._WATCH.update(t=0.0, body=None)
        tm._watch_publish(force=True)
        tm._apply_placement("#TM0R001", 4, 2, 1, 2, row(32, 10, 0, 10, 10, 0),
                            rnd=random.Random(6))
    finally:
        tmstore.kv.reset(live)
    check("a write that fails raises nothing, and the placement still lands",
          2 in tm._MATCH_BOARD[key2])
    os.environ["POL_TM_WATCH_KEY"] = "0"
    before = raw()
    tm._WATCH.update(t=0.0, body=None)
    time.sleep(0.01)
    tm._watch_publish(force=True)
    check("POL_TM_WATCH_KEY=0 writes nothing", raw() == before)
    os.environ["POL_TM_WATCH_KEY"] = KEY
    print("[tm_watch_test] OK -- %d checks" % len(CHECKS))


if __name__ == "__main__":
    main()
