"""What the web board reads: the live-match marker (deploy gate) and the matches in progress
(the watch document, in Valkey under tmstore.watch_key()).
"""
import json
import os
import threading
import tmbattle
import tmstore
import time
from . import (
    boardrules, common, matchmaking, matchstart, placement, pushqueue, rematch, shopdoors,
    tablesettings, trade, turns, vscom,
)


def _live_matches_write():
    """`<POL_DATA_DIR>/tm-matches-live.json` -- the DEPLOY GATE's input.

    Deploy restarts wiped four live games' state in one afternoon
    (2026-08-22: a wedged board, two eaten stakes, a hung rematch vote).
    `pol-git-sync` / `pol-stale-check --restart` now DEFER recreating
    login/authsess while this marker says matches are live and fresh; the
    stale-check backstop retries every 3 minutes, so the deferred restart
    lands as soon as the last match ends. Touched by every match-flow
    handler -- INCLUDING `@CardSelect=`, so the pre-deal card-select phase is
    inside the window and not the blind spot it was until 2026-08-25 -- so the
    stamp stays fresh through the post-game screens; a
    wedged match stops touching it and the checkers' grace window expires.
    POL_TM_LIVE_MARKER=0 disables the writes (the checkers then never
    defer).
    """
    _watch_touch()                   # the web watch file rides the same beats
    if not common._env_int("POL_TM_LIVE_MARKER", 1):
        return
    try:
        path = os.path.join(os.environ.get("POL_DATA_DIR", "/data"),
                            "tm-matches-live.json")
        # WARNING: THE WINDOW IS THE WHOLE MATCH, NOT JUST THE DEALT PART. This
        # counted `_MATCH_TURN`, which is not created until THE DEAL
        # (`@CardSelect=` from the last player) -- so a match sitting in CARD
        # SELECT reported 0 and both checkers restarted straight through it.
        # Measured 2026-08-25T03:01:05Z: pol-git-sync recreated login/authsess
        # for efbd5e6c->1658e3a9 while a 2-player match was picking cards; 8s
        # later the fresh process took member 16's `@CardSelect=` with no
        # roster and dealt a ONE-SIDED game, and the table's observer stayed
        # parked on "players are selecting their cards" for the rest of the
        # night. `_MATCH_STARTED` is the right window -- it opens on the host's
        # `@GameReady=` (Start Game) and closes on the room-return `@GameExit=`,
        # so it spans card select, the deal, play and the post-game screens.
        # `_CARD_READY` is belt-and-braces for a COM game, which never takes a
        # `_MATCH_STARTED` entry -- and `_COM_AT` closes the rest of that hole.
        # A COM game fills NONE of the three stores above until its first
        # `@CardSelect=`, so everything before that -- character select, the
        # rules dialog, the wager -- was still a blind spot after the
        # 2026-08-25 widening (measured on the wire: `@GameENC=` 02:43:49Z,
        # the `@ComGame=` roster 02:44:51Z -- over a minute of it). `_COM_AT`
        # is the whole window by construction: bound at `@GameENC=`, released
        # on the room-return `@GameExit=`. It also keeps a finished COM game
        # counted through the post-game screens, where the other three are
        # cleared for the rematch.
        # WARNING: A key present in two stores under different index TYPES would count
        # twice. That is deliberate: over-counting only DEFERS a restart, which
        # is the safe direction, and the stamp's own staleness bounds it.
        keys = set(k for k, v in matchmaking._MATCH_STARTED.items() if v)
        keys |= set(boardrules._MATCH_TURN)
        keys |= set(matchstart._CARD_READY)
        keys |= set(vscom._COM_AT)
        # THE ACCEPT WINDOW (Start Game -> deal). Measured 2026-09-03T22:57Z:
        # a restart between two `@GameOK=` wiped the quorum and the match never
        # dealt. `_MATCH_STARTED` covers it once written from `@GameReady=`;
        # a pending accept set is counted too, for a roster that reached
        # `@GameOK=` on a table this process never saw start.
        keys |= {("accept",) + k for k, v in matchmaking._MATCH_ACCEPTS.items() if v}
        # WARNING: TRADES AND ARMED CHECKOUTS ARE LIVE SESSIONS TOO (09-02 review
        # H5). A recreate mid-trade used to sail through on "0 live matches",
        # wipe `_TRADE_PENDING`, and park both clients on the trade board
        # polling (0xA2,28) for a completion that no process remembers --
        # regression #25's shape, one subsystem over. Key sets are disjointly
        # prefixed because over-counting only DEFERS a restart (see above),
        # but under-counting eats somebody's evening.
        keys |= {("trade",) + (k if isinstance(k, tuple) else (k,))
                 for k in trade._TRADE_PENDING}
        keys |= {("checkout",) + (k if isinstance(k, tuple) else (k,))
                 for k in shopdoors._CHECKOUT_PULL}
        keys |= {("eventshop",) + (k if isinstance(k, tuple) else (k,))
                 for k in shopdoors._EVENTSHOP_PENDING}
        blob = json.dumps({"count": len(keys),
                           "stamp": time.time()})
        # Per-writer tmp name: every handler thread used to share `path +
        # ".tmp"`, so two concurrent writers could interleave into one file
        # and os.replace() could publish corrupt JSON -- which the external
        # checkers may read as "0 live" (the unsafe direction).
        tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w") as f:
            f.write(blob)
        os.replace(tmp, path)
    except Exception as e:
        # Best-effort by design -- but a PERMANENTLY failing write (perms,
        # disk full) used to disable the deploy gate with no symptom at all.
        # Say so once per process.
        global _LIVE_MARKER_WHINED
        if not _LIVE_MARKER_WHINED:
            _LIVE_MARKER_WHINED = True
            common._say("tm: WARNING: could not write the live-match marker (%r) -- the "
                 "deploy gate is BLIND until this is fixed (said once)" % (e,))


# ---------------------------------------------------------------------------
# THE WEB WATCH DOCUMENT -- Valkey `tm:tables-live` (2026-09-13; it was the file
# `<POL_DATA_DIR>/tm-tables-live.json` until the move to polcore.kv)
#
# The board service (services/boardtm.py: its own container) draws live
# matches at tm.example.com/watch, and it cannot see this process, so this
# process publishes them -- as the server owner chose on 2026-09-13:
#
#   * only tables whose creator allows observing: `@Tab=/in=` 0 "Possible"
#     (the client's own default) or 2 "No Comments Allowed". 1 "Impossible"
#     and any PASSWORD table never (the JongHoLow precedent). A table that may
#     not be shown is listed {"watchable": false} with NO state, so the board
#     drops it at once;
#   * HANDS HIDDEN -- counts only, as the game's own observer (@WatchInfo /H=);
#     a card's face is public once it is on the board;
#   * LIVE (no delay), and VS. COM games included.
#
# Shape: {"stamp": epoch, "tables": {"<room>-<table>": {"watchable": bool,
# "state": {...}}}}, written on change (at most once a second, and at once
# after a move) and at least every WATCH_EVERY_S, so a stale stamp means this
# process is gone; the key expires WATCH_TTL_S after the last write as well. The state carries the MOVES of the game -- each
# placement's battles (the rolls the players were shown), the tiles that
# changed hands (after a won battle, the game's COMBO) and a chance block's
# effect -- recorded where every placement path ends (`_note_combo`), so the
# page can ANIMATE a move instead of redrawing a board.
#
# WARNING: IT MUST NEVER COST A MATCH: every entry point swallows every exception
# (logged once), reads the match dicts through copies, and writes nothing but
# this one key. And only a process that HOLDS a match writes it -- the login
# container imports this module too, and its empty dicts must never overwrite
# authsess's document. POL_TM_WATCH_KEY=0 turns it off (or names another key);
# POL_TM_WEB_WATCH=0 keeps writing it with every table unwatchable.
# ---------------------------------------------------------------------------
WATCH_EVERY_S = 5.0
#: the key outlives its last write by this long; the board calls a document
#: older than its own WATCH_STALE_S (60 s) gone anyway
WATCH_TTL_S = 120
WATCH_STEPS_MAX = 40
_WATCH = {"t": 0.0, "body": None, "owner": False, "whined": False}
#: match key -> {"rec": its _MATCH_TURN dict, "steps": [...], "seq": n, "began": t}
#: -- a new deal is a new dict, which is how a rematch starts a fresh list
_WATCH_GAMES = {}
#: match key -> {"battles": [...], "effects": [...]}: the placement in progress
_WATCH_PENDING = {}
_WATCH_LOCK = threading.RLock()


def _watch_file():
    """The key the watch document is published under, or None when it is off."""
    return tmstore.watch_key()


def _watch_whine(what, e):
    if not _WATCH["whined"]:
        _WATCH["whined"] = True
        common._say("tm: WARNING: the web watch file's %s failed (%r) -- no match is affected; "
             "said once" % (what, e))


def _watch_touch():
    """A match message arrived. The first time this process is seen holding a
    match it becomes the file's writer (for good: an ended match must still be
    published as gone); then publish, throttled."""
    try:
        if not _WATCH["owner"] and (boardrules._MATCH_TURN or vscom._COM_AT
                                    or any(v for v in list(matchmaking._MATCH_STARTED.values()))):
            _WATCH["owner"] = True
    except Exception as e:                       # noqa: BLE001
        _watch_whine("owner check", e)
    _watch_publish()


def _watch_battle(chan, index, att, dfn, res, verdict):
    """One battle of the placement in progress, as the players were shown it."""
    try:
        with _WATCH_LOCK:
            p = _WATCH_PENDING.setdefault((chan, index), {"battles": [], "effects": []})
            p["battles"].append({
                "att": int(att), "def": int(dfn),
                "a_raw": int(res.get("a_raw", 0)), "a_roll": int(res.get("a_roll", 0)),
                "a_sel": int(res.get("a_sel", 0)),
                "d_raw": int(res.get("d_raw", 0)), "d_roll": int(res.get("d_roll", 0)),
                "d_sel": int(res.get("d_sel", 0)),
                "win": ("att" if verdict == tmbattle.ATTACKER else
                        "def" if verdict == tmbattle.DEFENDER else "draw")})
    except Exception as e:                       # noqa: BLE001
        _watch_whine("battle note", e)


def _watch_effect(chan, index, eff, target, hit, aim=None):
    """A chance block's effect in the placement in progress, or a rotating
    block's ray ("ray": `aim` = the tile it fires at)."""
    try:
        if not eff:
            return
        with _WATCH_LOCK:
            p = _WATCH_PENDING.setdefault((chan, index), {"battles": [], "effects": []})
            e = {"kind": str(eff), "tile": int(target),
                 "tiles": [int(t) for t in (hit or []) if isinstance(t, int)]}
            if aim is not None:
                e["to"] = int(aim)
            p["effects"].append(e)
    except Exception as e:                       # noqa: BLE001
        _watch_whine("effect note", e)


def _watch_card(card, code=0):
    """A card (or block) on a tile, as the page draws it."""
    row = card.row
    cid = int(row.get("id", 0))
    out = {"owner": card.owner, "placer": card.placer, "arrows": int(card.arrows)}
    if cid >= 0x8000:
        out["block"] = int(code or (cid - 0x8000))
    else:
        out.update(id=cid, atk=int(row.get("attack", 0)), type=int(row.get("type", 0)),
                   pdef=int(row.get("pdef", 0)), mdef=int(row.get("mdef", 0)),
                   # a special tile's ability, inherited by the card played on
                   # it: 1 attack up (spent by its first battle), 2 defense up,
                   # 3 all eight arrows (tmbattle SPECIAL_CODES)
                   ability=int(card.ability or 0) & 0xF,
                   # rec+0x26, what power up / power down moved: the card face
                   # adds it to each stat and colours the line (0xB500A..0xB505E)
                   mod=int(getattr(card, "modifier", 0) or 0))
    return out


def _watch_board(board, codes, turn=0):
    """{tile: card} as the page draws it, from a board dict. A rotating block
    also carries its CURRENT phase: its arrow turns a step every turn
    (`tmbattle.rotating_phase`; the ray fires at (phase - 4) & 7), and the
    dealt mask alone left the page's arrow frozen where the deal put it."""
    adv = int(turn or 0) * common._env_int("POL_TM_ROTATING_STEP", 1)
    out = {}
    for t, c in sorted(dict(board).items()):
        e = _watch_card(c, codes[t] if t < len(codes) else 0)
        if tmbattle.is_rotating(c):
            ph = tmbattle.rotating_phase(c, adv)
            if ph is not None:
                e["phase"] = ph
        out[str(t)] = e
    return out


def _watch_game(key):
    rec = boardrules._MATCH_TURN.get(key)
    if rec is None:
        _WATCH_GAMES.pop(key, None)
        return None
    g = _WATCH_GAMES.get(key)
    if g is None or g["rec"] is not rec:
        g = _WATCH_GAMES[key] = {"rec": rec, "steps": [], "seq": 0, "began": time.time()}
    return g


def _watch_move(chan, index, actor, board, before, placed):
    """A placement has ended (every path ends in `_note_combo`): one step."""
    try:
        key = (chan, index)
        with _WATCH_LOCK:
            p = _WATCH_PENDING.pop(key, None) or {"battles": [], "effects": []}
            game = _watch_game(key)
            if game is None:
                return
            after = {int(t): c.owner for t, c in list(board.items())}
            placed = int(placed)
            beaten = {b["def"] for b in p["battles"] if b["win"] == "att"}
            changed = [{"tile": t, "from": before[t], "to": o,
                        "how": ("battle" if t in beaten
                                else "combo" if p["battles"] else "flip")}
                       for t, o in sorted(after.items())
                       if t != placed and t in before and before[t] != o]
            card = board.get(placed)
            game["seq"] += 1
            game["steps"].append({
                "seq": game["seq"], "t": round(time.time(), 2),
                "turn": int((boardrules._MATCH_TURN.get(key) or {}).get("turn") or 0),
                "seat": int(actor), "tile": placed,
                "card": _watch_card(card) if card is not None else None,
                # the placed card itself lost its battle and changed hands
                "lost": bool(card is not None and card.owner != actor),
                "battles": p["battles"], "changed": changed, "effects": p["effects"],
                "owners": {str(t): o for t, o in sorted(after.items())}})
            del game["steps"][:-WATCH_STEPS_MAX]
            # the shown board moves on WITH its step (see `_watch_state`)
            codes = list(boardrules._MATCH_OBJECTS.get(key) or [])
            game["board"] = _watch_board(board, codes,
                                         turn=(boardrules._MATCH_TURN.get(key) or {}).get("turn"))
            game["objects"] = codes
        _watch_publish(force=True)
    except Exception as e:                       # noqa: BLE001
        _watch_whine("move note", e)


def _watch_id(chan, index):
    """`<room>-<table>`: "#TM0R002" table 5 -> "2-5"."""
    try:
        room = int(str(chan)[-3:])
    except ValueError:
        room = 0
    return "%d-%d" % (room, int(index))


def _watch_allowed(chan, index):
    """May the web show this table? Its creator's own observe setting."""
    if not common._env_int("POL_TM_WEB_WATCH", 1) or not chan or index is None:
        return False
    rules = tablesettings._table_rules_get(chan, index) or {}

    def num(k):
        try:
            return int(rules.get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0
    if num("pa") or str(rules.get("pw") or "").strip() not in ("", "0"):
        return False                              # a password table: never
    return num("in") != 1                         # 1 = "Impossible"


def _watch_name(mid, seat):
    try:
        import tmroom
        name = (tmroom.name_of(mid) or "").strip()
    except Exception:                            # noqa: BLE001
        name = ""
    return (name or "Player %d" % (seat + 1))[:15]


def _watch_seats(key):
    """([(name, is_com, hand, com_index, vote_key)], n, (chan, index)) for a
    match key. com_index = the client's `/Com=` value, the PlPrm.BIN record
    the page names and pictures the opponent by; vote_key = the seat's key in
    `_MATCH_CONTINUE[...]["votes"]` (None for a COM, which always plays again)."""
    chan, index = key
    hands = boardrules._MATCH_HANDS.get(key) or {}

    def count(h):
        return len(h) if h is not None else None
    if chan is None:                              # VS. COM: (None, push_key)
        got = vscom._COM_GAME.get(index) or {}
        n = int(got.get("n") or 2)
        human = got.get("member")
        seats = [(_watch_name(human, 0), False, count(hands.get(pushqueue._push_key(human))),
                  None, pushqueue._push_key(human) if human is not None else None)]
        coms = got.get("coms") or []
        pattern = os.environ.get("POL_TM_COM_NAME", "COM %d")
        for s in range(1, n):
            # WARNING: NO `/Com=` YET = NO OPPONENT YET. Before the client's
            # character select sends @ComGame there is no index; falling back to
            # the seat number named every fresh game "Flower Girl Natasha"
            # (PlPrm record 1) on the page and in Discord (live testing, 2026-09-13)
            ci = coms[s - 1] if s - 1 < len(coms) else None
            try:
                ci = int(ci) if ci is not None else None
                nm = "COM" if ci is None else (pattern % ci) if "%d" in pattern else pattern
            except (TypeError, ValueError):
                nm, ci = "COM", None
            seats.append((nm[:15], True, count(hands.get(("com", s))), ci, None))
        return seats, n, tuple(got.get("table") or (None, None))
    roster = turns._roster_at(chan, index)
    return ([(_watch_name(m, i), False, count(hands.get(pushqueue._push_key(m))), None, pushqueue._push_key(m))
             for i, (m, _v) in enumerate(roster)], len(roster), (chan, index))


def _watch_state(key, phase):
    """(table id, {"watchable", "state"?}) for one match, or (None, None)."""
    seats, n, (chan, index) = _watch_seats(key)
    if chan is None or index is None:
        return None, None
    tid = _watch_id(chan, index)
    if not _watch_allowed(chan, index):
        return tid, {"watchable": False}
    rec = boardrules._MATCH_TURN.get(key) or {}
    n = int(rec.get("n") or n or 2)
    game = _watch_game(key) if phase == "play" else None
    # WARNING: THE BOARD AS OF THE LAST MOVE SHOWN, NOT THE LIVE ONE. A PvP placement
    # is RESUMABLE: `_begin_placement` puts the card down at once and
    # `_advance_placement` resolves its battles one at a time as the clients
    # ask (parking for a @BattleSelect), writing every capture to the live
    # board long before `_watch_move` writes the move's step. Publishing the
    # live board let the page show those results first, then replay the move
    # over them -- cards flipping before their battle, then flipping again
    # (live testing, 2026-09-13). While a placement is pending the board stays the
    # one the last step left; `_watch_move` moves it on with the step.
    if game is not None and key in placement._PENDING_BATTLE and game.get("board") is not None:
        shown, codes = game["board"], list(game.get("objects") or [])
    else:
        codes = list(boardrules._MATCH_OBJECTS.get(key) or [])
        shown = _watch_board(boardrules._MATCH_BOARD.get(key) or {}, codes, turn=rec.get("turn"))
        if game is not None:
            game["board"], game["objects"] = shown, codes
    scores = [0] * n
    for c in shown.values():
        o = c.get("owner")
        if o is not None and 0 <= o < n:
            scores[o] += 1
    over = bool(rec.get("result_sent"))
    winner = None
    if over and scores:
        top = [i for i, s in enumerate(scores) if s == max(scores)]
        winner = top[0] if len(top) == 1 else -1  # -1 = a draw
    seats = (seats + [("Player %d" % (i + 1), False, None, None, None)
                      for i in range(len(seats), n)])[:n]
    # the "Play again?" panel: who has answered (0 deciding, 1 again, 2 not)
    votes = ((rematch._MATCH_CONTINUE.get(key) or {}).get("votes") or {}) if over else {}

    def vote(com, vk):
        if not over:
            return None
        if com:
            return rematch.CONTINUE_AGAIN
        v = int(votes.get(vk, rematch.CONTINUE_UNANSWERED)) if vk is not None else rematch.CONTINUE_UNANSWERED
        return v if v in (rematch.CONTINUE_AGAIN, rematch.CONTINUE_DECLINE) else rematch.CONTINUE_UNANSWERED
    return tid, {"watchable": True, "state": {
        "id": tid, "room": int(tid.split("-")[0]), "table": int(index), "n": n,
        "tiles": boardrules._board_tiles(n), "com": key[0] is None,
        "phase": "over" if over else phase,
        "turn": int(rec.get("turn") or 0), "active": int(rec.get("active") or 0),
        "limit": common._env_int("POL_TM_TURN_LIMIT", 5 * n),
        # "mid" = the member id, for the board to find the player's PlayOnline
        # portrait in the account database; the board STRIPS it before serving anything
        "players": [{"name": nm, "com": com, "hand": h, "score": scores[i],
                     "ci": ci, "vote": vote(com, vk),
                     "mid": (int(vk) if isinstance(vk, int) or str(vk).isdigit() else None)}
                    for i, (nm, com, h, ci, vk) in enumerate(seats)],
        "board": shown,
        "objects": codes,
        "steps": list(game["steps"]) if game else [],
        "winner": winner,
        "began": round(game["began"], 2) if game else None}}


def _watch_tables():
    out = {}
    for key in list(boardrules._MATCH_TURN):
        tid, entry = _watch_state(key, "play")
        if tid:
            out[tid] = entry
    # card select: a started table not yet dealt, and a COM game before its deal
    for (chan, index), who in list(matchmaking._MATCH_STARTED.items()):
        if who and (chan, index) not in boardrules._MATCH_TURN:
            tid, entry = _watch_state((chan, index), "select")
            if tid and tid not in out:
                out[tid] = entry
    for (chan, index), pk in list(vscom._COM_AT.items()):
        if (None, pk) not in boardrules._MATCH_TURN:
            tid, entry = _watch_state((None, pk), "select")
            if tid and tid not in out:
                out[tid] = entry
    for k in [k for k in list(_WATCH_GAMES) if k not in boardrules._MATCH_TURN]:
        _WATCH_GAMES.pop(k, None)
    return out


def _watch_publish(force=False):
    """Publish the document: on change (at most once a second, unless `force`)
    or every WATCH_EVERY_S. Never raises."""
    key = _watch_file()
    if not key or not _WATCH["owner"]:
        return
    now = time.time()
    if not force and now - _WATCH["t"] < 1.0:
        return
    try:
        with _WATCH_LOCK:
            tables = _watch_tables()
        body = json.dumps(tables, sort_keys=True, separators=(",", ":"))
        if body == _WATCH["body"] and now - _WATCH["t"] < WATCH_EVERY_S:
            return
        tmstore.kv.set(key, '{"stamp":%.3f,"tables":%s}' % (now, body),
                       ttl=WATCH_TTL_S)
        _WATCH.update(t=now, body=body)
    except RuntimeError:
        return                     # a match dict changed under us: next beat
    except Exception as e:                       # noqa: BLE001
        _watch_whine("write", e)
_LIVE_MARKER_WHINED = False
