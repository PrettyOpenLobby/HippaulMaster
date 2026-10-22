"""tmadmin.py -- Tetra Master in an OpenLobby admin panel (the panel's admin protocol, olport.py).

Runs inside the Tetra Master board's process (boardtm.start), which has this repository's code
and the database but holds no player's connection: restarting it disconnects nobody. Off unless
POL_TM_ADMIN_PORT is set.

  POL_TM_ADMIN_PORT    the HTTP port, e.g. 8821
  POL_TM_ADMIN_BIND    default 127.0.0.1; the panel's container reaches this host-networked
                       process through the Docker gateway (for example 172.17.0.1, Docker's default bridge, with a token)
  POL_TM_ADMIN_TOKEN   the token the panel sends (POL_ADMIN_GAME_TM_TOKEN on its side)

The panel gets three pages, each its own moderator permission, and their actions:

  Auction house   every listing (seller, card, price, high bid, time left); Take down returns
                  the card to its seller's Check Out the way an unsold listing is returned
                  (tmplugin.auction's sweep), and refuses a listing that has bids
  Tournament      the cup the server runs now: phase, window, missions, prizes (tmcup)
  Gifts           gil to a player's Check Out ("Collecting Payment", the auction's payout path),
                  or a card into their collection (rolled and issued exactly as the card shop
                  issues one, tmgame.cardshop), refused while they are signed in to PlayOnline,
                  because a running client can save its old collection over the new card

The account window's "In the games" asks it about one player (player.json): each of the
account's members with a collection, by handle, with its cards, gil, card level, VS. rating,
average finish and title, and whether it is in a Tetra Master room now.

Every change is logged here and, through the panel, in the panel's activity log with the
moderator's name.
"""
import os
import random
import sys
import time

import olpage
import olport
import tm_cardprm
import tmauction
import tmblob
import tmcup

PORT = int(os.environ.get("POL_TM_ADMIN_PORT", "0") or 0)
BIND = os.environ.get("POL_TM_ADMIN_BIND", "127.0.0.1")
TOKEN = os.environ.get("POL_TM_ADMIN_TOKEN", "")
GIL_MAX = 1_000_000


def _say(msg):
    print("[tmadmin] " + msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# reading the game's records
# --------------------------------------------------------------------------- #
def _auction():
    from tmplugin import auction
    return auction


def listings(now=None):
    """Every listing: [{ai, seller_member, seller, card_id, card, price, high, bids, ends, store}]."""
    auc = _auction()
    now = time.time() if now is None else now
    out = []
    for fn in auc._exhibit_names():
        try:
            blob = auc._read_resource(fn)
        except auc._store_errors():
            continue
        for i in range(tmauction.count(blob or b"")):
            try:
                r = tmauction.read_record(blob, i)
            except ValueError:
                break
            nbids = tmauction.bid_count(auc._auction_bids(r["ai"]))
            out.append({"ai": r["ai"], "seller_member": fn.split(".", 1)[0], "seller": r["seller"],
                        "card_id": r["ii"][0], "card": tm_cardprm.name(r["ii"][0]),
                        "price": r["sp"], "high": r["cm"], "bidder": r["bidder"], "bids": nbids,
                        "ends": r["nc"], "left": max(0, int(r["nc"] - now)), "store": fn})
    out.sort(key=lambda x: x["ends"])
    return out


def take_down(ai, actor):
    """Remove listing `ai` and queue its card back to the seller -> (ok, message)."""
    auc = _auction()
    try:
        ai = int(ai)
    except (TypeError, ValueError):
        return False, "No such listing."
    hit = auc._auction_find(ai)
    if hit is None:
        return False, "Listing %d is gone (sold, returned or taken down)." % ai
    fn = hit[0]
    if tmauction.bid_count(auc._auction_bids(ai)):
        return False, ("Listing %d has bids. Bidders' gil is held on it, so it settles at its end "
                       "instead." % ai)
    with tmblob.locked(fn) as conn:
        blob = tmblob.read(fn, conn=conn) or b""
        idx = next((i for i in range(tmauction.count(blob))
                    if tmauction.read_record(blob, i)["ai"] == ai), None)
        if idx is None:
            return False, "Listing %d changed while you looked; reload the page." % ai
        rec = tmauction.read_record(blob, idx)
        seller = fn.split(".", 1)[0]
        # the card is owed first: if this fails the listing stays, so the card is never lost
        tmauction.add_pending(seller, card=rec["ii"])
        keep = b"".join(blob[k * tmauction.REC:(k + 1) * tmauction.REC]
                        for k in range(tmauction.count(blob)) if k != idx)
        tmblob.write(fn, keep, conn=conn)
    card = tm_cardprm.name(rec["ii"][0])
    _say("%s took down auction %d (%s by %s); card queued to member %s" % (actor, ai, card,
                                                                             rec["seller"], seller))
    try:
        auc._auction_notice(seller, "returned", card=card)
    except Exception:                                   # noqa: BLE001 -- the courtesy, not the card
        pass
    return True, "%s went back to %s's Check Out." % (card, rec["seller"] or "the seller")


def member_of(handle):
    """(member id, the handle as stored) for a handle, or (None, None)."""
    import accounts
    db = accounts.connect()
    try:
        r = db.execute("SELECT member_id, handle_name FROM handle WHERE lower(handle_name) = lower(%s)",
                       (str(handle or "").strip(),)).fetchone()
        return (r["member_id"], r["handle_name"]) if r else (None, None)
    finally:
        db.close()


def signed_in(member_id):
    """Whether the member has a live PlayOnline session (the panel's own Online query)."""
    import accounts
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db = accounts.connect()
    try:
        return db.execute("SELECT 1 FROM session WHERE member_id = %s AND expires_at > %s",
                          (member_id, now)).fetchone() is not None
    finally:
        db.close()


def give_gil(handle, amount, actor):
    try:
        amount = int(str(amount).replace(",", "").strip())
    except ValueError:
        return False, "The amount is a whole number of gil."
    if not 0 < amount <= GIL_MAX:
        return False, "Give between 1 and %d gil." % GIL_MAX
    member, name = member_of(handle)
    if member is None:
        return False, "No player with the handle %r." % str(handle or "")
    tmauction.add_pending(member, money=amount)
    _say("%s gave %d gil to %s (member %s) through Check Out" % (actor, amount, name, member))
    return True, "%d gil is waiting for %s at the auction's Check Out." % (amount, name)


def card_choices():
    """[(id, name)] of every card, by name."""
    n = len(tm_cardprm.NAMES)
    return sorted(((i, tm_cardprm.name(i)) for i in range(n) if tm_cardprm.name(i) not in ("", "?")),
                  key=lambda c: c[1].lower())


def mint_card(card_id, rnd=None):
    """A new instance of `card_id`, issued exactly as the card shop issues one
    (tmgame.cardshop._draw_pack): stats rolled below the card's ceiling, its power
    byte, a fresh arrow draw and a new-card slot."""
    import tmbattle
    import tmroll
    from tmgame import cardshop, collection
    rnd = rnd or random.Random()
    base = tm_cardprm.row(card_id)
    atk, pdf, mdf = (tmroll.roll_stat(rnd, base[0]), tmroll.roll_stat(rnd, base[2]),
                     tmroll.roll_stat(rnd, base[3]))
    return [int(card_id), atk, base[1], pdf, mdf,
            tmbattle.power_byte(base[4], atk, pdf, mdf, base[0], base[2], base[3]),
            cardshop._card_arrows(rnd, base), collection._new_card_slot(base[6])]


def find_card(text):
    """A card id from a number or a name (case and spaces ignored), or None."""
    text = str(text or "").strip()
    if text.isdigit():
        i = int(text)
        return i if tm_cardprm.name(i) not in ("", "?") else None
    want = text.lower().replace(" ", "")
    for i, name in card_choices():
        if name.lower().replace(" ", "") == want:
            return i
    return None


def give_card(handle, card, actor):
    from tmgame import collection
    member, name = member_of(handle)
    if member is None:
        return False, "No player with the handle %r." % str(handle or "")
    cid = find_card(card)
    if cid is None:
        return False, "No card called %r." % str(card or "")
    if signed_in(member):
        return False, ("%s is signed in to PlayOnline. A running Tetra Master can save its old "
                       "collection over a new card; give it once they have signed out." % name)
    row = mint_card(cid)
    collection._collection_add_cards(member, [row])
    _say("%s gave %s (card %d, row %s) to %s (member %s)" % (actor, tm_cardprm.name(cid), cid, row,
                                                              name, member))
    return True, "%s is in %s's collection; they see it the next time they start Tetra Master." % (
        tm_cardprm.name(cid), name)


# --------------------------------------------------------------------------- #
# one player, for the panel's account window
# --------------------------------------------------------------------------- #
def members_of(who):
    """{member id: handle name} for the account the panel describes: its member ids, its
    handle names (any case) and its POL ID, each as the account tables hold them."""
    import accounts
    ids = set()
    for m in who.get("members") or []:
        if str(m).strip().isdigit():
            ids.add(int(str(m).strip()))
    db = accounts.connect()
    try:
        for h in who.get("handles") or []:
            r = db.execute("SELECT member_id FROM handle WHERE lower(handle_name) = lower(%s)",
                           (str(h or "").strip(),)).fetchone()
            if r:
                ids.add(int(r["member_id"]))
        polid = str(who.get("polid") or "").strip().upper()
        if polid:
            ids |= {int(r["id"]) for r in db.execute(
                "SELECT id FROM member WHERE polid = %s", (polid,)).fetchall()}
        return {m: accounts.primary_handle(db, m) or "" for m in sorted(ids)}
    finally:
        db.close()


def _x100(v):
    return "%d.%02d" % (int(v) // 100, int(v) % 100)


def player(who):
    """What Tetra Master knows of one account: one entry per member with a collection,
    named by its handle -> {"characters": [...]} or None when none has played."""
    import tm_title
    import tmrank
    import tmroom
    chars = []
    pools = tmrank.observed_pools()
    for mid, name in members_of(who).items():
        data = tmrank.collection_of(mid)
        if not data:
            continue
        stats = tmrank.stats_of(data)
        blk = data.get("rank") if isinstance(data.get("rank"), dict) else {}
        money = int(data.get("money") or 0)
        cards = data.get("cards") if isinstance(data.get("cards"), list) else []
        level = (pools.get(str(mid)) or {}).get("card_level")
        avg = int(blk.get("avg_rank") or 0)
        facts = [{"label": "cards", "value": "{:,}".format(len(cards))},
                 {"label": "gil", "value": "{:,}".format(money)}]
        if level is not None:
            facts.append({"label": "card level", "value": str(level)})
        if stats["games"]:
            facts.append({"label": "VS. rating", "value": _x100(stats["rating"])})
            if avg:
                facts.append({"label": "average finish", "value": _x100(avg)})
            if int(blk.get("streak") or 0):
                facts.append({"label": "best win streak", "value": str(int(blk["streak"]))})
        title = (tm_title.title_name(tm_title.title_index(money, level or 0, avg))
                 if stats["games"] and avg else "")
        games = stats["games"]
        detail = ", ".join(x for x in (title, "%d game%s" % (games, "" if games == 1 else "s")) if x)
        try:
            online = tmroom.room_of(mid) is not None and signed_in(mid)
        except Exception:                               # noqa: BLE001 -- the room list is a nicety
            online = None
        chars.append({"name": name or "member %d" % mid, "detail": detail, "online": online,
                      "facts": facts[:6]})
    return {"characters": chars, "note": ""} if chars else None


# --------------------------------------------------------------------------- #
# the pages
# --------------------------------------------------------------------------- #
def _left(sec):
    if sec <= 0:
        return "ended"
    h, m = divmod(sec // 60, 60)
    return "%dh %02dm" % (h, m) if h else "%dm" % m


def auction_page(note=None):
    rows = []
    for x in listings():
        rows.append([x["card"], x["seller"], "{:,}".format(x["price"]),
                     ("{:,} by {}".format(x["high"], x["bidder"]) if x["bids"] else olpage.Raw(
                         '<span class="muted">no bids</span>')),
                     _left(x["left"]), olpage.Raw('<span class="mono">%d</span>' % x["ai"]),
                     olpage.form([], "Take down", hidden={"take": x["ai"]}, danger=True, inline=True,
                                 confirm="Returns the card to the seller's Check Out")
                     if not x["bids"] else ""])
    return olpage.page(
        "Auction house", olpage.panel("", olpage.table(
            ["Card", "Seller", "Price", "High bid", "Time left", "Id", ""], rows,
            empty="Nothing is up for auction.")),
        sub="Every listing on the server. Taking one down returns the card to its seller's Check "
            "Out. A listing with bids settles at its end.", note=note)


def tournament_page():
    info = tmcup.event_info() or {}
    phase, start, end = tmcup.event_phase()
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t)) if t else "not set"
    missions = [[tmcup.mission_text(t, n)] for t, n in (tmcup.event_missions(info) or [])]
    p = tmcup.event_prizes(info)
    places = ("1st", "2nd", "3rd")
    prizes = [[places[i], "{:,} gil".format(p["money"][i]), "%d card%s" % (p["cards"][i], "" if p["cards"][i] == 1 else "s")]
              for i in range(3)]
    return olpage.page(
        "Tournament",
        olpage.panel("Now", olpage.kv([("Event", info.get("name") or "-"), ("Phase", PHASES.get(phase, phase)),
                                        ("Starts", fmt(start)), ("Ends", fmt(end))])),
        olpage.grid(
            olpage.panel("Missions", olpage.table(["Mission"], missions, empty="No missions.")),
            olpage.panel("Prizes", olpage.table(["Place", "Gil", "Cards"], prizes),
                         olpage.kv([("Pack for taking part", p["pack"]),
                                    ("Per mission cleared", "{:,} gil".format(p["mission_money"]))]))),
        sub="The cup the server runs now (tmcup: the event calendar, or the POL_TM_EVENT_* settings).")


#: tmcup.event_phase's words, as a moderator reads them
PHASES = {"pending": "Not started yet", "running": "Running", "over": "Tallying the last games",
          "closed": "Finished: results and prizes are open"}


def gifts_page(note=None):
    return olpage.page(
        "Gifts",
        olpage.panel("Gil", olpage.form([("handle", "Player handle", ""), ("gil", "Gil", "")],
                                        "Give gil")),
        olpage.panel("A card", olpage.form([("handle", "Player handle", ""),
                                            ("card", "Card name or number", "")], "Give the card")),
        sub="Gil waits at the auction's Check Out as \"Collecting Payment\". A card is issued as the "
            "card shop issues one, with rolled stats; give it while the player is signed out.",
        note=note)


def build():
    port = olport.AdminPort(
        "tm", "Tetra Master", platform="PlayOnline: PC and PS2", token=TOKEN, bind=BIND, port=PORT,
        about="Matches run in authsess; these tools run in the Tetra Master board.",
        content_ids=[2])

    @port.status
    def status():
        try:
            import live_sessions
            m = live_sessions.read_marker("tm") or {}
            matches = int(m.get("count") or 0) if time.time() - float(m.get("stamp") or 0) < 60 else None
        except Exception:                               # noqa: BLE001
            matches = None
        rows = listings()
        facts = [{"label": "listings", "value": len(rows)},
                 {"label": "cup", "value": PHASES.get(tmcup.event_phase()[0], "?")}]
        if matches is not None:
            facts.insert(0, {"label": "matches", "value": matches})
        return {"up": True, "rooms_label": "listings",
                "rooms": [{"name": "%s by %s" % (x["card"], x["seller"]),
                           "detail": "%s gil, %s" % ("{:,}".format(x["high"] or x["price"]), _left(x["left"])),
                           "state": "bids" if x["bids"] else "open"} for x in rows[:50]],
                "facts": facts}

    port.player(player)

    @port.tool("auction", "Auction house", "auction")
    def auction(method, sub, query, body, actor):
        note = None
        if method == "POST":
            f = olpage.posted(body)
            if f.get("take"):
                note = take_down(f["take"], actor)
        return 200, olpage.CTYPE, auction_page(note).encode()

    @port.tool("cup", "Tournament", "cup")
    def cup(method, sub, query, body, actor):
        return 200, olpage.CTYPE, tournament_page().encode()

    @port.tool("gifts", "Gifts", "gifts")
    def gifts(method, sub, query, body, actor):
        note = None
        if method == "POST":
            f = olpage.posted(body)
            if f.get("gil"):
                note = give_gil(f.get("handle"), f["gil"], actor)
            elif f.get("card"):
                note = give_card(f.get("handle"), f["card"], actor)
        return 200, olpage.CTYPE, gifts_page(note).encode()

    @port.action("gil", "Give gil", fields=[{"id": "handle", "label": "Player"},
                                            {"id": "amount", "label": "Gil"}],
                 about="Waits at the auction's Check Out.")
    def gil(params, actor):
        return give_gil(params.get("handle"), params.get("amount"), actor)

    @port.action("takedown", "Take down a listing", danger=True,
                 fields=[{"id": "id", "label": "Listing id"}],
                 about="Returns the card to the seller's Check Out.")
    def takedown(params, actor):
        return take_down(params.get("id"), actor)

    return port


def start():
    """Called from boardtm.start. Never raises: a bad setting is logged, the board runs on."""
    if not PORT:
        return None
    try:
        port = build()
        host, n = port.start()
    except (OSError, ValueError) as e:
        _say("admin port not started (%s)" % e)
        return None
    _say("OpenLobby admin protocol on %s:%d" % (host, n))
    return port
