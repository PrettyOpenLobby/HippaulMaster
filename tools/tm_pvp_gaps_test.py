"""The 2026-10-08 gap pass: PvP moves relay, observers stop, PvP wins pay.

Each block replays a shape the live logs showed, then turns the fix's knob
off and checks the old failure comes back, so no check here can pass on a
server that does not have the fix.

  1. A PvP move was not relayed (2026-10-03T17:24:13Z, "member 64 played hand
     slot 2 but we hold 0 card(s)"): member 52 leaving an OLD cup match at
     table 4 popped member 64's roster entry for the match 64 was playing at
     table 2. The pop is now limited to entries that name the old match, a
     lost entry is restored from the other seats, and a lost play hand is
     rebuilt from the deal minus the board.
  2. Observer pushes dropped after 120-150 s "for peer <nick>, which this
     connection never carries" (member 47, 2026-10-03): 47 had left the watch
     (@ExitCh=) and was never taken off the watcher list.
  3. PvP wins paid no house prize; SE's guidebook prices against players
     too. Cup (event) games are checked to keep the old settlement.
  4. The card shop's NOT DEBITING warning names the balance trail.
  5. The Check Out log line says what the wire carries.

Needs the test database (tm_run_all.py gives it one).

    python tools/tm_run_all.py -k tm_pvp_gaps
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tm_testenv                                                  # noqa: E402
tm_testenv.setup()          # this tree's services + the OpenLobby core

TMP = tempfile.mkdtemp(prefix="tm-pvp-gaps-")
os.environ.setdefault("POL_DATA_DIR", TMP)
os.environ.setdefault("POL_LOG_DIR", TMP)
os.environ["POL_TM_START_SEAT"] = "0"

import tetramaster  # noqa: E402,F401
import tmbattle                                                    # noqa: E402
from tmgame import (                                               # noqa: E402
    boardrules, collection, common, dispatch, matchmaking, pots, protocol, purse,
    pushqueue, tableaudit, tournament, turns, watchers,
)

FAILS = []
LOG = []
_real_say = common._say


def _say(msg, *a, **k):
    LOG.append(str(msg))


common._say = _say


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


class env:
    """Set environment knobs for one block, restoring them after."""

    def __init__(self, **kv):
        self.kv = kv
        self.old = {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = str(v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


CHAN = "#TM0R081"
PEER = b"UPEERTEST"


def reset():
    for d in (matchmaking._MATCH_ROSTER, matchmaking._MATCH_BOTS, matchmaking._MATCH_PEER,
              boardrules._MATCH_HANDS, boardrules._MATCH_TURN, boardrules._MATCH_BOARD,
              boardrules._MATCH_OBJECTS, pushqueue._PUSHES, watchers._MATCH_WATCHERS,
              pots._PVP_STAKES, tournament._EVENT_MATCH):
        d.clear()
    tournament._EVENT_DEALT.clear()
    del LOG[:]


def queued(mid, needle=b""):
    return [e[1] for e in pushqueue._PUSHES.get(pushqueue._push_key(mid)) or []
            if needle in e[1]]


def row(cid, arrows=0):
    return b"%d|30|0|20|20|5|%d|255" % (cid, arrows)


# --------------------------------------------------------------------------- 1
def block_roster_kept():
    print("1. leaving an OLD match must not drop a seat of a NEWER one")
    for knob, expect_kept in (("1", True), ("0", False)):
        reset()
        old4 = [(64, b"a"), (52, b"b")]
        new2 = [(64, b"a"), (47, b"c")]
        matchmaking._MATCH_ROSTER[52] = (CHAN, 4, old4)
        matchmaking._MATCH_ROSTER[64] = (CHAN, 2, new2)
        matchmaking._MATCH_ROSTER[47] = (CHAN, 2, new2)
        boardrules._MATCH_TURN[(CHAN, 2)] = {"turn": 4, "active": 0, "n": 2}
        with env(POL_TM_ABANDON_OWN_KEY_ONLY=knob):
            tableaudit._match_abandoned(52, "seated at %s table 3 now" % CHAN)
        kept = matchmaking._MATCH_ROSTER.get(64) == (CHAN, 2, new2)
        if expect_kept:
            check(kept and 52 not in matchmaking._MATCH_ROSTER,
                  "member 52 retired from table 4; member 64 keeps table 2",
                  repr(matchmaking._MATCH_ROSTER))
        else:
            check(not kept, "knob off: the 10-03 pop comes back (the check can fail)")


def block_relay():
    print("1b. a PvP move relays after the roster entry AND the play hand are lost")
    for knobs, expect in (({}, True),
                          ({"POL_TM_RECOVER_ROSTER": "0", "POL_TM_HAND_FROM_DECK": "0"}, False)):
        reset()
        seats = [(64, b"a"), (47, b"c")]
        matchmaking._remember_match(CHAN, 2, seats)
        for m, _v in seats:
            matchmaking._MATCH_PEER[pushqueue._push_key(m)] = PEER
        h64 = [row(140), row(111), row(75), row(32), row(31)]
        h47 = [row(95), row(76), row(78), row(33), row(24)]
        hands = boardrules._MATCH_HANDS.setdefault((CHAN, 2), {})
        hands[64], hands[("deck", 0)] = list(h64), list(h64)
        hands[47], hands[("deck", 1)] = list(h47), list(h47)
        boardrules._MATCH_TURN[(CHAN, 2)] = {"turn": 0, "active": 0, "n": 2}
        dispatch.handle_line(protocol._turn_code(protocol.PUTDATA_CMD, 0)
                             + b"@PutData=/P=0/H=3/F=0",
                             peer_nick=PEER, member_id=64)
        first = queued(47, b"@PutCard=")
        check(any(b"/D=32|" in b for b in first), "turn 0: 64's card 32 reaches 47",
              repr(first))
        pushqueue._PUSHES.clear()
        # the 10-03 loss: the entry and the play hand both gone mid-game
        matchmaking._MATCH_ROSTER.pop(64, None)
        hands.pop(64, None)
        boardrules._MATCH_TURN[(CHAN, 2)] = {"turn": 2, "active": 0, "n": 2}
        with env(**knobs) if knobs else env():
            dispatch.handle_line(protocol._turn_code(protocol.PUTDATA_CMD, 2)
                                 + b"@PutData=/P=0/H=2/F=15",
                                 peer_nick=PEER, member_id=64)
        got = queued(47, b"@PutCard=")
        if expect:
            # hand after card 32 left: 140, 111, 75, 31 -> slot 2 is 75
            check(any(b"/H=2/F=15/D=75|" in b for b in got),
                  "turn 2: slot 2 rebuilt as card 75 and relayed to 47", repr(got))
            check((matchmaking._MATCH_ROSTER.get(64) or (None, None))[:2] == (CHAN, 2),
                  "member 64's roster entry is restored from the other seat")
            check(any("REBUILT" in l for l in LOG) and any("RESTORED" in l for l in LOG),
                  "both repairs are logged")
        else:
            check(not got and any("but we hold 0 card(s)" in l for l in LOG),
                  "knobs off: the 10-03 warning and no relay (the check can fail)")


def block_hand_from_deck():
    print("1c. the rebuilt hand is the deal minus THIS seat's placed cards, in order")
    reset()
    d0 = [row(1), row(2), row(3), row(2), row(5)]
    boardrules._MATCH_HANDS[(CHAN, 7)] = {("deck", 0): list(d0), ("deck", 1): [row(9)] * 5}
    board = boardrules._MATCH_BOARD.setdefault((CHAN, 7), {})
    board[0] = tmbattle.Card(row(2), 0)
    board[3] = tmbattle.Card(row(9), 1)                # the other seat's card
    flipped = tmbattle.Card(row(5), 0)
    flipped.owner = 1                                  # captured: still 0's placement
    board[6] = flipped
    got = boardrules._hand_from_deck(CHAN, 7, 0)
    check(got == [row(1), row(3), row(2)], "first copy of a duplicate goes, capture counted",
          repr(got))
    check(boardrules._hand_from_deck(CHAN, 7, 2) == [], "no deal copy: nothing invented")


# --------------------------------------------------------------------------- 2
def block_watch_exit():
    print("2. an observer who leaves the watch stops getting the match")
    for knob, expect_gone in (("1", True), ("0", False)):
        reset()
        wpeer = b"UE7QN1N9G"
        watchers._MATCH_WATCHERS[(CHAN, 3)] = {pushqueue._push_key(47): (47, wpeer)}
        watchers._watch_push(CHAN, 3, protocol._turn_code(protocol.PUTCARD_CMD, 6)
                             + b"@PutCard=/A=1/H=0/F=14/D=148|10|0|9|10|2|85|255", "t")
        pushqueue._queue_push(47, b"D7000000@ETelop=/ID=1", "ticker", after=9)
        check(len(queued(47, b"@PutCard=")) == 1, "the watched move is queued for 47")
        with env(POL_TM_WATCH_EXIT_CLEANUP=knob):
            dispatch.handle_line(b"20000000@ExitCh=", peer_nick=b"UKXDBA266", member_id=47)
            watchers._watch_push(CHAN, 3, protocol._turn_code(protocol.PUTCARD_CMD, 7)
                                 + b"@PutCard=/A=0/H=1/F=9/D=187|78|2|29|31|16|9|255", "t")
        left = queued(47, b"@PutCard=")
        if expect_gone:
            check(not left and (CHAN, 3) not in watchers._MATCH_WATCHERS,
                  "@ExitCh=: off the watcher list, queued and later moves not sent",
                  repr(left))
            check(queued(47, b"@ETelop="), "...and pushes for anything else stay queued")
        else:
            check(len(left) == 2, "knob off: the stream keeps coming (the check can fail)")
    reset()
    watchers._MATCH_WATCHERS[(CHAN, 3)] = {pushqueue._push_key(47): (47, b"UW")}
    matchmaking._remember_match(CHAN, 2, [(47, b"c"), (64, b"a")])
    check((CHAN, 3) not in watchers._MATCH_WATCHERS,
          "a watcher seated in a match of their own stops watching")


# --------------------------------------------------------------------------- 3
def member(mid, money, avg=None, cards=0):
    data = collection._collection_load(mid)
    data["money"] = money
    if avg is not None:
        data["rank"] = {"avg_rank": avg, "placed": 10, "place_total": avg * 10,
                        "games": 10}
    else:
        data.pop("rank", None)
    if cards:
        data["cards"] = [[10 + i, 30, 0, 20, 20, 5, 0, 255] for i in range(cards)]
    collection._collection_store(mid, data)


def finish(index, seats, tiles, stake=100, event=False):
    """Run the last turn of a PvP match at (CHAN, index) with each seat owning
    `tiles[seat]` tiles; returns {member: (money after, /D= occ 1 served)}."""
    matchmaking._remember_match(CHAN, index, seats)
    for m, _v in seats:
        matchmaking._MATCH_PEER[pushqueue._push_key(m)] = PEER
    if event:
        tournament._EVENT_DEALT.add((CHAN, index))
    if stake:
        pots._stake_pvp_pot(CHAN, index, seats, stake, "wager")
    board = boardrules._MATCH_BOARD.setdefault((CHAN, index), {})
    t = 0
    for seat, k in enumerate(tiles):
        for _ in range(k):
            board[t] = tmbattle.Card(row(20 + t), seat)
            t += 1
    hands = boardrules._MATCH_HANDS.setdefault((CHAN, index), {})
    for seat, (m, _v) in enumerate(seats):
        hands[("deck", seat)] = [row(20 + seat)] * 5
    last = 5 * len(seats)                    # the client's own turn limit
    boardrules._MATCH_TURN[(CHAN, index)] = {"turn": last - 1, "active": 1, "n": len(seats)}
    turns._queue_next_turn(CHAN, index, seats, last, 0)
    out = {}
    for m, _v in seats:
        res = queued(m, b"@ResultData=")
        occ1 = None
        if res:
            mm = re.search(rb"@No0=/D=(\d+)\|(\d+)\|", res[-1])
            occ1 = int(mm.group(2)) if mm else None
        out[m] = (purse.money_of(m), occ1)
    return out


def block_pvp_prize():
    print("3. a PvP win pays SE's prize formula; cup games do not change")
    reset()
    member(101, 1000, avg=150)
    member(102, 1000, avg=120)
    seats = [(101, b"a"), (102, b"b")]
    got = finish(11, seats, [6, 4])
    # opponent 102's average rank 1.20: ((400-120)//2+100) x (100+100) / 100 = 480
    check(got[101] == (900 + 480, 480), "winner: stake 100 taken, prize 480 paid and served",
          repr(got))
    check(got[102] == (900, 0), "loser: stake forfeit, nothing served", repr(got))
    check(any("PvP WIN -- house prize 480" in l for l in LOG), "the prize is logged")

    reset()
    member(101, 1000, avg=150)
    member(102, 1000, avg=120)
    got = finish(12, seats, [10, 0])
    check(got[101] == (900 + 1440, 1440), "a perfect win pays x3 (the code's and srpm082's)",
          repr(got))

    reset()
    member(101, 1000, avg=150)
    member(102, 1000, avg=120)
    got = finish(13, seats, [5, 5])
    check(got[101] == (1000, 100) and got[102] == (1000, 100),
          "a draw returns each stake", repr(got))

    reset()
    member(101, 0, avg=150)
    member(103, 0)                                      # never placed: 2.00, OURS
    got = finish(14, [(101, b"a"), (103, b"d")], [6, 4], stake=0)
    # unranked opponent = 2.00: ((400-200)//2+100) x (0+100) / 100 = 200
    check(got[101] == (200, 200), "a broke player's stake-0 win still pays (unranked = 2.00)",
          repr(got))

    reset()
    member(101, 1000, avg=150)
    member(102, 1000, avg=120)
    member(104, 1000, avg=300)
    got = finish(15, [(101, b"a"), (102, b"b"), (104, b"e")], [5, 3, 1])
    # 101: mean(120, 300) = 210 -> (95+100) x 200 x 2 below / 100 = 780
    # 102: mean(150, 300) = 225 -> (87+100) x 200 x 1 below / 100 = 374
    check(got[101][0] == 900 + 780 and got[102][0] == 900 + 374 and got[104][0] == 900,
          "3P: x places below you, second place paid too, last nothing", repr(got))

    with env(POL_TM_HOUSE_PRIZE_PVP="0"):
        reset()
        member(101, 1000, avg=150)
        member(102, 1000, avg=120)
        got = finish(16, seats, [6, 4])
    check(got[101] == (1100, 100), "knob off: the old pot settlement (the check can fail)",
          repr(got))

    print("3b. the cup path: an event deal and an event result are untouched")
    reset()
    member(201, 500, avg=150, cards=8)
    member(202, 500, avg=120, cards=8)
    ev = [(201, b"a"), (202, b"b")]
    tournament._event_deal(ev, lambda m: PEER, CHAN, 21)
    check((CHAN, 21) in tournament._EVENT_DEALT, "the event deal marks its table as a cup game")
    hands = boardrules._MATCH_HANDS.get((CHAN, 21)) or {}
    check(len(hands.get(201) or []) == 5 and len(hands.get(202) or []) == 5
          and hands.get(("deck", 0)) and hands.get(("deck", 1)),
          "both cup players are dealt five with deal copies", repr(sorted(map(str, hands))))
    check(all(queued(m, b"@EventCard=") for m, _v in ev), "@EventCard goes to both")
    pushqueue._PUSHES.clear()
    got = finish(21, ev, [6, 4], stake=0, event=True)
    check(got[201] == (500, 0) and got[202] == (500, 0),
          "a cup game pays no house prize (money and /D= occ 1 unchanged)", repr(got))
    check(not any("house prize" in l for l in LOG), "...and logs none")
    reset()
    matchmaking._remember_match(CHAN, 21, ev)
    check((CHAN, 21) not in tournament._EVENT_DEALT,
          "a new non-cup match at that table is not a cup game")


# --------------------------------------------------------------------------- 4/5
def block_shop_and_checkout():
    print("4. NOT DEBITING names the balance trail")
    reset()
    member(301, 0)
    purse._set_money(301, 9500, "test credit")
    purse._set_money(301, 7979, "pack /No=20")
    dispatch.handle_line(b"B2001401@Buy=/No=3", peer_nick=PEER, member_id=301)
    check(purse.money_of(301) == 7979, "short balance: not debited (unchanged rule)")
    trail = [l for l in LOG if "balance trail" in l]
    check(trail and "9500 -> 7979 (pack /No=20)" in trail[0], "the trail is in the log",
          repr(trail))

    print("5. the Check Out log line says what the wire carries")
    reset()
    member(302, 1410)
    dispatch.handle_line(b"B0000100@ShReq=/NN=0/HID=0/Dm=0/Vol=0/CN=", peer_nick=PEER,
                         member_id=302)
    line = [l for l in LOG if "auction Check Out --" in l and "/M=" in l]
    check(line and "1410 (the live wallet)" in line[0] and "NOT the" not in line[0],
          "the opener's /M= is logged as the live wallet", repr(line))


def main():
    try:
        block_roster_kept()
        block_relay()
        block_hand_from_deck()
        block_watch_exit()
        block_pvp_prize()
        block_shop_and_checkout()
    finally:
        common._say = _real_say
    print()
    if FAILS:
        print("FAILED (%d):" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("all pvp gap checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
