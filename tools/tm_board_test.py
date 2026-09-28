#!/usr/bin/env python3
"""tm_board_test.py -- the Tetra Master board (services/boardtm.py) and what
it adds to polboards: the extra auction feed and the MESSAGE SET.

    python tools/tm_board_test.py

Pins: the five published lists read back from tmrank's own store, in the
file's order, with the client's tie rule, the hide-name row and last week's
rank; the live "this week so far" list and where its names come from; the
auction split the way the game's browse sees it (past its end WITH bids =
sold, left out of the Price List counts), with <II>'s transposed type byte;
Discord as the server owner chose on 2026-09-13 -- one message per ranking
list, each with its own window, and one message per LISTING with the card as
its image, edited live, ended and deleted -- and that snapshots, renders and
every message leave every game file byte-identical: nothing settled, nothing
staged read, nothing written.
"""
import copy
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tm_testenv                                                  # noqa: E402
tm_testenv.setup(need_core=False)   # services/ ahead of tools/ (tmrank.py twice)
import tmpg                                                        # noqa: E402



class FakeNet:
    """Stands in for urllib.request.urlopen: records every request, answers
    from a script of (status, json)."""

    def __init__(self):
        self.calls = []
        self.script = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, req.data or b""))
        st, data = self.script.pop(0) if self.script else (200, {})
        raw = json.dumps(data).encode()
        if st >= 400:
            raise urllib.error.HTTPError(req.full_url, st, "x", {}, _Body(raw))
        return _Resp(st, raw)


class _Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self, *a):
        return self.raw

    def close(self):
        pass


class _Resp(_Body):
    def __init__(self, st, raw):
        super().__init__(raw)
        self.status = st

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


CHECKS = []
NOW = 1789500000                            # a Wednesday; the next publish is Sunday
NEXT = 1789862400


def check(label, cond, detail=""):
    CHECKS.append(label)
    print("  %-72s %s%s" % (label, "PASS" if cond else "FAIL",
                            ("  " + str(detail)) if detail and not cond else ""), flush=True)
    if not cond:
        raise AssertionError(label)


class AutoNet(FakeNet):
    """FakeNet that hands every unscripted POST a fresh message id."""

    def __init__(self, channel=None):
        super().__init__()
        self.n = 0
        self.auths = []
        self.channel = channel          # what a GET on a webhook answers

    def __call__(self, req, timeout=None):
        self.auths.append(req.get_header("Authorization"))
        if not self.script and req.get_method() == "POST":
            self.n += 1
            self.script = [(200, {"id": "m%d" % self.n})]
        if not self.script and req.get_method() == "GET" and self.channel:
            self.script = [(200, {"channel_id": self.channel, "guild_id": "G0"})]
        return super().__call__(req, timeout)

    def since(self, k):
        return [(c[0], c[1].rsplit("/", 1)[-1]) for c in self.calls[k:]]


def tree(root):
    """{relative path: sha256} of every file under root."""
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def png_size(blob):
    return struct.unpack(">II", blob[16:24])


def ii(cid, atk, pdef, typ, mdef, arrows=0):
    """<II>'s 16 values: id, then the u8 block attack, P defense, TYPE, M defense."""
    return [cid, 0, 0, 0, 0, 0, 0, 0, atk, pdef, typ, mdef, 3, arrows, 0, 0]


def setup(tmp):
    res = os.path.join(tmp, "resources")
    os.makedirs(res)
    os.environ["POL_RESOURCE_DIR"] = res
    os.environ["POL_DATA_DIR"] = tmp
    os.environ["POL_TM_ROSTER_KEY"] = "tm:test:%s:roster" % os.path.basename(tmp)
    os.environ["POL_TM_WATCH_KEY"] = "tm:test:%s:tables-live" % os.path.basename(tmp)
    # the live-match marker stays in this process's own store
    os.environ.pop("POL_VALKEY_URL", None)
    # the PlayOnline portrait: handle_profile field 19 = z_ficon (sheet*8 + tile);
    # Elena's PRIMARY handle has hnf304 tile 7, her other one something else
    tmpg.pol_accounts({1: [("NotLex", True, None)],
                       3: [("Elena", True, {19: 2439, 5: 77}),
                           ("OldMaria", False, {19: 16})]})
    import tmauction
    import tmrank
    players = [
        {"name": "Lex", "rating": 400, "rating_last": 300, "prize_total": 12652,
         "prize_week": 2472, "last_rank": {0: 2, 4: 1, 5: 1}},
        {"name": "Corvin", "rating": 364, "rating_last": 364, "prize_total": 295,
         "prize_week": 295, "last_rank": {}},
        {"name": "Tie A", "rating": 193, "rating_last": 193, "last_rank": {0: 3}},
        {"name": "Tie B", "rating": 193, "rating_last": 150, "last_rank": {0: 9}},
        {"name": "Shy", "rating": 150, "rating_last": 100, "hide_name": True,
         "last_rank": {0: 4}},
    ]
    files = {tmrank.path_for(rid): tmrank.build_list(rid, players) for rid in tmrank.LISTS}
    files[tmrank.RKDATA] = tmrank.build_rkdata({0: 5, 5: 5, 1: 5, 2: 5}, NEXT)
    tmrank.write_store(files)
    # collections: 1 and 3 have played, 2 has not, 4 is named only by the roster;
    # 5 is the best rating on file but has not played this week, 6 has one game
    week = tmrank.week_start(NOW)
    for m, blk in ((1, {"games": 10, "score_total": 90, "tiles_total": 100, "tiled_games": 10,
                        "prize_week": 700, "last_played": NOW - 3600,
                        "week_of": week, "week_games": 3}),
                   (2, {"games": 0}),
                   (3, {"games": 6, "score_total": 36, "tiles_total": 60, "tiled_games": 6,
                        "last_played": week + 60}),
                   (4, {"games": 5, "score_total": 40, "tiles_total": 80, "tiled_games": 5,
                        "prize_week": 50, "last_played": NOW - 60,
                        "week_of": week, "week_games": 1}),
                   (5, {"games": 20, "score_total": 300, "tiles_total": 320, "tiled_games": 20,
                        "last_played": week - 60, "week_of": week - 7 * 86400,
                        "week_games": 9}),
                   (6, {"games": 1, "score_total": 16, "tiles_total": 16, "tiled_games": 1,
                        "prize_week": 900, "last_played": NOW - 60,
                        "week_of": week, "week_games": 1})):
        with open(os.path.join(res, "%d%s" % (m, tmrank.COLLECTION_SUFFIX)), "w") as fh:
            json.dump({"cards": [], "money": 100, "rank": blk}, fh)
    import tmstore
    tmstore.Snapshot(tmstore.roster_key()).write(
        {"names": {"1": "Lex", "2": "Quinn", "4": "Star*Man", "5": "Idle",
                   "6": "OneGame"}})
    # the auction: two sellers' stores
    exhibit = lambda m: os.path.join(res, "%d.U_g_TM0_EXHIBITLIST.bin" % m)   # noqa: E731
    with open(exhibit(1), "wb") as fh:
        fh.write(tmauction.build_record(ii(65, 90, 77, 1, 35, 0b10000001), "Lex", "", ai=21,
                                        sp=500, bi=50, ed=NOW - 7200,
                                        nc=NOW + 3 * 86400 + 5 * 3600 + 30, et=77, am=1)
                 + tmauction.build_record(ii(107, 60, 30, 0, 75), "Lex", "Corvin", ai=22,
                                          sp=1500, bi=100, ed=NOW - 3600, nc=NOW + 7210,
                                          et=3, am=0, cm=1800, bc=2))
    with open(exhibit(3), "wb") as fh:
        fh.write(tmauction.build_record(ii(21, 33, 25, 2, 20), "Elena", "Lex", ai=23, sp=80,
                                        bi=10, ed=NOW - 2 * 86400, nc=NOW - 60, et=48, am=0,
                                        cm=150, bc=1)
                 + tmauction.build_record(ii(1, 5, 5, 0, 5), "Elena", "", ai=24, sp=80, bi=10,
                                          ed=NOW - 3 * 86400, nc=NOW - 600, et=48, am=0))
    bids = lambda ai: os.path.join(res, "auction-%d.bids.bin" % ai)   # noqa: E731
    with open(bids(22), "wb") as fh:
        fh.write(tmauction.build_bid("Quinn", 1600, NOW - 3000, 2)
                 + tmauction.build_bid("Corvin", 1800, NOW - 1000, 5))
    with open(bids(23), "wb") as fh:
        fh.write(tmauction.build_bid("Lex", 150, NOW - 4000, 1))
    # a FINISHED auction's bids under a reused id: older than listing 21
    with open(bids(21), "wb") as fh:
        fh.write(tmauction.build_bid("Ghost", 999, NOW - 90000, 9))
    # per-request STAGED copies and settlement bookkeeping: never the store
    decoy = tmauction.build_record(ii(200, 1, 1, 0, 1), "Ghost", "", ai=99, sp=1, nc=NOW + 99999)
    for n in ("1.U_g_TM0_AUCLIST.bin", "1.U_g_TM0_BIDLIST.bin"):
        with open(os.path.join(res, n), "wb") as fh:
            fh.write(decoy)
    with open(os.path.join(res, "auction-pending-3.json"), "w") as fh:
        json.dump({"money": 400, "cards": [], "won": [], "refund": 0}, fh)
    # the live-match marker, stamped 30 s before the suite's clock
    import live_sessions
    from polcore import kv
    kv.set(live_sessions.marker_key("tm"),
           json.dumps({"count": 2, "stamp": NOW - 30}))
    return res


def ranking_feed_checks(boardtm, polboards, snap, args):
    print("Discord -- the rankings: one message per list")
    slots = boardtm.discord_messages(snap, args)
    check("six slots, one per list, none of them ever 'ended'",
          [s["slot"] for s in slots] == ["rank-%d" % i for i in range(6)]
          and not any(s["ended"] for s in slots))
    built = [s["build"]() for s in slots]
    embeds = [p["embeds"][0] for p, _f in built]
    check("each is titled for its own list",
          [e["title"] for e in embeds] == ["VS. Rating", "Top 30", "Best Rookies", "Grand Total",
                                           "Weekly Total", "This Week So Far"],
          [e["title"] for e in embeds])
    names = ["tm-%s.png" % s for s in boardtm.SLUGS]
    check("...with its OWN window as the image, attached under its own name",
          all(f and f[0][0] == n and f[0][2][:4] == b"\x89PNG"
              and e["image"]["url"] == "attachment://" + n
              for (_p, f), e, n in zip(built, embeds, names))
          and built[0][1][0][2] != built[3][1][0][2])
    e0 = embeds[0]
    check("VS. Rating: its rows, the next update, the tally period, a link to its tab",
          "**Lex** 4.00" in e0["description"] and "<t:%d:F>" % NEXT in e0["description"]
          and e0["footer"]["text"].startswith("Tally Period: ")
          and e0["url"] == "https://tm.example/#0", e0)
    check("...a hidden name stays hidden", "-------" in e0["description"])
    check("This Week So Far says it is live, and markdown in a name is inert",
          embeds[5]["footer"]["text"] == "Live standings, not yet published"
          and "StarMan" in embeds[5]["description"] and "Star*Man" not in embeds[5]["description"])
    blob = json.dumps([p for p, _f in built], ensure_ascii=False)
    check("no em or en dashes, no one pinged",
          chr(0x2014) not in blob and chr(0x2013) not in blob
          and all(p["allowed_mentions"] == {"parse": []} for p, _f in built))
    check("a list's sig is stable", [s["sig"] for s in slots]
          == [s["sig"] for s in boardtm.discord_messages(snap, args)])
    moved = copy.deepcopy(snap)
    moved["tabs"][3]["rows"][0]["name"] = "Someone"
    diff = [a["sig"] != b["sig"] for a, b in zip(slots, boardtm.discord_messages(moved, args))]
    check("...and changes for the list that changed, only", diff == [False, False, False,
                                                                     True, False, False], diff)
    moved = copy.deepcopy(snap)
    moved["week"]["next"] = NEXT + 604800
    ev = boardtm.discord_events(snap, moved)
    check("a new weekly publish is news (with the VS. Rating #1)",
          len(ev) == 1 and "Lex" in ev[0] and "4.00" in ev[0], ev)
    check("...an unchanged week is not", boardtm.discord_events(snap, snap) == [])
    check("the rankings feed is a message SET",
          polboards.feed_fn(boardtm, "", "messages") is boardtm.discord_messages
          and polboards.feed_fn(boardtm, "", "message") is None)


def auction_feed_checks(boardtm, polboards, snap, args):
    print("Discord -- the auction: one message per listing")
    slots = {s["slot"]: s for s in boardtm.discord_auction_messages(snap, args)}
    check("a slot per listing: live ones open, sold and ended-unsold ones ENDED",
          {k: v["ended"] for k, v in slots.items()}
          == {"auction-21": False, "auction-22": False, "auction-24": True, "auction-23": True},
          {k: v["ended"] for k, v in slots.items()})
    p, f = slots["auction-21"]["build"]()
    e = p["embeds"][0]
    fields = {x["name"]: x["value"] for x in e["fields"]}
    check("a new listing: the card's name, its opening bid, a live countdown",
          e["title"] == "Bahamut" and "Opening bid **500T**" in e["description"]
          and fields["Ends"] == "<t:%d:R>" % (NOW + 3 * 86400 + 5 * 3600 + 30)
          and fields["Seller"] == "Lex" and fields["Min. Raise"] == "50T"
          and "Type M" in fields["Card"] and "2 arrows" in fields["Card"], e)
    check("...and THE CARD as its image: on the game's 80x96 card base, 3x, room for arrows",
          f and f[0][0] == "card-21.png" and e["image"]["url"] == "attachment://card-21.png"
          and png_size(f[0][2]) == (80 * 3 + 36, 96 * 3 + 36), f and png_size(f[0][2]))
    check("...its arrows drawn on it",
          boardtm.card_png(dict(e and slots and snap["auction"]["listings"][0]["card"],
                                arrows=0)) != f[0][2])
    e = slots["auction-22"]["build"]()[0]["embeds"][0]
    check("a bid: the high bid, the bidder, the count",
          "High bid **1,800T** by **Corvin** (2 bids)" in e["description"], e["description"])
    e = slots["auction-23"]["build"]()[0]["embeds"][0]
    check("a sale: 'Sold', to whom, for how much",
          e["title"] == "Cerberus - Sold" and "Sold to **Lex** for **150T**" in e["description"]
          and e["color"] == 0x3BA55C, e)
    e = slots["auction-24"]["build"]()[0]["embeds"][0]
    check("no bids and no relists left: it ends and goes back to the seller",
          e["title"] == "Fang - Ended" and "goes back to Elena" in e["description"], e)
    retire = lambda slot: boardtm.discord_auction_retire(slot, snap, args)   # noqa: E731
    check("a listing that left the store is retired from what it last was",
          retire("auction-23")[0]["embeds"][0]["title"] == "Cerberus - Sold"
          and retire("auction-24")[0]["embeds"][0]["title"] == "Fang - Ended"
          and retire("auction-21")[0]["embeds"][0]["title"] == "Bahamut - No longer listed"
          and retire("auction-99") is None and retire("bogus") is None)
    check("a listing's sig ignores the clock (Discord counts down by itself)",
          [s["sig"] for s in slots.values()]
          == [s["sig"] for s in boardtm.discord_auction_messages(snap, args)])
    check("the auction feed is a message SET with a retire, and has no screen message",
          polboards.feed_fn(boardtm, "auction", "messages") is boardtm.discord_auction_messages
          and polboards.feed_fn(boardtm, "auction", "retire") is boardtm.discord_auction_retire
          and polboards.feed_fn(boardtm, "auction", "message") is None)


def set_checks(tmp, boardtm, polboards, snap, args):
    print("polboards -- a message SET, edited in place")
    hook = "https://discord.com/api/webhooks/9/AUCSECRET"
    state = os.path.join(tmp, "state", "set.json")
    os.makedirs(os.path.dirname(state), exist_ok=True)
    with open(state, "w") as fh:
        json.dump({"hook": hashlib.sha1(hook.encode()).hexdigest(), "message_id": "old1",
                   "events": []}, fh)
    net = AutoNet()
    ds = polboards.DiscordSet("tm_auction", hook, state, every=15, ttl=600,
                              ended_ttl=600, per_tick=3, opener=net)

    def sl(slot, sig, ended=False):
        return {"slot": slot, "sig": sig, "ended": ended,
                "build": lambda: ({"content": "%s %s" % (slot, sig)}, [])}
    check("the old single message is queued for deletion",
          [e["id"] for e in ds.events] == ["old1"])
    k = len(net.calls)
    did = ds.sync([sl("A", "a1"), sl("B", "b1")], now=1000.0)
    check("first sync: the old message deleted, one post per slot",
          net.since(k) == [("DELETE", "old1"), ("POST", "AUCSECRET?wait=true"),
                           ("POST", "AUCSECRET?wait=true")]
          and did == [("posted", "A"), ("posted", "B")]
          and ds.msgs["A"]["id"] == "m1" and ds.msgs["B"]["id"] == "m2", net.since(k))
    k = len(net.calls)
    ds.sync([sl("A", "a1"), sl("B", "b1")], now=1005.0)
    ds.sync([sl("A", "a2"), sl("B", "b1")], now=1010.0)
    check("nothing for unchanged slots, nothing inside the per-slot interval",
          net.since(k) == [])
    ds.sync([sl("A", "a2"), sl("B", "b1")], now=1016.0)
    check("a changed slot is EDITED once the interval has passed",
          net.since(k) == [("PATCH", "m1")])
    k = len(net.calls)
    ds.sync([sl("A", "a3", True), sl("B", "b1")], now=1017.0)
    check("the edit that ENDS a slot goes out at once", net.since(k) == [("PATCH", "m1")]
          and ds.msgs["A"]["ended"] == 1017.0)
    k = len(net.calls)
    ds.sync([sl("A", "a3", True), sl("B", "b1")], now=1017.0 + 599)
    check("...it stays up until --discord-ended-ttl", net.since(k) == [])
    ds.sync([sl("A", "a3", True), sl("B", "b1")], now=1017.0 + 600)
    check("...then it is deleted", net.since(k) == [("DELETE", "m1")]
          and "A" not in ds.msgs and "A" in ds.done)
    k = len(net.calls)
    ds.sync([sl("A", "a3", True), sl("B", "b1")], now=2000.0)
    check("an ended slot that was deleted is not posted again", net.since(k) == [])
    ds.sync([sl("A", "a3", True)], retire=lambda s: ({"content": "final " + s}, []), now=2100.0)
    check("a slot that has GONE is edited to its final form",
          net.since(k) == [("PATCH", "m2")] and b"final B" in net.calls[-1][2]
          and ds.msgs["B"]["ended"] == 2100.0)
    k = len(net.calls)
    ds.sync([sl("A", "a3", True)], now=2700.0)
    check("...and deleted after the same TTL", net.since(k) == [("DELETE", "m2")]
          and "B" not in ds.msgs)
    k = len(net.calls)
    ds.sync([sl("A", "a4")], now=3000.0)
    check("a slot back to life (a relist) is a new post", net.since(k)
          == [("POST", "AUCSECRET?wait=true")] and "A" not in ds.done)
    ds2 = polboards.DiscordSet("tm_auction", hook, state, opener=net)
    k = len(net.calls)
    ds2.sync([sl("A", "a4")], now=3001.0)
    check("across a restart the ids are kept: no second post",
          ds2.msgs["A"]["id"] == ds.msgs["A"]["id"] and net.since(k) == [])
    net3 = AutoNet()
    ds3 = polboards.DiscordSet("x", hook, os.path.join(tmp, "state", "x.json"), per_tick=3,
                               opener=net3)
    five = [sl("S%d" % i, "s") for i in range(5)]
    ds3.sync(five, now=100.0)
    first = len(net3.calls)
    ds3.sync(five, now=105.0)
    check("at most --per-tick writes per poll: 3, then the other 2",
          first == 3 and len(net3.calls) == 5)
    net3.script = [(429, {"retry_after": 30})]
    check("a 429 is waited out", ds3.sync(five + [sl("S9", "s")], now=110.0) == []
          and ds3.sync(five + [sl("S9", "s")]) == [])
    ds3.hold_until = 0.0
    net3.script = [(404, {"message": "Unknown Message"})]
    did = ds3.sync([sl("S0", "changed")] + five[1:], now=200.0)
    check("a message someone deleted is posted again",
          ("lost", "S0") in did and "S0" not in ds3.msgs
          and ("posted", "S0") in ds3.sync([sl("S0", "changed")] + five[1:], now=201.0))

    print("polboards -- the watchers")
    net4 = AutoNet()
    d4 = polboards.DiscordSet("tm", "https://discord.com/api/webhooks/8/RANKSECRET",
                              os.path.join(tmp, "state", "tm_discord.json"), opener=net4)
    boardtm._SNAP.update(t=0.0, snap=None)
    polboards.watch_set(boardtm, args, d4, period=0.01, rounds=3)
    posts = [c for c in net4.calls if c[0] == "POST"]
    check("the rankings watcher posts the six lists, three per poll",
          len(posts) == 6 and all(("tm-%s.png" % s).encode() in c[2]
                                  for c, s in zip(posts, boardtm.SLUGS)), len(posts))
    snaps = [snap, copy.deepcopy(snap)]
    snaps[1]["auction"]["listings"] = [r for r in snaps[1]["auction"]["listings"] if r["ai"] != 21]
    it = iter(snaps + [snaps[1]])
    fake = types.SimpleNamespace(NAME="tm", cached_snapshot=lambda a: next(it),
                                 discord_auction_messages=boardtm.discord_auction_messages,
                                 discord_auction_retire=boardtm.discord_auction_retire)
    net5 = AutoNet()
    d5 = polboards.DiscordSet("tm_auction", hook, os.path.join(tmp, "state", "a.json"),
                              opener=net5)
    polboards.watch_set(fake, args, d5, period=0.01, rounds=2, feed="auction")
    check("the auction watcher posts each LIVE listing (never one first seen ended)",
          [c[0] for c in net5.calls[:2]] == ["POST", "POST"]
          and set(d5.msgs) == {"auction-21", "auction-22"}
          and b"card-21.png" in net5.calls[0][2] + net5.calls[1][2])
    check("...and a listing that leaves the store is edited to its end",
          net5.calls[-1][0] == "PATCH" and b"No longer listed" in net5.calls[-1][2]
          and d5.msgs["auction-21"]["ended"] is not None, [c[0] for c in net5.calls])


#: RFC 8032 section 7.1, TEST 1 and TEST 2: (secret, public, message, signature)
RFC1 = ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", b"",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a3"
        "3bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
RFC2 = ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", b"\x72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e1599"
        "6e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")


def ed_sign(polboards, sk, msg):
    """RFC 8032 5.1.6 signing on polboards' own curve arithmetic -- the test's
    stand-in for Discord. Returns (public key, signature)."""
    h = hashlib.sha512(sk).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    pub = polboards._ed_bytes(polboards._ed_mul(a, polboards._ED_B))
    r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % polboards._ED_L
    big_r = polboards._ed_bytes(polboards._ed_mul(r, polboards._ED_B))
    k = int.from_bytes(hashlib.sha512(big_r + pub + msg).digest(), "little") % polboards._ED_L
    return pub, big_r + ((r + k * a) % polboards._ED_L).to_bytes(32, "little")


def bot_checks(tmp, boardtm, polboards, snap, args):
    print("the bot -- Ed25519, against RFC 8032's own test vectors")
    for sk, pk, msg, sig in (RFC1, RFC2):
        check("RFC 8032 vector (%d-byte message) verifies" % len(msg),
              polboards.ed25519_verify(bytes.fromhex(pk), msg, bytes.fromhex(sig)))
        check("...and the test's signer reproduces it exactly",
              ed_sign(polboards, bytes.fromhex(sk), msg) == (bytes.fromhex(pk), bytes.fromhex(sig)))
    pk, sig = bytes.fromhex(RFC2[1]), bytes.fromhex(RFC2[3])
    check("a changed message, signature or key, or a short one, does not verify",
          not polboards.ed25519_verify(pk, b"\x73", sig)
          and not polboards.ed25519_verify(pk, b"\x72", sig[:-1] + bytes([sig[-1] ^ 1]))
          and not polboards.ed25519_verify(bytes.fromhex(RFC1[1]), b"\x72", sig)
          and not polboards.ed25519_verify(pk[:31], b"\x72", sig)
          and not polboards.ed25519_verify(pk, b"\x72", sig[:63]))

    print("the bot -- the rankings post: one window, flipped in place")
    boardtm._VIEW.update(tab=0, page=0, t=0.0)
    p, f = boardtm.discord_bot_message(snap, args)
    e = p["embeds"][0]
    ids = [c.get("custom_id") for row in p["components"] for c in row["components"]]
    check("ONE post: the VS. Rating WINDOW as its image, no lists of text, no emoji",
          f == [] and e["title"] == "VS. Rating"
          and e["image"]["url"].startswith("https://tm.example/render.png?tab=0&page=0&v=")
          and "\U0001F947" not in json.dumps(p) and "Lex" not in e.get("description", ""), e)
    check("...a button per list (the one on show lit), page arrows, the board",
          ids == ["tm:view:%d:0" % i for i in range(6)] + ["tm:page:0:-1", "tm:page:0:1", None]
          and p["components"][0]["components"][0]["style"] == 1
          and p["components"][0]["components"][1]["style"] == 2
          and p["components"][1]["components"][1]["disabled"]
          and p["components"][1]["components"][2]["disabled"]
          and len(p["components"][0]["components"]) == 5, ids)
    check("...every custom id unique (Discord refuses duplicates)",
          len([i for i in ids if i]) == len({i for i in ids if i}))
    r = boardtm.discord_interaction({"type": 3, "data": {"custom_id": "tm:view:3:0"}}, args)
    check("a list button FLIPS the post itself (UPDATE_MESSAGE), no new post",
          r["type"] == 7 and "flags" not in r["data"]
          and r["data"]["embeds"][0]["title"] == "Grand Total"
          and "tab=3&page=0" in r["data"]["embeds"][0]["image"]["url"], r)
    check("...the watcher's next edit keeps that view",
          boardtm.discord_bot_message(snap, args)[0]["embeds"][0]["title"] == "Grand Total")
    boardtm._VIEW["t"] = time.time() - boardtm.VIEW_IDLE_S - 1
    check("...and it goes back to VS. Rating after 10 minutes without a click",
          boardtm.discord_bot_message(snap, args)[0]["embeds"][0]["title"] == "VS. Rating")
    r = boardtm.discord_interaction({"type": 3, "data": {"custom_id": "tm:rank:1"}}, args)
    check("the first bot post's buttons (tm:rank:N) flip it too",
          r["type"] == 7 and r["data"]["embeds"][0]["title"] == "Top 30")
    big = copy.deepcopy(snap)
    big["tabs"][0]["rows"] = [dict(big["tabs"][0]["rows"][0], rank=i + 1) for i in range(25)]
    p2 = boardtm.discord_bot_message(big, args, 0, 1)[0]
    c2 = p2["components"][1]["components"]
    check("a 3-page list: page 2 of 3, both arrows live, the image asks for page 1",
          "(page 2 of 3)" in p2["embeds"][0]["title"] and not c2[1]["disabled"]
          and not c2[2]["disabled"] and c2[1]["custom_id"] == "tm:page:0:0"
          and c2[2]["custom_id"] == "tm:page:0:2" and "page=1" in p2["embeds"][0]["image"]["url"])
    check("a button for a list that is not there gets a private note",
          boardtm.discord_interaction({"type": 3, "data": {"custom_id": "tm:view:99:0"}},
                                      args)["data"]["flags"] == 64)

    print("the bot -- a listing's post flips between the card and its bids")
    slots = {s["slot"]: s for s in boardtm.discord_auction_messages(snap, args, bot=True)}
    p, f = slots["auction-22"]["build"]()
    comps = p["components"][0]["components"]
    check("the bot's listing: the card by the board's own URL (no upload), Bid History, "
          "the board", f == [] and p["attachments"] == []
          and p["embeds"][0]["image"]["url"].startswith("https://tm.example/card.png?id=107&a=")
          and comps[0]["custom_id"] == "tm:bids:22" and not comps[0]["disabled"]
          and comps[1]["url"] == "https://tm.example/#6", p)
    check("...a listing with no bids has its Bid History greyed",
          slots["auction-21"]["build"]()[0]["components"][0]["components"][0]["disabled"])
    r = boardtm.discord_interaction({"type": 3, "data": {"custom_id": "tm:bids:22"}}, args)
    em = r["data"]["embeds"][0]
    check("Bid History flips THAT post to the bids, newest first",
          r["type"] == 7 and em["title"] == "Zidane - Bid History"
          and em["description"].index("Corvin") < em["description"].index("Quinn")
          and "`1,800T`" in em["description"]
          and em["thumbnail"]["url"].startswith("https://tm.example/card.png?id=107")
          and r["data"]["components"][0]["components"][0]["custom_id"] == "tm:card:22", r)
    check("...a watcher edit keeps it there",
          "Bid History" in slots["auction-22"]["build"]()[0]["embeds"][0]["title"])
    r = boardtm.discord_interaction({"type": 3, "data": {"custom_id": "tm:card:22"}}, args)
    check("...and Back to the card flips it back",
          r["type"] == 7 and r["data"]["embeds"][0]["title"] == "Zidane")
    check("a click on a listing that has left the store gets a private note",
          boardtm.discord_interaction({"type": 3, "data": {"custom_id": "tm:bids:9999"}},
                                      args)["data"]["flags"] == 64)
    wp, wf = boardtm.discord_auction_messages(snap, args)[0]["build"]()
    check("...one posted by the webhook carries no buttons and keeps its upload",
          "components" not in wp and wf and wf[0][2][:4] == b"\x89PNG")

    print("the bot -- clicks and /tmboard over HTTP, signed")
    sk, pub = bytes.fromhex(RFC1[0]), RFC1[1]
    x = socket.socket()
    x.bind(("127.0.0.1", 0))
    port = x.getsockname()[1]
    x.close()
    sargs = polboards.build_parser().parse_args(["--tm-port", str(port), "--tm-url",
                                                 "https://tm.example", "--tm-discord-public-key", pub])
    srv = polboards.serve(boardtm, sargs, port)

    def post(obj, sign=True, bad=False):
        body, ts = json.dumps(obj).encode(), "1789500000"
        sig = ed_sign(polboards, sk, ts.encode() + body)[1]
        if bad:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        req = urllib.request.Request("http://127.0.0.1:%d/discord/interactions" % port,
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if sign:
            req.add_header("X-Signature-Ed25519", sig.hex())
            req.add_header("X-Signature-Timestamp", ts)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, None

    def cmd(sub, perms):
        return {"type": 2, "channel_id": "555", "guild_id": "G1",
                "member": {"permissions": perms},
                "data": {"name": "tmboard", "options": [{"type": 1, "name": sub}]}}
    try:
        check("an unsigned request is refused (401)", post({"type": 1}, sign=False)[0] == 401)
        check("a mis-signed request is refused (401)", post({"type": 1}, bad=True)[0] == 401)
        check("Discord's PING is answered", post({"type": 1}) == (200, {"type": 1}))
        st, r = post({"type": 3, "data": {"custom_id": "tm:view:3:0"}})
        check("a signed click flips the post (UPDATE_MESSAGE)",
              st == 200 and r["type"] == 7 and r["data"]["embeds"][0]["title"] == "Grand Total")
        with urllib.request.urlopen("http://127.0.0.1:%d/card.png?id=65&a=181" % port,
                                    timeout=20) as resp:
            ok = resp.status == 200 and resp.read()[:4] == b"\x89PNG"
        check("/card.png draws a card on its base for the bot's listing posts", ok)
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/card.png?id=999" % port, timeout=20)
            refused = False
        except urllib.error.HTTPError as err:
            refused = err.code == 400
        check("...and refuses a card that does not exist", refused)
        st, r = post(cmd("auction", "0"))
        check("/tmboard from someone who cannot manage the server is refused",
              "Only members" in r["data"]["content"]
              and "tm_auction" not in polboards.bot_channels()["chosen"])
        st, r = post(cmd("auction", str(0x20)))
        check("/tmboard auction (Manage Server) moves the auction HERE",
              r["data"]["flags"] == 64 and "<#555>" in r["data"]["content"]
              and polboards.bot_channels()["chosen"]["tm_auction"] == {"G1": "555"}, r)
        st, r = post(cmd("status", "0"))
        check("/tmboard status says where each feed posts (anyone may ask)",
              "auction**: <#555>" in r["data"]["content"]
              and "rankings**: its webhook's channel" in r["data"]["content"], r)
    finally:
        srv.shutdown()

    print("the bot -- its command and its channels")
    c = polboards.command_of(boardtm, "tm")
    check("/tmboard rankings | auction | live | status: Manage Server only, servers only",
          c["name"] == "tmboard" and [o["name"] for o in c["options"]]
          == ["rankings", "auction", "live", "status"] and c["default_member_permissions"] == "32"
          and c["contexts"] == [0])
    net = AutoNet()
    st = polboards.register_commands(boardtm, "tm", "BOTTOKEN", "APP1", opener=net)
    check("it is registered with the bot's token",
          st == 200 and net.calls[-1][0] == "PUT"
          and net.calls[-1][1].endswith("/applications/APP1/commands")
          and net.auths[-1] == "Bot BOTTOKEN" and b'"tmboard"' in net.calls[-1][2])
    hook = "https://discord.com/api/webhooks/8/RANKSECRET"
    net = AutoNet(channel="77")
    d = polboards.run_bot_feed(boardtm, args, "tm", "", hook, "BOTTOKEN", opener=net,
                               rounds=1, period=0.01)
    dels = [u for m, u, _b in net.calls if m == "DELETE"]
    posts = [(u, b, a) for (m, u, b), a in zip(net.calls, net.auths) if m == "POST"]
    check("to the bot: the six posts the webhook made are deleted",
          len(dels) == 6 and all(u.startswith(hook + "/messages/") for u in dels), dels)
    check("...and ONE rankings post goes up AS THE BOT in the webhook's channel",
          len(posts) == 1 and posts[0][0] == "https://discord.com/api/v10/channels/77/messages"
          and posts[0][2] == "Bot BOTTOKEN" and b"tm:view:0:0" in posts[0][1]
          and b"tm:view:5:0" in posts[0][1]
          and polboards.bot_channels()["posted"]["tm"] == {"G0": "77"},
          posts and posts[0][0])
    polboards._note_channel("chosen", "tm", "88")
    net2 = AutoNet(channel="77")
    polboards.run_bot_feed(boardtm, args, "tm", "", hook, "BOTTOKEN", opener=net2,
                           rounds=1, period=0.01)
    seq = [(m, u) for m, u, _b in net2.calls]
    check("/tmboard rankings elsewhere: the old post goes, then the new one goes up",
          seq[0] == ("DELETE", "https://discord.com/api/v10/channels/77/messages/%s" % d.msg_id)
          and ("POST", "https://discord.com/api/v10/channels/88/messages") in seq
          and not any(m == "GET" for m, _u in seq), seq)
    net3 = AutoNet(channel="99")
    # two polls: at most 3 posts go out per poll, and the watcher's snapshot is
    # on the REAL clock, where every fixture listing is still running
    polboards.run_bot_feed(boardtm, args, "tm_auction", "auction",
                           "https://discord.com/api/webhooks/9/AUCSECRET", "BOTTOKEN",
                           opener=net3, rounds=2, period=0.01)
    live = [r["ai"] for r in boardtm.cached_snapshot(args)["auction"]["listings"] if r["left"] > 0]
    aposts = [b for m, u, b in net3.calls if m == "POST"
              and u == "https://discord.com/api/v10/channels/555/messages"]
    check("the auction as the bot: each live listing, in the channel /tmboard chose, "
          "with its Bid History button", len(aposts) == len(live) >= 2
          and any(b"tm:bids:21" in b for b in aposts), (len(aposts), live))


def watch_checks(tmp, boardtm, polboards, args):
    print("watching -- the live matches document")
    import tmstore
    state = {"id": "1-3", "room": 1, "table": 3, "n": 2, "tiles": 16, "com": False,
             "phase": "play", "turn": 3, "active": 1, "limit": 10,
             "players": [{"name": "Lex", "com": False, "hand": 3, "score": 2},
                         {"name": "Cor*vin", "com": False, "hand": 4, "score": 1}],
             "board": {}, "objects": [], "steps": [], "winner": None, "began": 1.0}

    def write(tables, stamp=None):
        tmstore.kv.set_json(tmstore.watch_key(),
                            {"stamp": time.time() if stamp is None else stamp,
                             "tables": tables})
        boardtm._TABLES.update(t=0.0, d={})
    write({"1-3": {"watchable": True, "state": state}, "1-4": {"watchable": False}})
    t = boardtm.live_tables()
    check("only the watchable matches, with their state", list(t) == ["1-3"]
          and t["1-3"]["players"][0]["name"] == "Lex")
    sm = boardtm.live_summary(t)
    check("the list: where, who, the scores, the turn",
          sm == [{"id": "1-3", "room": 1, "table": 3, "phase": "play", "com": False,
                  "names": ["Lex", "Cor*vin"], "scores": [2, 1], "turn": 3, "limit": 10,
                  "n": 2}], sm)
    write({"1-3": {"watchable": True, "state": state}}, stamp=time.time() - 120)
    check("a stale document (the writer is gone) shows nothing", boardtm.live_tables() == {})
    write({"1-3": {"watchable": True, "state": state}})
    ok, st = boardtm.route("/watch.json", {"t": ["1-3"]}, args)[:2]
    check("/watch.json?t= serves that match", ok == 200 and json.loads(st)["state"]["id"] == "1-3")
    ok, st = boardtm.route("/watch.json", {"t": ["1-4"]}, args)[:2]
    check("...a match not shown is an answer, state null (not a 404)",
          ok == 200 and json.loads(st) == {"id": "1-4", "state": None})
    ok, st = boardtm.route("/watch.json", {}, args)[:2]
    check("/watch.json alone lists them", ok == 200 and json.loads(st)["tables"][0]["id"] == "1-3")
    ok, page, ctype = boardtm.route("/watch", {}, args)[:3]
    check("/watch is the page", ok == 200 and ctype.startswith("text/html")
          and "watch.json" in page and "Tetra Master - Watch" in page)
    print("watching -- names and faces")
    named = copy.deepcopy(state)
    named.update(id="2-7", room=2, table=7, com=True, board={"5": {
        "id": 65, "owner": 0, "placer": 0, "arrows": 4, "atk": 90, "type": 0, "pdef": 20,
        "mdef": 20, "ability": 0, "mod": 0}})
    named["players"] = [{"name": "Elena", "com": False, "hand": 3, "score": 1, "ci": None, "mid": 3},
                        {"name": "COM 4", "com": True, "hand": 3, "score": 0, "ci": 4, "mid": None}]
    write({"2-7": {"watchable": True, "state": named}})
    boardtm._FACE_IDS.update(t=0.0, map={})
    got = boardtm.live_tables()["2-7"]
    p0, p1 = got["players"]
    check("a player's own PlayOnline portrait: field 19 of the PRIMARY handle",
          p0.get("face_id") == 2439, p0)
    served = boardtm.route("/watch.json", {"t": ["2-7"]}, args)[1] + boardtm.route(
        "/watch.json", {}, args)[1]
    check("...and the member id never leaves the board", "mid" not in p0 and "mid" not in p1
          and '"mid"' not in served)
    check("a COM by its own name and portrait (PlPrm.BIN 4, gW080 fac00004)",
          p1["name"] == "Bogart the Gladiator" and p1.get("face") == "face_04.png", p1)
    check("cards carry their names", got["board"]["5"]["name"] == boardtm.tm_cardprm.name(65))
    if not os.path.exists(os.path.join(boardtm.FACES_DIR, "hnf304.png")):
        # the portrait sheets are cut from the user's Viewer, not shipped
        print("[SKIP] /face.png: services/boardart/faces holds no portrait sheets")
    else:
        code, png, ctype, cache = boardtm.route("/face.png", {"id": ["2439"]}, args)[:4]
        import io
        from PIL import Image
        check("/face.png cuts one 64x96 portrait out of hnf304",
              code == 200 and ctype == "image/png"
              and Image.open(io.BytesIO(png)).size == (64, 96))
        check("...no portrait (0) or no such sheet is a 404, garbage a 400",
              boardtm.route("/face.png", {"id": ["0"]}, args)[0] == 404
              and boardtm.route("/face.png", {"id": ["65535"]}, args)[0] == 404
              and boardtm.route("/face.png", {"id": ["x"]}, args)[0] == 400)
    write({"1-3": {"watchable": True, "state": state}})

    boardtm._SNAP.update(t=0.0, snap=None)
    snap = boardtm.snapshot(args)
    check("the rankings snapshot carries the live list", [x["id"] for x in snap["live"]] == ["1-3"])

    print("watching -- the live Discord posts")
    msgs = boardtm.discord_live_messages(snap, args)
    p = msgs[0]["build"]()[0]
    check("a post per watchable match, with who, where, the turn and the Watch link",
          [m["slot"] for m in msgs] == ["live-1-3"] and not msgs[0]["ended"]
          and "Lex vs Corvin" in p["content"] and "Room 1, Table 3" in p["content"]
          and "turn 4 of 10" in p["content"] and "https://tm.example/watch#1-3" in p["content"]
          and "Cor*vin" not in p["content"] and p["flags"] == 4, p)
    pb = boardtm.discord_live_messages(snap, args, bot=True)[0]["build"]()[0]
    check("...as the bot: a 'Watch this match' button, not a bare link",
          "https://" not in pb["content"]
          and pb["components"][0]["components"][0]["url"] == "https://tm.example/watch#1-3"
          and pb["components"][0]["components"][0]["label"] == "Watch this match")
    check("...deleted as soon as the match is gone", boardtm.discord_live_ended_ttl == 0.0
          and polboards.feed_fn(boardtm, "live", "ended_ttl") == 0.0)
    moved = copy.deepcopy(snap)
    moved["live"][0]["turn"] = 4
    check("...and edited as the turn moves on",
          boardtm.discord_live_messages(moved, args)[0]["sig"] != msgs[0]["sig"])
    check("/tmboard live moves it", polboards.feed_names(boardtm, "tm").get("live") == "tm_live")
    net = AutoNet(channel="55")
    d = polboards.run_bot_feed(boardtm, args, "tm_live", "live",
                               "https://discord.com/api/webhooks/7/LIVESECRET", "BOTTOKEN",
                               opener=net, rounds=1, period=0.01)
    check("as the bot, the live feed keeps ITS own 0 s end (not the 10 min default)",
          d is not None and d.ended_ttl == 0.0 and any(
              m == "POST" and u.endswith("/channels/55/messages") for m, u, _b in net.calls), d and d.ended_ttl)
    tmstore.kv.delete(tmstore.watch_key())
    boardtm._TABLES.update(t=0.0, d={})


def main():
    tmp = tempfile.mkdtemp(prefix="boardtm-")
    res = setup(tmp)
    import boardtm
    import polboards
    if not os.path.exists(os.path.join(boardtm.ART_DIR, "backdrop.png")):
        # The board draws with art baked from the client, which does not ship;
        # every feed below renders through it, so there is nothing to pin yet.
        print("[SKIP] tm_board: services/boardart/tm holds no baked art -- run "
              "tools/tm_boardart_bake.py against your client first")
        return
    print("imports")
    # the names and portraits come through OpenLobby's accounts functions,
    # so `accounts` is loaded; the game servers and the lobby are not
    check("the board pulls in no game server and no responders",
          not {"responders", "tetramaster", "tmroom"} & set(sys.modules),
          sorted({"responders", "tetramaster", "tmroom"} & set(sys.modules)))
    accounts_before = tmpg.accounts_fingerprint()

    before = tree(tmp)
    s = boardtm.snapshot(now=NOW)
    print("the published rankings")
    tabs = s["tabs"]
    check("five published lists in the menu's order, then this week",
          [t["name"] for t in tabs] == ["VS. Rating", "Top 30", "Best Rookies", "Grand Total",
                                        "Weekly Total", "This Week"]
          and all(t["published"] for t in tabs[:5]) and not tabs[5]["published"])
    vs = tabs[0]["rows"]
    check("VS. Rating: the file's order and the client's %d.%02d",
          [(r["rank"], r["name"], r["text"]) for r in vs]
          == [(1, "Lex", "4.00"), (2, "Corvin", "3.64"), (3, "Tie A", "1.93"),
              (3, "Tie B", "1.93"), (5, "-------", "1.50")], vs)
    check("equal values share a rank, and the next rank skips (competition ranking)",
          [r["rank"] for r in vs] == [1, 2, 3, 3, 5])
    check("last week's rank: up / new / same / up / down",
          [(r["move"], r["last"]) for r in vs]
          == [("up", 2), ("new", None), ("same", 3), ("up", 9), ("down", 4)], vs)
    check("a hide-name row draws '-------' (Ranking.BIN record 11)", vs[4]["name"] == "-------")
    gt = tabs[3]["rows"]
    check("Grand Total is money, comma-grouped, best first",
          tabs[3]["money"] and [r["text"] for r in gt[:2]] == ["12,652", "295"])
    wk = tabs[4]["rows"]
    check("Weekly Total: zeros tie at 3rd -- and get no medal",
          [r["rank"] for r in wk] == [1, 2, 3, 3, 3]
          and [boardtm.medal(r) for r in wk] == [True, True, False, False, False])
    check("the header's next update and tally period ride along",
          s["week"]["next"] == NEXT and s["week"]["to"] - s["week"]["from"] == 6 * 86400)

    print("this week so far (live)")
    live = tabs[5]["rows"]
    check("only members who played this week with the minimum games, best VS. Rating first",
          [r["member"] for r in live] == ["1", "3", "4"], live)
    check("...not the best career rating idle since last week, not a 1-game 4.00",
          not {"5", "6"} & {r["member"] for r in live}
          and boardtm.tmrank.min_games() == 5, live)
    check("games = this week's count; before the counter existed, the career count",
          [r["games"] for r in live] == [3, 6, 1], live)
    check("names: the TM roster first, then the account database's primary handle",
          [r["name"] for r in live] == ["Lex", "Elena", "Star*Man"], live)
    check("the rating is tmrank's one formula", live[0]["rating"] == 370
          and live[0]["rating_text"] == "3.70" and live[0]["prize_text"] == "700", live[0])

    print("the auction")
    a = s["auction"]
    check("listings: every stored one except SOLD (past its end with bids)",
          sorted(r["ai"] for r in a["listings"]) == [21, 22, 24]
          and [r["ai"] for r in a["sold"]] == [23], (a["listings"], a["sold"]))
    check("the staged AUCLIST / BIDLIST copies are never read",
          all(r["seller"] != "Ghost" for r in a["listings"] + a["sold"]))
    check("the Price List counts: All 3, Cheap 2, Affordable 1, Expensive 0, Exorbitant 0",
          [b["count"] for b in a["bands"]] == [3, 2, 1, 0, 0], a["bands"])
    by = {r["ai"]: r for r in a["listings"]}
    c21 = by[21]["card"]
    check("<II>: type is u8[2], P defense u8[1] (transposed against the shop)",
          c21["name"] == "Bahamut" and c21["type"] == "M" and c21["pdef"] == 77
          and c21["atk"] == 90 and c21["mdef"] == 35 and c21["arrows"] == 0b10000001, c21)
    check("no bids = no bidder (the client draws '-' when <BC> is 0)",
          by[21]["bidder"] == "" and by[21]["bids"] == 0)
    check("...and a reused id's OLD bids (placed before the listing) are not its bids",
          by[21]["bids"] == 0 and all(x["who"] != "Ghost" for x in a["activity"]))
    check("the bid count comes from the bid FILE, the high bid from <CM>",
          by[22]["bids"] == 2 and by[22]["bidder"] == "Corvin" and by[22]["price_text"] == "1,800")
    check("Time Left is the client's: days of 24 hours, then hours, 0 at the end",
          by[21]["left_text"] == "3d 5h" and by[22]["left_text"] == "2h"
          and by[24]["left_text"] == "0h", [by[k]["left_text"] for k in (21, 22, 24)])
    check("a sold card names its winner and price", a["sold"][0]["winner"] == "Lex"
          and a["sold"][0]["price_text"] == "150")
    check("recent activity is newest first: the sale, the bids, the listings",
          [(x["kind"], x["who"]) for x in a["activity"][:4]]
          == [("sold", "Lex"), ("bid", "Corvin"), ("bid", "Quinn"), ("listed", "Lex")]
          and len(a["activity"]) == 8, a["activity"])
    check("matches in progress come from the live marker", s["matches_live"] == 2
          and s["players"] == 6)

    if not os.path.exists(os.path.join(boardtm.ART_DIR, "backdrop.png")):
        print("[SKIP] the renders: services/boardart/tm holds no baked art "
              "(run tools/tm_boardart_bake.py against your client first)")
    else:
        print("the renders")
        for t in range(7):
            png = boardtm.render(t, 0, s)
            check("tab %d renders the window (2x, 1280x896)" % t,
                  png[:8] == b"\x89PNG\r\n\x1a\n" and png_size(png) == (1280, 896))
        check("a page past the end is clamped",
              boardtm.render(0, 99, s) == boardtm.render(0, 0, s))

    args = polboards.build_parser().parse_args(["--tm-port", "1", "--tm-url", "https://tm.example"])
    ranking_feed_checks(boardtm, polboards, s, args)
    auction_feed_checks(boardtm, polboards, s, args)
    set_checks(tmp, boardtm, polboards, s, args)

    print("polboards -- the extra feed's flags")
    check("tm has three feeds: the rankings, the auction, the live matches",
          polboards.feed_keys("tm") == [("", "tm"), ("auction", "tm_auction"),
                                        ("live", "tm_live")]
          and "tm_live" in polboards.FEED_SHARES_MAIN
          and polboards.feed_keys("jan")[0] == ("", "jan"))
    os.environ["POL_BOARDS_TM_AUCTION_DISCORD_WEBHOOK"] = "https://discord.com/api/webhooks/9/AUCSECRET"
    a2 = polboards.build_parser().parse_args(["--tm-port", "1"])
    del os.environ["POL_BOARDS_TM_AUCTION_DISCORD_WEBHOOK"]
    check("...its webhook comes from POL_BOARDS_TM_AUCTION_DISCORD_WEBHOOK",
          a2.tm_auction_discord_webhook.endswith("/AUCSECRET") and a2.tm_discord_webhook == ""
          and a2.jan_discord_webhook == "")
    os.environ["POL_BOARDS_STATE_DIR"] = os.path.join(tmp, "state")
    check("...and keeps its own message id file",
          polboards.state_path(a2, "tm_auction").endswith("tm_auction_discord.json")
          and polboards.state_path(a2, "tm").endswith("tm_discord.json"))
    check("a message-set feed's knobs: 15 s per message, ended messages kept 10 min",
          a2.discord_slot_every == 15.0 and a2.discord_ended_ttl == 600.0)
    bot_checks(tmp, boardtm, polboards, s, args)
    watch_checks(tmp, boardtm, polboards, args)

    print("read-only")
    after = tree(tmp)
    after = {k: v for k, v in after.items() if not k.startswith("state" + os.sep)}
    check("every game file is byte-identical after snapshots, renders and every message",
          after == before, sorted(set(after.items()) ^ set(before.items()))[:6])

    print("an unpublished list")
    os.remove(os.path.join(res, "tmrank", "U_g_TM0_RANKLIST1.bin"))
    boardtm._SNAP.update(t=0.0, snap=None)
    s2 = boardtm.snapshot(now=NOW)
    check("a missing list is 'not published', never the fixture's zero row",
          not s2["tabs"][4]["published"] and s2["tabs"][4]["rows"] == [])
    check("...and it still renders", boardtm.render(4, 0, s2)[:4] == b"\x89PNG")
    check("the account tables are untouched by every snapshot and render",
          tmpg.accounts_fingerprint() == accounts_before)

    def unreachable():
        raise ConnectionError("the account database is down")
    real_conn = boardtm._accounts_conn
    boardtm._accounts_conn = unreachable
    boardtm._NAMES.update(t=0.0, map={})
    try:
        s3 = boardtm.snapshot(now=NOW)
    finally:
        boardtm._accounts_conn = real_conn
    check("with the account database unreachable the rows still come back",
          [r["name"] for r in s3["tabs"][5]["rows"]] == ["Lex", "", "Star*Man"])

    print("the service")
    x = socket.socket()
    x.bind(("127.0.0.1", 0))
    port = x.getsockname()[1]
    x.close()
    sargs = polboards.build_parser().parse_args(["--tm-port", str(port)])
    srv = polboards.serve(boardtm, sargs, port)
    base = "http://127.0.0.1:%d" % port

    def get(p):
        try:
            with urllib.request.urlopen(base + p, timeout=20) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()
    try:
        st, ct, body = get("/")
        check("/ is the page", st == 200 and ct.startswith("text/html") and b"Tetra Master" in body)
        st, ct, body = get("/state.json")
        j = json.loads(body)
        check("/state.json is the snapshot", st == 200 and j["board"] == "tm"
              and len(j["tabs"]) == 6 and "auction" in j)
        for bad in ("/art/../boardtm.py", "/art/%2e%2e/tmrank.py", "/art/tm-text.ttf",
                    "/art/nope.png"):
            check("%s is refused" % bad, get(bad)[0] == 404)
        st, ct, _ = get("/art/tm-text.woff2")
        check("the text face is served as font/woff2", st == 200 and ct == "font/woff2")
        st, ct, body = get("/art/board.json")
        check("the layout is served as JSON", st == 200 and "tabs_caption" in json.loads(body))
        st, ct, body = get("/art/card_065.png")
        check("card art is served", st == 200 and ct == "image/png" and body[:4] == b"\x89PNG")
        st, ct, body = get("/render.png?tab=6")
        check("/render.png draws the auction", st == 200 and ct == "image/png")
        check("/render.png refuses tab 7", get("/render.png?tab=7")[0] == 400)
        check("/healthz", get("/healthz")[0] == 200)
        check("--tm-port 0 would start nothing", polboards.build_parser().parse_args([]).tm_port == 0)
    finally:
        srv.shutdown()
    print("[tm_board_test] OK -- %d checks" % len(CHECKS))


if __name__ == "__main__":
    _url = tmpg.fresh_database()
    if _url is None:
        sys.exit(tmpg.skip_or_fail("tm_board_test"))
    # The account tables are read from this database: polcore.db is pointed at
    # it directly. POL_DATABASE_URL itself is dropped, so the board's Discord
    # bookkeeping stays on the state files this suite pins, as in a checkout
    # with no database (polboards.state_path).
    from polcore import db as _db
    _db.configure(_url)
    os.environ.pop("POL_DATABASE_URL", None)
    main()
