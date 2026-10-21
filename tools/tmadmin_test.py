#!/usr/bin/env python3
"""tmadmin_test.py -- Tetra Master's tools for an OpenLobby admin panel (services/tmadmin.py),
over HTTP, the way the panel's proxy calls them.

    python tools/tmadmin_test.py

Two made-up players, one signed in; one listing with no bids and one with a bid. Checks the
token, the description, the status, the three pages, taking a listing down (its card owed back
to the seller, the row gone, a listing with bids refused), gil to Check Out, and a card into a
signed-out player's collection (refused while signed in). Throwaway PostgreSQL (tmpg.py).
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="tmadmin-")
os.environ["POL_RESOURCE_DIR"] = TMP
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_TM_ADMIN_TOKEN"] = TOKEN = "tmadmin-test-token-1"
os.environ["POL_TM_AUCTION_NOTICE"] = "0"
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, os.pardir, "services"))
import tmpg         # noqa: E402

if tmpg.fresh_database() is None:
    sys.exit(tmpg.skip_or_fail("tmadmin_test"))

import tmtitle      # noqa: E402,F401  the title's modules, as the board process has them
import accounts     # noqa: E402
import tmadmin      # noqa: E402
import tmauction    # noqa: E402
import tmblob       # noqa: E402
from tmgame import collection  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            "  " + str(detail) if detail and not ok else ""), flush=True)
    if not ok:
        fails.append(name)


c = accounts.connect()
accounts.create_polid(c, "LEXP1234", "polid-pw-1")
lex = accounts.add_member(c, "LEXP1234", "Lex", "pw-lex-0001")
accounts.set_handle(c, lex, "Lex")
accounts.create_polid(c, "MIRA5678", "polid-pw-2")
mira = accounts.add_member(c, "MIRA5678", "Mira", "pw-mira-0001")
accounts.set_handle(c, mira, "Mira")
accounts.open_session(c, mira, nick="Mira", peer_ip="127.0.0.1")
c.commit()
c.close()

# two listings in Lex's store: auction 5 (no bids) and auction 6 (one bid by Mira)
ii = [3, 0, 0, 0, 0, 0, 0, 0, 3, 10, 1, 20, 30, 0, 0, 0]
now = int(time.time())
store = "%s.%s" % (lex, "U_g_TM0_EXHIBITLIST.bin")
tmblob.write(store, tmauction.build_record(ii, seller="Lex", ai=5, sp=300, nc=now + 7200) +
             tmauction.build_record(ii, seller="Lex", ai=6, sp=400, cm=450, bidder="Mira", nc=now + 3600))
tmblob.write("auction-6.bids.bin", tmauction.build_bid("Mira", 450, now, mira))

port = tmadmin.build()
host, n = port.start()
url = "http://127.0.0.1:%d" % n


def get(path, form=None, token=TOKEN):
    data = None if form is None else urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url + path, data=data, method="GET" if form is None else "POST",
                                 headers={"X-Devtool-Token": token, "X-Devtool-Actor": "lex-gm",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


check("without the token nothing answers", get("/openlobby/module.json", token="x")[0] == 403)
desc = json.loads(get("/openlobby/module.json")[1])
check("it describes itself as tm with three pages",
      (desc["key"], [t["id"] for t in desc["tools"]]) == ("tm", ["auction", "cup", "gifts"]), desc)
live = json.loads(get("/openlobby/status.json")[1])
check("the status lists both listings, the one with a bid marked",
      [r["state"] for r in live["rooms"]] == ["bids", "open"], live["rooms"])

st, body = get("/auction")
card = tmadmin.tm_cardprm.name(3)
check("the auction page shows the listings, the card by name",
      st == 200 and card in body and "450 by Mira" in body, body[:300])
check("...with Take down only on the listing without bids", body.count("Take down") == 1)
st, body = get("/auction", {"take": "6"})
check("a listing with bids is refused", "has bids" in body)
st, body = get("/auction", {"take": "5"})
check("taking a listing down says where the card went", "Check Out" in body and card in body, body[-300:])
rest = [tmauction.read_record(tmblob.read(store), i)["ai"] for i in range(tmauction.count(tmblob.read(store)))]
check("...the row is gone and the other stays", rest == [6], rest)
owed = tmauction.pending(lex)
check("...and the card is owed back to the seller verbatim", owed["cards"] == [ii], owed)
check("taking it down again says it is gone", "is gone" in get("/auction", {"take": "5"})[1])

st, body = get("/gifts", {"handle": "mira", "gil": "2,500"})
check("gil waits at Check Out (handle in any case)", tmauction.pending(mira)["money"] == 2500, body[-200:])
check("a silly amount is refused", "between 1 and" in get("/gifts", {"handle": "Mira", "gil": "0"})[1])
check("an unknown handle is refused", "No player" in get("/gifts", {"handle": "Nobody", "gil": "5"})[1])
st, body = get("/gifts", {"handle": "Mira", "card": card})
check("a card is refused while the player is signed in", "signed in" in body and
      not (collection._collection_load(mira).get("cards")), body[-200:])
st, body = get("/gifts", {"handle": "Lex", "card": card.upper()})
got = collection._collection_load(lex).get("cards") or []
check("a card goes into a signed-out player's collection", len(got) == 1 and got[0][0] == 3, got)
base = tmadmin.tm_cardprm.row(3)
check("...issued as the shop issues one: 8 values, stats below the ceiling",
      len(got[0]) == 8 and got[0][1] < base[0] and got[0][3] < base[2] and got[0][4] < base[3], (got, base))
check("an unknown card is refused", "No card" in get("/gifts", {"handle": "Lex", "card": "Nope"})[1])

st, body = get("/cup")
check("the tournament page shows the cup", st == 200 and "Missions" in body and "Prizes" in body)
check("no page carries a script", all("<script" not in get(p)[1] for p in ("/auction", "/cup", "/gifts")))
r = urllib.request.Request(url + "/openlobby/actions/gil", data=json.dumps({"handle": "Lex", "amount": "10"}).encode(),
                           headers={"X-Devtool-Token": TOKEN, "X-Devtool-Actor": "lex-gm"}, method="POST")
with urllib.request.urlopen(r, timeout=15) as resp:
    out = json.loads(resp.read())
check("the gil action works for the panel too", out["ok"] and tmauction.pending(lex)["money"] == 10, out)

port.stop()
print("tmadmin_test: %s" % ("all checks passed" if not fails else "%d FAILED" % len(fails)))
sys.exit(1 if fails else 0)
