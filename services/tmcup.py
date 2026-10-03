"""Tetra Master tournaments: which event, what phase, its missions and prizes.

A small module on purpose: the game server (tetramaster, in authsess) imports
it, and so can a website's API, so a site can never disagree with the game
about which cup is on, when it ends, or what the missions are. It reads the
core's event calendar (eventcal, when the core provides it) and the
POL_TM_EVENT_* knobs; nothing here talks to a client. Without a calendar the
tournament runs in rolling two-hour blocks.
"""
import os
import sys
import time


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)), 0)
    except ValueError:
        return default


def _say(msg):
    sys.stderr.write(msg + "\n")


#: A plain description of each mission type, for a website (the game draws
#: its own wording from the client). Only the counted types are ever set
#: (_COUNTED_MISSIONS).
MISSION_TEXT = {
    0: ("More than {n} combos", "More than {n} combos"),
    1: ("One perfect game", "{n} perfect games"),
    2: ("{n} first places in a row", "{n} first places in a row"),
    3: ("More than {n} card(s) turned by a Rotating Block",) * 2,
    4: ("One tied game", "{n} tied games"),
    5: ("One first place when moving first", "{n} first places when moving first"),
    6: ("More than {n} card(s) turned by Chance Blocks",) * 2,
    7: ("One win over a Defense Up card on a special tile",
         "{n} wins over a Defense Up card on a special tile"),
}


def mission_text(t, n):
    one, many = MISSION_TEXT.get(int(t), ("Mission {n}", "Mission {n}"))
    return (one if int(n) == 1 else many).format(n=int(n))


class _NoCalendar(Exception):
    """The core ships no event calendar (eventcal): rolling blocks."""


def event_info(now=None):
    """The Tetra Master event the tournament clock follows, as a dict with at
    least start, end, name, id (and guide, ticker, missions, prizes when the
    calendar gives them).

    From the event calendar (services/eventcal.py): the event running
    now; else the one that ended less than POL_TM_EVENT_RESULTS_HOURS (12) ago,
    so its results and prizes stay up; else the next one, shown as pending
    with a countdown. POL_TM_EVENT_WINDOW="HH:MM-HH:MM" (UTC, daily) overrides
    the calendar for testing; with no calendar, rolling two-hour blocks."""
    now = time.time() if now is None else now
    spec = os.environ.get("POL_TM_EVENT_WINDOW", "").strip()
    if spec:
        day = int(now // 86400) * 86400
        try:
            a, b = spec.split("-")
            ah, am = (int(x) for x in a.split(":"))
            bh, bm = (int(x) for x in b.split(":"))
            start = day + ah * 3600 + am * 60
            end = day + bh * 3600 + bm * 60
            if end <= start:
                end += 86400
                if now < end - 86400:          # still yesterday's overnight run
                    start -= 86400
                    end -= 86400
            return {"start": start, "end": end, "name": "Chocobo Cup",
                    "id": "window-%d" % start, "kind": "window"}
        except ValueError:
            pass
    try:
        import eventcal
    except ImportError:
        eventcal = None             # no calendar in this core: rolling blocks
    try:
        if eventcal is None:
            raise _NoCalendar
        cal = eventcal.load()
        if (cal.get("tm") or {}):
            cur = eventcal.current("tm", now, cal)
            if cur:
                return cur
            last = eventcal.last_ended("tm", now, cal)
            # A session's results stay up for an hour, so the next session's
            # countdown shows in the gap; a long cup's stay up for 12 hours.
            if last and last.get("kind") == "session":
                keep = max(0, _env_int("POL_TM_EVENT_SESSION_RESULTS_MIN", 60)) * 60
            else:
                keep = max(0, _env_int("POL_TM_EVENT_RESULTS_HOURS", 12)) * 3600
            if last and now < last["end"] + max(keep, _env_int(
                    "POL_TM_EVENT_SETTLE", 180)):
                return last
            nxt = eventcal.upcoming("tm", now, 1, cal)
            if nxt:
                return nxt[0]
    except _NoCalendar:
        pass
    except Exception as exc:
        _say("tm: event calendar unreadable (%r) -- rolling blocks" % (exc,))
    start = int(now // 7200) * 7200
    gap = max(0, min(3600, _env_int("POL_TM_EVENT_ROLL_GAP", 900)))
    return {"start": start, "end": start + 7200 - gap, "name": "Chocobo Cup",
            "id": "roll-%d" % start, "kind": "rolling"}


def event_window(now=None):
    """(start_epoch, end_epoch) of the event the clock follows (event_info)."""
    info = event_info(now)
    return int(info["start"]), int(info["end"])


#: Mission types the server COUNTS (tmeventstate): perfect wins, wins in a
#: row, ties -- and the counts a rotating week picks from.
#: The first rotation (perfect wins, streak, ties only), kept for the cups that
#: started under it; the seven-type draw applies from Mon 2026-09-28 00:00 UTC.
_ROTATION_V1 = ((1, (1, 2, 3)), (2, (2, 3, 4)), (4, (1, 2)))
_ROTATION_V2_FROM = 1790553600
_COUNTED_MISSIONS = ((0, (2, 3, 4)), (1, (1, 2)), (2, (2, 3)),
                     (3, (1, 2, 3)), (4, (1, 2)), (5, (1, 2, 3)), (6, (1, 2, 3)))


def event_missions(info=None):
    """[(type, count), ...] for this event: POL_TM_EVENT_MISSIONS if set, else
    the calendar's 'missions' ("t:n,..."), where "rotate" picks counts by the
    ISO week so no two weekends match. Types the server cannot count are
    dropped, so nobody is set a mission that can never tick."""
    info = info if info is not None else event_info()
    spec = os.environ.get("POL_TM_EVENT_MISSIONS") or str(info.get("missions") or "rotate")
    counted = {t for t, _c in _COUNTED_MISSIONS}
    if spec.strip().lower() == "rotate":
        # Three of the counted types a week, a different trio each week, each
        # with a week-dependent count. Deterministic, so the game, the website
        # and a restart all agree.
        import random as _r
        wk = int(info.get("week") or (int(info["start"]) // (7 * 86400)))
        if int(info.get("start", 0)) < _ROTATION_V2_FROM:
            # Cups that began before the seven-type pool keep the missions
            # their players (and the website) already saw.
            return sorted((t, cs[(wk + i) % len(cs)])
                          for i, (t, cs) in enumerate(_ROTATION_V1))
        if info.get("kind") == "session":
            # Every session its own trio: two sessions share a day.
            wk = int(info.get("start", 0)) // 3600
            rng = _r.Random(wk)
        else:
            rng = _r.Random(int(info.get("start", 0)) // 86400)
        trio = rng.sample(list(_COUNTED_MISSIONS), 3)
        return sorted((t, cs[(wk + i) % len(cs)]) for i, (t, cs) in enumerate(trio))
    out = []
    for part in spec.split(","):
        try:
            t, n = (int(x) for x in part.split(":"))
        except ValueError:
            continue
        if t in counted and 0 < n < 256:
            out.append((t, n))
    return sorted(out)


def event_prizes(info=None):
    """money[3], cards[3], pack, mission_money, class_money[3] for this event;
    POL_TM_EVENT_* overrides win. The fallbacks are the 2026-10-03 scale (about
    a tenth of the first cup's, which paid more than a career's earnings)."""
    info = info if info is not None else event_info()
    p = dict(info.get("prizes") or {})

    def ints(name, key, default):
        raw = os.environ.get(name)
        vals = raw.split(",") if raw else p.get(key, default)
        try:
            return [int(x) for x in vals][:3] + [0] * (3 - len(list(vals)[:3]))
        except (TypeError, ValueError):
            return list(default)
    return {"money": ints("POL_TM_EVENT_PRIZE_MONEY", "money", [5000, 3000, 1500]),
            "cards": ints("POL_TM_EVENT_PRIZE_CARDS", "cards", [3, 2, 1]),
            "pack": _env_int("POL_TM_EVENT_PRIZE_PACK", int(p.get("pack", 20))),
            "mission_money": _env_int("POL_TM_EVENT_MISSION_MONEY",
                                      int(p.get("mission_money", 500))),
            "class_money": ints("POL_TM_EVENT_CLASS_MONEY", "class_money", [300, 800, 1500])}


def event_phase(now=None):
    """('pending' | 'running' | 'over' | 'closed', start, end).

    over = time is up and a tournament game is still being played; closed =
    results final, rankings and prizes open. Over lasts only while a game is
    live (games_live, written by the game server), capped at
    POL_TM_EVENT_SETTLE seconds (default 180): a fixed 3 minutes kept every
    client on "Tallying tournament results... Retry/Exit" when nothing was
    left to tally (live, 2026-10-03)."""
    now = time.time() if now is None else now
    start, end = event_window(now)
    if now < start:
        return "pending", start, end
    if now < end:
        return "running", start, end
    if now < end + max(0, _env_int("POL_TM_EVENT_SETTLE", 180)) and games_live(end, now):
        return "over", start, end
    return "closed", start, end


#: Tournament games being played now, written by the game server (Valkey
#: `tm:event-games-live`): {"end": that event's end, "n": count, "at": time}.
_GAMES_LIVE_TTL = 900
_GAMES_CACHE = [0.0, None]


def games_live_key():
    return os.environ.get("POL_TM_EVENT_GAMES_KEY", "tm:event-games-live")


def note_games_live(event_end, n, now=None):
    try:
        import tmstore
        tmstore.kv.set_json(games_live_key(), {"end": int(event_end), "n": int(n),
                                                "at": time.time() if now is None else now},
                            ttl=_GAMES_LIVE_TTL)
        _GAMES_CACHE[0] = 0.0
    except Exception:                                        # noqa: BLE001
        pass


def games_live(event_end, now=None):
    """True while a game of the event ending at `event_end` is still live.
    Unknown (no record, store down) counts as live, so the cap decides --
    never close early on missing data."""
    now = time.time() if now is None else now
    if now - _GAMES_CACHE[0] > 2.0:
        try:
            import tmstore
            _GAMES_CACHE[1] = tmstore.kv.get_json(games_live_key())
        except Exception:                                    # noqa: BLE001
            _GAMES_CACHE[1] = None
        _GAMES_CACHE[0] = now
    d = _GAMES_CACHE[1]
    if not isinstance(d, dict) or int(d.get("end", -1)) != int(event_end):
        return True
    return int(d.get("n", 1)) > 0


#: The tournament's latest moment, written by the game server and read by the
#: ticker in both processes: the Valkey key `tm:event-live` (it was the file
#: `<POL_DATA_DIR>/tm-event-live.json`). Live state: it expires after
#: MOMENT_TTL_S, past the longest age any reader accepts.
MOMENT_TTL_S = 3600


def moment_key():
    return os.environ.get("POL_TM_EVENT_LIVE_KEY", "tm:event-live")


def note_moment(event_id, text, now=None):
    """The latest thing that happened in this event, for the ticker and the
    website (written by the game server, read by both)."""
    try:
        import tmstore
        tmstore.kv.set_json(moment_key(),
                            {"id": event_id, "moment": text,
                             "at": time.time() if now is None else now},
                            ttl=MOMENT_TTL_S)
    except Exception:                                        # noqa: BLE001
        pass


def last_moment(event_id, max_age=900, now=None):
    now = time.time() if now is None else now
    try:
        import tmstore
        d = tmstore.kv.get_json(moment_key()) or {}
        if d.get("id") == event_id and now - float(d.get("at", 0)) < max_age:
            return str(d.get("moment") or "")
    except Exception:                                        # noqa: BLE001
        pass
    return ""


def ticker_pages(moment=None, name_of=None, now=None):
    """The live ticker's pages (at most 10, each under 100 characters): the
    latest moment, the leader and top 3, event stats, countdowns; "begins in"
    before, "Winner" after. The same lines go to the game and the website."""
    info = event_info(now)
    ph, ws, we = event_phase(now)
    name = str(info.get("name") or "tournament")
    now = time.time() if now is None else now
    pages = []
    try:
        import tmeventstate
        board = tmeventstate.standings(ws)
    except Exception:
        board = {}
    ranked = sorted(board.items(), key=lambda kv: -int(kv[1].get("steps", 0)))

    def nm(mid):
        try:
            # Never the member id: the same lines go to the public website.
            return (name_of(mid) if name_of else "") or "A player"
        except Exception:
            return "A player"
    moment = moment or last_moment(info.get("id"), now=now)
    if moment:
        pages.append(moment)
    if ph == "pending":
        mins = max(0, int((ws - now) // 60))
        pages.append("The %s begins in %s!" % (
            name, "%d hour(s) %d min" % divmod(mins, 60) if mins >= 60 else "%d min" % mins))
    elif ph == "running":
        if ranked and int(ranked[0][1].get("steps", 0)) > 0:
            top = [(nm(m), int(r.get("steps", 0))) for m, r in ranked[:3]]
            pages.append("%s leads the %s with %d steps!" % (top[0][0], name, top[0][1]))
            if len(top) > 1:
                pages.append("Top 3: " + ", ".join("%s %d" % t for t in top))
        else:
            pages.append(str(info.get("ticker") or "The %s is under way!" % name))
        games = sum(int(r.get("games", 0)) for r in board.values()) // 2
        perf = sum(int(r.get("perfect", 0)) for r in board.values())
        if board:
            pages.append("%d player(s) entered, %d game(s) played, %d perfect win(s)"
                         % (len(board), games, perf))
        left = int(we - now)
        if left <= 300:
            pages.append("Final %d minutes!" % max(1, left // 60))
        elif left <= 1800:
            pages.append("%d minutes left in the %s!" % (left // 60, name))
    else:
        if ranked and int(ranked[0][1].get("steps", 0)) > 0:
            pages.append("The %s is over! Winner: %s with %d steps"
                         % (name, nm(ranked[0][0]), int(ranked[0][1].get("steps", 0))))
        else:
            pages.append("The %s is over. Thanks for playing!" % name)
    return pages[:10]
