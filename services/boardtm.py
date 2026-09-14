#!/usr/bin/env python3
"""boardtm.py -- Tetra Master's RANKINGS and AUCTION as a live board
(polboards, tm.example.com, 2026-09-12).

WHAT IT SHOWS (as chosen for the live deployment):

  * the five PUBLISHED ranking lists, exactly what the game's Rankings screen
    shows -- the weekly files tools/tmrank.py writes every Sunday 00:05 UTC,
    read back with tmrank's own decoders, in the file's row order (the rank IS
    the row position; equal values share it, the client's tie rule);
  * "This Week So Far", OURS and labelled so: live standings from each
    player's collection file, which legitimately differ from the game's
    screen until the next Sunday publish;
  * the AUCTION: every listing in every seller's store, the Price List's five
    bands with the counts the game would show, and the RECENTLY SOLD --
    listings past their end with bids, which the game settles lazily on the
    next browse, so a sale is only knowable until then;
  * on wide screens, the auction's recent activity and the matches in
    progress, beside the window.

The page is drawn from the client's own art (tools/tm_boardart_bake.py ->
services/boardart/tm/board.json), with every word set as real text.

THINGS THIS MODULE MUST NEVER DO -- each is what the obvious call does:

  * run tools/tmrank.py, even --dry-run. --publish rewrites every collection
    file and grants prizes; _from_db opens accounts.db for writing.
  * settle the auction. responders._tm_auction_reply runs _auction_sweep(),
    which credits money, sends POL messages and relists; settlement is lazy on
    purpose. Nothing here imports responders -- the count rule it needs
    (drop listings that are expired AND have bids) is re-implemented below.
  * read *.U_g_TM0_AUCLIST.bin / *.U_g_TM0_BIDLIST.bin: per-request staged
    copies, not the store.
  * open accounts.db through accounts.connect() (a write lock). Names come
    from the TM roster's own JSON, then a read-only SQLite URI.
"""
import glob
import hashlib
import io
import json
import os
import sqlite3
import threading
import time

import tm_cardprm
import tmauction
import tmrank

NAME = "tm"
TITLE = "Tetra Master - Rankings & Auction"
#: the Discord bot's words for this board's feeds: /tmboard rankings | auction | live
DISCORD_TITLE = "Tetra Master"
DISCORD_FEED_NAMES = {"": "rankings", "auction": "auction", "live": "live"}

#: NOT under services/fedata/ (restarts FE) and NOT named tm*.py (pol-git-sync's
#: TM rule restarts login + authsess for those).
ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boardart", "tm")

#: (menu index, RankID) in the Rankings menu's order (tmrank's banner). Menu 3,
#: Prize Center, is the prize shop and shows no list.
RANK_TABS = ((0, 2), (1, 3), (2, 4), (4, 0), (5, 1))
MONEY_MENUS = frozenset((4, 5))
#: menu -> the row field its value column shows (tmrank's banner)
VALUE_FIELD = {0: "rating", 1: "rating_last", 2: "rating_last",
               4: "prize_total", 5: "prize_week"}
#: the client's caps (formatter jump table 0x5106D44)
RATING_CAP = 0x98967F
MONEY_CAP = 0x3B9AC9FF
HIDDEN = "-------"                  # Ranking.BIN record 11: a hide-name row
#: CardPrm's type byte 0..3, in the card overlay's own glyph order (gW000
#: on_card1: P M X A)
TYPE_LETTERS = "PMXA"
AUCTION_TAB = 6
LIVE_MAX = tmrank.MAX_ROWS
ACTIVITY_MAX = 14

EXHIBIT_SUFFIX = ".U_g_TM0_EXHIBITLIST.bin"
BIDS_NAME = "auction-%d.bids.bin"
#: tetramaster._live_matches_write's marker; trusted for pol-git-sync's grace
MATCHES_MARKER = "tm-matches-live.json"
LIVE_GRACE_S = float(os.environ.get("POL_DEPLOY_MATCH_GRACE_S", "900") or 900)

_SNAP = {"t": 0.0, "snap": None}
_SNAP_LOCK = threading.Lock()
_NAMES = {"t": 0.0, "map": {}}
_NAMES_LOCK = threading.Lock()
_RENDER = {}
_RENDER_LOCK = threading.Lock()
_WARNED = set()


def data_dir():
    return os.environ.get("POL_DATA_DIR", "/data")


def resource_dir():
    """Where the collections and the auction store live: tmrank's own rule
    (POL_RESOURCE_DIR, else <POL_DATA_DIR>/resources), read at call time --
    tmauction.RESOURCE_DIR is frozen at import."""
    return os.path.dirname(tmrank.store_dir())


def accounts_path():
    """POL_ACCOUNTS_DB, else /data/accounts.db (prod's login/authsess env)."""
    return os.environ.get("POL_ACCOUNTS_DB", "/data/accounts.db")


def _warn(key, text):
    if key not in _WARNED:
        _WARNED.add(key)
        print("[boardtm] %s" % text, flush=True)


# ---------------------------------------------------------------------------
# names: the roster TM itself draws, then the accounts DB, read-only
# ---------------------------------------------------------------------------
def roster_names():
    path = os.environ.get("POL_TM_ROSTER_FILE") or os.path.join(data_dir(), "tm-roster.json")
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh) or {}
        return {str(k): str(v) for k, v in (d.get("names") or {}).items() if v}
    except (OSError, ValueError, AttributeError):
        return {}


def member_names(members, ttl=60.0):
    """{member id (str): name} -- the roster's name, else the primary handle
    through a read-only URI (never accounts.connect()), cached `ttl` s."""
    members = [str(m) for m in members]
    now = time.time()
    with _NAMES_LOCK:
        if now - _NAMES["t"] < ttl and all(m in _NAMES["map"] for m in members):
            return dict(_NAMES["map"])
    out = roster_names()
    want = [m for m in members if not out.get(m) and m.isdigit()]
    if want:
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % accounts_path(), uri=True, timeout=5)
            try:
                for m in want:
                    row = conn.execute(
                        "SELECT handle_name FROM handle WHERE member_id = ?"
                        " ORDER BY is_primary DESC, id ASC LIMIT 1", (int(m),)).fetchone()
                    if row and row[0]:
                        out[m] = str(row[0])
            finally:
                conn.close()
        except sqlite3.Error as e:
            _warn("names", "cannot read names from %s (%s)" % (accounts_path(), e))
    out = {m: out.get(m, "") for m in set(members) | set(out)}
    with _NAMES_LOCK:
        _NAMES.update(t=now, map=out)
    return dict(out)


# ---------------------------------------------------------------------------
# the rankings
# ---------------------------------------------------------------------------
def rating_text(v):
    """%d.%02d of value/100, the client's VS. Rating format."""
    v = max(0, min(int(v), RATING_CAP))
    return "%d.%02d" % (v // 100, v % 100)


def money_text(v):
    return "{:,}".format(max(0, min(int(v), MONEY_CAP)))


def published(menu, rank_id):
    """One published list as rows, or None when nothing is published (the
    game then serves its one-zero-row fixture: "No rankings information.")."""
    blob = tmrank.stored(tmrank.path_for(rank_id))
    if not blob:
        return None
    rows, field = [], VALUE_FIELD[menu]
    for i in range(min(len(blob) // tmrank.REC, tmrank.MAX_ROWS)):
        d = tmrank.decode_row(blob[i * tmrank.REC:(i + 1) * tmrank.REC])
        if not d["name"]:
            continue                        # an empty name is "no data", never a player
        v = int(d[field])
        # the rank is the row's position; equal values share the rank above
        rank = rows[-1]["rank"] if rows and rows[-1]["value"] == v else len(rows) + 1
        # last week's rank byte, read SIGNED by the client: <= 0 = not ranked
        last = d["last_rank"].get(menu, 0)
        last = last if 0 < last < 128 else None
        move = ("new" if last is None else "up" if last > rank
                else "down" if last < rank else "same")
        rows.append({"rank": rank, "last": last, "move": move,
                     "name": HIDDEN if d["hide_name"] else d["name"], "value": v,
                     "text": money_text(v) if menu in MONEY_MENUS else rating_text(v)})
    return rows


def week_info():
    """b/g/TM0RkData: the next update and the tally period the game shows."""
    blob = tmrank.stored(tmrank.RKDATA)
    if not blob:
        return None
    d = tmrank.decode_rkdata(blob)
    return {"next": d["stamp"], "from": d["tally_from"], "to": d["tally_to"],
            "players": {str(k): v for k, v in d["players"].items()}}


def collection_members():
    try:
        names = os.listdir(resource_dir())
    except OSError:
        return []
    return sorted((f[:-len(tmrank.COLLECTION_SUFFIX)] for f in names
                   if f.endswith(tmrank.COLLECTION_SUFFIX)),
                  key=lambda m: (not m.isdigit(), int(m) if m.isdigit() else 0, m))


def live_standings(members, names):
    """THIS WEEK SO FAR: everyone who has played, by the VS. Rating their
    record earns now (tmrank.stats_of, the one formula), then this week's
    prize money. Not what the game shows until the next publish."""
    out = []
    for m in members:
        data = tmrank.collection_of(m, resource_dir())
        blk = data.get("rank") if isinstance(data.get("rank"), dict) else {}
        try:
            games = int(blk.get("games") or 0)
        except (TypeError, ValueError):
            games = 0
        if games <= 0:
            continue
        st = tmrank.stats_of(data)
        name = HIDDEN if st["hide_name"] else (names.get(m) or "")
        out.append({"member": m, "name": name, "games": games,
                    "rating": st["rating"], "prize": st["prize_week"],
                    "rating_text": rating_text(st["rating"]),
                    "prize_text": money_text(st["prize_week"])})
    out.sort(key=lambda r: (-r["rating"], -r["prize"], r["name"].lower()))
    out = out[:LIVE_MAX]
    for i, r in enumerate(out):
        r["rank"] = out[i - 1]["rank"] if i and out[i - 1]["rating"] == r["rating"] else i + 1
    return out


# ---------------------------------------------------------------------------
# the auction -- read-only, never the sweep
# ---------------------------------------------------------------------------
def card_of(ii):
    """The card in a listing's <II>: id at [0], then the u8 block attack,
    P defense, TYPE, M defense, grade, ARROWS (tmauction.card_row_from_ii --
    type and P defense are transposed against the shop's order)."""
    u8 = list(ii[8:16]) + [0] * 8
    cid = int(ii[0])
    return {"id": cid, "name": tm_cardprm.name(cid),
            "art": ("card_%03d.png" % cid) if 0 <= cid < tm_cardprm.CARD_COUNT else "",
            "atk": u8[0], "pdef": u8[1], "type": TYPE_LETTERS[u8[2]] if u8[2] < 4 else "?",
            "mdef": u8[3], "arrows": u8[5] & 0xFF}


def time_left_text(secs):
    """The client's "Time Left": hours, then days of 24 (0x12222C), 0 at the end."""
    hours = max(0, int(secs)) // 3600
    d, h = divmod(hours, 24)
    return ("%dd %dh" % (d, h)) if d else ("%dh" % h)


def _whole(blob, rec):
    return blob[:len(blob) // rec * rec]


def _bids(ai):
    try:
        with open(os.path.join(resource_dir(), BIDS_NAME % int(ai)), "rb") as fh:
            blob = _whole(fh.read(), tmauction.BID_REC)
    except OSError:
        return []
    rows = [tmauction.read_bid(blob, i) for i in range(tmauction.bid_count(blob))]
    rows.sort(key=lambda b: b["amount"])            # the client's own order
    return rows


def auction(now=None):
    """Every stored listing, split the way the game's browse would see them.

    A listing past its end WITH bids is sold but not settled (the sweep does
    that on the next browse): it is left out of the listings and the Price
    List counts -- responders._auction_count_rows' rule -- and shown as
    recently sold. Every other listing is counted as it stands.
    """
    now = int(now if now is not None else time.time())
    listings, sold, activity = [], [], []
    for fn in sorted(glob.glob(os.path.join(resource_dir(), "*" + EXHIBIT_SUFFIX))):
        try:
            with open(fn, "rb") as fh:
                blob = _whole(fh.read(), tmauction.REC)
        except OSError:
            continue
        for i in range(tmauction.count(blob)):
            try:
                rec = tmauction.read_record(blob, i)
            except ValueError:
                break
            # WARNING: ONLY BIDS PLACED SINCE IT WAS LISTED. A settled auction's bid
            # file outlives its row, and the mint once handed that id back out:
            # 2026-09-13 a fresh listing #1 inherited the 08-20 test bids and
            # posted "High bid 0T by <a test account> (2 bids)". responders'
            # `_auction_next_id` now skips ids with a bid file; this keeps the
            # board honest if an old file ever lines up with a new id again.
            listed = int(rec.get("ed") or 0)
            bids = [b for b in _bids(rec["ai"]) if not listed or int(b["when"]) >= listed]
            card = card_of(rec["ii"])
            top = bids[-1] if bids else None
            row = {"ai": rec["ai"], "card": card, "seller": rec["seller"],
                   "start": rec["sp"], "high": rec["cm"], "raise": rec["bi"],
                   "bids": len(bids),
                   # the client draws the bidder only when <BC> > 0
                   "bidder": (rec["bidder"] or (top or {}).get("name", "")) if bids else "",
                   "ends": rec["nc"], "listed": rec["ed"], "relists": rec["am"],
                   "left": max(0, int(rec["nc"]) - now),
                   "left_text": time_left_text(int(rec["nc"]) - now),
                   "price": tmauction.asking_price(rec)}
            row["price_text"] = money_text(row["price"])
            is_sold = bool(tmauction.expired(rec, now)) and bool(bids)
            if is_sold:
                row["winner"] = (top or {}).get("name", "") or row["bidder"]
                sold.append(row)
                activity.append({"kind": "sold", "t": int(rec["nc"]), "who": row["winner"],
                                 "card": card["name"], "amount": row["price"]})
            else:
                listings.append(row)
            if rec["ed"]:
                activity.append({"kind": "listed", "t": int(rec["ed"]), "who": rec["seller"],
                                 "card": card["name"], "amount": rec["sp"]})
            for b in bids:
                activity.append({"kind": "bid", "t": int(b["when"]), "who": b["name"],
                                 "card": card["name"], "amount": b["amount"]})
    listings.sort(key=lambda r: (r["ends"], r["ai"]))      # ending soonest first
    sold.sort(key=lambda r: -r["ends"])
    bands = []
    for name, lo, hi in tmauction.BANDS:
        n = sum(1 for r in listings if (lo is None or r["price"] >= lo)
                and (hi is None or r["price"] <= hi))
        bands.append({"lo": lo, "hi": hi, "count": n})
    activity.sort(key=lambda e: -e["t"])
    return {"listings": listings, "sold": sold, "bands": bands,
            "activity": activity[:ACTIVITY_MAX]}


def in_band(row, band):
    return (band["lo"] is None or row["price"] >= band["lo"]) and \
        (band["hi"] is None or row["price"] <= band["hi"])


# ---------------------------------------------------------------------------
# WATCHING (2026-09-13): the matches tetramaster.py publishes to
# <POL_DATA_DIR>/tm-tables-live.json. IT decides who may be seen -- the table
# creator's own observe setting; an unwatchable table comes with no state --
# and hands are counts only, so the view is LIVE: nothing in the file is worth
# relaying to a player. /watch#<room>-<table> animates each move with the
# game's own pieces; /watch alone lists the matches.
# ---------------------------------------------------------------------------
TABLES_FILE = "tm-tables-live.json"
#: the writer beats every 5 s while it holds a match; this long without a beat
#: and it is gone (authsess restarted), so nothing is shown
WATCH_STALE_S = 60.0
_TABLES = {"t": 0.0, "d": {}}
_TABLES_LOCK = threading.Lock()


def live_tables(now=None):
    """{table id: state} of every watchable match right now; {} when the file
    is missing, unreadable or stale. Cached half a second."""
    now = time.time() if now is None else now
    with _TABLES_LOCK:
        if 0 <= now - _TABLES["t"] < 0.5:
            return _TABLES["d"]
    out = {}
    try:
        with open(os.path.join(data_dir(), TABLES_FILE), encoding="utf-8") as fh:
            d = json.load(fh) or {}
        if 0 <= now - float(d.get("stamp") or 0) < WATCH_STALE_S:
            for tid, e in (d.get("tables") or {}).items():
                if isinstance(e, dict) and e.get("watchable") and isinstance(e.get("state"), dict):
                    out[str(tid)] = e["state"]
    except (OSError, ValueError, TypeError, AttributeError):
        out = {}
    watch_enrich(out)
    with _TABLES_LOCK:
        _TABLES.update(t=now, d=out)
    return out


#: the PlayOnline portrait sheets (tools/tm_boardart_bake.py copies them)
FACES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boardart", "faces")
#: a profile's portrait is handle_profile field 19 (accounts.py "portrait",
#: responders `_PROFILE_FICON`), on the member's PRIMARY handle
PORTRAIT_FIELD = 19
_FACE_IDS = {"t": 0.0, "map": {}}
_FACE_PNG = {}


def face_ids(members, ttl=60.0):
    """{member id: z_ficon} through a read-only URI, cached `ttl` s; 0 = none."""
    members = [int(m) for m in members if str(m).isdigit()]
    now = time.time()
    with _NAMES_LOCK:
        fresh = now - _FACE_IDS["t"] < ttl
        out = dict(_FACE_IDS["map"]) if fresh else {}
    want = [m for m in members if m not in out]
    if not want:
        return out
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % accounts_path(), uri=True, timeout=5)
        try:
            for m in want:
                row = conn.execute(
                    "SELECT val_int FROM handle_profile WHERE field_id = ? AND handle_id ="
                    " (SELECT id FROM handle WHERE member_id = ?"
                    "  ORDER BY is_primary DESC, id ASC LIMIT 1)", (PORTRAIT_FIELD, m)).fetchone()
                out[m] = int(row[0]) if row and row[0] else 0
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn("faces", "cannot read portraits from %s (%s)" % (accounts_path(), e))
        for m in want:
            out.setdefault(m, 0)
    with _NAMES_LOCK:
        _FACE_IDS.update(t=now if not fresh else _FACE_IDS["t"], map=out)
    return out


def face_png(fid):
    """One portrait: tile `fid & 7` of hnf<fid >> 3>.png (app.dll 0x4a7d199),
    or None. A sheet is 4 x 2 portraits of 64x96; the tiles are counted
    left to right, then the second row."""
    fid = int(fid)
    if fid <= 0:
        return None
    if fid in _FACE_PNG:
        return _FACE_PNG[fid]
    png = None
    try:
        from PIL import Image
        with Image.open(os.path.join(FACES_DIR, "hnf%03d.png" % (fid >> 3))) as im:
            im = im.convert("RGBA")
            tw, th = im.width // 4, im.height // 2
            t = fid & 7
            tile = im.crop(((t % 4) * tw, (t // 4) * th, (t % 4 + 1) * tw, (t // 4 + 1) * th))
            buf = io.BytesIO()
            tile.save(buf, "PNG", optimize=True)
            png = buf.getvalue()
    except (OSError, ValueError, ImportError):
        png = None
    if len(_FACE_PNG) > 512:
        _FACE_PNG.clear()
    _FACE_PNG[fid] = png
    return png


def watch_enrich(tables):
    """What the page shows that the publisher does not know: each VS. COM
    opponent's own name and portrait (PlPrm.BIN / gW080, baked into
    board.json), each card's name, and each player's PlayOnline portrait id.
    The member ids that portrait lookup needs are REMOVED here -- nothing the
    board serves carries one."""
    coms = (((board() or {}).get("watch") or {}).get("coms") or {})
    mids = [p.get("mid") for st in tables.values() for p in (st.get("players") or [])
            if isinstance(p, dict) and p.get("mid") is not None]
    faces = face_ids(mids) if mids else {}
    for st in tables.values():
        for p in st.get("players") or []:
            if not isinstance(p, dict):
                continue
            mid = p.pop("mid", None)
            if p.get("com"):
                c = coms.get(str(p.get("ci")))
                nm = str(p.get("name") or "")
                if c:
                    if nm.startswith("COM ") and nm[4:].isdigit():   # the default label only
                        p["name"] = c["name"]
                    p["face"] = c["face"]
            elif mid is not None and str(mid).isdigit() and faces.get(int(mid)):
                p["face_id"] = faces[int(mid)]
        cards = list((st.get("board") or {}).values())
        cards += [s["card"] for s in (st.get("steps") or []) if isinstance(s, dict) and s.get("card")]
        for c in cards:
            if isinstance(c, dict) and "id" in c:
                c["name"] = tm_cardprm.name(int(c["id"]))
    return tables


def live_summary(tables=None):
    """The watchable matches for a list: where, who, the score, the turn."""
    tables = live_tables() if tables is None else tables
    out = []
    for tid, st in sorted(tables.items(), key=lambda kv: (kv[1].get("room") or 0,
                                                         kv[1].get("table") or 0)):
        pl = st.get("players") or []
        out.append({"id": tid, "room": st.get("room"), "table": st.get("table"),
                    "phase": st.get("phase"), "com": bool(st.get("com")),
                    "names": [str(p.get("name") or "") for p in pl],
                    "scores": [p.get("score") for p in pl],
                    "turn": st.get("turn"), "limit": st.get("limit"), "n": st.get("n")})
    return out


def matches_live(now=None):
    """Matches in progress: the marker's count while fresh, 0 once stale,
    None when there is no marker (the page then says nothing)."""
    try:
        with open(os.path.join(data_dir(), MATCHES_MARKER), encoding="utf-8") as fh:
            d = json.load(fh) or {}
        stamp, count = float(d.get("stamp") or 0), int(d.get("count") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    now = time.time() if now is None else now
    return count if 0 <= now - stamp < LIVE_GRACE_S else 0


# ---------------------------------------------------------------------------
# the snapshot the page polls
# ---------------------------------------------------------------------------
def _hash(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def snapshot(args=None, now=None):
    now = time.time() if now is None else float(now)
    b = board() or {}
    captions = b.get("tabs_caption") or ["VS. Rating", "Top 30", "Best Rookies",
                                         "Grand Total", "Weekly Total", "This Week", "Auction"]
    week = week_info()
    tabs = []
    for i, (menu, rid) in enumerate(RANK_TABS):
        rows = published(menu, rid)
        tabs.append({"id": i, "kind": "rank", "menu": menu, "name": captions[i],
                     "money": menu in MONEY_MENUS, "published": rows is not None,
                     "players": (week or {}).get("players", {}).get(str(menu)),
                     "rows": rows or []})
    members = collection_members()
    names = member_names(members)
    live = live_standings(members, names)
    tabs.append({"id": 5, "kind": "live", "name": captions[5], "published": False,
                 "rows": live})
    auc = auction(now)
    rank_view = [[(r["rank"], r["name"], r["text"], r.get("last")) for r in t["rows"]]
                 for t in tabs[:5]]
    return {"board": NAME, "title": TITLE, "updated": int(now),
            "poll_s": float(getattr(args, "poll", 5.0) or 5.0),
            "art": bool(b), "week": week, "tabs": tabs, "auction": auc,
            "players": len(members), "matches_live": matches_live(now),
            "live": live_summary(),
            "sig": _hash([rank_view, week,
                          [(r["name"], r["rating"], r["prize"]) for r in live]]),
            "auction_sig": _hash([[(r["ai"], r["high"], r["bids"], r["bidder"], r["relists"])
                                   for r in auc["listings"]],
                                  [(r["ai"], r["high"]) for r in auc["sold"]]])}


def cached_snapshot(args=None, ttl=2.0):
    now = time.time()
    with _SNAP_LOCK:
        if _SNAP["snap"] is not None and now - _SNAP["t"] < ttl:
            return _SNAP["snap"]
    snap = snapshot(args, now)
    with _SNAP_LOCK:
        _SNAP.update(t=now, snap=snap)
    return snap


def art_files():
    """What the page may load: the baked PNGs, the woff2 faces, board.json.
    The .ttf copies (Pillow's) are not served."""
    try:
        return frozenset(n for n in os.listdir(ART_DIR)
                         if n.endswith((".png", ".woff2")) or n == "board.json")
    except OSError:
        return frozenset()


_BOARD = {"d": None, "loaded": False}


def board():
    """board.json from the bake, or None when the art is not baked."""
    if not _BOARD["loaded"]:
        _BOARD["loaded"] = True
        try:
            with open(os.path.join(ART_DIR, "board.json"), encoding="utf-8") as fh:
                _BOARD["d"] = json.load(fh)
        except (OSError, ValueError):
            _BOARD["d"] = None
    return _BOARD["d"]


# ---------------------------------------------------------------------------
# render.png: the window for Discord, drawn by the page's own layout
# ---------------------------------------------------------------------------
def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def _fmt_date(t):
    return time.strftime("%m/%d/%Y", time.gmtime(t))


def tab_pages(snap, tab, band=0):
    L = board() or {}
    if tab == AUCTION_TAB:
        per = (L.get("auction_rows") or {}).get("count", 5)
        return max(1, -(-len(auction_rows(snap, band)) // per))
    per = (L.get("rows") or {}).get("count", 10)
    return max(1, -(-len(snap["tabs"][tab]["rows"]) // per))


def auction_rows(snap, band):
    a = snap["auction"]
    if band >= len(a["bands"]):
        return a["sold"]
    return [r for r in a["listings"] if in_band(r, a["bands"][band])]


def medal(r):
    """The gold 1st / 2nd / 3rd: the top three ranks, but not for a zero --
    a money list that is mostly 0 ties everyone at 3rd, and eight medals for
    nothing is noise."""
    return r["rank"] <= 3 and r["value"] > 0


#: arrow bit i = direction i, clockwise from north (tmbattle.DIRECTIONS), as
#: (x, y) fractions of the card
ARROW_AT = ((.5, 0), (1, 0), (1, .5), (1, 1), (.5, 1), (0, 1), (0, .5), (0, 0))


def arrow_tris(mask, x, y, w, h, size=5.0):
    """The card's arrows as triangles pointing out of its edges -- the page
    draws the same ones (thumb())."""
    out = []
    for k, (fx, fy) in enumerate(ARROW_AT):
        if not (int(mask) >> k) & 1:
            continue
        cx, cy = x + fx * w, y + fy * h
        dx, dy = fx * 2 - 1, fy * 2 - 1
        n = (dx * dx + dy * dy) ** 0.5 or 1.0
        ux, uy = dx / n, dy / n
        out.append((cx + ux * size, cy + uy * size, cx - uy * size * .8, cy + ux * size * .8,
                    cx + uy * size * .8, cy - ux * size * .8))
    return out


def render(tab=0, page=0, snap=None, scale=2):
    """The window for one tab and page as PNG bytes -- None when the art is
    not baked or Pillow is missing. Same layout, fonts and art as the page."""
    L, Image = board(), _pil()
    if L is None or Image is None:
        return None
    from PIL import ImageDraw, ImageFont
    snap = snap or cached_snapshot()
    tab = max(0, min(AUCTION_TAB, int(tab)))
    page = max(0, min(tab_pages(snap, tab) - 1, int(page)))
    S = scale
    art = lambda n: Image.open(os.path.join(ART_DIR, n)).convert("RGBA")      # noqa: E731
    fonts = {}

    def font(kind, size):
        key = (kind, int(round(size * S)))
        if key not in fonts:
            fonts[key] = ImageFont.truetype(os.path.join(ART_DIR, L["fonts"][kind]), key[1])
        return fonts[key]

    W, H = L["screen"]
    base = art(L["layers"]["backdrop"]).resize((W * S, H * S), Image.LANCZOS)
    base = Image.blend(base, Image.new("RGBA", base.size, (0, 0, 0, 255)), 0.35)
    sheet = art(L["layers"]["panel"]).resize((W * S, H * S), Image.LANCZOS)
    mask = art("mask_main.png").getchannel("A").resize((W * S, H * S), Image.LANCZOS)
    base.paste(sheet, (0, 0), mask)
    cv = base
    d = ImageDraw.Draw(cv)

    def paste(name, x, y, w=None, h=None):
        im = art(name)
        w, h = w or im.width, h or im.height
        cv.alpha_composite(im.resize((int(w * S), int(h * S)), Image.LANCZOS),
                           (int(x * S), int(y * S)))

    def text(s, x, align, y, h, kind="text", size=15, fill=(238, 228, 204), shadow=None):
        if not s:
            return
        f = font(kind, size)
        w = d.textlength(s, font=f)
        X = x * S - (w if align == "right" else w / 2 if align == "center" else 0)
        Y = (y + h / 2.0) * S
        if shadow:
            d.text((X + S, Y + S), s, font=f, fill=tuple(shadow), anchor="lm")
        d.text((X, Y), s, font=f, fill=tuple(fill), anchor="lm")

    def label(s, x, align, y, h, size=None):
        lb = L["label"]
        text(s, x, align, y, h, "title", size or lb["size"], lb["fill"], lb["shadow"])

    # the title, orange to gold with a dark edge, as the page's SVG
    T = L["title"]
    title = L["titles"][tab]
    tf = font("title", T["size"])
    tw = d.textlength(title, font=tf)
    if tw > T["max_width"] * S:
        tf = font("title", T["size"] * T["max_width"] * S / tw)
        tw = d.textlength(title, font=tf)
    tx, ty = T["x"] * S - tw / 2, T["baseline"] * S
    lay = Image.new("RGBA", cv.size, (0, 0, 0, 0))
    ImageDraw.Draw(lay).text((tx, ty), title, font=tf, anchor="ls", fill=tuple(T["stroke"]) + (255,),
                             stroke_width=T["stroke_px"] * S, stroke_fill=tuple(T["stroke"]) + (255,))
    cv.alpha_composite(lay)
    m = Image.new("L", cv.size, 0)
    ImageDraw.Draw(m).text((tx, ty), title, font=tf, anchor="ls", fill=255)
    bb = m.getbbox() or (0, 0, 1, 1)
    grad = Image.new("RGBA", cv.size)
    gd = ImageDraw.Draw(grad)
    for yy in range(bb[1], bb[3] + 1):
        k = (yy - bb[1]) / max(1, bb[3] - bb[1])
        gd.line([(bb[0], yy), (bb[2], yy)], fill=tuple(
            int(T["top"][i] + (T["bottom"][i] - T["top"][i]) * k) for i in range(3)) + (255,))
    grad.putalpha(m)
    cv.alpha_composite(grad)

    a = L["arrows"]
    paste(L["layers"]["arrow"], *a["next"])
    left = art(L["layers"]["arrow"]).transpose(Image.FLIP_LEFT_RIGHT)
    cv.alpha_composite(left.resize((a["prev"][2] * S, a["prev"][3] * S), Image.LANCZOS),
                       (a["prev"][0] * S, a["prev"][1] * S))
    hr = L["header"]["rect"]
    paste(L["layers"]["header"], *hr)
    paste(L["layers"]["sep"], *L["sep"]["rect"])
    R, st = L["rows"], L["strings"]
    fill, dim = R["fill"], R["dim"]
    hy, hh = hr[1], hr[3]

    if tab < 5:
        C = L["columns"]["rank"]
        t = snap["tabs"][tab]
        label(st["rank"], *C["rank"], hy, hh)
        label(st["last_week"], *C["last"], hy, hh)
        label(st["player"], *C["name"], hy, hh)
        label(st["prize_money"] if t["money"] else st["vs_rating"], *C["value"], hy, hh)
        rows = t["rows"][page * R["count"]:(page + 1) * R["count"]]
        for i, r in enumerate(rows):
            y = R["y"] + R["pitch"] * i
            if medal(r):
                im = art(L["ranks"][r["rank"] - 1])
                mh = R.get("medal_h", im.height)
                mw = im.width * mh / float(im.height)
                paste(L["ranks"][r["rank"] - 1], C["rank"][0] - mw, y + (R["pitch"] - mh) / 2.0, mw, mh)
            else:
                text(str(r["rank"]), *C["rank"], y, R["pitch"], "text", R["size"], fill)
            lw = {"new": "New", "same": str(r["last"])}.get(
                r["move"], ("%s %d" % ("▲" if r["move"] == "up" else "▼", r["last"] or 0)))
            text(lw, *C["last"], y, R["pitch"], "text", 13,
                 {"up": (127, 212, 106), "down": (224, 96, 74)}.get(r["move"], dim))
            text(r["name"], *C["name"], y, R["pitch"], "bold", R["size"], fill)
            text(r["text"], *C["value"], y, R["pitch"], "text", R["size"], fill)
        if not rows:
            text(st["no_info"], 320, "center", R["y"], R["pitch"] * 3, "text", R["size"], dim)
        w = snap.get("week")
        info = ("Tally Period: %s - %s   Next update: %s 00:00 UTC"
                % (_fmt_date(w["from"]), _fmt_date(w["to"]), _fmt_date(w["next"]))
                if w and t["published"] and w["next"] else st["no_info"])
    elif tab == 5:
        C = L["columns"]["live"]
        t = snap["tabs"][5]
        for key, lab in (("rank", st["rank"]), ("name", st["player"]), ("games", st["games"]),
                         ("rating", st["vs_rating"]), ("prize", st["prize_money"])):
            label(lab, *C[key], hy, hh)
        for i, r in enumerate(t["rows"][page * R["count"]:(page + 1) * R["count"]]):
            y = R["y"] + R["pitch"] * i
            text(str(r["rank"]), *C["rank"], y, R["pitch"], "text", R["size"], fill)
            text(r["name"], *C["name"], y, R["pitch"], "bold", R["size"], fill)
            text(str(r["games"]), *C["games"], y, R["pitch"], "text", R["size"], fill)
            text(r["rating_text"], *C["rating"], y, R["pitch"], "text", R["size"], fill)
            text(r["prize_text"], *C["prize"], y, R["pitch"], "text", R["size"], fill)
        info = st["live_note"]
    else:
        C, A = L["columns"]["auction"], L["auction_rows"]
        label(st["cards"], *C["card"], hy, hh)
        label(st["seller"], *C["seller"], hy, hh)
        label(st["high_bid"], *C["price"], hy, hh)
        label(st["bidder"], *C["who"], hy, hh)
        label(st["time_left"], *C["left"], hy, hh)
        rows = auction_rows(snap, 0)[page * A["count"]:(page + 1) * A["count"]]
        for i, r in enumerate(rows):
            y = A["y"] + A["pitch"] * i
            c = r["card"]
            if c["art"]:
                tw_, th_ = A["thumb"]
                paste(c["art"], C["card"][0], y + 2, tw_, th_)
                for (ax, ay, bx, by, cx, cy) in arrow_tris(c["arrows"], C["card"][0], y + 2, tw_, th_):
                    d.polygon([(ax * S, ay * S), (bx * S, by * S), (cx * S, cy * S)],
                              fill=(242, 194, 74), outline=(58, 30, 4))
            text(c["name"], C["name"][0], "left", y + 4, 22, "bold", R["size"], fill)
            text("%s  %d / %d / %d" % (c["type"], c["atk"], c["pdef"], c["mdef"]),
                 C["name"][0], "left", y + 26, 20, "text", 12, L["label"]["fill"])
            text(r["seller"], *C["seller"], y, A["pitch"], "text", R["size"], fill)
            if r["bids"]:
                text(r["price_text"] + "T", *C["price"], y, A["pitch"], "text", R["size"], fill)
            else:
                # no bid yet: the client's "Currently" is empty; show what opens it
                text(r["price_text"] + "T", *C["price"], y + 4, 22, "text", R["size"], dim)
                text(st["opening"], *C["price"], y + 26, 20, "text", 11, dim)
            text(r["bidder"] or "-", *C["who"], y, A["pitch"], "text", R["size"], fill)
            text(r["left_text"], *C["left"], y, A["pitch"], "text", R["size"], fill)
        if not rows:
            text(st["no_cards"], 320, "center", A["y"], A["pitch"] * 2, "text", R["size"], dim)
        info = "   ".join("%s %d" % (n, b["count"]) for n, b in
                          zip(st["bands"], snap["auction"]["bands"]))
    I = L["info"]
    text(info, I["rect"][0] + I["rect"][2] / 2.0, "center", I["rect"][1], I["rect"][3],
         "text", I["size"], I["fill"])
    Tb = L["tabs"]
    # one caption size for all seven: what the longest needs to keep its pad
    room = Tb["w"] - 2 * Tb.get("pad", 5)
    tsize = min([Tb["size"]] + [Tb["size"] * room * S / d.textlength(cap, font=font("title", Tb["size"]))
                                for cap in L["tabs_caption"]])
    for i, cap in enumerate(L["tabs_caption"]):
        x = Tb["x0"] + i * (Tb["w"] + Tb["gap"])
        paste(L["layers"]["tab"]["s3" if i == tab else "s0"], x, Tb["y"])
        text(cap, x + Tb["w"] / 2.0, "center", Tb["y"] + 1, Tb["h"] - 4, "title",
             tsize, Tb["fill"][1 if i == tab else 0], (30, 16, 4))
    out = io.BytesIO()
    cv.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def render_cached(tab, page, args=None):
    snap = cached_snapshot(args)
    key = (snap["sig"], snap["auction_sig"], snap["updated"] // 60)
    with _RENDER_LOCK:
        hit = _RENDER.get((int(tab), int(page)))
        if hit and hit[0] == key:
            return hit[1]
    png = render(tab, page, snap)
    with _RENDER_LOCK:
        _RENDER[(int(tab), int(page))] = (key, png)
    return png


def route(path, query, args):
    """polboards' hook for this board's own routes: /render.png?tab=&page=,
    and /card.png?id=&a= (a card on its base with arrow mask a -- the bot's
    listing posts show it by URL), and the watching page: /watch, and
    /watch.json?t=<room>-<table> (one match, "state": null when it is not
    shown -- an answer, not a 404) or /watch.json alone (the list)."""
    if path in ("/watch", "/watch/"):
        return 200, WATCH_PAGE, "text/html; charset=utf-8", "no-store"
    if path == "/watch.json":
        tables = live_tables()
        tid = (query.get("t") or [""])[0]
        if not tid:
            body = {"tables": live_summary(tables)}
        else:
            body = {"id": tid, "state": tables.get(tid)}
        return (200, json.dumps(body, separators=(",", ":")),
                "application/json; charset=utf-8", "no-store")
    if path == "/face.png":
        try:
            fid = int((query.get("id") or [""])[0])
        except ValueError:
            return 400, "id is a number", "text/plain", "no-store"
        png = face_png(fid) if 0 < fid < 1 << 16 else None
        if png is None:
            return 404, "no such portrait", "text/plain", "public, max-age=3600"
        return 200, png, "image/png", "public, max-age=86400"
    if path == "/card.png":
        try:
            cid = int((query.get("id") or [""])[0])
            arrows = int((query.get("a") or ["0"])[0])
        except ValueError:
            return 400, "id and a are numbers", "text/plain", "no-store"
        if not 0 <= cid < tm_cardprm.CARD_COUNT or not 0 <= arrows < 256:
            return 400, "id is 0..249, a is 0..255", "text/plain", "no-store"
        png = card_png({"id": cid, "art": "card_%03d.png" % cid, "arrows": arrows})
        if png is None:
            return 503, "the card art is not baked on this server", "text/plain", "no-store"
        return 200, png, "image/png", "public, max-age=86400"
    if path != "/render.png":
        return None
    try:
        tab = int((query.get("tab") or ["0"])[0])
        page = int((query.get("page") or ["0"])[0])
    except ValueError:
        return 400, "tab and page are numbers", "text/plain", "no-store"
    if not 0 <= tab <= AUCTION_TAB or page < 0:
        return 400, "tab is 0..6, page >= 0", "text/plain", "no-store"
    png = render_cached(tab, page, args)
    if png is None:
        return 503, "the board art is not baked on this server", "text/plain", "no-store"
    return 200, png, "image/png", "no-cache"


# ---------------------------------------------------------------------------
# Discord: TWO messages, each edited in place -- the rankings on one webhook,
# the auction on the other (polboards.FEEDS) -- and short posts for the news
# ---------------------------------------------------------------------------
DISCORD_TOP = 5
MOVE_MARK = {"new": "\U0001F195", "up": "▲", "down": "▼", "same": "▫"}


def md(s):
    """A player's text made inert in Discord markdown."""
    s = str(s or "")
    for ch in "*_~`|>\\":
        s = s.replace(ch, "")
    return s or "(no name)"


def _image(tab, filename, args, payload, embed):
    png = render_cached(tab, 0, args) if board() is not None else None
    if png is None:
        payload["attachments"] = []
        return []
    embed["image"] = {"url": "attachment://%s" % filename}
    payload["attachments"] = [{"id": 0, "filename": filename}]
    return [(filename, "image/png", png)]


#: the file name of each ranking tab's image
SLUGS = ("vs-rating", "top-30", "best-rookies", "grand-total", "weekly-total", "this-week")
RANK_TOP = 10


def _stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _url(args, tab):
    url = (getattr(args, "tm_url", "") or "").strip().rstrip("/")
    return ("%s/#%d" % (url, tab)) if url else ""


def _rank_payload(snap, i, t, args, image="attach"):
    """One ranking list as its own message: its top 10 and its own window."""
    b = board() or {}
    titles = b.get("titles") or ["VS. Rating", "Top 30", "Best Rookies", "Grand Total",
                                 "Weekly Total", "This Week So Far"]
    w = snap.get("week") or {}
    if t["kind"] == "live":
        lines = ["`%2d.` **%s** %s (%d game%s)" % (r["rank"], md(r["name"]), r["rating_text"],
                                                    r["games"], "" if r["games"] == 1 else "s")
                 for r in t["rows"][:RANK_TOP]]
        desc = "\n".join(lines) or "*No games yet this week.*"
        foot = "Live standings, not yet published"
    else:
        lines = ["`%2d.` %s **%s** %s" % (r["rank"], MOVE_MARK.get(r["move"], ""), md(r["name"]),
                                          r["text"]) for r in t["rows"][:RANK_TOP]]
        desc = "\n".join(lines) or "*No rankings information.*"
        foot = ("Tally Period: %s - %s" % (_fmt_date(w["from"]), _fmt_date(w["to"]))
                if t["published"] and w.get("from") else "Tetra Master Rankings")
    if w.get("next"):
        desc += "\n\nNext update <t:%d:F> (<t:%d:R>)" % (w["next"], w["next"])
    embed = {"author": {"name": "Tetra Master Rankings"}, "title": titles[i],
             "description": desc[:4000], "color": 0xE0781E,
             "timestamp": _stamp(snap["updated"]), "footer": {"text": foot}}
    if _url(args, i):
        embed["url"] = _url(args, i)
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if image == "url":
        # a button's private answer: the image by the board's own public
        # render.png (an interaction reply carries no upload); v= changes
        # with the lists, so Discord's image cache never shows a stale one
        base = (getattr(args, "tm_url", "") or "").strip().rstrip("/")
        if base:
            embed["image"] = {"url": "%s/render.png?tab=%d&v=%s" % (base, i, snap["sig"])}
        return payload, []
    return payload, _image(i, "tm-%s.png" % SLUGS[i], args, payload, embed)


def discord_messages(snap, args=None, bot=False):
    """The RANKINGS feed: ONE MESSAGE PER LIST, each with that tab's window as
    its image (decided 2026-09-13: not one super long post), each edited in
    place when its list changes (polboards.DiscordSet)."""
    w = snap.get("week") or {}
    out = []
    for i, t in enumerate(snap["tabs"]):
        sig = _hash([[(r["rank"], r["name"], r.get("text") or r.get("rating_text"),
                       r.get("move"), r.get("games"), r.get("prize")) for r in t["rows"]],
                     t["published"], w.get("next")])
        out.append({"slot": "rank-%d" % i, "sig": sig, "ended": False,
                    "build": (lambda i=i, t=t: _rank_payload(snap, i, t, args))})
    return out


def discord_events(prev, snap):
    """The weekly publish landed (the next-update stamp moved on)."""
    pw, w = (prev.get("week") or {}), (snap.get("week") or {})
    if not pw.get("next") or not w.get("next") or pw["next"] == w["next"]:
        return []
    top = snap["tabs"][0]["rows"][:1]
    lead = (" VS. Rating #1: **%s** (%s)" % (md(top[0]["name"]), top[0]["text"])) if top else ""
    return ["\U0001F4DC This week's Tetra Master rankings are out.%s" % lead]


# ---------------------------------------------------------------------------
# the LIVE feed (2026-09-13): a post per watchable match while it
# runs -- who, where, the turn -- with a "Watch this match" button as the bot,
# deleted the moment the match is gone. It shares the rankings channel unless
# /tmboard live (or its own webhook) says otherwise.
# ---------------------------------------------------------------------------
discord_live_ended_ttl = 0.0


def live_text(tb):
    """One line for a live match. No markdown from player names, no emoji."""
    names = [md(n) for n in (tb.get("names") or []) if n]
    who = " vs ".join(names) if names else "a match"
    where = "Room %s, Table %s" % (tb.get("room"), tb.get("table"))
    if tb.get("phase") == "select":
        what = "choosing cards"
    elif tb.get("phase") == "over":
        what = "just finished"
    else:
        limit = int(tb.get("limit") or 0)
        what = "turn %d of %d" % (min(int(tb.get("turn") or 0) + 1, limit or 99), limit)
    return "**Tetra Master, live now:** %s (%s), %s." % (who, where, what)


def discord_live_messages(snap, args=None, bot=False):
    """polboards' message-set feed: a slot per watchable match."""
    url = _base(args)
    out = []
    for tb in snap.get("live") or []:
        text = live_text(tb)
        link = "%s/watch#%s" % (url, tb["id"]) if url else ""
        payload = {"content": (text if (bot or not link) else "%s\nWatch it: %s" % (text, link)),
                   "allowed_mentions": {"parse": []}, "flags": 4}
        if bot and link:
            payload["components"] = [{"type": 1, "components": [
                {"type": 2, "style": 5, "label": "Watch this match", "url": link}]}]
        out.append({"slot": "live-%s" % tb["id"], "ended": False, "sig": _hash([text, link]),
                    "build": (lambda p=payload: (p, []))})
    return out


# ---------------------------------------------------------------------------
# the AUCTION feed: ONE MESSAGE PER LISTING (decided 2026-09-13), posted
# when the card goes up, edited live as bids land (Discord's own <t:..:R>
# counts the time down), edited to its result when it ends, and deleted
# --discord-ended-ttl later. The image is the card itself, arrows and all.
# ---------------------------------------------------------------------------
_CARDS = {}
_CARDS_LOCK = threading.Lock()


def card_png(card, scale=3):
    """The card as the game draws it -- its art on the blue card base
    (card_frame.png, 80x96), at `scale`, with its arrows out of the base's
    edges -- as PNG bytes on a clear ground. None without the art or Pillow."""
    Image = _pil()
    if Image is None or not card.get("art"):
        return None
    key = (card["id"], card["arrows"], scale)
    with _CARDS_LOCK:
        if key in _CARDS:
            return _CARDS[key]
    from PIL import ImageDraw
    try:
        im = Image.open(os.path.join(ART_DIR, card["art"])).convert("RGBA")
    except OSError:
        return None
    try:
        base = Image.open(os.path.join(ART_DIR, "card_frame.png")).convert("RGBA")
    except OSError:
        base = Image.new("RGBA", (im.width, im.height), (0, 0, 0, 0))
    base.alpha_composite(im, ((base.width - im.width) // 2, (base.height - im.height) // 2))
    w, h, m = base.width * scale, base.height * scale, 6 * scale
    cv = Image.new("RGBA", (w + 2 * m, h + 2 * m), (0, 0, 0, 0))
    cv.alpha_composite(base.resize((w, h), Image.LANCZOS), (m, m))
    d = ImageDraw.Draw(cv)
    for ax, ay, bx, by, cx, cy in arrow_tris(card["arrows"], m, m, w, h, size=5.0 * scale):
        d.polygon([(ax, ay), (bx, by), (cx, cy)], fill=(242, 194, 74, 255),
                  outline=(58, 30, 4, 255), width=max(1, scale // 2))
    out = io.BytesIO()
    cv.save(out, "PNG", optimize=True)
    png = out.getvalue()
    with _CARDS_LOCK:
        _CARDS[key] = png
    return png


def listing_state(r, sold=False):
    """live, sold (past its end with bids, not yet settled), or unsold (past
    its end with none: it relists or goes back to the seller)."""
    if sold:
        return "sold"
    return "unsold" if r["left"] <= 0 else "live"


def _auction_payload(r, state, args, bot=False):
    if bot:
        return _auction_bot_payload(r, state, args, _AUC_VIEW.get(int(r["ai"]), "card"))
    c = r["card"]
    stats = "Type %s · %d / %d / %d · %d arrow%s" % (
        c["type"], c["atk"], c["pdef"], c["mdef"], bin(c["arrows"]).count("1"),
        "" if bin(c["arrows"]).count("1") == 1 else "s")
    fields = [{"name": "Seller", "value": md(r["seller"]), "inline": True}]
    if state == "sold":
        title, color = "%s - Sold" % c["name"], 0x3BA55C
        desc = "Sold to **%s** for **%sT**" % (md(r.get("winner") or r["bidder"]), r["price_text"])
    elif state in ("unsold", "returned"):
        title, color = "%s - Ended" % c["name"], 0x7A7266
        desc = ("No bids. It goes back up for auction (%d relist%s left)."
                % (r["relists"], "" if r["relists"] == 1 else "s")
                if r["relists"] and state == "unsold"
                else "No bids. The card goes back to %s." % md(r["seller"]))
    elif state == "withdrawn":
        title, color, desc = "%s - No longer listed" % c["name"], 0x7A7266, ""
    else:
        title, color = c["name"], 0xC89B3C
        if r["bids"]:
            desc = "High bid **%sT** by **%s** (%d bid%s)" % (
                money_text(r["high"]), md(r["bidder"]), r["bids"], "" if r["bids"] == 1 else "s")
        else:
            desc = "Opening bid **%sT**, no bids yet" % money_text(r["start"])
        fields.append({"name": "Ends", "value": "<t:%d:R>" % r["ends"], "inline": True})
        fields.append({"name": "Min. Raise", "value": "%sT" % money_text(r["raise"]),
                       "inline": True})
    fields.append({"name": "Card", "value": stats, "inline": False})
    embed = {"author": {"name": "Tetra Master Auction"}, "title": title, "description": desc,
             "color": color, "fields": fields,
             "timestamp": _stamp(r["listed"] if state == "live" else r["ends"]),
             "footer": {"text": "Auction #%d" % r["ai"]}}
    if _url(args, AUCTION_TAB):
        embed["url"] = _url(args, AUCTION_TAB)
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    png = card_png(c)
    if png is None:
        payload["attachments"] = []
        return payload, []
    fn = "card-%d.png" % r["ai"]
    embed["image"] = {"url": "attachment://%s" % fn}
    payload["attachments"] = [{"id": 0, "filename": fn}]
    return payload, [(fn, "image/png", png)]


def discord_auction_messages(snap, args=None, bot=False):
    """One slot per listing: live ones, ended-unsold ones (awaiting their
    relist or return) and sold-but-unsettled ones -- the last two ENDED, so
    polboards edits them to their result and deletes them later."""
    out = []
    a = snap["auction"]
    for r, sold in [(r, False) for r in a["listings"]] + [(r, True) for r in a["sold"]]:
        state = listing_state(r, sold)
        sig = _hash([state, r["high"], r["bids"], r["bidder"], r["relists"], r["ends"],
                     r.get("winner")])
        out.append({"slot": "auction-%d" % r["ai"], "sig": sig, "ended": state != "live",
                    "build": (lambda r=r, state=state: _auction_payload(r, state, args, bot))})
    return out


def discord_auction_retire(slot, snap, args=None, bot=False):
    """The final form of a listing that has left the store, from the last
    snapshot that still had it: sold (settled), returned unsold, or -- gone
    while still running -- no longer listed."""
    try:
        ai = int(str(slot).split("-", 1)[1])
    except (IndexError, ValueError):
        return None
    for r in snap["auction"]["sold"]:
        if r["ai"] == ai:
            return _auction_payload(r, "sold", args, bot)
    for r in snap["auction"]["listings"]:
        if r["ai"] == ai:
            return _auction_payload(r, "returned" if r["left"] <= 0 else "withdrawn", args, bot)
    return None


# ---------------------------------------------------------------------------
# the Discord BOT (2026-09-13): ONE rankings post that shows a
# list's WINDOW (the board's own image) and is FLIPPED IN PLACE by its buttons
# -- a list per button, page arrows -- and listing posts whose Bid History
# button flips the post between the card and its bids. No text lists, no new
# post per click ("the having to make a new post when you click the buttons
# is weird"). It is one shared post: the last click is what the channel sees,
# and the rankings go back to VS. Rating after VIEW_IDLE_S without one.
# Images are the board's own public URLs (render.png, card.png), so a click's
# answer, which carries no upload, can swap them.
# ---------------------------------------------------------------------------
_VIEW = {"tab": 0, "page": 0, "t": 0.0}
VIEW_IDLE_S = 600
#: auction id -> "card" or "bids": what its post shows now
_AUC_VIEW = {}


def rank_view(now=None):
    """(tab, page) the rankings post shows: the last click's, or VS. Rating
    once VIEW_IDLE_S has passed without one."""
    now = time.time() if now is None else now
    if now - _VIEW["t"] > VIEW_IDLE_S:
        return 0, 0
    return _VIEW["tab"], _VIEW["page"]


def _base(args):
    return (getattr(args, "tm_url", "") or "").strip().rstrip("/")


def _titles():
    return (board() or {}).get("titles") or ["VS. Rating", "Top 30", "Best Rookies",
                                             "Grand Total", "Weekly Total", "This Week So Far"]


def discord_bot_message(snap, args=None, tab=None, page=None):
    """The rankings post: one list's window as the image, a button per list
    (the one on show lit), page arrows, a link to the board. (payload, files)."""
    if tab is None:
        tab, page = rank_view()
    tab = max(0, min(len(snap["tabs"]) - 1, int(tab)))
    pages = tab_pages(snap, tab)
    page = max(0, min(pages - 1, int(page or 0)))
    titles, w, t = _titles(), snap.get("week") or {}, snap["tabs"][tab]
    embed = {"author": {"name": "Tetra Master Rankings"},
             "title": titles[tab] + ("  (page %d of %d)" % (page + 1, pages) if pages > 1 else ""),
             "color": 0xE0781E, "timestamp": _stamp(snap["updated"])}
    if t["kind"] == "live":
        embed["footer"] = {"text": "Live standings, not yet published"}
    elif t["published"] and w.get("from"):
        embed["footer"] = {"text": "Tally Period: %s - %s" % (_fmt_date(w["from"]),
                                                               _fmt_date(w["to"]))}
    if w.get("next"):
        embed["description"] = "Next update <t:%d:F> (<t:%d:R>)" % (w["next"], w["next"])
    if _url(args, tab):
        embed["url"] = _url(args, tab)
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}, "attachments": []}
    files = []
    if _base(args):
        embed["image"] = {"url": "%s/render.png?tab=%d&page=%d&v=%s"
                                 % (_base(args), tab, page, snap["sig"])}
    else:
        png = render_cached(tab, page, args)
        if png is not None:
            fn = "tm-%s.png" % SLUGS[tab]
            embed["image"] = {"url": "attachment://" + fn}
            payload["attachments"] = [{"id": 0, "filename": fn}]
            files = [(fn, "image/png", png)]
    btn = lambda i: {"type": 2, "style": 1 if i == tab else 2,          # noqa: E731
                     "label": titles[i], "custom_id": "tm:view:%d:0" % i}
    second = [btn(5),
              {"type": 2, "style": 2, "label": "◀ Prev",
               "custom_id": "tm:page:%d:%d" % (tab, page - 1), "disabled": page <= 0},
              {"type": 2, "style": 2, "label": "Next ▶",
               "custom_id": "tm:page:%d:%d" % (tab, page + 1), "disabled": page >= pages - 1}]
    if _url(args, tab):
        second.append({"type": 2, "style": 5, "label": "Open the board", "url": _url(args, tab)})
    payload["components"] = [{"type": 1, "components": [btn(i) for i in range(5)]},
                             {"type": 1, "components": second}]
    return payload, files


def _auction_bot_payload(r, state, args, view="card"):
    """A listing as the bot posts it: the card by the board's own card.png
    (no upload, so a click can swap it), or -- view "bids" -- its bids,
    newest first; a button flips between the two, and one opens the board."""
    payload, files = _auction_payload(r, state, args)            # the webhook form
    c, ai, base = r["card"], int(r["ai"]), _base(args)
    link = ([{"type": 2, "style": 5, "label": "Open the board", "url": _url(args, AUCTION_TAB)}]
            if _url(args, AUCTION_TAB) else [])
    card_url = ("%s/card.png?id=%d&a=%d" % (base, c["id"], c["arrows"])
                if base and c.get("art") else None)
    embed = payload["embeds"][0]
    if view == "bids":
        bids = sorted(_bids(ai), key=lambda x: -int(x["when"]))
        lines = ["`%sT`  **%s**  <t:%d:R>" % (money_text(x["amount"]), md(x["name"]), int(x["when"]))
                 for x in bids[:20]] or ["No bids yet."]
        new = {"author": embed["author"], "title": "%s - Bid History" % c["name"],
               "description": "\n".join(lines), "color": embed["color"],
               "footer": embed["footer"], "timestamp": embed["timestamp"]}
        if embed.get("url"):
            new["url"] = embed["url"]
        if card_url:
            new["thumbnail"] = {"url": card_url}
        payload["embeds"] = [new]
        first = {"type": 2, "style": 1, "label": "Back to the card", "custom_id": "tm:card:%d" % ai}
    else:
        first = {"type": 2, "style": 2, "label": "Bid History (%d)" % r["bids"],
                 "custom_id": "tm:bids:%d" % ai, "disabled": not r["bids"]}
    payload["components"] = [{"type": 1, "components": [first] + link}]
    if card_url or view == "bids":
        embed.pop("image", None)
        if card_url and view != "bids":
            embed["image"] = {"url": card_url}
        payload["attachments"] = []
        return payload, []
    return payload, files                          # no public URL: keep the upload


def _find_listing(snap, ai):
    for r in snap["auction"]["sold"]:
        if int(r["ai"]) == ai:
            return r, "sold"
    for r in snap["auction"]["listings"]:
        if int(r["ai"]) == ai:
            return r, listing_state(r)
    return None, None


def discord_interaction(data, args=None):
    """A button click (polboards checks the signature). tm:view:<tab>:<page>
    and tm:page:... flip the rankings post (tm:rank:<tab>, the first bot
    post's buttons, too); tm:bids:<id> / tm:card:<id> flip a listing's post.
    The answer EDITS the clicked post (UPDATE_MESSAGE, type 7); only a click
    on something gone gets a private note."""
    cid = str(((data or {}).get("data") or {}).get("custom_id") or "")
    parts = cid.split(":")
    snap = cached_snapshot(args)

    def note(text):
        return {"type": 4, "data": {"flags": 64, "content": text,
                                    "allowed_mentions": {"parse": []}}}
    try:
        nums = [int(x) for x in parts[2:]]
    except ValueError:
        nums = []
    if parts[:2] in (["tm", "view"], ["tm", "page"], ["tm", "rank"]) and nums:
        tab = nums[0]
        if not 0 <= tab < len(snap["tabs"]):
            return note("That list is not on the board.")
        page = max(0, min(tab_pages(snap, tab) - 1, nums[1] if len(nums) > 1 else 0))
        _VIEW.update(tab=tab, page=page, t=time.time())
        return {"type": 7, "data": discord_bot_message(snap, args, tab, page)[0]}
    if parts[:2] in (["tm", "bids"], ["tm", "card"]) and nums:
        r, state = _find_listing(snap, nums[0])
        if r is None:
            return note("That listing has ended.")
        _AUC_VIEW[nums[0]] = "bids" if parts[1] == "bids" else "card"
        return {"type": 7, "data": _auction_bot_payload(r, state, args, _AUC_VIEW[nums[0]])[0]}
    return note("That is not on the board any more.")


# ---------------------------------------------------------------------------
# the WATCHING page: a live match on the game's own board, every move
# animated the game's way -- the card slides in from its player's side, each
# battle's numbers count down from the stat to the roll (0xD34D0's own
# countdown), the loser bursts and flips to the winner's colour, a combo
# flips after the COMBO mark, a chance block says what it did; GAME START as
# a game begins, WIN / DRAW as it ends. Hands are card backs (counts only).
# ---------------------------------------------------------------------------
WATCH_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tetra Master - Watch</title>
<style>
@font-face{font-family:"TMTitle";src:url(art/tm-title.woff2) format("woff2");font-display:block}
@font-face{font-family:"TMText";src:url(art/tm-text.woff2) format("woff2");font-weight:500;font-display:block}
@font-face{font-family:"TMText";src:url(art/tm-text-bold.woff2) format("woff2");font-weight:700;font-display:block}
html,body{margin:0;height:100%;overflow:hidden;background:#07080a}
#bg{position:fixed;inset:-24px;background:#0b0c0f url(art/backdrop.png) 50% 50%/cover no-repeat;filter:blur(3px) brightness(.8)}
#vig{position:fixed;inset:0;background:radial-gradient(ellipse at 50% 45%,rgba(0,0,0,0) 30%,rgba(0,0,0,.7) 100%);pointer-events:none}
#wrap{position:fixed;left:0;top:0;width:640px;height:448px;transform-origin:0 0;-webkit-user-select:none;user-select:none;filter:drop-shadow(0 6px 14px rgba(0,0,0,.8))}
#stage{position:absolute;inset:0;overflow:hidden;background:#1d201e url(art/panel.png) 0 0/640px 448px;-webkit-mask:url(art/mask_main.png) 0 0/100% 100% no-repeat;mask:url(art/mask_main.png) 0 0/100% 100% no-repeat}
#stage img{position:absolute;display:block;pointer-events:none}
#boardbg{left:140px;top:0;width:360px;height:448px}
#layer,#fx{position:absolute;inset:0;pointer-events:none}
.card{position:absolute;transition:left .45s cubic-bezier(.2,.8,.2,1),top .45s cubic-bezier(.2,.8,.2,1),width .45s cubic-bezier(.2,.8,.2,1),height .45s cubic-bezier(.2,.8,.2,1),opacity .3s}
.card img{position:absolute}
.card .base{left:0;top:0;width:100%;height:100%}
.card .art{left:5%;top:0;width:90%;height:100%}
.card svg{position:absolute;overflow:visible}
/* a card turning shows its BACK between 45 and 225 degrees (0xB3EC5); no
   highlight is drawn on the fighting cards (TM.dll states 6-17 draw none) */
.card.back img:not(.base){visibility:hidden}
.num{position:absolute;width:0;height:24px;z-index:8}
.num img{position:absolute;top:0}
.add{mix-blend-mode:plus-lighter}
.lbl{position:absolute;z-index:12}
.combo img{position:absolute;z-index:12}
.gsov{position:absolute;inset:0;background:#000;z-index:19;opacity:0}
.gs{position:absolute;inset:0;z-index:20}
.gs img,.rb img{position:absolute;z-index:20}
/* the rotating block's arrow turns about its base (the block's centre); its
   glow and the current player's plate pulse additively, (sin(t/16)+1)/2 --
   a 100-tick period, every pulse in step (0xB76DF, 0xD4DCF) */
.card img.rotarrow{position:absolute;transform-origin:50% 100%}
.card img.rotglow,img.plhl{position:absolute;mix-blend-mode:plus-lighter;animation:pulse2 1.675s ease-in-out infinite}
@keyframes pulse2{0%,100%{opacity:0}50%{opacity:1}}
.rl{position:absolute;inset:0;z-index:18}
.rl img,.rl .rlp{position:absolute}
.rlp .t{color:#ecd6be;text-shadow:1px 1px 0 #121212}
.pop{position:absolute;z-index:9;white-space:nowrap;font:24px "TMTitle",Georgia,serif;color:#ffd86a;-webkit-text-stroke:1px #3a1804;text-shadow:0 2px 3px rgba(0,0,0,.8);transform:translate(-50%,-50%) scale(.3);opacity:0;transition:transform .3s cubic-bezier(.2,1.5,.4,1),opacity .3s}
.pop.on{transform:translate(-50%,-50%) scale(1);opacity:1}
.banner{position:absolute;z-index:20;transform:translate(-50%,-50%) scale(.2);opacity:0;transition:transform .4s cubic-bezier(.2,1.4,.4,1),opacity .4s}
.banner.on{transform:translate(-50%,-50%) scale(1);opacity:1}
.t{position:absolute;white-space:pre;line-height:1;font-family:"TMText","M PLUS 1p",sans-serif;font-weight:500;color:#eee4c8;pointer-events:none}
.b{font-weight:700}
.lab{font-family:"TMTitle",Georgia,serif;color:#f6d880;text-shadow:1px 1px 0 #241204,0 0 3px rgba(0,0,0,.9)}
.digits{position:absolute;height:32px}
.digits img{position:absolute;top:0}
.backs img{position:absolute;width:26px;height:31px}
a.nav{position:absolute;font:13px "TMTitle",Georgia,serif;color:#f6d880;text-decoration:none;text-shadow:1px 1px 0 #241204;pointer-events:auto;z-index:30}
a.nav:hover{color:#fff1c0}
#list{position:absolute;left:150px;top:92px;width:340px;list-style:none;margin:0;padding:0;font-family:"TMText",sans-serif;color:#eee4c8}
#list li a{display:block;padding:8px 10px;margin:0 0 6px;border-radius:6px;background:rgba(0,0,0,.35);color:inherit;text-decoration:none;pointer-events:auto}
#list li a:hover{background:rgba(246,216,128,.18)}
#list .who{font-weight:700;font-size:15px}
#list .where{font-size:12px;color:#d6be82;margin-top:3px}
#msg{position:absolute;left:0;right:0;top:0;bottom:0;display:flex;align-items:center;justify-content:center;text-align:center;font:16px "TMText",system-ui,sans-serif;color:#eee4c8;z-index:25}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.card img.ca{position:absolute;filter:drop-shadow(0 0 1px #000)}
/* the special tile's icon on the card that took it: its full 74x70 cell over
   the card's centre, ADDITIVE at half alpha (0xB49BC); on an empty tile the
   same cell, opaque (0xB6DFD) */
.card img.badge{position:absolute;left:4.88%;top:14.29%;width:90.24%;height:71.43%;opacity:.5;mix-blend-mode:plus-lighter;pointer-events:none}
.card img.st,#cinfo img.st{position:absolute}
img.st.up{filter:sepia(1) saturate(7) brightness(1.15)}
img.st.down{filter:sepia(1) saturate(4) hue-rotate(130deg) brightness(.95)}
.spicon{position:absolute}
.spr{position:absolute;z-index:9;pointer-events:none}
.bolt{position:absolute;z-index:9;pointer-events:none;transform-origin:0 50%}
.face{position:absolute;border:1px solid #7a5626;border-radius:3px;background:#1a1208;box-shadow:0 1px 3px rgba(0,0,0,.7)}
.panelbox{position:absolute;background:rgba(10,8,4,.8);border:1px solid #8a6a2c;border-radius:6px;box-shadow:0 4px 14px rgba(0,0,0,.8)}
#result{z-index:22;left:175px;top:268px;width:290px;padding:10px 0 12px;text-align:center;font-family:"TMText",sans-serif;color:#eee4c8}
#result .h{font:22px "TMTitle",Georgia,serif;color:#f6d880;text-shadow:1px 1px 0 #241204}
#result .score{font-size:14px;font-weight:700;margin:4px 0 8px}
#result .v{font-size:12px;line-height:18px}
#result .v b{color:#f6d880;font-weight:700}
#msg .panelbox{position:relative;padding:14px 18px;max-width:300px}
#cinfo{z-index:21;left:8px;top:192px;width:124px;padding:8px 0 10px;text-align:center;font-family:"TMText",sans-serif;color:#eee4c8}
#cinfo .pic{position:relative;width:80px;height:96px;margin:0 auto 6px}
#cinfo .pic img{position:absolute}
#cinfo .nm{font-size:12px;font-weight:700;line-height:15px;padding:0 6px}
#cinfo .ln{position:relative;height:12px;margin:5px auto 0}
#cinfo .ln img{position:absolute;top:0}
#cinfo .who{font-size:11px;color:#d6be82;margin-top:4px}
</style></head><body>
<div id="bg"></div><div id="vig"></div>
<div id="wrap"><div id="stage"><div id="msg">Loading...</div></div></div>
<p class="sr" id="sr" aria-live="polite"></p>
<script>
"use strict";
let L = null, W = null, S = null, ID = '', SEQ = -1, BUSY = false, QUEUE = [], OK = true;
let PHASE = null, SHOWN_END = false, LISTING = false;
// GEN: bumped whenever the view resets (the match went away, a rematch, a new
// hash). A move animating under an older GEN stops at its next step instead of
// drawing on a board that is no longer there -- that is what left battle
// numbers on the "match has ended" screen (seen 2026-09-13).
let GEN = 0, ENDED = false, REMATCH = false, LAST = null, INTRO = null;
const $ = s => document.querySelector(s), stage = $('#stage'), wrap = $('#wrap');
const PAD = 14, CARDS = {}, SEAT_COLOR = ['b', 'r', 'g'];
const DIRS = [[.5, 0], [1, 0], [1, .5], [1, 1], [.5, 1], [0, 1], [0, .5], [0, 0]];
const EFFECT = {power_up: 'Power Up!', power_down: 'Power Down', take: 'Take!', scramble: 'Scramble!'};
function art(n){ return 'art/' + n; }
function el(tag, cls, parent){ const e = document.createElement(tag); if (cls) e.className = cls; (parent || stage).appendChild(e); return e; }
function box(e, r){ e.style.left = r[0] + 'px'; e.style.top = r[1] + 'px'; e.style.width = r[2] + 'px'; e.style.height = r[3] + 'px'; }
function sz(n){ return (W.sizes || {})[n] || [0, 0]; }
function wait(ms){ return new Promise(r => setTimeout(r, ms * (QUEUE.length > 1 ? 0.3 : 1))); }
function fit(){
  const k = Math.min((innerWidth - 2 * PAD) / 640, (innerHeight - 2 * PAD) / 448);
  wrap.style.transform = 'scale(' + k + ')';
  wrap.style.left = ((innerWidth - 640 * k) / 2) + 'px';
  wrap.style.top = ((innerHeight - 448 * k) / 2) + 'px';
}
function geo(){ return W.boards[String(S.tiles)] || W.boards['16']; }
function rect(t){
  const g = geo(), c = t % g.cols, r = Math.floor(t / g.cols);
  return [140 + g.x[c] + 3, g.y[r] + 3, g.x[c + 1] - g.x[c] - 6, g.y[r + 1] - g.y[r] - 6];
}
function center(t){ const r = rect(t); return [r[0] + r[2] / 2, r[1] + r[3] / 2]; }
// the seats' panels: 0 on the left, 1 on the right, 2 below it
function panelAt(s){ const n = S ? S.n : 2; return s === 0 ? [8, 64] : (n > 2 && s === 2) ? [508, 250] : [508, 64]; }
function handAt(s){ const p = panelAt(s); return [p[0] + 50, p[1] + 110]; }
function blockKind(code){ return code <= 2 ? 'plain' : (code >= 9 && code <= 16) ? 'rot' : 'chance'; }
function arrowsSvg(d, mask, w, h){
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('width', w + 10); svg.setAttribute('height', h + 10);
  svg.style.left = '-5px'; svg.style.top = '-5px';
  let p = '';
  DIRS.forEach(([fx, fy], k) => {
    if (!(mask >> k & 1)) return;
    const cx = 5 + fx * w, cy = 5 + fy * h, dx = fx * 2 - 1, dy = fy * 2 - 1, n = Math.hypot(dx, dy) || 1;
    const ux = dx / n, uy = dy / n, s = 6;
    p += '<polygon points="' + [cx + ux * s, cy + uy * s, cx - uy * s * .8, cy + ux * s * .8, cx + uy * s * .8, cy - ux * s * .8].map(v => v.toFixed(1)).join(',') + '"/>';
  });
  svg.innerHTML = '<g fill="#f2c24a" stroke="#3a1e04" stroke-width="1">' + p + '</g>';
  d.appendChild(svg);
  return svg;
}
// a 16x16 renderer cell centred (dx, dy) from the card's centre, in its
// 80x96 space -- in percent, so it rides the card's slide-in and scaling
function cell(parent, f, dx, dy, cls){
  const F = W.card_face || {cell: 16}, c = F.cell || 16, im = el('img', cls, parent);
  im.alt = ''; im.src = art(f);
  im.style.width = (c / 80 * 100) + '%'; im.style.height = (c / 96 * 100) + '%';
  im.style.left = ((40 + dx - c / 2) / 80 * 100) + '%'; im.style.top = ((48 + dy - c / 2) / 96 * 100) + '%';
  return im;
}
// the arrow tips the game draws ON the card (gW000 on_card1), where TM.dll puts
// them: 37 across and 45 down from the centre (0x51C339C, layout kind 0)
function cardArrows(d, mask){
  const F = W.card_face || {arrow_dx: 37, arrow_dy: 45};
  DIRS.forEach(([fx, fy], i) => {
    if (mask >> i & 1) cell(d, W.card_arrows[i], (fx * 2 - 1) * F.arrow_dx, (fy * 2 - 1) * F.arrow_dy, 'ca');
  });
}
// the card face's stat line: attack, type, pdef, mdef (TM.dll 0xB5062/0xB5374/
// 0xB55EC/0xB58D9). A stat shows its TENS digit, 100+ the star, below 0 a 0,
// after the power up/down modifier; a boosted line is drawn yellow, a lowered one teal
function statChars(c){
  const m = c.mod || 0, g = v => { v += m; return v < 0 ? '0' : v > 99 ? '*' : String(Math.floor(v / 10)); };
  return [g(c.atk || 0), 'PMXA'[c.type] || 'P', g(c.pdef || 0), g(c.mdef || 0)];
}
function statGlyphs(d, c){
  if (!W.glyphs || !W.card_face) return;
  const F = W.card_face, tint = c.mod > 0 ? 'up' : c.mod < 0 ? 'down' : '';
  statChars(c).forEach((ch, i) => { const im = cell(d, W.glyphs[ch], F.stats_x[i], F.stats_y, 'st' + (tint && i !== 1 ? ' ' + tint : '')); });
}
function baseFor(c){
  if (c.block !== undefined && !(c.owner >= 0 && c.owner < S.n)) return W.blocks[blockKind(c.block)];
  return W.bases[c.owner] || W.bases[0];
}
function makeCard(t, c, from){
  const d = el('div', 'card', $('#layer'));
  d.dataset.tile = t;
  const r = from || rect(t);
  box(d, r);
  const base = el('img', 'base', d); base.alt = ''; base.src = art(baseFor(c));
  if (c.block === undefined){
    const a = el('img', 'art', d); a.alt = ''; a.src = art('card_' + String(c.id).padStart(3, '0') + '.png');
  }
  const rr = rect(t), rot = c.block !== undefined && blockKind(c.block) === 'rot' && W.rot;
  let rotArrow = null;
  if (rot) rotArrow = rotParts(d, c);
  else if (c.arrows && !(c.block !== undefined && blockKind(c.block) === 'plain')){
    if (W.card_arrows) cardArrows(d, c.arrows); else arrowsSvg(d, c.arrows, rr[2], rr[3]);
  }
  // a special tile's ability rides on the card played there (1 atk, 2 def, 3 all
  // arrows): its icon over the card's centre, under the arrows and the stat line
  if (c.ability >= 1 && c.ability <= 3 && W.icons && W.icons[c.ability]){
    const b = el('img', 'badge', d); b.alt = ''; b.src = art(W.icons[c.ability]);
    const first = d.querySelector('img.ca, img.st'); if (first) d.insertBefore(b, first);
  }
  if (c.block === undefined) statGlyphs(d, c);
  if (c.block === undefined){ d.style.pointerEvents = 'auto'; d.onmouseenter = () => cardInfo(t); d.onmouseleave = () => cardInfo(null); d.onclick = () => cardInfo(t, true); }
  CARDS[t] = {el: d, c: Object.assign({}, c), base, rotArrow, rotAngle: rotArrow ? rotAngleOf(c) : 0};
  return CARDS[t];
}
function clearBoard(){ $('#layer').innerHTML = ''; for (const k in CARDS) delete CARDS[k]; }
// every running effect off the board, and any move still animating told to stop
function clearFx(){ GEN++; QUEUE.length = 0; const f = $('#fx'); if (f) f.innerHTML = ''; }
// an UNPLAYED special tile: its icon, opaque, the card-sized 74x70 cell
// (sword = attack up, shield = defense up, star = all arrows; codes 3/4/5)
function drawSpecials(){
  document.querySelectorAll('#layer .spicon').forEach(e => e.remove());
  (S.objects || []).forEach((code, t) => {
    if (!(code >= 3 && code <= 5) || String(t) in (S.board || {})) return;
    const f = W.icons && W.icons[code - 2];
    if (!f) return;
    const r = rect(t), w = r[2] * 74 / 82, h = r[3] * 70 / 98, ic = el('img', 'spicon', $('#layer'));
    ic.alt = ''; ic.src = art(f); ic.dataset.tile = t; ic.dataset.code = code;
    box(ic, [r[0] + (r[2] - w) / 2, r[1] + (r[3] - h) / 2, w, h]);
  });
  // under the cards, so a card played on a special covers it
  const L = $('#layer'); document.querySelectorAll('#layer .spicon').forEach(e => L.prepend(e));
}
// the board as the state has it, no animation: the first view, and after a scramble
function drawBoard(){
  clearBoard();
  for (const [t, c] of Object.entries(S.board || {})) makeCard(+t, c);
  drawSpecials();
}
// the live board converges on the state without redrawing what already matches
function syncBoard(){
  const want = S.board || {};
  for (const t of Object.keys(CARDS)) if (!(t in want)){ CARDS[t].el.remove(); delete CARDS[t]; }
  for (const [t, c] of Object.entries(want)){
    const k = CARDS[t];
    if (!k || k.c.id !== c.id || k.c.block !== c.block || (k.c.ability || 0) !== (c.ability || 0)
        || (k.c.mod || 0) !== (c.mod || 0)){ if (k) k.el.remove(); makeCard(+t, c); }
    else {
      setOwner(+t, c.owner, false);
      // a rotating block's arrow turns a step each turn, in place (0xC73BE)
      if (k.rotArrow && c.phase != null && k.c.phase !== c.phase){ k.c.phase = c.phase; rotTo(k, c.phase); }
    }
  }
  drawSpecials();
}
function digitsEl(parent, value, color, x, y, align){
  const e = el('div', 'digits', parent);
  const s = String(Math.max(0, value | 0));
  let w = 0;
  for (const ch of s){ const im = el('img', '', e); const f = W.score[color][+ch]; im.src = art(f); im.alt = ''; im.style.left = w + 'px'; w += sz(f)[0] + 1; }
  e.style.width = w + 'px';
  e.style.left = (align === 'right' ? x - w : x) + 'px'; e.style.top = y + 'px';
  return e;
}
function text(parent, s, x, y, o){
  o = o || {};
  const e = el('span', 't' + (o.cls ? ' ' + o.cls : ''), parent);
  e.textContent = s; e.style.fontSize = (o.size || 13) + 'px';
  const w = e.offsetWidth;
  e.style.left = (o.align === 'right' ? x - w : o.align === 'center' ? x - w / 2 : x) + 'px';
  e.style.top = y + 'px';
  if (o.max && w > o.max){ e.style.fontSize = ((o.size || 13) * o.max / w) + 'px'; }
  return e;
}
function drawPanels(){
  const P = $('#panels'); P.innerHTML = '';
  (S.players || []).forEach((p, s) => {
    const [x, y] = panelAt(s), col = SEAT_COLOR[s] || 'n';
    const plate = el('img', '', P); plate.alt = ''; plate.src = art(W.plates[col] || W.plates.n); box(plate, [x, y, 124, 17]);
    // whose turn: an additive copy of the plate, pulsing in step with every
    // other pulse on the page (0xD4DCF)
    if (S.phase === 'play' && S.active === s && !INTRO){
      const h = el('img', 'plhl', P); h.alt = ''; h.src = plate.src; box(h, [x, y, 124, 17]);
      h.style.animationDelay = -(performance.now() % 1675) + 'ms';
    }
    const com = p.com && W.coms ? W.coms[String(p.ci)] : null;
    // real side room on the plate (captions must not run edge to edge)
    text(P, (com && /^COM \d+$/.test(p.name || '') ? com.name : p.name) || ('Player ' + (s + 1)), x + 62, y + 3, {align: 'center', size: 12, cls: 'b', max: 102});
    // the opponent's portrait (gW080 fac000NN, the same index as its PlPrm record)
    // the opponent's portrait, or a player's own PlayOnline portrait (64x96)
    const face = p.face || (com && com.face);
    if (face || p.face_id){
      const [fw, fh] = face ? sz(face) : [64, 96], k = Math.min(40 / fw, 46 / fh), f = el('img', 'face', P);
      f.alt = ''; f.src = face ? art(face) : 'face.png?id=' + encodeURIComponent(p.face_id);
      f.onerror = () => f.remove();
      box(f, [x + 4, y + 22, fw * k, fh * k]);
    }
    digitsEl(P, p.score || 0, col in W.score ? col : 'b', x + 62 - 12, y + 26, 'left');
    if (p.com) text(P, 'COM', x + 120, y + 36, {align: 'right', size: 11, cls: 'lab'});
    const n = p.hand == null ? 0 : p.hand;
    const backs = el('div', 'backs', P);
    for (let i = 0; i < n; i++){ const im = el('img', '', backs); im.alt = ''; im.src = art(W.back); im.style.left = (x + 4 + i * 23) + 'px'; im.style.top = (y + 72) + 'px'; }
    if (S.phase === 'play' && S.active === s){
      const h = el('img', '', P); h.alt = ''; h.src = art(W.hand);
      const hs = sz(W.hand); box(h, [s === 0 ? x + 124 : x - hs[0], y - 2, hs[0], hs[1]]);
      if (s !== 0) h.style.transform = 'scaleX(-1)';
    }
  });
  const I = $('#info'); I.innerHTML = '';
  const where = 'Room ' + S.room + ', Table ' + S.table;
  const turn = S.phase === 'select' ? 'Choosing cards' : S.phase === 'over' ? 'Game over'
             : 'Turn ' + Math.min((S.turn || 0) + 1, S.limit) + ' of ' + S.limit;
  text(I, where, 70, 404, {align: 'center', size: 12});
  text(I, turn, 70, 422, {align: 'center', size: 12, cls: 'lab'});
  $('#sr').textContent = (S.players || []).map(p => p.name + ' ' + p.score).join(', ') + '. ' + turn;
  drawResult();
}
// ---------------------------------------------------------------------------
// THE ANIMATIONS, as TM.dll draws them (reverse-engineered 2026-09-13). Every
// time below is in the client's own 60 Hz ticks (T ms): its scene clock,
// tweens and effect timers are all normalised to 60 ([0x528F568] = fps).
// ---------------------------------------------------------------------------
const T = 1000 / 60;
const TINT = ['sepia(1) saturate(5) hue-rotate(185deg)', 'sepia(1) saturate(6) hue-rotate(-35deg)',
              'sepia(1) saturate(5) hue-rotate(65deg)'];
function spd(){ return QUEUE.length > 1 ? 0.3 : 1; }
function LK(){ return S && S.tiles === 25 ? .825 : .925; }        // the board's layout scale (0x2335A0)
function tileRC(t){ const g = geo(); return [t % g.cols, Math.floor(t / g.cols)]; }
function cardsOf(seat){ return Object.keys(CARDS).map(Number).filter(t => CARDS[t].c.block === undefined && CARDS[t].c.owner === seat); }
// fn(tick) once per 60 Hz tick for n ticks; false if the view reset under it
async function ticks(n, gen, fn){
  for (let i = 0; i < n; i++){ if (gen !== GEN) return false; if (fn(i) === false) return true; await wait(T); }
  return true;
}
function banner(img, ms){
  return new Promise(res => {
    const b = el('img', 'banner', $('#fx')); b.alt = ''; b.src = art(img);
    const [w, h] = sz(img), k = Math.min(1, 330 / w);
    b.style.width = (w * k) + 'px'; b.style.height = (h * k) + 'px';
    b.style.left = '320px'; b.style.top = '210px';
    requestAnimationFrame(() => requestAnimationFrame(() => b.classList.add('on')));
    setTimeout(() => { b.classList.remove('on'); setTimeout(() => { b.remove(); res(); }, 400); }, ms || 1200);
  });
}
function pop(label, t){
  const [x, y] = center(t), p = el('div', 'pop', $('#fx'));
  p.textContent = label; p.style.left = x + 'px'; p.style.top = y + 'px';
  requestAnimationFrame(() => requestAnimationFrame(() => p.classList.add('on')));
  setTimeout(() => { p.classList.remove('on'); setTimeout(() => p.remove(), 300); }, 900);
}
function spr(at, w, h, filter){
  const [x, y] = typeof at === 'number' ? center(at) : at, im = el('img', 'spr', $('#fx'));
  im.alt = ''; box(im, [x - w / 2, y - h / 2, w, h]); if (filter) im.style.filter = filter;
  return im;
}
async function frames(im, list, ms, loops, gen){
  for (let l = 0; l < (loops || 1); l++) for (const f of list){ if (gen !== GEN) return; im.src = art(f); await wait(ms); }
}
// the battle numbers, drawn by the card renderer (0xB5C34): 24x24 digits on
// the card's centre -- one digit at -12, two from -22 and three from -32, 18
// apart; a value below 0 hides it
function numAt(t, value){
  const [x, y] = center(t), e = el('div', 'num', $('#fx'));
  const set = v => {
    e.innerHTML = '';
    if (v < 0) return;
    const s = String(v | 0), x0 = s.length === 1 ? -12 : s.length === 2 ? -22 : -32;
    [...s].forEach((ch, i) => { const im = el('img', '', e); im.alt = ''; im.src = art((W.num || W.digits)[+ch]); box(im, [x0 + i * 18, 0, 24, 24]); });
    e.style.left = x + 'px'; e.style.top = (y - 12) + 'px';
  };
  set(value);
  return {el: e, set};
}
// while the numbers show, the stat each side fights with flickers in random
// colours (R = 0xFF, G/B random every frame -- 0xB5308/0xB5884/0xB5B71);
// selector bits 1 attack, 4 pdef, 8 mdef
function flicker(t, sel){
  const k = CARDS[t];
  if (!k || !sel) return () => {};
  const st = k.el.querySelectorAll('img.st'), idx = [];
  if (sel & 1) idx.push(0); if (sel & 4) idx.push(2); if (sel & 8) idx.push(3);
  const id = setInterval(() => idx.forEach(i => { if (st[i]) st[i].style.filter = 'sepia(1) saturate(9) hue-rotate(' + (Math.random() * 360 | 0) + 'deg) brightness(1.4)'; }), 50);
  return () => { clearInterval(id); idx.forEach(i => { if (st[i]) st[i].style.filter = ''; }); };
}
// both cards jump 10 % of the way to each other and glide back over 8 ticks (0xCF36A)
function lunge(ta, td){
  const A = CARDS[ta], D = CARDS[td];
  if (!A || !D) return;
  const [ax, ay] = center(ta), [dx, dy] = center(td), vx = (dx - ax) * .1, vy = (dy - ay) * .1;
  A.el.animate([{transform: 'translate(' + vx + 'px,' + vy + 'px)'}, {transform: 'none'}], {duration: 133 * spd(), easing: 'linear'});
  D.el.animate([{transform: 'translate(' + (-vx) + 'px,' + (-vy) + 'px)'}, {transform: 'none'}], {duration: 133 * spd(), easing: 'linear'});
}
// type 1: effec_00's 12 frames, 2 ticks each, additive, 48 x scale; a quarter
// of the time it throws 4 sparks (type 2) (0xF13D7)
function burst(x, y, scale, gen){
  const F = W.fx;
  if (!F || !F.hit) return;
  const s = 48 * scale * LK(), im = spr([x, y], s, s); im.classList.add('add');
  (async () => { for (const f of F.hit){ if (gen !== GEN) break; im.src = art(f); await wait(2 * T); } im.remove(); })();
  if (Math.random() < .25) for (let q = 0; q < 4; q++) spark(x, y, scale * .67, q, gen);
}
function spark(x, y, scale, q, gen){
  const F = W.fx, k = (Math.random() * 3 | 0) + 1, sp = ((Math.random() * 8 | 0) * .5 + 4) * LK() / k;
  const a = ((Math.random() * 90 | 0) + 90 * q) * Math.PI / 180, s = 24 * scale * LK(), im = spr([x, y], s, s);
  im.classList.add('add');
  let px = x, py = y;
  ticks(12 * k, gen, i => { px += Math.cos(a) * sp; py += Math.sin(a) * sp; im.src = art(F.hit[Math.min(11, i / k | 0)]); box(im, [px - s / 2, py - s / 2, s, s]); }).then(() => im.remove());
}
// type 11 -> 12 type-4 shards off a struck block: thrown out, bouncing under
// gravity 1 a tick, x0.6 a bounce, fading over their last 16 of 120 ticks (0xF15F4)
function shards(x, y, gen){
  const F = W.fx;
  if (!F || !F.shards) return;
  for (let k = 0; k < 12; k++){
    const L = LK(), f = F.shards[(Math.random() * F.shards.length) | 0];
    const sp = ((Math.random() * 15 | 0) * .1 + .5) * L, a = ((Math.random() * 90 | 0) + 90 * (k % 4)) * Math.PI / 180;
    const vx = Math.cos(a) * sp, vy = Math.sin(a) * sp, s = 32 * ((Math.random() * 10 | 0) * .1 + .5) * L;
    let px = x + vx * 8, py = y + vy * 8, h = 0, vh = 4 + Math.random() * 3, rest = false, rot = 0;
    const im = spr([px, py], s, s); im.src = art(f);
    ticks(120, gen, i => {
      if (!rest){ px += vx; py += vy; h += vh; vh -= 1; rot += 12;
        if (h <= 0){ h = 0; vh = -vh * .6; if (Math.random() < .25 || vh < 1) rest = true; } }
      im.style.opacity = i > 104 ? (120 - i) / 16 : 1;
      box(im, [px - s / 2, py - h - s / 2, s, s]); im.style.transform = 'rotate(' + rot + 'deg)';
    }).then(() => im.remove());
  }
}
// type 3: an effec_01 puff, tinted by param (3 white, 4 dark = normal alpha),
// k = 2..4 ticks a frame, carrying the velocity it was thrown with
function puff(x, y, scale, tint, vx, vy, gen){
  const F = W.fx, k = (Math.random() * 3 | 0) + 2, s = 48 * scale, im = spr([x, y], s, s);
  if (tint === 4) im.style.filter = 'brightness(.3)';
  else { im.classList.add('add'); if (TINT[tint]) im.style.filter = TINT[tint]; }
  const rot = 'rotate(' + (Math.random() * 360 | 0) + 'deg)';
  let px = x, py = y;
  ticks(12 * k, gen, i => { px += vx; py += vy; im.src = art(F.smoke[Math.min(11, i / k | 0)]);
    box(im, [px - s / 2, py - s / 2, s, s]); im.style.transform = rot; }).then(() => im.remove());
}
// type 10: three puffs thrown outward at ticks 3, 5 and 7
function puffs(x, y, scale, tint, gen){
  [3, 5, 7].forEach(d => setTimeout(() => {
    if (gen !== GEN) return;
    const a = Math.random() * 2 * Math.PI, v = 1.4 * LK();
    puff(x, y, scale * .5, tint, Math.cos(a) * v, Math.sin(a) * v, gen);
  }, d * T * spd()));
}
// type 12: a column of dark puffs, one every 2 ticks for 100, rising
function smokeColumn(x, y, gen){
  ticks(100, gen, i => { if (!(i % 2)) puff(x + (Math.random() - .5) * 24, y, LK() * 1.1, 4, 0, -1.2 * LK(), gen); });
}
// types 13/14/15: the reaper / the girl, 80x80 poses; 13 is revealed upward
// out of the ground over 110 ticks, the others fade in and out
function figure(list, x, y, scale, life, gen, o){
  o = o || {};
  const s = 80 * scale, im = spr([x, y], s, s); im.src = art(list[0]);
  if (o.add) im.classList.add('add');
  ticks(life, gen, i => {
    im.src = art(list[(i / 8 | 0) % list.length]);
    im.style.opacity = o.rise ? Math.min(1, (life - i) / 16) : Math.min(1, i / 16, (life - i) / 16);
    if (o.rise) im.style.clipPath = 'inset(' + Math.max(0, 100 - i / o.rise * 100) + '% 0 0 0)';
  }).then(() => im.remove());
}
// type 16: an orb that homes onto a card, then bursts (at t = 110)
function orb(x0, y0, t, scale, gen){
  const F = W.fx, [tx, ty] = center(t), s = 60 * scale, im = spr([x0, y0], s, s * .93);
  im.classList.add('add');
  let px = x0, py = y0;
  ticks(150, gen, i => {
    if (i < 110){ px += (tx - px) / Math.max(1, 110 - i); py += (ty - py) / Math.max(1, 110 - i);
      im.src = art(F.up[(i / 4 | 0) % F.up.length]); box(im, [px - s / 2, py - s * .47, s, s * .93]); }
    else { if (i === 110) burst(tx, ty, 1.2, gen); im.style.opacity = (150 - i) / 40; }
  }).then(() => im.remove());
}
// type 19: an effec_06 coin -- homes onto a card, or scatters and bounces
function coin(x0, y0, target, scale, gen){
  const F = W.fx, s = 40 * scale, im = spr([x0, y0], s, s * 1.1);
  let px = x0, py = y0, h = 0, vh = 4, vx = (Math.random() - .5) * 5, vy = (Math.random() - .5) * 4;
  const to = target != null ? center(target) : null;
  ticks(180, gen, i => {
    im.src = art(F.take[(i / 5 | 0) % F.take.length]);
    if (to){ if (i < 60){ px += (to[0] - px) / (60 - i); py += (to[1] - py) / (60 - i); }
      h = Math.max(0, Math.sin(Math.min(1, i / 60) * Math.PI) * 30); }
    else { px += vx; py += vy; h += vh; vh -= .5; if (h < 0){ h = 0; vh = -vh * .6; } vx *= .98; vy *= .98; }
    im.style.opacity = i > 164 ? (180 - i) / 16 : 1;
    box(im, [px - s / 2, py - h - s / 2, s, s * 1.1]);
  }).then(() => im.remove());
}
// type 6: one of the ring of 8 bouncing chips (gravity .25)
function chip(x, y, i, gen){
  const F = W.fx, a = i * Math.PI / 4, s = 32 * LK(), pp = [0, 1, 2, 3, 2, 1];
  let px = x, py = y, h = 0, vh = 5;
  const im = spr([x, y], s, s);
  ticks(170, gen, j => {
    if (j < 60){ px += Math.cos(a) * 1.5; py += Math.sin(a) * 1.5; }
    h += vh; vh -= .25; if (h < 0){ h = 0; vh = -vh * .6; }
    if (F.chips && F.chips.length) im.src = art(F.chips[pp[(j / 4 | 0) % 6] % F.chips.length]);
    im.style.opacity = j > 154 ? (170 - j) / 16 : 1;
    box(im, [px - s / 2, py - h - s / 2, s, s]);
  }).then(() => im.remove());
}
// type 20: a wandering emitter -- a big soft orb (type 9) every 4 ticks, a white puff every 8
function wander(x, y, gen){
  let px = x, py = y, vx = 0, vy = 0;
  ticks(150, gen, i => {
    vx = (vx + (Math.random() - .5) * .8) * .95; vy = (vy + (Math.random() - .5) * .8) * .95; px += vx; py += vy;
    if (i % 4 === 0) glow(px, py, gen);
    if (i % 8 === 0) puff(px, py, LK() * .8, 3, 0, -.5, gen);
  });
}
// type 9: effec_04's big orb, grey, additive, growing 1.5 -> 7.3 as it fades (30 ticks)
function glow(x, y, gen){
  const F = W.fx, im = spr([x, y], 1, 1); im.classList.add('add'); im.src = art(F.orb[0]);
  ticks(30, gen, i => { const s = 64 * (1.5 + 5.8 * i / 30) * LK() * .5; box(im, [x - s / 2, y - s / 2, s, s]); im.style.opacity = .5 * (1 - i / 30); }).then(() => im.remove());
}
// type 24: a pop where the ray lands -- scale +0.1 a tick, fading over its last 8 of 20
function popAt(x, y, gen){
  const F = W.fx, im = spr([x, y], 1, 1); im.classList.add('add');
  ticks(20, gen, i => { const s = 64 * LK() * .1 * (i + 1); im.src = art(F.burst[Math.min(3, i / 5 | 0)]);
    box(im, [x - s / 2, y - s / 2, s, s]); im.style.opacity = i > 12 ? (20 - i) / 8 : 1; }).then(() => im.remove());
}
// type 23: the ray, effec_08's 128x40 cells stretched tail to head; the head
// reaches the next tile in 8 ticks, the cell re-randomises for 20 then holds,
// fading over the last 32 of 60
function bolt(a, b, gen){
  const F = W.fx, [x1, y1] = center(a), [x2, y2] = center(b), len = Math.hypot(x2 - x1, y2 - y1);
  const im = el('img', 'bolt add', $('#fx')); im.alt = '';
  box(im, [x1, y1 - 20, 1, 40]); im.style.transform = 'rotate(' + Math.atan2(y2 - y1, x2 - x1) + 'rad)';
  let cur = 0;
  ticks(60, gen, i => {
    if (i < 20) cur = (Math.random() * F.bolt.length) | 0;
    im.src = art(F.bolt[cur]); im.style.width = (len * Math.min(1, (i + 1) / 8)) + 'px';
    im.style.opacity = i > 28 ? (60 - i) / 32 : 1;
  }).then(() => im.remove());
}
// the word the game floats over a card (on_card2, English client): alpha up
// over 16 ticks, held to 32, gone at 48; power up grows it, power down shrinks
// it, a special tile's label rises a pixel a tick from 12 below the centre
function label(t, key, mode, gen){
  const f = W.labels && W.labels[key];
  if (!f) return;
  const [x, y] = center(t), [w, h] = sz(f), im = el('img', 'lbl', $('#fx')); im.alt = ''; im.src = art(f);
  ticks(48, gen, i => {
    const s = mode === 'grow' ? 1 + .02 * i : mode === 'shrink' ? 1 - .02 * i : 1;
    box(im, [x - w * s / 2, mode === 'rise' ? y + 12 - i - h / 2 : y - h * s / 2, w * s, h * s]);
    im.style.opacity = i < 16 ? i / 16 : i < 32 ? 1 : (48 - i) / 16;
  }).then(() => im.remove());
}
// which way a captured card turns: N/S about X, E/W about Y, the diagonals
// about X and +/-Z (0xB3B10)
function axisFor(from, to){
  const [fc, fr] = tileRC(from), [tc, tr] = tileRC(to), dx = Math.sign(tc - fc), dy = Math.sign(tr - fr);
  if (dx === 0) return [1, 0, 0];
  if (dy === 0) return [0, 1, 0];
  return dx > 0 ? [1, 0, 1] : [1, 0, -1];
}
function showBack(k, on){ k.el.classList.toggle('back', on); k.base.src = art(on ? W.back : baseFor(k.c)); }
// a card changing hands: one full turn, linear (667 ms = 40 ticks); its back
// shows from 45 to 225 degrees, so the old colour for 5 ticks, the new from 25.
// A battle's loser also jumps half a tile away from the winner at twice the
// size (15 ticks) and settles back (by ~34), under a white flash fading over 32
function flipCard(t, owner, o){
  const k = CARDS[t];
  if (!k) return Promise.resolve();
  o = o || {};
  const e = k.el, full = o.dur || 667, sp = spd(), dur = full * sp;
  const R = 'rotate3d(' + (o.from != null && o.from !== t ? axisFor(o.from, t) : [0, 1, 0]).join(',') + ',';
  k.c.owner = owner;
  let dx = 0, dy = 0;
  if (o.away != null){ const [x1, y1] = center(o.away), [x2, y2] = center(t), f = o.frac || .5; dx = (x2 - x1) * f; dy = (y2 - y1) * f; }
  const kf = o.away != null
    ? [[0, 0, 0, 1], [(o.go || 250) / full, dx, dy, 2], [(o.hold || 267) / full, dx, dy, 2], [(o.back || 567) / full, 0, 0, 1], [1, 0, 0, 1]]
    : [[0, 0, 0, 1], [1, 0, 0, 1]];
  e.style.zIndex = 8;
  e.animate(kf.map(([off, x, y, s]) => ({offset: off, transform: 'perspective(700px) translate(' + x + 'px,' + y + 'px) scale(' + s + ') ' + R + (360 * off) + 'deg)'})),
            {duration: dur, easing: 'linear'});
  if (o.flash) e.animate([{filter: 'brightness(2.4)'}, {filter: 'brightness(1)'}], {duration: 533 * sp});
  setTimeout(() => { if (CARDS[t] === k) showBack(k, true); }, dur * .125);
  setTimeout(() => { if (CARDS[t] === k) showBack(k, false); }, dur * .625);
  return wait(full).then(() => { e.style.zIndex = ''; });
}
function setOwner(t, owner, animate){
  const k = CARDS[t];
  if (!k || k.c.owner === owner) return;
  if (!animate){ k.c.owner = owner; k.base.src = art(baseFor(k.c)); return; }
  flipCard(t, owner, {});
}
// COMBO, over the beaten card from tick 20 to 84 when the chain reaches 2
// (0xD00F6): the word at (-88, -16), the count's 32x32 digits from +32
function comboBanner(t, count, gen){
  const [x, y] = center(t), g = el('div', 'combo', $('#fx'));
  const w = el('img', '', g); w.alt = ''; w.src = art(W.combo); box(w, [x - 88, y - 16, 112, 32]);
  String(count).split('').forEach((d, i) => { const im = el('img', '', g); im.alt = ''; im.src = art((W.combo_digits || [])[+d] || W.combo); box(im, [x + 32 + i * 32, y - 16, 32, 32]); });
  g.style.opacity = 0;
  return ticks(64, gen, i => { g.style.opacity = i < 16 ? i / 16 : i > 48 ? (64 - i) / 16 : 1; }).then(() => g.remove());
}
// a card is played: set on its tile at 3x and dim, it drops to 1x in 15 ticks
// (0xC2D47), then brightens over 8; on a special tile the tile's word rises
async function drop(t, card, gen){
  const sp = document.querySelector('#layer .spicon[data-tile="' + t + '"]'), code = sp ? +sp.dataset.code : 0;
  if (CARDS[t]){ CARDS[t].el.remove(); delete CARDS[t]; }
  const k = makeCard(t, card), e = k.el;
  e.style.zIndex = 7;
  e.animate([{transform: 'scale(3)', filter: 'brightness(.25)'}, {transform: 'scale(1)', filter: 'brightness(.25)'}], {duration: 250 * spd(), easing: 'linear'});
  await wait(250);
  e.style.zIndex = '';
  e.animate([{filter: 'brightness(.25)'}, {filter: 'brightness(1)'}], {duration: 133 * spd()});
  if (code >= 3 && code <= 5 && gen === GEN) label(t, 'sp' + (code - 2), 'rise', gen);
}
// the rotating block's ray (states 30-34): the converge flash on the block,
// then the ray a tile every 16 ticks, a pop where it lands, and each opponent
// card it reaches turned (a full turn over 60 ticks, pushed a third of a tile
// out at 2x, white flash) and sent back
async function ray(e, gen, seat){
  const F = W.fx, L = LK(), [bx, by] = center(e.tile);
  const cf = spr([bx, by], 64 * L, 64 * L); cf.classList.add('add');
  ticks(130, gen, i => { cf.src = art(F.burst[(i / 4 | 0) % F.burst.length]); const s = 64 * L * (i >= 40 ? 2.5 : 1);
    box(cf, [bx - s / 2, by - s / 2, s, s]); cf.style.opacity = Math.min(1, i / 16, (130 - i) / 16); }).then(() => cf.remove());
  await wait(41 * T);
  if (gen !== GEN) return;
  const chain = [];
  if (e.to != null && e.to >= 0) chain.push(e.to);
  (e.tiles || []).forEach(t => { if (!chain.includes(t)) chain.push(t); });
  let prev = e.tile;
  for (const t of chain){
    if (gen !== GEN) return;
    bolt(prev, t, gen);
    await wait(8 * T);
    const [x, y] = center(t);
    popAt(x, y, gen);
    if ((e.tiles || []).includes(t) && CARDS[t]){
      const o = S && (S.board || {})[String(t)];
      flipCard(t, o ? o.owner : seat, {from: e.tile, away: e.tile, frac: 1 / 3, dur: 1000, go: 333, hold: 667, back: 990, flash: true});
    }
    prev = t;
    await wait(8 * T);
  }
  await wait(77 * T);
}
// scramble (states 25/26): the cards leave their tiles, circle the block
// spinning (1-4 turns over 140 ticks), and land where the redeal put them
async function orbit(bx, by, gen){
  const ks = Object.values(CARDS).filter(k => k.c.block === undefined);
  const home = ks.map(k => [parseFloat(k.el.style.left), parseFloat(k.el.style.top), parseFloat(k.el.style.width), parseFloat(k.el.style.height)]);
  const pos = home.map(h => [h[0] + h[2] / 2, h[1] + h[3] / 2]), turns = ks.map(() => (Math.random() * 4 | 0) + 1);
  const put = (k, j, x, y, s, ang) => { k.el.style.left = (x - home[j][2] / 2) + 'px'; k.el.style.top = (y - home[j][3] / 2) + 'px';
    k.el.style.transform = 'perspective(700px) scale(' + s + ') rotateY(' + ang + 'deg)'; };
  ks.forEach(k => { k.el.style.transition = 'none'; k.el.style.zIndex = 7; });
  const ok = await ticks(120, gen, i => {
    const t = i + 41;
    ks.forEach((k, j) => {
      // (i & 7) + 2 times 40 in the client's space; halved to stay on this board (inferred)
      const r = ((j & 7) + 2) * 20 * LK(), a = j * Math.PI / 6 - t * Math.PI / 60, n = Math.max(1, 70 - t / 4);
      pos[j][0] += (bx + r * Math.cos(a) - pos[j][0]) / n; pos[j][1] += (by + r * Math.sin(a) - pos[j][1]) / n;
      put(k, j, pos[j][0], pos[j][1], 1.2 + .005 * t, 360 * turns[j] * Math.min(1, i / 140));
    });
  });
  if (!ok) return;
  const want = Object.entries(S.board || {}).filter(([t, c]) => c.block === undefined), used = new Set();
  const dest = ks.map(k => { const hit = want.find(([t, c]) => c.id === k.c.id && !used.has(t)); if (hit){ used.add(hit[0]); return center(+hit[0]); } return null; });
  const from = pos.map(p => p.slice());
  await ticks(40, gen, i => {
    const f = (i + 1) / 40;
    ks.forEach((k, j) => {
      if (!dest[j]){ k.el.style.opacity = 1 - f; return; }
      put(k, j, from[j][0] + (dest[j][0] - from[j][0]) * f, from[j][1] + (dest[j][1] - from[j][1]) * f,
          2 + (1 - 2) * f, 360 * turns[j] * Math.min(1, (i + 120) / 140));
    });
  });
}
// a chance block's effect, the game's way (states 18-29)
async function effect(e, gen, st){
  const F = W.fx, seat = st ? st.seat : (S ? S.active : 0);
  if (!F || e.tile == null){ if (EFFECT[e.kind]){ pop(EFFECT[e.kind], e.tile); await wait(500); } return; }
  const [bx, by] = center(e.tile), L = LK(), hh = rect(e.tile)[3] / 2;
  const own = t => { const o = S && (S.board || {})[String(t)]; return o ? o.owner : seat; };
  if (e.kind === 'power_down' || e.kind === 'power_up'){
    const up = e.kind === 'power_up', mine = (e.tiles && e.tiles.length) ? e.tiles : cardsOf(seat);
    puffs(bx, by, 3, up ? 3 : 4, gen);
    await wait(21 * T); if (gen !== GEN) return;
    if (up) figure(F.girl, bx, by, 1.5 * L, 140, gen, {add: true});
    else {
      mine.forEach(t => { const [x, y] = center(t); figure(F.reaper, x, y, 1.5 * L, 209, gen, {add: true}); });
      figure(F.reaper, bx, by + hh - 80 * L, 2 * L, 209, gen, {rise: 110});
      smokeColumn(bx, by + hh, gen);
    }
    if (up){ await wait(80 * T); if (gen !== GEN) return; mine.forEach(t => orb(bx, by, t, 2 * L, gen)); await wait(140 * T); }
    else await wait(220 * T);
    if (gen !== GEN) return;
    // state 19 / 21: power down snaps each card to half size and back (20
    // ticks); power up flashes it; then the word, and the stat line recolours
    mine.forEach(t => {
      const k = CARDS[t];
      if (!k) return;
      if (up) k.el.animate([{filter: 'brightness(2.4)'}, {filter: 'brightness(1)'}], {duration: 533 * spd()});
      else k.el.animate([{transform: 'scale(.5)'}, {transform: 'scale(1)'}], {duration: 333 * spd(), easing: 'linear'});
      label(t, up ? 'power_up' : 'power_down', up ? 'grow' : 'shrink', gen);
    });
    await wait(48 * T);
  } else if (e.kind === 'take'){
    const to = e.tiles || [];
    puffs(bx, by, 4.5, to.length ? own(to[0]) : seat, gen);
    await wait(21 * T); if (gen !== GEN) return;
    for (let i = 0; i < 8; i++) chip(bx, by, i, gen);
    await wait(20 * T); if (gen !== GEN) return;
    for (let i = 0; i < 12; i++) coin(bx, by, null, L, gen);
    await wait(20 * T); if (gen !== GEN) return;
    to.forEach(t => coin(bx, by, t, 2 * L, gen));
    await wait(160 * T); if (gen !== GEN) return;
    to.forEach(t => { const [x, y] = center(t); puff(x, y, 1.25, own(t), 0, 0, gen); });
    await wait(20 * T); if (gen !== GEN) return;
    to.forEach(t => label(t, 'take', 'none', gen));
    await wait(16 * T); if (gen !== GEN) return;
    // state 23: the colour changes INSTANTLY, with the white flash -- no turn
    to.forEach(t => { const k = CARDS[t]; if (!k) return; k.c.owner = own(t); k.base.src = art(baseFor(k.c));
      k.el.animate([{filter: 'brightness(2.4)'}, {filter: 'brightness(1)'}], {duration: 533 * spd()}); });
    await wait(32 * T);
  } else if (e.kind === 'scramble'){
    puffs(bx, by, 3, 3, gen);
    await wait(21 * T); if (gen !== GEN) return;
    wander(bx, by + hh, gen);
    await wait(20 * T); if (gen !== GEN) return;
    await orbit(bx, by, gen);
    if (gen !== GEN) return;
    drawBoard();
    Object.keys(CARDS).forEach(t => { if (CARDS[t].c.block === undefined) label(+t, 'scramble', 'none', gen); });
    await wait(48 * T);
  } else if (e.kind === 'ray') await ray(e, gen, seat);
  else if (EFFECT[e.kind]){ pop(EFFECT[e.kind], e.tile); await wait(500); }
}
// GAME START (0xFA160): the screen darkens to half; "GAME" and "START!" slide
// in 10 px a tick and meet at the centre in 32 ticks; then for 129 ticks the
// banner fades (0.5 -> 0), grows (+0.006), turns (+0.004 rad) and drifts
// (+1, -0.5) about its pivot, with three additive copies spreading out
async function gameStart(){
  if (!W.start_halves) return banner(W.start, 1300);
  const gen = GEN, fx = $('#fx'), ov = el('div', 'gsov', fx), g = el('div', 'gs', fx);
  const mk = add => { const i = el('img', add ? 'add' : '', g); i.alt = ''; return i; };
  const layers = [0, 1, 2, 3].map(j => { const a = mk(j > 0), b = mk(j > 0); a.src = art(W.start_halves[0]); b.src = art(W.start_halves[1]); return [a, b]; });
  const OFF = [[0, 0], [1, 1], [-1, 0], [0, -1]];
  const ok = await ticks(32, gen, i => {
    ov.style.opacity = Math.min(.5, (i + 1) * 10 / 128);
    layers.forEach(([a, b], j) => { a.style.opacity = b.style.opacity = j ? 0 : 1;
      box(a, [-248 + (i + 1) * 10, 188, 248, 72]); box(b, [640 - (i + 1) * 10, 188, 248, 72]); });
  });
  let aa = 64, s = 1, ang = 0, px = 320, py = 224;
  if (ok) await ticks(129, gen, () => {
    aa -= .5; s += .006; ang += .004; px += 1; py -= .5;
    const d = (64 - aa) / 4;
    layers.forEach(([a, b], j) => {
      const [ox, oy] = OFF[j];
      [[a, -248 * s], [b, 0]].forEach(([im, lx]) => {
        const left = px + lx + ox * d, top = py - 36 * s + oy * d;
        im.style.opacity = aa / 128; box(im, [left, top, 248 * s, 72 * s]);
        im.style.transformOrigin = (px - left) + 'px ' + (py - top) + 'px'; im.style.transform = 'rotate(' + ang + 'rad)';
      });
    });
  });
  ov.remove(); g.remove();
}
// WIN / DRAW (0xCCC40): the face additive over its shadow at a third of the
// alpha; in over 32 ticks, held to 120, out over 32 -- no movement
async function resultBanner(kind){
  const gen = GEN, g = el('div', 'rb', $('#fx'));
  const layers = kind === 'win' && W.win_layers ? [[W.win_layers[1], 196, 168, 247, 87, 1 / 3, ''], [W.win_layers[0], 196, 168, 247, 79, 1, 'add']]
                                                : [[W.draw, 320 - sz(W.draw)[0] / 2, 160, sz(W.draw)[0], sz(W.draw)[1], 1, 'add']];
  const ims = layers.map(([f, x, y, w, h, k, cls]) => { const im = el('img', cls, g); im.alt = ''; im.src = art(f); box(im, [x, y, w, h]); im.style.opacity = 0; return [im, k]; });
  await ticks(185, gen, i => { const a = i < 32 ? i / 32 : i <= 152 ? 1 : Math.max(0, 1 - (i - 152) / 32); ims.forEach(([im, k]) => { im.style.opacity = a * k; }); });
  g.remove();
}
// the rotating block's layers over its frame: the glow pulsing on the orb and
// the arrow, its base on the centre (x +-15, y -30..0 of the 82x98 card),
// turned to ((phase-4)&7)*45 deg clockwise from up -- where the ray fires
function rotAngleOf(c){ const ph = c.phase != null ? c.phase : (c.block - 9); return ((ph - 4) & 7) * 45; }
function rotParts(d, c){
  const pc = (v, of) => (v / of * 100) + '%';
  const g = el('img', 'rotglow', d); g.alt = ''; g.src = art(W.rot.glow);
  g.style.left = pc(21, 82); g.style.top = pc(29, 98); g.style.width = pc(40, 82); g.style.height = pc(40, 98);
  g.style.animationDelay = -(performance.now() % 1675) + 'ms';
  const a = el('img', 'rotarrow', d); a.alt = ''; a.src = art(W.rot.arrow);
  a.style.left = pc(26, 82); a.style.top = pc(19, 98); a.style.width = pc(30, 82); a.style.height = pc(30, 98);
  a.style.transform = 'rotate(' + rotAngleOf(c) + 'deg)';
  return a;
}
// each turn +45 deg CLOCKWISE, linear, over 20 ticks (0xB3AB0) -- never back
function rotTo(k, phase){
  const target = ((phase - 4) & 7) * 45, cur = k.rotAngle || 0, delta = ((target - cur) % 360 + 360) % 360;
  if (!delta) return;
  k.rotAngle = cur + delta;
  k.rotArrow.style.transition = 'transform ' + (333 * spd() * delta / 45) + 'ms linear';
  k.rotArrow.style.transform = 'rotate(' + k.rotAngle + 'deg)';
}
// WHO GOES FIRST (states 0x1B-0x1D, 0xD4E80): the reel slides in over a dark
// overlay (16 ticks), spins -- 2 frames a tick, slowing as 2/(t>>4) -- and from
// tick 64 stops on frame 3*first; held 41 ticks; then fades and slides away
// (16). Seen from seat 0: the plates are the seats in order, and the reel's
// centre shows seat 0's place in the order. Positions are the client's, moved
// 30 px left to sit on this page's board (its board is centred 30 px right)
async function roulette(){
  const R = W.roulette;
  if (!R || !S) return;
  const gen = GEN, n = Math.min(3, Math.max(2, S.n || 2)), first = (((S.active || 0) % n) + n) % n;
  const reels = n === 3 ? R.reel3 : R.reel2, cyc = 3 * n, dx = -30, fx = $('#fx');
  const ov = el('div', 'gsov', fx), g = el('div', 'rl', fx), parts = [];
  const img = f => { const i = el('img', '', g); i.alt = ''; i.src = art(f); return i; };
  if (n === 3) parts.push([img(R.ornA), 276, 131, 95, 31]);
  parts.push([img(R.ornB), 276, 222, 95, 31], [img(R.arm), 281, 137, 207, 71], [img(R.tassel), 393, 208, 95, 216]);
  [[120, 181], [171, 258], [171, 104]].slice(0, n).forEach(([x, y], i) => {
    const p = (S.players || [])[i] || {}, d = el('div', 'rlp', g), pl = el('img', '', d);
    pl.alt = ''; pl.src = art(W.plates[SEAT_COLOR[i]] || W.plates.n); box(pl, [0, 0, 155, 20]);
    text(d, p.name || ('Player ' + (i + 1)), 77, 4, {align: 'center', size: 12, cls: 'b', max: 136});
    parts.push([d, x, y, 155, 20]);
  });
  const reel = img(reels[0]); parts.push([reel, 356, 93, 56, 200]);
  const place = (dy, a) => parts.forEach(([im, x, y, w, h]) => { box(im, [x + dx, y + dy, w, h]); im.style.opacity = a; });
  let pos = 0, stopped = false;
  let ok = await ticks(113, gen, t => {
    ov.style.opacity = t < 16 ? 6 * t / 128 : .75;
    if (!stopped){ pos += 2 / Math.max(1, t >> 4); if (t >= 64 && Math.floor(pos) % cyc === 3 * first) stopped = true; }
    reel.src = art(reels[stopped ? 3 * first : Math.floor(pos) % cyc]);
    place(t < 16 ? (16 - t) * 20 : 0, 1);
  });
  if (ok) ok = await ticks(41, gen, () => {});
  if (ok) await ticks(16, gen, t => { const a = (96 - 6 * t) / 128; ov.style.opacity = a; place(20 * t, a / .75); });
  ov.remove(); g.remove();
}
// one move, the game's way
async function play(st){
  const gen = GEN, seat = st.seat, card = Object.assign({}, st.card || {}, {owner: seat});
  const byTile = {}, handled = new Set(), scrambled = (st.effects || []).some(e => e.kind === 'scramble');
  (st.effects || []).forEach(e => {
    (byTile[e.tile] = byTile[e.tile] || []).push(e);
    // take and the ray turn their own cards; the move's change list must not turn them again
    if (e.kind === 'take' || e.kind === 'ray') (e.tiles || []).forEach(t => handled.add(t));
  });
  if (st.card){ await drop(st.tile, card, gen); if (gen !== GEN) return; }
  let beaten = null, captures = 0;
  for (const b of st.battles || []){
    if (gen !== GEN) return;
    if (CARDS[b.def] && CARDS[b.def].c.block !== undefined){
      // a block is not fought for its numbers. Every battle's
      // lunge and midpoint burst (state 9); then a CHANCE block is struck --
      // a big burst and its shards (state 17) -- while a ROTATING block goes
      // straight to its ray (state 30): no explosion (2026-09-13)
      const [ax, ay] = center(b.att), [x, y] = center(b.def);
      lunge(b.att, b.def);
      burst((ax + x) / 2, (ay + y) / 2, 3.5, gen);
      await wait(21 * T);
      if (gen !== GEN) return;
      if (blockKind(CARDS[b.def].c.block) !== 'rot'){ burst(x, y, 5, gen); shards(x, y, gen); await wait(21 * T); }
      for (const e of byTile[b.def] || []){ await effect(e, gen, st); if (gen !== GEN) return; }
      delete byTile[b.def];
      continue;
    }
    // states 6-13: both numbers up, the lunge and the burst at the midpoint;
    // at 21 the countdown may start, at 42 it does -- each number steps down
    // by 1 with probability 50/60 a tick, independently, to its roll
    const na = numAt(b.att, b.a_raw), nd = numAt(b.def, b.d_raw);
    const stopA = flicker(b.att, b.a_sel), stopD = flicker(b.def, b.d_sel);
    try {
      lunge(b.att, b.def);
      const [ax, ay] = center(b.att), [dx, dy] = center(b.def);
      burst((ax + dx) / 2, (ay + dy) / 2, 3.5, gen);
      await wait(42 * T);
      let a = b.a_raw, d = b.d_raw;
      while ((a > b.a_roll || d > b.d_roll) && gen === GEN){
        if (a > b.a_roll && Math.random() < 50 / 60) a--;
        if (d > b.d_roll && Math.random() < 50 / 60) d--;
        na.set(a); nd.set(d);
        await wait(T);
      }
      stopA(); stopD();
      if (gen !== GEN) return;
      const loser = b.win === 'att' ? b.att : b.win === 'def' ? b.def : null;
      if (loser !== null){
        const winTile = loser === b.att ? b.def : b.att, winner = CARDS[winTile] ? CARDS[winTile].c.owner : seat;
        (loser === b.att ? na : nd).set(-1);
        const turn = flipCard(loser, winner, {from: winTile, away: winTile, flash: true});
        await wait(31 * T);
        (loser === b.att ? nd : na).set(-1);
        await turn;
        if (loser === b.def){ beaten = b.def; captures++; }
      }
    } finally { stopA(); stopD(); na.el.remove(); nd.el.remove(); }
  }
  if (gen !== GEN) return;
  for (const es of Object.values(byTile)) for (const e of es){ await effect(e, gen, st); if (gen !== GEN) return; }
  if (!scrambled){
    // state 14: everything the capture chains turns in the SAME frame, flashing;
    // COMBO shows from tick 20 to 84 when the chain is 2 or more
    const combos = (st.changed || []).filter(x => x.how === 'combo' && !handled.has(x.tile));
    if (combos.length){
      const from = beaten != null ? beaten : st.tile, n = captures + combos.length;
      combos.forEach(x => flipCard(x.tile, x.to, {from, flash: true}));
      const cb = n >= 2 ? wait(20 * T).then(() => gen === GEN ? comboBanner(from, n, gen) : null) : null;
      await wait(84 * T);
      if (cb) await cb;
    }
    // state 35: flips with no fight, all at once, no flash; 30 ticks after
    const flips = (st.changed || []).filter(x => x.how === 'flip' && !handled.has(x.tile));
    if (flips.length){ flips.forEach(x => flipCard(x.tile, x.to, {from: st.tile})); await wait(41 * T); }
  }
  if (gen !== GEN) return;
  if (scrambled) drawBoard();
  for (const [t, o] of Object.entries(st.owners || {})) if (CARDS[t] && CARDS[t].c.owner !== o) setOwner(+t, o, true);
  await wait(250);
}
async function run(){
  if (BUSY || INTRO) return;
  BUSY = true;
  const gen = GEN;
  try {
    while (QUEUE.length && gen === GEN){
      // a hidden tab gets no animation -- its timers crawl, and a move still
      // mid-battle when the match went away is what stranded the numbers
      if (document.hidden){ QUEUE.length = 0; break; }
      await play(QUEUE.shift());
    }
    if (gen !== GEN || !S) return;
    syncBoard(); drawPanels();
    if (S.phase === 'over' && !SHOWN_END){
      SHOWN_END = true;
      if (!document.hidden){
        if (S.winner === -1) await resultBanner('draw');
        else if (S.winner != null && S.players[S.winner]){
          const w = S.players[S.winner];
          const tag = el('div', 'pop', $('#fx')); tag.textContent = w.name + ' wins';
          tag.style.left = '320px'; tag.style.top = '262px';
          requestAnimationFrame(() => requestAnimationFrame(() => tag.classList.add('on')));
          await resultBanner('win');
          tag.remove();
        }
      }
      if (gen !== GEN || !S) return;
      ENDED = true; drawResult();
    }
  } catch (e) {
    // never leave a half-drawn battle behind: wipe the effects, redraw the truth
    clearFx();
    if (S){ try { drawBoard(); drawPanels(); } catch (e2) {} }
  } finally { BUSY = false; }
}
addEventListener('visibilitychange', () => { if (!document.hidden && S) run(); });
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }
function resultHead(st){
  const pl = st.players || [];
  return st.winner === -1 ? 'Draw' : (st.winner != null && pl[st.winner]) ? pl[st.winner].name + ' wins' : 'Game over';
}
function scoreLine(st){ return (st.players || []).map(p => esc(p.name) + ' ' + (p.score || 0)).join(' · '); }
// the result, and who has answered the game's "Play again?" panel
function drawResult(){
  let R = $('#result');
  if (!S || S.phase !== 'over' || !ENDED){ if (R) R.remove(); return; }
  if (!R){ R = el('div', 'panelbox'); R.id = 'result'; }
  const pl = S.players || [];
  let h = '<div class="h">' + esc(resultHead(S)) + '</div><div class="score">' + scoreLine(S) + '</div>';
  if (pl.some(p => p.vote != null)){
    h += '<div class="v">' + (pl.some(p => p.vote === 0) ? 'The players are deciding whether to play again.' : 'Play again?') + '</div>';
    h += pl.map(p => '<div class="v">' + esc(p.name) + ': <b>'
                 + (p.vote === 1 ? 'Playing again' : p.vote === 2 ? 'Not playing again' : 'Deciding...') + '</b></div>').join('');
  }
  R.innerHTML = h;
}
// the card under the cursor (or tapped), large on the left like the game's own card info
let INFO_PIN = null;
// the stat line again, larger, under the card in the info panel
function statLine(parent, c){
  if (!W.glyphs) return null;
  const ln = el('div', 'ln', parent), tint = c.mod > 0 ? 'up' : c.mod < 0 ? 'down' : '';
  ln.style.width = '72px'; ln.style.height = '18px';
  statChars(c).forEach((ch, i) => {
    const im = el('img', 'st' + (tint && i !== 1 ? ' ' + tint : ''), ln);
    im.alt = ''; im.src = art(W.glyphs[ch]); box(im, [i * 18, 0, 18, 18]);
  });
  return ln;
}
function cardInfo(t, pin){
  if (pin) INFO_PIN = INFO_PIN === t ? null : t;
  const at = t != null ? t : INFO_PIN, k = at != null ? CARDS[at] : null;
  let B = $('#cinfo');
  if (!k || k.c.block !== undefined || !S){ if (B) B.remove(); return; }
  if (!B){ B = el('div', 'panelbox'); B.id = 'cinfo'; }
  const c = k.c; B.innerHTML = '';
  const pic = el('div', 'pic', B);
  const base = el('img', '', pic); base.alt = ''; base.src = art(baseFor(c)); box(base, [0, 0, 80, 96]);
  const a = el('img', '', pic); a.alt = ''; a.src = art('card_' + String(c.id).padStart(3, '0') + '.png'); box(a, [4, 0, 72, 96]);
  if (W.card_arrows) cardArrows(pic, c.arrows || 0);
  const nm = el('div', 'nm', B); nm.textContent = c.name || ('Card ' + c.id);
  statLine(B, c);
  const who = (S.players || [])[c.owner];
  if (who){ const w = el('div', 'who', B); w.textContent = who.name + "'s card"; }
}
function build(){
  stage.innerHTML = '';
  const bg = el('img'); bg.id = 'boardbg'; bg.alt = '';
  el('div').id = 'layer'; el('div').id = 'fx';
  const P = el('div'); P.id = 'panels'; P.style.cssText = 'position:absolute;inset:0;pointer-events:none';
  const I = el('div'); I.id = 'info'; I.style.cssText = 'position:absolute;inset:0;pointer-events:none';
  const back = el('a', 'nav'); back.href = '.'; back.textContent = '◀ Rankings'; back.style.left = '12px'; back.style.top = '12px';
  const all = el('a', 'nav'); all.href = 'watch'; all.textContent = 'All matches'; all.style.right = '14px'; all.style.top = '12px';
  const m = el('div'); m.id = 'msg';
}
function show(){
  $('#msg').textContent = '';
  $('#boardbg').src = art(geo().img);
  const fresh = PHASE === null;
  // "Play again?" answered yes: the finished game goes, a new one is dealt
  if (PHASE === 'over' && S.phase !== 'over'){ REMATCH = true; clearFx(); SEQ = 0; clearBoard(); }
  if (fresh){ drawBoard(); SEQ = Math.max(0, ...(S.steps || []).map(s => s.seq)); }
  else {
    for (const st of S.steps || []) if (st.seq > SEQ){ QUEUE.push(st); SEQ = st.seq; }
    if (S.steps && S.steps.length === 0 && SEQ > 0){ SEQ = 0; clearFx(); clearBoard(); drawBoard(); }   // a rematch
  }
  LAST = S;
  drawPanels();
  const started = (PHASE === 'select' || PHASE === null) && S.phase === 'play' && (S.steps || []).length === 0
                  && !(S.turn > 0);
  if (S.phase !== 'over'){ SHOWN_END = false; ENDED = false; }
  PHASE = S.phase;
  // a game begins: GAME START, then the reel that picks who goes first; the
  // first moves wait for it
  if (started && !INTRO) INTRO = (async () => { try { await gameStart(); await roulette(); } finally { INTRO = null; if (S) { drawPanels(); run(); } } })();
  if (S.phase === 'select') $('#msg').textContent = REMATCH ? 'Rematch! The players are choosing their cards...' : 'The players are choosing their cards...';
  if (S.phase === 'play') REMATCH = false;
  run();
}
async function poll(){
  let wait_s = 1;
  try {
    ID = decodeURIComponent(location.hash.slice(1));
    const r = await fetch(ID ? 'watch.json?t=' + encodeURIComponent(ID) : 'watch.json', {cache: 'no-store'});
    if (r.ok){
      const j = await r.json(); OK = true;
      if (!ID){ showList(j.tables || []); wait_s = 5; }
      else if (j.state){ LISTING = false; S = j.state; show(); }
      else { gone(); wait_s = 2; }         // a match just starting shows up soon
    } else OK = false;
  } catch (e) { OK = false; }
  setTimeout(poll, wait_s * 1000);
}
function gone(){
  clearFx();
  const ended = !!(PHASE && LAST);
  // an ended match keeps its final board and score behind the message
  if (!ended){ clearBoard(); $('#panels').innerHTML = ''; $('#info').innerHTML = ''; $('#boardbg').src = art(W.boards['16'].img); }
  const R = $('#result'); if (R) R.remove();
  const I = $('#cinfo'); if (I) I.remove();
  // nobody's turn any more
  document.querySelectorAll('#panels img').forEach(i => { if (i.src.endsWith(W.hand)) i.remove(); });
  const m = $('#msg'); m.innerHTML = '';
  const d = document.createElement('div'); d.className = 'panelbox';
  let h = ended ? 'This match has ended.' : 'This match is not being shown.';
  if (ended && LAST.phase === 'over') h += '<br><b style="color:#f6d880">' + esc(resultHead(LAST)) + '</b><br>' + scoreLine(LAST);
  d.innerHTML = h + '<br><br>';
  const a = document.createElement('a'); a.href = 'watch'; a.textContent = 'See the matches being played';
  a.style.cssText = 'color:#f6d880;pointer-events:auto'; d.appendChild(a); m.appendChild(d);
  S = null;
}
function showList(tables){
  LISTING = true; PHASE = null; S = null; LAST = null; clearFx(); clearBoard();
  { const R = $('#result'); if (R) R.remove(); const I = $('#cinfo'); if (I) I.remove(); }
  $('#panels').innerHTML = ''; $('#info').innerHTML = '';
  $('#boardbg').src = art(W.boards['16'].img);
  const m = $('#msg'); m.innerHTML = '';
  const box2 = document.createElement('div'); box2.style.cssText = 'position:absolute;left:140px;top:0;width:360px;height:448px;background:rgba(0,0,0,.45)';
  m.appendChild(box2);
  const h = document.createElement('div'); h.textContent = 'Live Matches'; h.style.cssText = 'position:absolute;left:140px;width:360px;top:44px;text-align:center;font:26px "TMTitle",Georgia,serif;color:#ffd06a;-webkit-text-stroke:1px #3a1804';
  m.appendChild(h);
  const ul = document.createElement('ul'); ul.id = 'list'; m.appendChild(ul);
  if (!tables.length){ const li = document.createElement('li'); li.textContent = 'No matches are being played right now.'; li.style.cssText = 'text-align:center;color:#d6be82;padding-top:40px'; ul.appendChild(li); }
  for (const tb of tables){
    const li = document.createElement('li'), a = document.createElement('a');
    a.href = '#' + tb.id;
    const who = document.createElement('div'); who.className = 'who'; who.textContent = tb.names.join(' vs ');
    const where = document.createElement('div'); where.className = 'where';
    where.textContent = 'Room ' + tb.room + ', Table ' + tb.table + '   ' + (tb.phase === 'select' ? 'choosing cards' : tb.phase === 'over' ? 'just finished' : 'turn ' + Math.min((tb.turn || 0) + 1, tb.limit) + ' of ' + tb.limit);
    a.append(who, where); li.appendChild(a); ul.appendChild(li);
  }
}
addEventListener('hashchange', () => { PHASE = null; SEQ = -1; clearFx(); SHOWN_END = false; ENDED = false; REMATCH = false; LAST = null; INFO_PIN = null; clearBoard(); });
addEventListener('resize', fit);
async function init(){
  fit();
  // no-cache: the art route is max-age 86400, and a board.json from before
  // the watch art was baked (a rankings visit) had no "watch" -> this message
  try { const r = await fetch('art/board.json', {cache: 'no-cache'}); if (!r.ok) throw 0; L = await r.json(); W = L.watch; if (!W) throw 0; }
  catch (e) { $('#msg').textContent = 'The watching art is not installed on this server.'; return; }
  try { await Promise.all([document.fonts.load('500 13px "TMText"', 'A1'), document.fonts.load('700 13px "TMText"', 'A1'), document.fonts.load('24px "TMTitle"', 'Aa')]); } catch (e) {}
  build(); poll();
}
init();
</script></body></html>
"""


# ---------------------------------------------------------------------------
# the page: one window on TM's own menu backdrop, laid out in the game's
# 640x448 coordinates (board.json) and scaled with one transform. Seven tabs
# on the client's wood boards; the arrows page like L1 / R1 and roll into the
# neighbouring tab at either end.
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tetra Master - Rankings &amp; Auction</title>
<style>
@font-face{font-family:"TMTitle";src:url(art/tm-title.woff2) format("woff2");font-display:block}
@font-face{font-family:"TMText";src:url(art/tm-text.woff2) format("woff2");font-weight:500;font-display:block}
@font-face{font-family:"TMText";src:url(art/tm-text-bold.woff2) format("woff2");font-weight:700;font-display:block}
html,body{margin:0;height:100%;overflow:hidden;background:#07080a}
/* TM's menu backdrop, softened behind the sheets */
#bg{position:fixed;inset:-24px;background:#0b0c0f url(art/backdrop.png) 50% 50%/cover no-repeat;filter:blur(3px) brightness(.8)}
#vig{position:fixed;inset:0;background:radial-gradient(ellipse at 50% 45%,rgba(0,0,0,0) 30%,rgba(0,0,0,.7) 100%);pointer-events:none}
#wrap{position:fixed;left:0;top:0;width:640px;height:448px;transform-origin:0 0;-webkit-user-select:none;user-select:none;filter:drop-shadow(0 6px 14px rgba(0,0,0,.8))}
#stage,#side{position:absolute;top:0;height:448px;overflow:hidden}
#stage{left:0;width:640px;background:#1d201e url(art/panel.png) 0 0/640px 448px;-webkit-mask:url(art/mask_main.png) 0 0/100% 100% no-repeat;mask:url(art/mask_main.png) 0 0/100% 100% no-repeat}
#side{width:220px;background:#1d201e url(art/side.png) 0 0/220px 448px;-webkit-mask:url(art/mask_side.png) 0 0/100% 100% no-repeat;mask:url(art/mask_side.png) 0 0/100% 100% no-repeat}
#stage img{position:absolute;display:block;pointer-events:none}
.t{position:absolute;white-space:pre;line-height:1;font-family:"TMText","M PLUS 1p",sans-serif;font-weight:500;font-variant-numeric:tabular-nums;pointer-events:none}
.b{font-weight:700}
.lab{font-family:"TMTitle",Georgia,serif;font-weight:400;text-shadow:1px 1px 0 var(--sh),0 0 3px rgba(0,0,0,.9)}
button{position:absolute;margin:0;padding:0;border:0;background:none;cursor:pointer;-webkit-tap-highlight-color:transparent}
button:focus{outline:none}
/* the tabs: s0 normal, s3 the flat highlight (the tab you are on), s1 PRESSED
   (the board 2 px lower, caption with it), only while held */
.tab{background:var(--s0) no-repeat 0 0}
.tab:hover,.tab:focus-visible{filter:brightness(1.1)}
.tab[aria-pressed=true]{background-image:var(--s3)}
.tab:active{background-image:var(--s1);filter:none}
.tab:active .t{transform:translateY(2px)}
.arrow img{position:absolute;left:0;top:0}
.arrow .hi{opacity:0}
.arrow:hover .hi,.arrow:focus-visible .hi{opacity:1}
.arrow:active{transform:translateY(1px)}
#arrow-prev{transform:scaleX(-1)}
#arrow-prev:active{transform:scaleX(-1) translateY(1px)}
.band{font-family:"TMText",sans-serif;font-weight:500;font-size:13px;line-height:19px;height:19px;color:var(--c);white-space:pre;cursor:pointer}
.band[disabled]{opacity:.4;cursor:default}
.band[aria-pressed=true]{color:var(--on);font-weight:700}
.thumb{position:absolute;overflow:visible;pointer-events:none}
#side .t{position:static}
#act{list-style:none;margin:0;padding:0 14px;position:absolute;top:54px;left:0;right:0;font-family:"TMText",sans-serif;color:#eee4c8}
#act li{padding:5px 0 6px;border-bottom:1px solid rgba(214,190,130,.22)}
#act .r1,#act .r2{display:flex;justify-content:space-between;align-items:baseline;white-space:nowrap;gap:8px}
#act .nm{font-weight:700;font-size:14px;overflow:hidden;text-overflow:ellipsis}
#act .amt{font-size:13px;color:#f6d880;font-variant-numeric:tabular-nums}
#act .r2{font-size:12px;color:#b8ac92}
#act .none{border:0;color:#b8ac92;font-size:13px;text-align:center;padding-top:24px}
#side-foot{position:absolute;left:14px;right:14px;bottom:16px;text-align:center;font-family:"TMText",sans-serif;color:#d6be82;font-size:13px;line-height:18px}
#side-foot b{font-weight:700;color:#f6d880}
#msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font:16px system-ui,sans-serif;color:#eee4c8}
/* the way into watching: only while a match is being played */
.livebtn{position:absolute;left:14px;top:21px;padding:3px 9px 4px;border-radius:10px;background:rgba(0,0,0,.45);border:1px solid rgba(246,216,128,.5);font:13px "TMTitle",Georgia,serif;color:#f6d880;text-decoration:none;white-space:nowrap;z-index:6}
.livebtn:hover,.livebtn:focus-visible{background:rgba(246,216,128,.2);outline:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
</style></head><body>
<div id="bg"></div><div id="vig"></div>
<div id="wrap" aria-hidden="true"><div id="stage"><div id="msg">Loading...</div></div><aside id="side" hidden></aside></div>
<table class="sr" id="table"><caption id="cap"></caption><thead><tr id="thead"></tr></thead><tbody id="rows"></tbody></table>
<script>
"use strict";
let L = null, S = null, TAB = 0, PAGE = 0, BAND = 0, SIG = '', OK = false;
const AUC = 6, NTABS = 7, $ = s => document.querySelector(s);
const stage = $('#stage'), wrap = $('#wrap'), side = $('#side');
const E = {};
const PAD = 14;
function rgb(c){ return 'rgb(' + c.slice(0, 3).join(',') + ')'; }
function el(tag, cls, parent){ const e = document.createElement(tag); if (cls) e.className = cls; (parent || stage).appendChild(e); return e; }
function box(e, r){ e.style.left = r[0] + 'px'; e.style.top = r[1] + 'px'; e.style.width = r[2] + 'px'; e.style.height = r[3] + 'px'; }
function art(n){ return 'art/' + n; }
// one line of text at x with the column's alignment, centred in the band y..y+h
function put(parent, text, x, align, y, h, o){
  o = o || {};
  const e = el('span', 't' + (o.bold ? ' b' : '') + (o.label ? ' lab' : '') + (o.cls ? ' ' + o.cls : ''), parent);
  e.textContent = text;
  let size = o.size || L.rows.size;
  e.style.fontSize = size + 'px';
  e.style.color = rgb(o.fill || L.rows.fill);
  if (o.label) e.style.setProperty('--sh', rgb(L.label.shadow));
  let w = e.offsetWidth;
  if (o.fit && w > o.fit){ size = size * o.fit / w; e.style.fontSize = size + 'px'; w = e.offsetWidth; }
  e.style.left = (align === 'right' ? x - w : align === 'center' ? x - w / 2 : x) + 'px';
  e.style.top = (y + (h - e.offsetHeight) / 2) + 'px';
  return e;
}
function label(parent, text, col, o){
  const H = L.header.rect;
  return put(parent, text, col[0], col[1], H[1], H[3], Object.assign({label: true, size: L.label.size, fill: L.label.fill}, o || {}));
}
const NS = 'http://www.w3.org/2000/svg';
function build(){
  const Y = L.layers;
  stage.innerHTML = '';
  const img = (src, r, parent) => { const i = el('img', '', parent); i.alt = ''; i.src = art(src); box(i, r); return i; };
  img(Y.header, L.header.rect);
  img(Y.sep, L.sep.rect);
  // the title: real text, TM's orange-to-gold with a dark edge, as its headings
  const T = L.title, svg = document.createElementNS(NS, 'svg');
  svg.id = 'title'; svg.setAttribute('viewBox', '0 0 640 448'); svg.setAttribute('width', '640'); svg.setAttribute('height', '448');
  svg.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none;overflow:visible';
  svg.innerHTML = '<defs><linearGradient id="tg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + rgb(T.top) + '"/><stop offset="1" stop-color="' + rgb(T.bottom) + '"/></linearGradient></defs>';
  stage.appendChild(svg);
  const line = (fill, stroke, dx, dy) => {
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', T.x + dx); t.setAttribute('y', T.baseline + dy); t.setAttribute('text-anchor', 'middle');
    t.setAttribute('font-family', '"TMTitle",Georgia,serif');
    t.setAttribute('fill', fill); t.setAttribute('stroke', stroke); t.setAttribute('stroke-width', 2 * T.stroke_px);
    t.setAttribute('stroke-linejoin', 'round'); t.setAttribute('paint-order', 'stroke'); t.setAttribute('letter-spacing', T.spacing || 0);
    svg.appendChild(t); return t;
  };
  E.titleShadow = line(rgb(T.shadow), rgb(T.shadow), T.shadow_dx, T.shadow_dy);
  E.titleShadow.setAttribute('opacity', T.shadow[3]);
  E.title = line('url(#tg)', rgb(T.stroke), 0, 0);
  for (const [k, d, lab] of [['prev', -1, 'Previous (L1)'], ['next', 1, 'Next (R1)']]){
    const b = el('button', 'arrow'); b.id = 'arrow-' + k; box(b, L.arrows[k]);
    b.title = lab; b.setAttribute('aria-label', lab);
    img(Y.arrow, [0, 0, L.arrows[k][2], L.arrows[k][3]], b);
    img(Y.arrow_hi, [0, 0, L.arrows[k][2], L.arrows[k][3]], b).className = 'hi';
    b.onclick = () => step(d);
  }
  E.live = el('a', 'livebtn'); E.live.id = 'live'; E.live.hidden = true;
  E.dyn = el('div'); E.dyn.id = 'dyn';
  E.info = el('div'); E.info.id = 'info';
  E.status = el('span', 't'); E.status.id = 'status';
  const Tb = L.tabs;
  E.tabs = L.tabs_caption.map((cap, i) => {
    const b = el('button', 'tab'); box(b, [Tb.x0 + i * (Tb.w + Tb.gap), Tb.y, Tb.w, Tb.h]);
    b.dataset.tab = i; b.setAttribute('aria-label', cap);
    for (const s of ['s0', 's1', 's3']) b.style.setProperty('--' + s, 'url(' + art(Y.tab[s]) + ')');
    b.onclick = () => setTab(i);
    return b;
  });
  // one caption size for all seven: the size the longest one needed
  const caps = E.tabs.map((b, i) => put(b, L.tabs_caption[i], Tb.w / 2, 'center', 1, Tb.h - 4,
                                        {label: true, size: Tb.size, fill: Tb.fill[0], fit: Tb.w - 2 * (Tb.pad || 5)}));
  const fs = Math.min(...caps.map(c => parseFloat(c.style.fontSize)));
  E.caps = E.tabs.map((b, i) => { caps[i].remove(); return put(b, L.tabs_caption[i], Tb.w / 2, 'center', 1, Tb.h - 4, {label: true, size: fs, fill: Tb.fill[0]}); });
  for (const s of ['s1', 's3']){ const im = new Image(); im.src = art(Y.tab[s]); }
}
function fit(){
  const W = innerWidth, H = innerHeight, SW = L ? L.side.w : 220, GAP = L ? L.side.gap : 14;
  const k1 = Math.min((W - 2 * PAD) / 640, (H - 2 * PAD) / 448);
  const k2 = Math.min((W - 2 * PAD) / (640 + GAP + SW), (H - 2 * PAD) / 448);
  const withSide = !!L && W >= 700 && k2 >= k1 * 0.8;
  const k = withSide ? k2 : k1, w = withSide ? 640 + GAP + SW : 640;
  const was = side.hidden;
  side.hidden = !withSide;
  // a sheet that was hidden has no layout, so its list was never fitted:
  // fit it now that it shows (a window widened on a phone-shaped start)
  if (was && withSide) paintSide();
  side.style.left = (640 + GAP) + 'px';
  wrap.style.width = w + 'px';
  wrap.style.transform = 'scale(' + k + ')';
  wrap.style.left = ((W - w * k) / 2) + 'px';
  wrap.style.top = ((H - 448 * k) / 2) + 'px';
}
function auctionRows(){
  const a = S.auction;
  if (BAND >= a.bands.length) return a.sold;
  const b = a.bands[BAND];
  return a.listings.filter(r => (b.lo === null || r.price >= b.lo) && (b.hi === null || r.price <= b.hi));
}
function perPage(){ return TAB === AUC ? L.auction_rows.count : L.rows.count; }
function count(){ return TAB === AUC ? auctionRows().length : S.tabs[TAB].rows.length; }
function pages(){ return S ? Math.max(1, Math.ceil(count() / perPage())) : 1; }
function setTab(i){ TAB = i; PAGE = 0; show(); }
// L1 / R1: the game's paging; past either end, the neighbouring tab
function step(d){
  const p = PAGE + d;
  if (S && p >= 0 && p < pages()) PAGE = p;
  else { TAB = (TAB + d + NTABS) % NTABS; PAGE = 0; if (d < 0) PAGE = pages() - 1; }
  show();
}
function two(n){ return (n < 10 ? '0' : '') + n; }
function dateOf(t){ const d = new Date(t * 1000); return two(d.getMonth() + 1) + '/' + two(d.getDate()) + '/' + d.getFullYear(); }
function whenOf(t){
  const d = new Date(t * 1000);
  let tz = '';
  try { tz = ' ' + new Intl.DateTimeFormat(undefined, {timeZoneName: 'short'}).formatToParts(d).find(p => p.type === 'timeZoneName').value; } catch (e) {}
  return dateOf(t) + ' ' + two(d.getHours()) + ':' + two(d.getMinutes()) + tz;
}
function ago(t){
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? 'just now' : s < 3600 ? Math.round(s / 60) + 'm ago'
       : s < 86400 ? Math.round(s / 3600) + 'h ago' : Math.round(s / 86400) + 'd ago';
}
function statusText(){
  if (!OK) return 'Reconnecting...';
  const t = new Date(), when = two(t.getHours()) + ':' + two(t.getMinutes()) + ':' + two(t.getSeconds());
  const n = count();
  const who = TAB === AUC ? (BAND >= S.auction.bands.length ? n + (n === 1 ? ' recent sale' : ' recent sales')
                                                             : n + (n === 1 ? ' card' : ' cards') + ' up for auction')
            : TAB === 5 ? n + (n === 1 ? ' player' : ' players') + ' this week'
            : n ? n + (n === 1 ? ' player' : ' players') + ' ranked' : 'No one ranked';
  return who + '   Page ' + (PAGE + 1) + '/' + pages() + '   Live ' + when;
}
function paintStatus(){
  if (!L || !E.status) return;
  const R = L.status.rect;
  E.status.remove();
  E.status = put(stage, statusText(), R[0] + R[2] / 2, 'center', R[1], R[3], {size: L.status.size, fill: L.status.fill});
  E.status.id = 'status';
}
// "Watch live": the matches being played (tetramaster's watch file, only the
// tables their creators let be seen); one match links straight to it
function paintLive(){
  if (!S || !E.live) return;
  const live = S.live || [];
  E.live.hidden = !live.length;
  if (!live.length) return;
  E.live.textContent = 'Watch live (' + live.length + ')';
  E.live.href = live.length === 1 ? 'watch#' + live[0].id : 'watch';
}
function paintTitle(){
  const T = L.title, text = L.titles[TAB];
  const set = s => { for (const t of [E.titleShadow, E.title]){ t.textContent = text; t.setAttribute('font-size', s); } };
  set(T.size);
  const w = E.title.getBBox().width, sp = (T.spacing || 0) * (text.length - 1);
  if (w > T.max_width) set(T.size * (T.max_width - sp) / (w - sp));
}
// the card's eight arrows, bit i = direction i clockwise from north (tmbattle)
const DIRS = [[.5, 0], [1, 0], [1, .5], [1, 1], [.5, 1], [0, 1], [0, .5], [0, 0]];
function thumb(parent, c, x, y, w, h){
  const i = el('img', '', parent); i.alt = ''; i.src = art(c.art); box(i, [x, y, w, h]);
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'thumb'); svg.setAttribute('width', w + 8); svg.setAttribute('height', h + 8);
  svg.style.left = (x - 4) + 'px'; svg.style.top = (y - 4) + 'px';
  let p = '';
  DIRS.forEach(([fx, fy], k) => {
    if (!(c.arrows >> k & 1)) return;
    const cx = 4 + fx * w, cy = 4 + fy * h, dx = fx * 2 - 1, dy = fy * 2 - 1, n = Math.hypot(dx, dy) || 1;
    const ux = dx / n, uy = dy / n, s = 5;
    p += '<polygon points="' + [cx + ux * s, cy + uy * s, cx - uy * s * .8, cy + ux * s * .8, cx + uy * s * .8, cy - ux * s * .8].map(v => v.toFixed(1)).join(',') + '"/>';
  });
  svg.innerHTML = '<g fill="#f2c24a" stroke="#3a1e04" stroke-width="1">' + p + '</g>';
  parent.appendChild(svg);
}
function showRank(t){
  const C = L.columns.rank, st = L.strings, R = L.rows, D = E.dyn;
  label(D, st.rank, C.rank, {cls: 'h-rank'}); label(D, st.last_week, C.last, {cls: 'h-last'});
  label(D, st.player, C.name, {cls: 'h-name'});
  label(D, t.money ? st.prize_money : st.vs_rating, C.value, {cls: 'h-value'});
  const rows = t.rows.slice(PAGE * R.count, PAGE * R.count + R.count);
  rows.forEach((r, i) => {
    const y = R.y + R.pitch * i, row = el('div', 'row', D);
    row.dataset.i = i;
    // the gold 1st / 2nd / 3rd, but not for a zero (boardtm.medal)
    if (r.rank <= 3 && r.value > 0){
      // right-aligned on the rank column by its right edge, so it lines up
      // before the image has even loaded (the sprites are all 22 px tall)
      const im = el('img', 'medal', row), mh = R.medal_h || 22; im.alt = ''; im.src = art(L.ranks[r.rank - 1]);
      im.style.left = 'auto'; im.style.right = (640 - C.rank[0]) + 'px';
      im.style.height = mh + 'px'; im.style.width = 'auto';
      im.style.top = (y + (R.pitch - mh) / 2) + 'px';
    } else put(row, String(r.rank), C.rank[0], C.rank[1], y, R.pitch, {cls: 'c-rank'});
    const col = {up: [127, 212, 106], down: [224, 96, 74]}[r.move] || R.dim;
    const lw = r.move === 'new' ? 'New' : r.move === 'same' ? String(r.last) : (r.move === 'up' ? '▲ ' : '▼ ') + r.last;
    put(row, lw, C.last[0], C.last[1], y, R.pitch, {size: 13, fill: col, cls: 'c-last'});
    put(row, r.name, C.name[0], C.name[1], y, R.pitch, {bold: true, cls: 'c-name'});
    put(row, r.text, C.value[0], C.value[1], y, R.pitch, {cls: 'c-value'});
  });
  if (!rows.length) put(D, st.no_info, 320, 'center', R.y, R.pitch * 3, {fill: R.dim});
  const w = S.week;
  const info = w && t.published && w.next ? 'Tally Period: ' + dateOf(w.from) + ' - ' + dateOf(w.to) + '   Next update: ' + whenOf(w.next) : st.no_info;
  const I = L.info.rect;
  put(E.info, info, I[0] + I[2] / 2, 'center', I[1], I[3], {size: L.info.size, fill: L.info.fill});
}
function showLive(t){
  const C = L.columns.live, st = L.strings, R = L.rows, D = E.dyn;
  label(D, st.rank, C.rank, {cls: 'h-rank'}); label(D, st.player, C.name, {cls: 'h-name'});
  label(D, st.games, C.games, {cls: 'h-games'}); label(D, st.vs_rating, C.rating, {cls: 'h-rating'});
  label(D, st.prize_money, C.prize, {cls: 'h-prize'});
  const rows = t.rows.slice(PAGE * R.count, PAGE * R.count + R.count);
  rows.forEach((r, i) => {
    const y = R.y + R.pitch * i, row = el('div', 'row', D);
    put(row, String(r.rank), C.rank[0], C.rank[1], y, R.pitch, {cls: 'c-rank'});
    put(row, r.name, C.name[0], C.name[1], y, R.pitch, {bold: true, cls: 'c-name'});
    put(row, String(r.games), C.games[0], C.games[1], y, R.pitch, {cls: 'c-games'});
    put(row, r.rating_text, C.rating[0], C.rating[1], y, R.pitch, {cls: 'c-rating'});
    put(row, r.prize_text, C.prize[0], C.prize[1], y, R.pitch, {cls: 'c-prize'});
  });
  if (!rows.length) put(D, 'No games yet this week.', 320, 'center', R.y, R.pitch * 3, {fill: R.dim});
  const w = S.week, I = L.info.rect;
  put(E.info, st.live_note + (w && w.next ? '   Next update: ' + whenOf(w.next) : ''), I[0] + I[2] / 2, 'center', I[1], I[3], {size: L.info.size, fill: L.info.fill});
}
function showAuction(){
  const C = L.columns.auction, st = L.strings, A = L.auction_rows, R = L.rows, D = E.dyn;
  const sold = BAND >= S.auction.bands.length;
  label(D, st.cards, C.card, {cls: 'h-card'}); label(D, st.seller, C.seller, {cls: 'h-seller'});
  label(D, sold ? st.sold_for : st.high_bid, C.price, {cls: 'h-price'});
  label(D, sold ? st.winner : st.bidder, C.who, {cls: 'h-who'});
  label(D, sold ? st.ended : st.time_left, C.left, {cls: 'h-left'});
  const rows = auctionRows().slice(PAGE * A.count, PAGE * A.count + A.count);
  rows.forEach((r, i) => {
    const y = A.y + A.pitch * i, row = el('div', 'row', D), c = r.card;
    if (c.art) thumb(row, c, C.card[0], y + 2, A.thumb[0], A.thumb[1]);
    put(row, c.name, C.name[0], 'left', y + 4, 22, {bold: true, cls: 'c-name'});
    put(row, c.type + '  ' + c.atk + ' / ' + c.pdef + ' / ' + c.mdef, C.name[0], 'left', y + 26, 20, {size: 12, fill: L.label.fill, cls: 'c-stats'});
    put(row, r.seller, C.seller[0], C.seller[1], y, A.pitch, {cls: 'c-seller'});
    if (sold || r.bids) put(row, r.price_text + 'T', C.price[0], C.price[1], y, A.pitch, {cls: 'c-price'});
    else {
      // no bid yet: the client's "Currently" is empty; show what opens it
      put(row, r.price_text + 'T', C.price[0], C.price[1], y + 4, 22, {fill: R.dim, cls: 'c-price'});
      put(row, st.opening, C.price[0], C.price[1], y + 26, 20, {size: 11, fill: R.dim, cls: 'c-open'});
    }
    put(row, sold ? r.winner : (r.bidder || '-'), C.who[0], C.who[1], y, A.pitch, {cls: 'c-who'});
    put(row, sold ? ago(r.ends) : r.left_text, C.left[0], C.left[1], y, A.pitch, {cls: 'c-left'});
  });
  if (!rows.length) put(D, sold ? st.no_sales : st.no_cards, 320, 'center', A.y, A.pitch * 2, {fill: R.dim});
  // the Price List's bands, greyed at zero as the game greys them, and the sales
  const I = L.info.rect, names = st.bands.concat([st.sold]);
  const counts = S.auction.bands.map(b => b.count).concat([S.auction.sold.length]);
  const bs = names.map((n, i) => {
    const b = el('button', 'band', E.info);
    b.textContent = n.replace(' Cards', '') + ' ' + counts[i];
    b.dataset.band = i;
    b.style.setProperty('--c', rgb(L.info.fill)); b.style.setProperty('--on', rgb(L.tabs.fill[1]));
    b.setAttribute('aria-pressed', String(i === BAND));
    if (!counts[i] && i && i !== BAND) b.disabled = true;
    b.onclick = () => { BAND = i; PAGE = 0; show(); };
    return b;
  });
  const gap = 14, total = bs.reduce((s, b) => s + b.offsetWidth, 0) + gap * (bs.length - 1);
  let x = I[0] + (I[2] - total) / 2;
  for (const b of bs){ b.style.left = x + 'px'; b.style.top = I[1] + 'px'; x += b.offsetWidth + gap; }
}
function buildSide(){
  side.innerHTML = '';
  const T = L.title, svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 220 48'); svg.setAttribute('width', '220'); svg.setAttribute('height', '48');
  svg.style.cssText = 'position:absolute;left:0;top:4px;overflow:visible';
  svg.innerHTML = '<defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + rgb(T.top) + '"/><stop offset="1" stop-color="' + rgb(T.bottom) + '"/></linearGradient></defs>';
  for (const [dx, dy, fill, stroke, op] of [[T.shadow_dx, T.shadow_dy, rgb(T.shadow), rgb(T.shadow), T.shadow[3]], [0, 0, 'url(#sg)', rgb(T.stroke), 1]]){
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', 110 + dx); t.setAttribute('y', 34 + dy); t.setAttribute('text-anchor', 'middle');
    t.setAttribute('font-family', '"TMTitle",Georgia,serif'); t.setAttribute('font-size', 22);
    t.setAttribute('fill', fill); t.setAttribute('stroke', stroke); t.setAttribute('stroke-width', 2 * T.stroke_px * .8);
    t.setAttribute('stroke-linejoin', 'round'); t.setAttribute('paint-order', 'stroke'); t.setAttribute('opacity', op);
    t.textContent = 'Auction Activity';
    svg.appendChild(t);
  }
  side.appendChild(svg);
  E.act = document.createElement('ol'); E.act.id = 'act'; side.appendChild(E.act);
  E.sideFoot = document.createElement('div'); E.sideFoot.id = 'side-foot'; side.appendChild(E.sideFoot);
}
function paintSide(){
  // hidden = no layout: every offset reads 0 and the fit below would cut
  // the list to one line; fit() paints it when it shows
  if (!S || !E.act || side.hidden) return;
  const span = (cls, text) => { const e = document.createElement('span'); e.className = cls; e.textContent = text; return e; };
  E.act.innerHTML = '';
  const list = S.auction.activity || [];
  if (!list.length){ const li = document.createElement('li'); li.className = 'none'; li.textContent = 'No auction activity yet.'; E.act.appendChild(li); }
  const VERB = {bid: ' bid', listed: ' listed it', sold: ' won it'};
  for (const a of list){
    const li = document.createElement('li'), r1 = document.createElement('div'), r2 = document.createElement('div');
    r1.className = 'r1'; r2.className = 'r2';
    r1.append(span('nm', a.card), span('amt', a.amount.toLocaleString('en-US') + 'T'));
    r2.append(span('who', (a.who || '(no name)') + VERB[a.kind]), span('ago', ago(a.t)));
    li.append(r1, r2); E.act.appendChild(li);
  }
  E.sideFoot.innerHTML = '';
  const ml = S.matches_live, l1 = document.createElement('div'), l2 = document.createElement('div'), bold = document.createElement('b');
  bold.textContent = ml == null ? '' : ml ? ml + (ml === 1 ? ' match' : ' matches') + ' in progress' : 'No matches in progress';
  l1.appendChild(bold);
  l2.textContent = S.players + (S.players === 1 ? ' player' : ' players') + ' on file';
  E.sideFoot.append(l1, l2);
  const limit = E.sideFoot.offsetTop - 6;
  while (E.act.children.length > 1){
    const last = E.act.lastElementChild;
    if (E.act.offsetTop + last.offsetTop + last.offsetHeight <= limit) break;
    last.remove();
  }
}
function show(){
  if (!S || !L) return;
  try { history.replaceState(null, '', '#' + TAB); } catch (e) {}
  PAGE = Math.max(0, Math.min(PAGE, pages() - 1));
  paintTitle();
  E.dyn.innerHTML = ''; E.info.innerHTML = '';
  if (TAB === AUC) showAuction();
  else if (TAB === 5) showLive(S.tabs[5]);
  else showRank(S.tabs[TAB]);
  E.tabs.forEach((b, i) => b.setAttribute('aria-pressed', String(i === TAB)));
  E.caps.forEach((c, i) => c.style.color = rgb(L.tabs.fill[i === TAB ? 1 : 0]));
  paintStatus(); paintSide(); paintLive(); mirror();
}
// the hidden table: the same page for screen readers
function mirror(){
  $('#cap').textContent = L.titles[TAB] + ', page ' + (PAGE + 1) + ' of ' + pages();
  const head = TAB === AUC ? ['Card', 'Seller', 'Price', 'Bidder', 'Time Left'] : TAB === 5 ? ['Rank', 'Player', 'Games', 'VS. Rating', 'Prize Money']
             : ['Rank', 'Last Week', 'Player', S.tabs[TAB].money ? 'Prize Money' : 'VS. Rating'];
  $('#thead').innerHTML = ''; for (const h of head){ const th = document.createElement('th'); th.textContent = h; $('#thead').appendChild(th); }
  const tb = $('#rows'); tb.innerHTML = '';
  const per = perPage(), src = TAB === AUC ? auctionRows() : S.tabs[TAB].rows;
  for (const r of src.slice(PAGE * per, PAGE * per + per)){
    const v = TAB === AUC ? [r.card.name, r.seller, r.price_text, r.bidder || r.winner || '-', r.left_text]
            : TAB === 5 ? [r.rank, r.name, r.games, r.rating_text, r.prize_text] : [r.rank, r.last || 'New', r.name, r.text];
    const tr = document.createElement('tr');
    for (const x of v){ const td = document.createElement('td'); td.textContent = x; tr.appendChild(td); }
    tb.appendChild(tr);
  }
}
document.addEventListener('keydown', e => {
  if (e.key >= '1' && e.key <= '7') setTab(+e.key - 1);
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp' || e.key === '[') step(-1);
  else if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ']') step(1);
});
let tx = null;
addEventListener('touchstart', e => { tx = e.touches.length === 1 ? e.touches[0].clientX : null; }, {passive: true});
addEventListener('touchend', e => {
  if (tx === null) return;
  const dx = e.changedTouches[0].clientX - tx; tx = null;
  if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
}, {passive: true});
addEventListener('resize', fit);
async function poll(){
  try {
    const r = await fetch('state.json', {cache: 'no-store'});
    if (r.ok){
      const fresh = await r.json(), first = !S; S = fresh; OK = true;
      if (first){ const m = /^#([0-6])$/.exec(location.hash); if (m) TAB = +m[1]; show(); }
      else if (S.sig + S.auction_sig !== SIG) show();
      SIG = S.sig + S.auction_sig;
    } else OK = false;
  } catch (e) { OK = false; }
  paintStatus(); paintSide(); paintLive();
  setTimeout(poll, ((S && S.poll_s) || 5) * 1000);
}
async function init(){
  fit();
  try {
    const r = await fetch('art/board.json', {cache: 'no-cache'});   // re-baked art must show up
    if (!r.ok) throw new Error(r.status);
    L = await r.json();
  } catch (e) { $('#msg').textContent = 'The board art is not installed on this server.'; return; }
  try { await Promise.all([document.fonts.load('500 15px "TMText"', 'Aa1'), document.fonts.load('700 15px "TMText"', 'Aa1'),
                           document.fonts.load('30px "TMTitle"', 'VS. Rating')]); } catch (e) {}
  build(); buildSide(); fit(); poll();
}
init();
</script></body></html>
"""
