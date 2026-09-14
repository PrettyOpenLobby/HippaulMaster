#!/usr/bin/env python3
"""Tetra Master's RANKINGS: the 232-byte rank row, and the 28-byte header.

WHAT THIS IS. `Rankings` on the Tetra Master menu is six screens over FIVE
server lists. The client asks for one by id on POLpro class `R` (`<RR>`), we
answer `<RF>` naming a resource path plus `<LN>` the row count, and it fetches
that path over lobby `3:0` and reads `rows * 232` bytes. Everything below was
read out of the unpacked TM.dll (image base 0x04F90000, so file offset ==
rva) on 2026-08-20; the menu labels come from the shipped `data/Ranking.BIN`,
decoded with the BIN extractor's dump.

THE SIX MENU ITEMS, AND THE FIVE IDS BEHIND THEM. `0x51C6398` is the menu-index
-> RankID table (six dwords: 2, 3, 4, 0, 0, 1) and `0x8AE30` rejects a RankID
>= 5 with "CCltLobby::RankFileRequest RankID error":

    menu  Ranking.BIN label   RankID  column        value at   last week at
    0     VS. Ratings         2       VS. Rating    row+0xA0   row+0xC5
    1     Top 30              3       VS. Rating    row+0xA4   row+0xC4
    2     Best Rookies        4       VS. Rating    row+0xA4   row+0xC3
    3     Prize Center        0       (no list -- it is the prize SHOP)
    4     Grand Total         0       Prize Money   row+0x94   row+0xC6
    5     Weekly Total        1       Prize Money   row+0x9C   row+0xC7

Menus 3 and 4 share RankID 0. Menu 3's arm in the row loop is the jump table's
DEFAULT (rva 0x1753C4), which reads nothing out of the row at all, so a file
built for menu 4 serves both. Menus 1 and 2 read the SAME row field (+0xA4) --
they are both "last week's" lists ("Display last week's top 30 players by VS.
Rating"), while menu 0 shows the current rating from +0xA0. That is the whole
reason two fields exist.

THE ROW LOOP is rva 0x1752B1..0x1754C5, walking `ecx` from `rowbase + 0xC2` in
steps of 0xE8. It copies four things per row into a 44-byte DISPLAY record
(table base 0x5284748, 100 rows x 6 lists = 600 records):

    display+0x00  char[32]  the name           <- row+0x00, or "-------"
    display+0x20  u32       the value column   <- the row field named above
    display+0x24  u32       LAST WEEK's rank   <- the row BYTE named above
    display+0x28  u32       THIS week's rank

*** THE RANK IS THE ROW'S POSITION IN THE FILE, NOT A FIELD IN IT. *** The init
at rva 0x173FBF..0x173FF3 pre-fills every display record with `+0x28 = i + 1`,
and the only other write (0x175417..0x17542D) is the TIE rule: when a row's
value equals the previous row's, it inherits the previous row's rank. So the
server decides the ranking by the ORDER IT WRITES THE ROWS, and equal values
render as equal ranks -- standard competition ranking, done by the client.

THE VALUE COLUMNS ARE FIXED-POINT x100 OR MONEY, AND THE CLIENT DECIDES WHICH BY
MENU (formatter jump table 0x5106D44, arms at 0x17664D / 0x1766C5 / 0x1766FC):
menus 0..3 print `%d.%02d` of value/100, capped at 0x98967F (99999.99); menus 4
and 5 print comma-grouped money capped at 0x3B9AC9FF (999,999,999). So a VS.
Rating of 2.96 is the u32 296 -- the same x100 encoding `U/g/TM0DataFile` uses
for VS. Rating (+0x128) and Average Rank (+0x30).

THE ROW, 232 bytes (0xE8):

    +0x00  char[16]  player name, NUL-terminated (15 + NUL: the client's own
                     character-name buffer at 0x52452B0 is 16 bytes)
    +0x10  u32       cid low  ) the CHARACTER POOL identity, compared at
    +0x14  u32       cid high ) rva 0x17545E against the client's own
    +0x18  u32       csid     ) 0x52452A0 / 0x52452A4 / 0x524529C
    +0x94  u32       prize money, grand total     (menu 4)
    +0x9C  u32       prize money, this week       (menu 5)
    +0xA0  u32       VS. Rating x100, current     (menu 0)
    +0xA4  u32       VS. Rating x100, last week   (menus 1 and 2)
    +0xC0  u8        NON-ZERO HIDES THE NAME: the row draws Ranking.BIN record
                     11, "-------", instead (rva 0x1752D8/0x175304)
    +0xC2  u8        last week's rank in menu 3 -- AND the VS. Ratings list's
                     row terminator, see the trap below
    +0xC3  u8        last week's rank, Best Rookies
    +0xC4  u8        last week's rank, Top 30
    +0xC5  u8        last week's rank, VS. Ratings
    +0xC6  u8        last week's rank, Grand Total
    +0xC7  u8        last week's rank, Weekly Total

Everything else in the 232 bytes is untouched by this client.

*** THE +0xC2 TRAP. *** The menu-0 arm opens `cmp byte [ecx], 0` / `je 0x1754CD`
(rva 0x175331) -- a ZERO at row+0xC2 blanks that row's name and ENDS the list
right there. It is the only list that tests it. So every row of the VS. Ratings
file (RankID 2) must carry a non-zero +0xC2 or the screen truncates at the first
row that does not, and a file of all-zero rows renders one blank line. We
therefore write +0xC2 on every row of every list.

THE LAST-WEEK BYTES ARE READ SIGNED (`movsx`) and rva 0x175433 turns anything
<= 0 into 9999, which is the "no rank last week" sentinel. A byte cannot express
a last-week rank above 127; the renderer only uses it to decide whether the row
moved (rva 0x1762A5 compares it against this week's rank and draws nothing when
they are equal), so 0 is the right value for "new entry".

THE COUNT IS BOUNDED AT BOTH ENDS. `<LN>` 0 does not mean "empty list": the
scene tests the count BEFORE it fetches (rva 0x1750AE `jle`) and tears itself
down with no error -- that is the 2026-08-20 "Rankings closes straight back to
the menu" report. And `0x8AF00` refuses `num > 100` with "CCltLobby::ReadRankList
error. num = %d [0 < num <= %d]". So: 1 <= rows <= 100.

`b/g/TM0RkData` -- THE 28-BYTE HEADER, fetched by the same scene (rva 0x8AFE2,
`sqMgCpReadFile(path, 0x52223AC, 0x1C, ...)`) and unpacked at rva 0x1756E3:

    +0x00  u32  not read by this client
    +0x04  u32  total players in VS. Ratings  AND in Grand Total
    +0x08  u32  total players in Weekly Total
    +0x0C  u32  UNIX TIME of the ranking update -- see below
    +0x10  u32  not read by this client
    +0x14  u32  total players in Top 30
    +0x18  u32  total players in Best Rookies

The counts are the "N" in Ranking.BIN's `Your Rank: <rank>/<N> players`; a
player whose own row is not in the file gets "Did not rank/<N> players"
(records 34 and 48, chosen at rva 0x17692F). The timestamp drives the
`Tally Period: <a> - <b>` line, and the client derives BOTH ends from it:
`a = t - 14 days` and `b = t - 8 days` (rva 0x176AA3 / 0x176AC4, the constants
0xFFED8B00 and 0xFFF57400 read as signed). So `t` is the NEXT UPDATE time and
the period shown is the seven days ending eight days before it -- which is
exactly how SE's own screen read ("Next update: 01/02/2011 10:00 PST"). The
line is skipped entirely when `t <= 946080000` (1999-12-25, rva 0x176A60), so
zero is a legal "no period known".
"""
import json
import os
import re
import struct
import time

REC = 232                       #: bytes per rank row
MAX_ROWS = 100                  #: 0x8AF00's ceiling, and the display table's

#: rank row field offsets
NAME_OFF, NAME_MAX = 0x00, 16
CID_LO_OFF, CID_HI_OFF, CSID_OFF = 0x10, 0x14, 0x18
PRIZE_TOTAL_OFF, PRIZE_WEEK_OFF = 0x94, 0x9C
RATING_OFF, RATING_LAST_OFF = 0xA0, 0xA4
HIDE_NAME_OFF = 0xC0
#: menu index -> the row byte holding that list's last-week rank
LAST_RANK_OFF = {0: 0xC5, 1: 0xC4, 2: 0xC3, 3: 0xC2, 4: 0xC6, 5: 0xC7}

RKDATA = "b/g/TM0RkData"
RKDATA_LEN = 28

#: KEY: THE PS2 READS THE SAME FILE 24 BYTES SHORTER, AND FOUR BYTES OVER.
#:
#: `TMaster.pex` issues this fetch at `0x003aac54` with **`a3 = 24`**, not the
#: PC's `0x1C`. `a3` is the length argument, proved on the same call against two
#: siblings whose lengths were already measured and are already live:
#: `0x003aad48` is `a3 = 776` (TM0SML) and `0x003aaf18` is `a3 = 400` (TM0IML).
#:
#: Serving the PC's 32 (28 + 4) is what raised **`8250-37069`** -- measured
#: 2026-09-09T01:22:56Z, prod "serving 32B" against the console's own log ring
#: recovered from EE RAM: `CClient error: sqMgReadFileCheck (-8250)`
#: `<mg.c,3555>`, which is `0x00409eb0`. The rank tick's failure arm at
#: `0x003fc250` (`addiu a3, zero, 69`) renders that pair as `<code>-<37000+sel>`.
#:
#: AND THE LAYOUT DIFFERS BY THE SAME FOUR BYTES -- so a plain truncation to 24
#: would have traded a rejected read for five misread fields. The PS2's five
#: header getters are absolute loads off the read buffer `0x005C6EF0`:
#:
#:     0x003fc4e0  lw 0x005C6EF0   -> +0x00      0x003fc4d0  lw 0x005C6F00 -> +0x10
#:     0x003fc4b0  lw 0x005C6EF4   -> +0x04      0x003fc4c0  lw 0x005C6F04 -> +0x14
#:     0x003fc4a0  lw 0x005C6EF8   -> +0x08
#:
#: which is exactly this file's PC layout with the PC's UNREAD `+0x00` removed,
#: so `pc[4:28]` IS the PS2 file -- field for field, no re-encoding:
#:
#:     PS2 +0x00 = PC +0x04   VS. Ratings AND Grand Total
#:     PS2 +0x04 = PC +0x08   Weekly Total
#:     PS2 +0x08 = PC +0x0C   UNIX time of the next update
#:     PS2 +0x0C = PC +0x10   unread by both
#:     PS2 +0x10 = PC +0x14   Top 30
#:     PS2 +0x14 = PC +0x18   Best Rookies
#:
#: THREE INDEPENDENT CHECKS, none of them the PC analogy:
#:  1. `0x14 + 4 == 24` -- the read length closes exactly on the last field read,
#:     the same arithmetic that confirmed TM0IML (0x10 + 8*48 = 400) and TM0SML
#:     (0x08 + 16*48 = 776).
#:  2. The store block at `0x003fc1ac..0x003fc200` fans the five values into
#:     `0x005E3100 + id*4`, indexed at `0x003fd3d4` by the LIST ID -- giving
#:     id 0 and id 4 THE SAME field (VS. Ratings and Grand Total share a count,
#:     exactly as the PC does), id 3 (Prize Center) no field at all, and
#:     id 1/2/5 -> Top 30 / Best Rookies / Weekly.
#:  3. `+0x08` is guarded at `0x003fd518` by `sltu v0, 946080001` -- the PC's
#:     `t <= 946080000` "no tally period" sentinel to the second -- and is then
#:     fed `0xFFED8B00` (-14d) at `0x003fd568` and `0xFFF57400` (-8d) at
#:     `0x003fd598`. Same timestamp, same Tally Period arithmetic.
RKDATA_LEN_PS2 = 24
#: the PC word the PS2 build dropped; `to_ps2` is a slice, never a re-pack
RKDATA_PS2_SKIP = 4


def to_ps2(blob):
    """The PS2 view of a `b/g/TM0RkData` built by `build_rkdata`.

    A pure slice by design: both builds order the same fields identically, so
    re-packing here would create a second encoder that could drift from the
    first. See RKDATA_LEN_PS2 above for the measurements."""
    if blob is None:
        return None
    b = bytes(blob).ljust(RKDATA_LEN, b"\x00")
    return b[RKDATA_PS2_SKIP:RKDATA_PS2_SKIP + RKDATA_LEN_PS2]
#: `t <= this` skips the Tally Period line (rva 0x176A60)
RKDATA_STAMP_FLOOR = 946080000

#: RankID -> (menu index that reads it, label, sort key, cap). The menu index is
#: what picks the value field and the last-week byte, so it is the one number a
#: list file is really built for; menu 3 (Prize Center) reads no row fields and
#: rides menu 4's file.
#:
#: WARNING: TOP 30 AND BEST ROOKIES SORT ON `rating`, NOT `rating_last` (2026-09-13).
#: They are "last week's" lists, and the job publishes at Sunday 00:00 UTC, the
#: moment the tallied week closes -- so last week's rating IS the current one.
#: Sorting on `rating_last` (the copy the PREVIOUS publish saved) lagged a whole
#: week, and across the 09-07 rescale it ranked the 09-12 publish on OLD-scale
#: values: a 2-game player's 9.50 took #1, the champion pack and 1300 prize
#: points. `build_list` writes the same number into row+0xA4, the field those
#: two menus draw, so the column shown is the column sorted.
LISTS = {
    2: (0, "VS. Ratings", "rating", MAX_ROWS),
    3: (1, "Top 30", "rating", 30),
    4: (2, "Best Rookies", "rating", 10),
    0: (4, "Grand Total", "prize_total", MAX_ROWS),
    1: (5, "Weekly Total", "prize_week", MAX_ROWS),
}
#: the lists ranked on VS. Rating (menus 0/1/2), and the "last week" ones
RATED_LISTS = frozenset((2, 3, 4))
WEEKLY_LISTS = frozenset((3, 4))

BASE_PATH = "U/g/TM0_RANKLIST"


def path_for(rank_id):
    """The `<RF>` resource path for a list.

    ONE PATH PER LIST, and that is a decision worth keeping: the reply and the
    FETCH are answered by different containers (`authsess` speaks POLpro,
    `login` serves `3:0`), so nothing about "which
    list did they just ask for" can be carried between them in memory. Putting
    the id in the path makes the fetch self-describing and the two halves
    impossible to desynchronise.
    """
    return "%s%d" % (BASE_PATH, int(rank_id))


def is_list_path(path):
    """True for `U/g/TM0_RANKLIST` and `...RANKLIST<id>`. The bare name is the
    pre-2026-08-20 spelling and still resolves -- it is what `polpro.json`'s
    static `TM0:RR` fallback names when the live hook declines."""
    if path == BASE_PATH:
        return True
    tail = path[len(BASE_PATH):] if path.startswith(BASE_PATH) else ""
    return tail.isdigit() and int(tail) in LISTS


def rank_id_of(path):
    """The RankID a list path names, or None."""
    if not is_list_path(path):
        return None
    tail = path[len(BASE_PATH):]
    return int(tail) if tail else 2


def build_row(name="", cid=0, csid=0, rating=0, rating_last=0,
              prize_total=0, prize_week=0, hide_name=False, last_rank=None):
    """One 232-byte row.  `last_rank` is a {menu index: rank} mapping.

    A row carries EVERY list's numbers -- it is a player record, not a
    list-specific one -- so the same builder serves all five files and only the
    ORDER of the rows differs between them.
    """
    row = bytearray(REC)
    raw = name.encode("cp932", "replace") if isinstance(name, str) else bytes(name)
    raw = raw[:NAME_MAX - 1]
    row[NAME_OFF:NAME_OFF + len(raw)] = raw
    struct.pack_into("<I", row, CID_LO_OFF, int(cid) & 0xFFFFFFFF)
    struct.pack_into("<I", row, CID_HI_OFF, (int(cid) >> 32) & 0xFFFFFFFF)
    struct.pack_into("<I", row, CSID_OFF, int(csid) & 0xFFFFFFFF)
    struct.pack_into("<I", row, RATING_OFF, _u32(rating))
    struct.pack_into("<I", row, RATING_LAST_OFF, _u32(rating_last))
    struct.pack_into("<I", row, PRIZE_TOTAL_OFF, _u32(prize_total))
    struct.pack_into("<I", row, PRIZE_WEEK_OFF, _u32(prize_week))
    row[HIDE_NAME_OFF] = 1 if hide_name else 0
    for menu, off in LAST_RANK_OFF.items():
        v = int((last_rank or {}).get(menu, 0) or 0)
        row[off] = max(0, min(127, v))
    # THE +0xC2 TRAP (see the module banner): a zero here ends the VS. Ratings
    # list at this row. Menu 3 never reads it, so a floor of 1 costs nothing.
    if row[LAST_RANK_OFF[3]] == 0:
        row[LAST_RANK_OFF[3]] = 1
    return bytes(row)


def decode_row(blob):
    """The inverse of `build_row`, for reading a file back."""
    name = bytes(blob[NAME_OFF:NAME_OFF + NAME_MAX]).split(b"\x00")[0]
    lo, hi, csid = struct.unpack_from("<III", bytes(blob), CID_LO_OFF)
    return {
        "name": name.decode("cp932", "replace"),
        "cid": (hi << 32) | lo,
        "csid": csid,
        "rating": struct.unpack_from("<I", bytes(blob), RATING_OFF)[0],
        "rating_last": struct.unpack_from("<I", bytes(blob), RATING_LAST_OFF)[0],
        "prize_total": struct.unpack_from("<I", bytes(blob), PRIZE_TOTAL_OFF)[0],
        "prize_week": struct.unpack_from("<I", bytes(blob), PRIZE_WEEK_OFF)[0],
        "hide_name": bool(blob[HIDE_NAME_OFF]),
        "last_rank": {m: blob[o] for m, o in sorted(LAST_RANK_OFF.items())},
    }


def build_list(rank_id, players, since=None):
    """The file for one list: `ranked(rank_id, players, since)`, as rows.

    `players` is a list of dicts taking `build_row`'s keyword names plus the
    optional `rookie`, `games` and `last_played` that `_eligible` reads.

    Row+0xA4 ("last week's" rating, menus 1/2) gets `rating` too: see the
    LISTS banner -- at publish time they are the same number, and the stored
    `rating_last` is a week stale.
    """
    out = bytearray()
    for p in ranked(rank_id, players, since):
        out += build_row(
            p.get("name", ""), cid=p.get("cid", 0), csid=p.get("csid", 0),
            rating=p.get("rating", 0), rating_last=p.get("rating", 0),
            prize_total=p.get("prize_total", 0), prize_week=p.get("prize_week", 0),
            hide_name=p.get("hide_name", False), last_rank=p.get("last_rank"))
    return bytes(out)


def ranked(rank_id, players, since=None):
    """The players one list shows, in order: eligible, sorted DESCENDING on the
    list's key (ties keep their input order, which the client's tie rule then
    renders as equal ranks), capped.

    THE ONE SORT: `build_list` writes it, and `tools/tmrank.py`'s rollover
    stamps `last_rank`, the champion and the ranking prizes off it -- a second
    copy there is how the published list and the prizes could disagree.
    """
    _, _, key, cap = LISTS[int(rank_id)]
    rows = [p for p in players if _eligible(p, rank_id, since)]
    rows.sort(key=lambda p: _u32(p.get(key, 0)), reverse=True)
    return rows[:min(cap, MAX_ROWS)]


def min_games():
    """Games a member must have played before a VS. Rating list ranks them.

    OURS, not SE's: nothing in the client says what its server required. A
    career average over one or two games is noise, and with no floor it topped
    every rating list (periwinks, 2 games, 2026-09-12). `POL_TM_RANK_MIN_GAMES`
    moves the line; 0 turns it off."""
    try:
        return max(0, int(os.environ.get("POL_TM_RANK_MIN_GAMES", "5")))
    except ValueError:
        return 5


def tally_start(stamp):
    """The first second of the week a publish tallies, or None for no stamp.

    `stamp` is the NEXT update (`next_update`), and the client draws the period
    as `stamp - 14d .. stamp - 8d` (see the RKDATA banner), so the week began
    at `stamp - 14d`."""
    stamp = _int_or_zero(stamp)
    return stamp - 14 * 86400 if stamp > RKDATA_STAMP_FLOOR else None


def _eligible(player, rank_id, since=None):
    """Who a list may show. Three rules, each skipped when the player dict
    does not carry its key -- `load_players` always supplies them all, and a
    `--players` file or a test fixture without them must not come out empty.

    * BEST ROOKIES: the player's `rookie` flag. UNMEASURED -- Ranking.BIN says
      "last week's top 10 rookies" and nothing in the client says what makes a
      rookie; that was SE's server's business.
    * EVERY RATING LIST (menus 0/1/2): at least `min_games()` games.
    * TOP 30 AND BEST ROOKIES: played during the tallied week -- `last_played`
      at or after `since` (`tally_start`). They are "last week's top players";
      a member who has not played in weeks is not one, however good their
      career average (the 2026-09-12 publish crowned exactly that).
    """
    rid = int(rank_id)
    if rid == 4 and not bool(player.get("rookie", True)):
        return False
    if (rid in RATED_LISTS and player.get("games") is not None
            and _int_or_zero(player.get("games")) < min_games()):
        return False
    if (rid in WEEKLY_LISTS and since is not None
            and player.get("last_played") is not None
            and _int_or_zero(player.get("last_played")) < int(since)):
        return False
    return True


def build_rkdata(counts=None, stamp=0):
    """The 28-byte header. `counts` is {menu index: total players}."""
    counts = counts or {}
    blob = bytearray(RKDATA_LEN)
    struct.pack_into("<I", blob, 0x04, _u32(counts.get(0, counts.get(4, 0))))
    struct.pack_into("<I", blob, 0x08, _u32(counts.get(5, 0)))
    struct.pack_into("<I", blob, 0x0C, _u32(stamp))
    struct.pack_into("<I", blob, 0x14, _u32(counts.get(1, 0)))
    struct.pack_into("<I", blob, 0x18, _u32(counts.get(2, 0)))
    return bytes(blob)


def decode_rkdata(blob):
    f = struct.unpack_from("<7I", bytes(blob).ljust(RKDATA_LEN, b"\x00"))
    dated = f[3] > RKDATA_STAMP_FLOOR
    return {"players": {0: f[1], 4: f[1], 5: f[2], 1: f[5], 2: f[6]},
            "stamp": f[3],
            "tally_from": f[3] - 14 * 86400 if dated else 0,
            "tally_to": f[3] - 8 * 86400 if dated else 0}


#: 1970-01-01 was a THURSDAY, so a bare `now % 604800` puts week boundaries on
#: Thursdays. Four days of offset moves them to SUNDAY 00:00 UTC -- which is the
#: day SE's own screen named: "Next update: 01/02/2011 10:00 PST", and
#: 2011-01-02 was a Sunday. The tallied period then reads Sunday to Saturday.
_WEEK_PHASE = 4 * 86400


def next_update(now=None, period=7 * 86400, phase=_WEEK_PHASE):
    """A stamp for `build_rkdata`: the NEXT weekly rebuild.

    The client shows `t - 14d .. t - 8d`, so with `t` one period ahead of the
    last boundary `R` the window is exactly `[R - 7d, R - 1d]` -- the week that
    has just been tallied. Getting this backwards puts a period in the FUTURE on
    the screen, which is how it was caught.
    """
    now = int(now if now is not None else time.time())
    last = now - ((now + phase) % period)
    return last + period


# --- where a generated list lives -------------------------------------------
#
# NOT in the per-member resource store. `responders._resource_file` keys `U/g/`
# by the SESSION'S MEMBER, and a ranking list is the one thing on this server
# that is the same for everybody -- one copy per account is how `b/g/ZL` forked
# into four divergent versions across 17 accounts. So the generator writes ONE
# file per list under `<resources>/tmrank/`, which both containers see because
# `/data` is bind-mounted into all of them, and `responders._rank_list_blob`
# reads it there before it falls back to the shipped fixture.
#
# GENERATION IS A JOB, NOT A REQUEST HANDLER. The `<LN>` row count is answered
# by one container and the bytes are served by another, seconds later; a rebuild
# BETWEEN those two is a length mismatch, which is POL-5135's shape. SE rebuilt
# these weekly ("Next update: 01/02/2011 10:00 PST") and so do we -- run
# `tools/tmrank.py`, do not generate inline.
def store_dir():
    root = os.environ.get("POL_RESOURCE_DIR")
    if not root:
        root = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "resources")
    return os.path.join(root, "tmrank")


def store_file(path):
    return os.path.join(store_dir(), path.replace("/", "_") + ".bin")


def stored(path):
    """The generated blob for a ranking path, or None."""
    try:
        with open(store_file(path), "rb") as f:
            return f.read()
    except OSError:
        return None


def write_store(files):
    """{resource path: bytes} -> written filenames. Atomic per file."""
    os.makedirs(store_dir(), exist_ok=True)
    done = []
    for path, blob in files.items():
        dest = store_file(path)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, dest)
        done.append(dest)
    return done


def _u32(v):
    try:
        return max(0, min(int(v), 0xFFFFFFFF))
    except (TypeError, ValueError):
        return 0


# --- the player records the generator ranks ----------------------------------
#
# One member's ranking stats live beside their cards, in the same
# `<member>.tm_collection.json` the shop already writes
# (`tetramaster._collection_load`), under a `rank` key. Nothing on this server
# produces prize money or a VS rating yet -- there is no match engine -- so
# these default to zero and the screen honestly shows a list of real players
# with nothing won. When match results land they write here, and the next job
# run publishes them.
RANK_STATS = ("rating", "rating_last", "prize_total", "prize_week")

#: THE ROW'S IDENTITY IS THE MEMBER'S TETRA MASTER **CONTENT ID**, and this is
#: measured, not inferred. The client saves its character pool to us on POLpro
#: class P and the payload holds the pool verbatim -- three accounts out of our
#: own `authserv.log`:
#:
#:   <CR>(0x000000003B9ACA66,1000000102) <AN>(1000000102) <CI>(1,Card Level 0)
#:   <CR>(0x000000003B9ACCBE,1000000702) <AN>(1000000702) <CI>(1,Card Level 0)
#:   <CR>(0x000000003B9AD042,1000001602) <AN>(1000001602) <CI>(1,Card Level 116)
#:
#: The hex IS the decimal (`0x3B9ACA66 == 1000000102`), and every one of them is
#: the RETIRED computed mint's value for members 1, 7 and 16 (`1000000000 +
#: member*100 + content_code`). WARNING: Those exact numbers no longer exist: the
#: 2026-08-23 migration re-minted them as 8-digit serials, so read this capture
#: as the STRUCTURE it proves -- which field carries the identity -- and never as
#: values to compare against. So `<CR>` = (cid, cname) with
#: cname the same number as a string, `<AN>` = the account number, and `<CI>` =
#: (csid, cinfo) -- which is where `POOL_CSID` comes from, and the one part of
#: this that is a READING rather than a measurement.
#:
#: WHY IT MATTERS: `0x17545E` compares row+0x10/+0x14/+0x18 against the client's
#: own pool identity, and the row that matches becomes `Your Rank: <r>/<N>
#: players`. Get it wrong and the player is told `Did not rank` while their name
#: is on the screen; leave it ZERO and any client whose pool never loaded
#: matches whichever zero row came last. Neither is acceptable, and neither has
#: to happen -- we can compute this value for every member.
POOL_CONTENT_CODE = 2           #: Tetra Master, per the content-id map

#: WARNING: **RETRACTED, AND MEASURED THIS TIME: `csid` IS THE CONTENT ID AGAIN, NOT
#: 1.** This constant was `1` because `<CI>(1,"Card Level 0")` was read as
#: `(csid, cinfo)`. That cost a live test: every player was told "Did not rank"
#: while their own name was on the screen, because row+0x18 has to equal the
#: client's `0x524529C` and 1 never does. `<CI>`'s leading 1 is the message
#: builder's own index (`sqMgPfcSetCharacterPool` rva 0x1A5C20 passes esi = 0 to
#: the 0x1A3620 group writer) -- exactly the trap this project keeps paying for:
#: a literal matched without opening the function that emits it.
#:
#: WHAT IT REALLY IS. The pool struct at `0x5245298` is filled by polcore's
#: content-table getter (`0x14AD40`, vtable slot +0x2C4, the 64-slot table), so
#: it IS a lobby `1:3` record and its fields are ones WE author in
#: `responders._char_record`:
#:
#:   struct+0x00 flags        <- the loop's present bit
#:   struct+0x02 content code <- record+0x08   (2 = Tetra Master)
#:   struct+0x04 "Chara No"   <- record+0x0C   ** this is `csid` **
#:   struct+0x08/+0x0C cid    <- record+0x10/+0x14  ContentsID lo/hi
#:   struct+0x18 cname        <- record+0x18, the 15-byte Content ID STRING
#:
#: and `_char_record` writes the SAME `cid_num` into +0x0C, +0x10 and +0x14. So
#: csid == cid == the member's Content ID, which is why the client's own `<CR>`
#: reported cid and cname as the same number. Cross-checked against the live
#: `<CR>`: cname `"1000000102"` is struct+0x18, exactly what we put there.
#:
#: WARNING: `POL_CHAR_NUMS=0` in the login container makes `_char_record` leave all
#: three zero, and then the pool carries zeros and no row can match. That knob
#: and this default have to agree.
POOL_CSID_IS_CID = True


def pool_file():
    """Where the OBSERVED character pools are kept -- see `note_pool`."""
    return os.path.join(store_dir(), "pool.json")


def note_pool(member_id, cid=None, csid=None, cname=None, cinfo=None):
    """Record what a client told us its character pool holds.

    ECHOING BEATS DERIVING. `cid_for` computes the identity from the Content ID
    mint and that is measured right for every account we have seen -- but the
    client SENDS us the value on every `<CR>`, and a value we plant back
    verbatim cannot be wrong even if the mint changes under us. So the tally
    prefers what was observed and falls back to the formula for members who have
    not been seen since this landed.

    `cinfo` is the client's own status string (`Card Level 116`), which is a live
    per-player stat nothing else on this server records. No ranking column shows
    it -- it is kept because it is free and because a Card Level is exactly the
    sort of thing a later list will want.
    """
    mid = str(member_id or "").strip()
    if not mid:
        return False
    try:
        data = observed_pools()
        rec = dict(data.get(mid) or {})
        # `ci_index`, NOT `csid` -- see the POOL_CSID_IS_CID banner. It is the
        # group writer's own index and it is recorded only so the next reader
        # can see that it is always 1 and stop being tempted by it.
        for key, val in (("cid", cid), ("ci_index", csid),
                         ("cname", cname), ("cinfo", cinfo)):
            if val not in (None, ""):
                rec[key] = val
        lvl = _card_level(cinfo)
        if lvl is not None:
            rec["card_level"] = lvl
        if rec == (data.get(mid) or {}):
            return False
        data[mid] = rec
        os.makedirs(store_dir(), exist_ok=True)
        tmp = pool_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, pool_file())
        return True
    except OSError:
        return False


def observed_pools():
    try:
        with open(pool_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _card_level(cinfo):
    """`Card Level 116` -> 116. The string is the client's, so it is parsed
    leniently and a shape we do not recognise is simply not a level."""
    if not cinfo:
        return None
    m = re.search(r"(-?\d+)\s*$", str(cinfo))
    return int(m.group(1)) if m else None


#: The window the retired computed mint issued into. Mirrors
#: `accounts.RETIRED_CONTENT_ID_*`, duplicated rather than imported because this
#: module is also driven by `tools/tmrank.py` outside the services image.
_RETIRED_LO, _RETIRED_HI = 1_000_000_000, 1_999_999_999


def _int_or_zero(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def cid_for(member_id, content_id=None):
    """This member's pool cid: the STORED Content ID, or 0 if we do not have one.

    WARNING: **THE COMPUTED FALLBACK IS GONE (2026-08-23).** It used to return
    `1000000000 + member_id * 100 + POOL_CONTENT_CODE` when the caller had no
    stored value -- the retired mint, which was the correct answer for every
    account back when Content IDs were computed. They are ALLOCATED serials now
    (`accounts.allocate_content_id`) and the 10-digit ones have been migrated
    away (`tools/content_id_migrate.py`), so that formula is not "stale for new
    accounts" any more -- it is wrong for ALL of them, and a plausible wrong
    number here is the worst possible output: the ranking row is matched on this
    value, and a mismatch does not error, it tells the player **"Did not rank"
    while their own name is on the screen.**

    So it now returns **0**, which is honest, and 0 is a value the callers
    already understand as "no identity". Get the real one from
    `accounts.member_content_id(conn, member_id, POOL_CONTENT_CODE)` -- which is
    what `responders._tm_pool_note` and `tools/tmrank.py` both do -- or from the
    pool the client itself sent us (`note_pool`; an echoed value cannot be
    wrong). This function is now only a normaliser and a place for that rule to
    be written down.
    """
    if content_id:
        try:
            return int(str(content_id).strip())
        except ValueError:
            pass
    return 0


#: The per-member collection file `tetramaster._collection_file` writes. Named
#: here rather than imported because `responders` and `tools/tmrank.py` both
#: need to READ one and neither can import `tetramaster` -- it is a different
#: container's module and pulling 15k lines in for a filename is how a service
#: acquires a dependency it does not want.
COLLECTION_SUFFIX = ".tm_collection.json"


def collection_of(member_id, resource_dir=None):
    """One member's collection dict, or `{}`.

    The read-only twin of `tetramaster._collection_load`, for callers outside
    that container. `{}` covers every failure -- missing file, bad JSON, no
    member -- because every caller's answer to "we have no data" is the same:
    serve nothing rather than serve a default.
    """
    if member_id in (None, ""):
        return {}
    root = resource_dir or os.path.dirname(store_dir())
    try:
        with open(os.path.join(root, "%s%s" % (member_id, COLLECTION_SUFFIX)),
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: VS. Rating x100. 1.00 is the floor (a career of nothing), 4.00 the ceiling
#: (every tile of every board). The scale is the CLIENT'S: its Technical Rating
#: (TM.dll 0xCC72F) multiplies (400 - the average opponent rating), so a scale
#: that put ordinary players above 4.00 printed 0.00 on every result screen
#: (average tiles x100 did, 2026-08-22 .. 09-07). `POL_TM_RATING_SCALE=legacy`
#: restores that number for an A/B.
RATING_FLOOR = 100
RATING_SPAN = 300


def rating_of(blk):
    """The VS. Rating (x100) a `rank` block earns -- ONE formula, used by the
    result path, the save writer and `stats_of`, so no two surfaces can
    disagree. Blocks from before `tiles_total` was kept assume 16-tile boards."""
    try:
        games = int(blk.get("games") or 0)
        total = int(blk.get("score_total") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
    if games <= 0:
        return 0
    if (os.environ.get("POL_TM_RATING_SCALE") or "board").strip().lower() == "legacy":
        return max(0, total * 100 // games)
    # WARNING: THE DENOMINATOR MUST COVER THE SAME GAMES AS `score_total`
    # (2026-09-13). `tiles_total` only started on 09-07, so for anyone who
    # played before that it covered the last few games while `score_total`
    # covered all of them: one member's 441 tiles held over 185 board tiles is more
    # than the whole board and clipped to a flat 4.00. A game that never added
    # to `tiles_total` counts 16 tiles -- the assumption a block with no
    # `tiles_total` at all has always had.
    tiles = max(0, _int_or_zero(blk.get("tiles_total")))
    tiles += 16 * max(0, games - tiled_games_of(blk))
    tiles = tiles or 16 * games
    return RATING_FLOOR + max(0, min(RATING_SPAN, total * RATING_SPAN // tiles))


def tiled_games_of(blk):
    """How many of a block's `games` added to its `tiles_total`.

    Stored as `tiled_games` from 2026-09-13 (`tetramaster._bump_result_stats`).
    A block from before gets `tiles_total // 16`: exact while every tracked game
    was a 16-tile two-player board, a slight OVER-count once 25-tile boards are
    in (so that rating errs slightly high). Checked 09-13 against prod's nightly
    backups: exact for every member (185 = 10 x 16 + 25 -> 11, and
    128 -> 8, matching the games each played after 09-07)."""
    stored = blk.get("tiled_games")
    if stored is not None:
        return max(0, _int_or_zero(stored))
    tiles = max(0, _int_or_zero(blk.get("tiles_total")))
    return min(max(0, _int_or_zero(blk.get("games"))), tiles // 16)


def stats_of(collection):
    """The `rank` block of a collection dict, with every key defaulted.

    A block that carries games/score_total but no stored `rating` (results
    recorded before 2026-08-22 wrote only those two) gets the SAME value the
    result path writes now -- average score x100 -- so history counts instead
    of rendering 0.00 until the next game."""
    blk = collection.get("rank") if isinstance(collection, dict) else None
    blk = blk if isinstance(blk, dict) else {}
    out = {k: _u32(blk.get(k, 0)) for k in RANK_STATS}
    # The stored `rating` is a cache of `rating_of`; recompute whenever the
    # block can (games > 0) so a scale change reaches every player at once.
    _derived = rating_of(blk)
    if _derived:
        out["rating"] = _u32(_derived)
    # The two `_eligible` reads. A block with no `last_played` (nobody has
    # played since it was added, 2026-09-13) reads 0, i.e. not active.
    out["games"] = max(0, _int_or_zero(blk.get("games")))
    out["last_played"] = max(0, _int_or_zero(blk.get("last_played")))
    out["rookie"] = bool(blk.get("rookie", True))
    out["hide_name"] = bool(blk.get("hide_name", False))
    out["last_rank"] = {int(k): int(v) for k, v in
                        (blk.get("last_rank") or {}).items()}
    return out


def load_players(resource_dir=None, names=None, content_ids=None, rookies=None):
    """Every member with a collection file, as `build_list` records.

    `names` is {member id: display name}; a member we have no name for is
    SKIPPED, because a row with an empty name draws as a blank line (rva
    0x1762AD skips the delta marker on exactly that test) and a blank line is
    worse than one fewer player.

    `content_ids` is {member id: on-wire Content ID} where the caller has read
    the DB, and `rookies` is {member id: bool} from account age -- the only
    "Best Rookies" input this server can actually derive. A member absent from
    `rookies` keeps whatever their own record says, so an explicit
    `rank.rookie` is never overridden by a missing lookup.
    """
    root = resource_dir or os.path.dirname(store_dir())
    names = names or {}
    content_ids = content_ids or {}
    rookies = rookies or {}
    pools = observed_pools()
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    for fn in entries:
        if not fn.endswith(".tm_collection.json"):
            continue
        mid = fn[:-len(".tm_collection.json")]
        try:
            with open(os.path.join(root, fn)) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        name = str(names.get(str(mid), "")).strip()
        if not name:
            continue
        # OBSERVED FIRST, STORED SECOND -- see `note_pool`. A member whose
        # client has sent a `<CR>` gets the identity it told us, because a value
        # echoed back cannot be wrong.
        #
        # WARNING: EXCEPT WHEN IT IS STALE, which is what a migration makes every
        # echoed value (2026-08-23). A pool recorded before the ids were
        # re-minted holds a RETIRED 10-digit number, and preferring it over the
        # database would publish a row keyed to an identity that no longer
        # exists -- "Did not rank" with the player's own name on the screen,
        # which is the exact failure this identity chain exists to prevent. So
        # a retired-shaped observation is skipped and the STORED id wins; the
        # pool self-heals on that client's next `<CR>` anyway.
        seen = pools.get(str(mid)) or {}
        obs = seen.get("cid")
        stored = content_ids.get(str(mid))
        if obs and _RETIRED_LO <= _int_or_zero(obs) <= _RETIRED_HI and stored:
            # A stale observation, and we have something better. Drop it.
            print("tmrank: member %s's recorded pool cid %s is a RETIRED "
                  "Content ID; using the stored %s instead" % (mid, obs, stored))
            obs = None
        cid = cid_for(mid, obs or stored)
        if cid and _RETIRED_LO <= cid <= _RETIRED_HI:
            # Nothing better was available: the member has no stored Content ID
            # either. Left AS IT IS rather than zeroed, deliberately -- a zero
            # identity is matched by any client whose own pool failed to load
            # (see the POOL_CSID banner), so a retired number, which no live
            # client can ever send, is the safer of two wrong answers. Loud,
            # because it means that member has no `handle_content` link at all.
            print("tmrank: member %s publishes with RETIRED cid %s -- they have "
                  "NO Content ID linked for Tetra Master; that row cannot match "
                  "any client and the fix is a content link, not a tally rerun"
                  % (mid, cid))
        rec = {"member_id": mid, "name": name, "cid": cid,
               "csid": cid if POOL_CSID_IS_CID else 0}
        rec.update(stats_of(data))
        if str(mid) in rookies:
            rec["rookie"] = bool(rookies[str(mid)])
        out.append(rec)
    return out


def selftest():
    """Guard the offsets and the two traps that are silent when they break."""
    ok = [True]

    def check(cond, why):
        if not cond:
            ok[0] = False
            print("FAIL: %s" % why)

    row = build_row("Lex", cid=0xAB12CEB20D9067C4, csid=7, rating=296,
                    rating_last=250, prize_total=125000, prize_week=4000,
                    last_rank={0: 3, 4: 1})
    check(len(row) == REC, "a row is %d bytes, not %d" % (len(row), REC))
    back = decode_row(row)
    check(back["name"] == "Lex", "name round trip")
    check(back["cid"] == 0xAB12CEB20D9067C4 and back["csid"] == 7, "identity round trip")
    check(back["rating"] == 296 and back["rating_last"] == 250, "rating round trip")
    check(back["prize_total"] == 125000 and back["prize_week"] == 4000, "money round trip")
    check(back["last_rank"][0] == 3 and back["last_rank"][4] == 1, "last-week bytes")
    # THE +0xC2 TRAP: zero there ends the VS. Ratings list at this row.
    check(row[LAST_RANK_OFF[3]] != 0, "row+0xC2 must never be written as zero")
    check(bytes(build_row())[LAST_RANK_OFF[3]] != 0, "an EMPTY row must still set +0xC2")
    # A name must not reach the identity field.
    check(decode_row(build_row("X" * 40))["cid"] == 0, "a long name overran cid at +0x10")

    # THE CONTRACT, now that the computed fallback is retired. The three
    # accounts that MEASURED this identity (members 1, 7 and 16, straight off
    # authserv.log, `<CR>(0x3B9ACA66,1000000102)` and friends) are what used to
    # be pinned here as `cid_for(member) == 1000000102`; that formula is gone
    # and those ids have been migrated, so pinning them would now pin a value
    # nothing issues. What has to hold instead is the rule that replaced it:
    # the stored id wins, and the ABSENCE of one is 0 rather than a guess.
    check(cid_for(1) == 0 and cid_for(16) == 0,
          "no stored id must yield 0, not a computed guess")
    check(cid_for(1, "30001234") == 30001234, "a stored Content ID must win")
    check(cid_for(1, 30001234) == 30001234, "...as an int too")
    check(cid_for(1, "  30001234 ") == 30001234, "...and is whitespace-tolerant")
    check(cid_for(1, "not-a-number") == 0, "an unparseable stored id is 0, not a crash")
    # THE "Did not rank for everyone" BUG: row+0x18 is the pool's "Chara No",
    # which `responders._char_record` fills with the SAME Content ID it puts in
    # the cid pair -- not `<CI>`'s leading 1.
    # (These two use 10-digit sample values on purpose: the row codec must be
    # indifferent to how many digits an id has, and a number WIDER than anything
    # we now issue is the stronger test of that. They are not live ids.)
    ident = build_list(2, [{"name": "Lex", "cid": 1000000902,
                            "csid": 1000000902}])
    got = decode_row(ident)
    check(got["cid"] == got["csid"] == 1000000902,
          "csid must equal the Content ID, got %r" % (got["csid"],))
    check(decode_row(build_row("x", cid=1000000102))["cid"] == 1000000102,
          "cid must survive the row round trip")

    players = [{"name": "A", "rating": 100, "prize_total": 9},
               {"name": "B", "rating": 900, "prize_total": 1},
               {"name": "C", "rating": 100, "prize_total": 5, "rookie": False}]
    vs = build_list(2, players)          # menu 0, ranked on `rating`
    order = [decode_row(vs[i * REC:(i + 1) * REC])["name"]
             for i in range(len(vs) // REC)]
    check(order == ["B", "A", "C"], "VS. Ratings order is %r" % order)
    money = build_list(0, players)       # menu 4, ranked on `prize_total`
    order = [decode_row(money[i * REC:(i + 1) * REC])["name"]
             for i in range(len(money) // REC)]
    check(order == ["A", "C", "B"], "Grand Total order is %r" % order)
    check(len(build_list(4, players)) // REC == 2, "Best Rookies must drop rookie=False")
    check(len(build_list(3, [{"name": "n%d" % i} for i in range(50)])) // REC == 30,
          "Top 30 must cap at 30")
    check(len(build_list(2, [{"name": "n%d" % i} for i in range(200)])) // REC == MAX_ROWS,
          "no list may exceed 0x8AF00's 100 rows")

    def names(blob):
        return [decode_row(blob[i * REC:(i + 1) * REC])["name"]
                for i in range(len(blob) // REC)]

    # WARNING: 2026-09-13, the periwinks publish. Top 30 sorts on the rating AT
    # PUBLISH and draws it at row+0xA4 -- never the week-stale `rating_last`
    # an old-scale 9.50 rode to #1.
    stale = [{"name": "Lap", "rating": 200, "rating_last": 950},
             {"name": "Lex", "rating": 228, "rating_last": 713}]
    t30 = build_list(3, stale)
    check(names(t30) == ["Lex", "Lap"],
          "Top 30 must sort on `rating`, not `rating_last`: %r" % names(t30))
    check(decode_row(t30[:REC])["rating_last"] == 228,
          "row+0xA4 must carry the rating the list was sorted on")
    saved_min = os.environ.get("POL_TM_RANK_MIN_GAMES")
    os.environ["POL_TM_RANK_MIN_GAMES"] = "5"
    try:
        few = [{"name": "Lap", "rating": 278, "games": 2},
               {"name": "Lex", "rating": 228, "games": 64}]
        check(names(build_list(2, few)) == ["Lex"],
              "a 2-game career must not be ranked on a rating list")
        check(names(build_list(0, few)) == ["Lap", "Lex"],
              "...but the money lists have no games floor")
        act = [{"name": "Old", "rating": 300, "games": 9, "last_played": 999},
               {"name": "New", "rating": 200, "games": 9, "last_played": 1000},
               {"name": "Never", "rating": 390, "games": 9, "last_played": 0}]
        check(names(build_list(3, act, since=1000)) == ["New"],
              "Top 30 ranks only members who played in the tallied week")
        check(names(build_list(4, act, since=1000)) == ["New"],
              "...and so does Best Rookies")
        check(names(build_list(2, act, since=1000)) == ["Never", "Old", "New"],
              "VS. Ratings has no activity rule")
        os.environ["POL_TM_RANK_MIN_GAMES"] = "0"
        check(names(build_list(2, few)) == ["Lap", "Lex"],
              "POL_TM_RANK_MIN_GAMES=0 turns the floor off")
    finally:
        if saved_min is None:
            os.environ.pop("POL_TM_RANK_MIN_GAMES", None)
        else:
            os.environ["POL_TM_RANK_MIN_GAMES"] = saved_min
    check(tally_start(0) is None, "no stamp, no tally window")
    check(tally_start(next_update(1787198400))
          == decode_rkdata(build_rkdata({}, next_update(1787198400)))["tally_from"],
          "tally_start must be the Tally Period's first day the client draws")
    # WARNING: A MIXED CAREER: tiles_total started 09-07, score_total did not.
    # A real 09-13 block read 4.00 (441 over 185 tiles, clipped).
    lex = {"games": 64, "score_total": 441, "tiles_total": 185}
    check(tiled_games_of(lex) == 11, "185 tiles = 11 tracked games")
    check(rating_of(lex) == 228,
          "untracked games must count 16 tiles each: got %d" % rating_of(lex))
    check(rating_of(dict(lex, tiled_games=11)) == 228,
          "a stored tiled_games gives the same answer")
    check(rating_of({"games": 2, "score_total": 19}) == 278,
          "a block with no tiles_total is unchanged (16 per game)")
    check(rating_of({"games": 1, "score_total": 8, "tiles_total": 16,
                     "tiled_games": 1}) == 250,
          "a fully tracked block is unchanged")
    st = stats_of({"rank": {"games": 3, "last_played": 1234}})
    check((st["games"], st["last_played"]) == (3, 1234),
          "stats_of must hand _eligible games and last_played")
    check(stats_of({})["last_played"] == 0, "never played = 0, not active")

    hdr = build_rkdata({0: 12, 5: 12, 1: 30, 2: 10}, next_update(1787198400))
    check(len(hdr) == RKDATA_LEN, "the header is %d bytes, not %d" % (len(hdr), RKDATA_LEN))
    d = decode_rkdata(hdr)
    check(d["players"] == {0: 12, 4: 12, 5: 12, 1: 30, 2: 10}, "header counts")
    check(d["tally_to"] - d["tally_from"] == 6 * 86400, "the tally window is not 7 days")
    # The window must be in the PAST, which is the bug this catches.
    check(d["tally_to"] < 1787198400, "the tally period is in the FUTURE")
    check(decode_rkdata(build_rkdata({}, 0))["tally_from"] == 0,
          "stamp 0 must read as 'no period'")

    print("selftest OK" if ok[0] else "SELFTEST FAILED")
    return 0 if ok[0] else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(selftest() if "--selftest" in sys.argv else
                     print(__doc__) or 0)
