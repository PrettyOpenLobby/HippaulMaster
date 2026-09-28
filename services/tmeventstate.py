"""Tetra Master tournament standings, shared between the two processes.

`authsess` (tetramaster) scores each finished event game into the
`tm_event_standing` table (PostgreSQL, through tmstore; it was the file
`<POL_DATA_DIR>/tm-event-state.json`); `login` (tmtitle) builds
`b/g/TM0EventMemberList` from it on the client's re-entry fetch. Each score is
one transaction under an advisory lock, so two finished games never lose each
other's steps and a reader never sees half a game. Keyed by the event window's
start (tetramaster.event_window), so a new window starts a fresh board; the
rows of earlier windows stay in the table as history and nothing here reads
them.

THE MEMBER LIST (static reading of the client): 0x2808 bytes, count
u32 at +0x04, records of 0x28 from +0x08 (up to 256):

    +0x00 u64      member id -- the POL-ID the client matches against its own
                   (@Init= /NN=), so a player finds THEIR row
    +0x08 char[16] name, 15 + NUL
    +0x1C s32      score: the chocobo STEPS. Its low byte becomes the client's
                   own status chars 14-15, which is what the ranking board
                   draws for everyone (via the roster)
    +0x20 u32      mission-clear bits (bit t = mission type t -- INFERRED)
    +0x24 s32      negative hides the row

THE STEPS follow the game's own rules screen, read as outcome/effect pairs:
a perfect win +3, first place +2, second with a tie +1, a quit -1; a
two-player second place does not move. POL_TM_EVENT_STEPS
overrides, e.g. "perfect:3,win:2,tie:1,lose:0,quit:-1".

MISSIONS are POL_TM_EVENT_MISSIONS `type:count` (the same knob that fills the
Mission window). All but type 7 (wins against a "Defense Up" card) are
counted: 0 combos, 1 perfect wins, 2 firsts in a row, 3 Rotating Block
conversions, 4 ties, 5 wins acting first, 6 Chance Block conversions
(MISSION_RULES).
"""
import contextlib
import os
import struct
import time

import tmstore

MEMBER_LIST_SIZE = 0x2808
REC_OFF, REC_SIZE, REC_MAX = 0x08, 0x28, 256

#: The advisory lock every write takes: a score is read, changed and written
#: back, and two games finishing at once must not lose each other's steps.
_LOCK = "tm_event_standing"


@contextlib.contextmanager
def _board(window):
    """(conn, {member: row}) for one window, inside a write transaction that
    holds `_LOCK`. Rows put back into the dict are written on exit."""
    tmstore.ensure_schema()
    db = tmstore.db
    with db.transaction(lock=_LOCK) as conn:
        rows = db.query("SELECT member, data FROM tm_event_standing "
                        "WHERE event_window = %s", (int(window),), conn=conn)
        board = {r["member"]: dict(r["data"]) for r in rows}
        before = {k: dict(v) for k, v in board.items()}
        yield conn, board
        for member, row in board.items():
            if before.get(member) != row:
                db.execute("INSERT INTO tm_event_standing (event_window, member, data)"
                           " VALUES (%s, %s, %s::jsonb)"
                           " ON CONFLICT (event_window, member) DO UPDATE"
                           " SET data = EXCLUDED.data, updated_at = now()",
                           (int(window), str(member), tmstore.jsonb(row)), conn=conn)


def steps_table():
    out = {"perfect": 3, "win": 2, "tie": 1, "lose": 0, "quit": -1}
    for part in os.environ.get("POL_TM_EVENT_STEPS", "").split(","):
        k, _, v = part.partition(":")
        try:
            if k.strip() in out:
                out[k.strip()] = int(v)
        except ValueError:
            pass
    return out


def missions():
    """Fallback when the caller passes none: POL_TM_EVENT_MISSIONS, else the
    three counted types at their lowest counts."""
    out = []
    for part in os.environ.get("POL_TM_EVENT_MISSIONS", "1:1,2:2,4:1").split(","):
        try:
            t, n = (int(x) for x in part.split(":"))
        except ValueError:
            continue
        if 0 <= t < 8 and 0 < n < 256:
            out.append((t, n))
    return sorted(out)


#: How each mission type is judged from a player's row. The Mission window's
#: wording decides the comparison: types 0, 3 and 6 say "more than N" and are
#: strictly greater, the rest "at least N".
MISSION_RULES = {
    0: ("combos", "gt"),          # more than N combos
    1: ("perfect", "ge"),         # N perfect wins
    2: ("best_streak", "ge"),     # N first places in a row
    3: ("rot_flips", "gt"),       # more than N cards turned by a Rotating Block
    4: ("ties", "ge"),            # N ties
    5: ("first_wins", "ge"),      # N first places when moving first
    6: ("chance_flips", "gt"),    # more than N cards turned by Chance Blocks
}


def _mission_bits(row, active=None):
    bits = 0
    for t, n in (active if active is not None else missions()):
        rule = MISSION_RULES.get(t)
        if not rule:
            continue
        have = int(row.get(rule[0], 0))
        if (have > n) if rule[1] == "gt" else (have >= n):
            bits |= 1 << t
    return bits


def record_game(window, results, active_missions=None, extras=None):
    """Score one finished game. `results` is [(member_id, outcome), ...] with
    outcome one of perfect / win / tie / lose / quit. Returns {member: row}."""
    steps = steps_table()
    out = {}
    with _board(window) as (_conn, board):
        _score(board, results, steps, active_missions, extras, out)
    return out


def _score(board, results, steps, active_missions, extras, out):
    for mid, outcome in results:
        row = board.setdefault(str(mid), {"steps": 0, "games": 0, "wins": 0,
                                          "perfect": 0, "ties": 0,
                                          "streak": 0, "best_streak": 0})
        row["games"] += 1
        for k, v in ((extras or {}).get(mid) or {}).items():
            row[k] = int(row.get(k, 0)) + int(v)
        row["steps"] = max(0, min(255, row["steps"] + steps.get(outcome, 0)))
        if outcome in ("win", "perfect"):
            row["wins"] += 1
            row["streak"] += 1
            row["best_streak"] = max(row["best_streak"], row["streak"])
            if outcome == "perfect":
                row["perfect"] += 1
        else:
            row["streak"] = 0
            if outcome == "tie":
                row["ties"] += 1
        row["missions"] = _mission_bits(row, active_missions)
        row["at"] = time.time()
        out[str(mid)] = dict(row)


def set_deck(window, member_id, rows):
    """Remember the five cards a player picked on entering this window's
    tournament (8-value rows as bytes). Entering also lists them on the board."""
    with _board(window) as (_conn, board):
        row = board.setdefault(str(member_id), {"steps": 0, "games": 0, "wins": 0,
                                                "perfect": 0, "ties": 0,
                                                "streak": 0, "best_streak": 0,
                                                "missions": 0})
        row["deck"] = [r.decode("ascii") if isinstance(r, bytes) else str(r)
                       for r in rows][:5]
        row["at"] = row.get("at") or time.time()


def deck(window, member_id):
    """The five picked rows (bytes) for this window, or []."""
    row = standings(window).get(str(member_id)) or {}
    return [r.encode("ascii") for r in (row.get("deck") or [])][:5]


def mark_paid(window, member_id):
    """This player's tournament prize has been credited for this window."""
    with _board(window) as (_conn, board):
        row = board.setdefault(str(member_id), {"steps": 0, "missions": 0})
        row["paid"] = True


def standings(window):
    """{member: row} for one window ({} when nobody has played in it)."""
    tmstore.ensure_schema()
    rows = tmstore.db.query("SELECT member, data FROM tm_event_standing "
                            "WHERE event_window = %s", (int(window),))
    return {r["member"]: dict(r["data"]) for r in rows}


def member_list_blob(window, pol_id_of, name_of, n=MEMBER_LIST_SIZE):
    """The TM0EventMemberList bytes for this window, highest steps first."""
    board = standings(window)
    buf = bytearray(max(n, MEMBER_LIST_SIZE))
    rows = sorted(board.items(), key=lambda kv: (-kv[1].get("steps", 0),
                                                 kv[1].get("at", 0)))
    count = 0
    for mid, row in rows[:REC_MAX]:
        hexid = str(pol_id_of(mid) or "")
        try:
            pid = int(hexid, 16)
        except ValueError:
            continue                    # no POL-ID yet: no row can match them
        o = REC_OFF + count * REC_SIZE
        struct.pack_into("<Q", buf, o, pid)
        name = str(name_of(mid) or "").encode("latin-1", "replace")[:15]
        buf[o + 0x08:o + 0x18] = name.ljust(16, b"\x00")
        struct.pack_into("<iIi", buf, o + 0x1C, int(row.get("steps", 0)),
                         int(row.get("missions", 0)), 0)
        count += 1
    struct.pack_into("<I", buf, 0x04, count)
    return bytes(buf[:n])
