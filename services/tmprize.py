#!/usr/bin/env python3
"""Tetra Master's PRIZE POINTS -- the second currency, and the LUCKY CARDS.

WHAT THIS IS. `Money` buys packs; **prize points** buy the 30 Prize Center
cards, and nothing converts between them (`Cardsh.BIN` "not enough money to buy
this pack" vs `Ranking.BIN` "not enough prize points to exchange for this
prize"). Points have exactly TWO faucets, both weekly, and SE's server decided
both:

    Top 30      last week's top 30 by VS. Rating   RankingPrize.BIN[0..29]
    Best Rookie last week's top 10 rookies         RankingPrize.BIN[30..39]
    Lucky cards three card ids, announced weekly   "a maximum of 30 points"

This module owns the currency (balance, the weekly tallies, who has been paid)
and builds `@ExcInit=`, the Prize Center's opener. It is PURE except for one
JSON file per member, so `selftest()` drives a whole year through it.

WHAT IS MEASURED AND WHAT IS OURS
---------------------------------
Measured, off the unpacked TM.dll and the shipped `data/*.BIN`:

  * the wire (`excinit_body` below);
  * the three award slots are PENDING -- the client credits them only when the
    player presses OK on the notice screen (rva 0x178EC1) and zeroes them;
  * the balance saturates FLAT at 99,999 (`0x1869F`), NOT money's 99,999,999;
  * `RANKING_PRIZE_*`, verbatim from `data/RankingPrize.BIN`;
  * there are exactly THREE lucky cards (the loop at 0x1055C7 is fixed at 3)
    and the client's own text caps the award at "a maximum of 30 points".

OURS, not SE's -- because SE never shipped the rule to the client, and the one
page that wrote it down (`pml/game/tetra/guidebook/manual/mapm30.pml`) is not in
the mirror and not reachable:

  * WHICH three cards a week gets. `pick_lucky` is a deterministic draw from the
    card table, seeded by the week -- so every container computes the same three
    without sharing state, which matters because the responders are two
    processes. It is a POLICY. Change `pick_lucky` and the whole game changes;
    do not scatter a second copy of it.
  * WHAT a lucky card pays. `POL_TM_LUCKY_POINTS` a card, capped at
    `POL_TM_LUCKY_CAP`. Only the cap of 30 and the count of 3 are SE's; 10 a
    card is the obvious factorisation of "three cards, a maximum of 30" and is
    a knob, not a fact.
  * that "have these cards" means TOOK one in a match that week. `Playdat.BIN`
    83 is "lucky cards TAKEN this week" -- the client's own statistic counts
    takes, not holdings. That is the strongest signal available and it is still
    an inference.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
  * **It does not pay the ranking faucets.** `ranking_award()` prices a rank off
    the shipped ladder, and `announce()` carries whatever a caller passes in
    `top30` / `rookie`, but nothing computes last week's standings yet -- the
    rank lists are built by `tools/tmrank.py` from a players file, and wiring
    that job to a payment is separate work. Both slots default to 0, which is
    the one value that cannot be mistaken for a measurement.
  * **It does not debit purchases.** The Prize Center's own debit is client-side
    (`balance -= price` at rva 0x179338) and no message tells us it happened, so
    our balance drifts UP relative to the client's the moment a player spends.
    `spend()` is here for when the Prize Center's buy arm is read; until then
    the drift is real and known.

AND THE ONE THAT BITES: `announce()` PAYS. It credits our balance and marks the
week paid in the same breath, because the client's OK is client-local and never
reaches us. Announce the same week twice and the player is paid twice, so every
path to the notice screen must go through `announce()` -- never build the body
by hand.
"""
import hashlib
import json
import os
import struct
import time

import tm_cardprm

#: `data/RankingPrize.BIN`, 40 u32s, verbatim: 30 for the Top 30 ladder then 10
#: for Best Rookies. Decoded with the BIN extractor; do not retype these.
RANKING_PRIZE_TOP30 = (1000, 750, 600, 525, 465, 415, 390, 365, 340, 315,
                       290, 275, 260, 245, 230, 215, 200, 185, 170, 155,
                       140, 130, 120, 110, 100, 90, 80, 70, 60, 50)
RANKING_PRIZE_ROOKIE = (300, 250, 225, 200, 180, 160, 145, 130, 115, 100)

#: The client caps the BALANCE at 99,999 and writes the cap flat on overflow
#: (rva 0x178EF5 / 0x178F30). Ours must agree or the two diverge on the first
#: rich player: we would keep counting past a number the client refuses to hold.
BALANCE_MAX = 0x1869F

#: How many lucky cards a week has. Fixed at 3 by the client's own loop.
LUCKY_CARDS = 3


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)), 0)
    except (TypeError, ValueError):
        return default


def _points_per_card():
    """POLICY, NOT MEASUREMENT -- see the banner. 3 x 10 = the 30 the client's
    own text promises, which is why 10 is the default rather than a rounder
    guess."""
    return _env_int("POL_TM_LUCKY_POINTS", 10)


def _award_cap():
    return _env_int("POL_TM_LUCKY_CAP", 30)


# --------------------------------------------------------------------------
# 1. THE WEEK
# --------------------------------------------------------------------------
#
# The same boundary the rankings use, and for the same reason: SE's screen said
# "Next update: 01/02/2011 10:00 PST" and 2011-01-02 was a SUNDAY. `tmrank`
# derives it as `now - ((now + 4d) % 7d)`; this is that expression, kept local
# so a lucky-card tally cannot import a ranking-list builder just to know what
# day it is. The two MUST agree -- `selftest` checks them against each other
# when tmrank is importable.
WEEK = 7 * 86400
WEEK_PHASE = 4 * 86400


def week_start(now=None):
    """UNIX time of the Sunday 00:00 UTC that opened the week `now` is in."""
    now = int(now if now is not None else time.time())
    return now - ((now + WEEK_PHASE) % WEEK)


def week_id(now=None):
    """The week as a small integer.

    A stamp would work as a dict key too, but this is what goes in the JSON and
    in the seed, and an integer week reads correctly in a log and cannot be
    confused with "the moment something happened".
    """
    return week_start(now) // WEEK


def week_start_of(week):
    """The Sunday 00:00 UTC that OPENS week `week` -- the inverse of `week_id`.

    WARNING: NOT `week * WEEK`. A week id is `week_start // WEEK` and `week_start` is
    congruent to `-WEEK_PHASE` (mod WEEK), so the multiplication lands four days
    EARLY -- on the Wednesday/Thursday before. `--cards` printed exactly that
    and labelled the current week 2026-08-13 when it began on the 16th, which
    is the sort of off-by-a-phase that makes a correct draw look wrong.
    """
    return int(week) * WEEK + (WEEK - WEEK_PHASE)


def last_week(now=None):
    """The week that has just CLOSED -- the one a Sunday payout is for."""
    return week_id(now) - 1


# --------------------------------------------------------------------------
# 2. THE DRAW
# --------------------------------------------------------------------------

def lucky_pool():
    """Which card ids can be lucky. Every card the client knows, by default.

    A function and not a constant so that a narrower pool -- "nothing above
    level N", say -- is one edit in one place if the server owner wants one.
    `POL_TM_LUCKY_POOL` is a comma- or space-separated id list.
    """
    spec = os.environ.get("POL_TM_LUCKY_POOL")
    if spec:
        out = []
        for part in spec.replace(",", " ").split():
            try:
                cid = int(part, 0)
            except ValueError:
                continue
            if 0 <= cid < tm_cardprm.CARD_COUNT and cid not in out:
                out.append(cid)
        if len(out) >= LUCKY_CARDS:
            return out
    return list(range(tm_cardprm.CARD_COUNT))


def pick_lucky(week=None, now=None, count=LUCKY_CARDS):
    """The week's lucky cards: `count` distinct ids, deterministically.

    DETERMINISTIC ON PURPOSE. There is no shared "this week's cards" record and
    there must not be one: `login` and `authsess` are separate processes, so
    anything that ROLLS a value has two answers. A hash of the week is the same
    in both, needs no storage, and replays a past week exactly -- which is how
    `selftest` checks a year of them without a clock.

    SHA-256 and not `random.seed()`: `hash()` is salted per process and the
    stdlib generator's stream is not contracted to be stable across versions.
    """
    week = week_id(now) if week is None else int(week)
    pool = lucky_pool()
    out = []
    counter = 0
    while len(out) < count and counter < 4096:
        digest = hashlib.sha256(b"tetra-lucky/%d/%d" % (week, counter)).digest()
        pick = pool[struct.unpack_from(">I", digest)[0] % len(pool)]
        if pick not in out:
            out.append(pick)
        counter += 1
    return out


def lucky_names(ids):
    return [tm_cardprm.name(c) for c in ids]


def ranking_award(rank, rookie=False):
    """Points for finishing `rank` (1-based) on a weekly board, 0 if unplaced."""
    ladder = RANKING_PRIZE_ROOKIE if rookie else RANKING_PRIZE_TOP30
    if not isinstance(rank, int) or not 1 <= rank <= len(ladder):
        return 0
    return ladder[rank - 1]


def lucky_award(takes):
    """What `takes` lucky cards in one week pays. Capped, per the client text."""
    return min(_award_cap(), max(0, int(takes)) * _points_per_card())


# --------------------------------------------------------------------------
# 3. THE PER-MEMBER RECORD
# --------------------------------------------------------------------------
#
# Beside the collection JSON and for the reason `_collection_file` gives: that
# directory is the bind-mounted per-member state every container already sees.
# Its own suffix, so it can never collide with a POL resource path or with the
# collection.
SUFFIX = ".tm_prize.json"


def blank():
    """An EMPTY record, and every key one can hold.

    Written out in full on the first save so a hand-edited file has something to
    copy, and so a missing key is a bug here rather than a KeyError three call
    sites away.
    """
    return {"points": 0,            # the spendable balance, our copy
            "week": 0,              # which week `takes` counts
            "takes": 0,             # lucky cards TAKEN this week
            "take_ids": [],         # ...and which, for the log and for tests
            "paid": [],             # weeks already announced (most recent last)
            "acquired": 0,          # lifetime total -- Playdat's own statistic
            # THE RANKING FAUCETS (wired 2026-09-04). The weekly publish
            # (`tools/tmrank.py --publish`, `pol-tmrank.timer`) GRANTS a
            # Top 30 / Best Rookie award here; the Prize Center's next open
            # PAYS it through `announce` (the client adds the /PP= slots to
            # the balance it is handed, so the grant must ride that message
            # and not be credited silently). `rank_paid` remembers which
            # publish ids were paid, so a re-run of the publish cannot pay
            # twice and a re-opened Prize Center cannot either.
            "pending": {},          # {"week": publish id, "top30": pts, "rookie": pts}
            "rank_paid": []}        # publish ids already paid (most recent last)


def grant_ranking(member_id, publish_id, top30_rank=None, rookie_rank=None,
                  say=None, replace=False):
    """Record last week's ranking award for one member, to be paid on their
    next Prize Center open. `publish_id` identifies the weekly publish (the
    week id); a member already granted or paid for it is left alone.
    Returns the (top30, rookie) points granted, or None if nothing changed.

    `replace=True` is for CORRECTING a publish (`tools/tmrank.py --correct`):
    an UNPAID grant for the same publish is overwritten -- or withdrawn, when
    the corrected lists no longer place the member. A PAID one is never
    touched: the client has already added those points to its balance."""
    t30 = ranking_award(top30_rank or 0)
    rk = ranking_award(rookie_rank or 0, rookie=True)
    if not (t30 or rk) and not replace:
        return None
    data = load(member_id)
    pid = str(publish_id)
    if pid in (data.get("rank_paid") or []):
        return None
    pending = data.get("pending") or {}
    if str(pending.get("week")) == pid:
        if not replace or (int(pending.get("top30") or 0) == t30
                           and int(pending.get("rookie") or 0) == rk):
            return None
    elif not (t30 or rk):
        return None
    if not (t30 or rk):
        data["pending"] = {}
        if not store(member_id, data):
            return None
        if say:
            say("tm: member %s's UNPAID ranking grant for publish %s WITHDRAWN "
                "(was Top 30 %s, Best Rookie %s)" % (member_id, pid,
                pending.get("top30"), pending.get("rookie")))
        return 0, 0
    data["pending"] = {"week": pid, "top30": int(t30), "rookie": int(rk)}
    if not store(member_id, data):
        return None
    if say:
        say("tm: member %s GRANTED ranking prize points for publish %s: Top 30 "
            "rank %s -> %d, Best Rookie rank %s -> %d (paid at the next Prize "
            "Center open)" % (member_id, pid, top30_rank, t30, rookie_rank, rk))
    return int(t30), int(rk)


def _dir():
    root = os.environ.get("POL_RESOURCE_DIR")
    if not root:
        root = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "resources")
    return root


def path_for(member_id):
    if member_id is None:
        return None
    return os.path.join(_dir(), "%s%s" % (member_id, SUFFIX))


def load(member_id):
    path = path_for(member_id)
    data = blank()
    if not path or not os.path.exists(path):
        return data
    try:
        with open(path, "r") as f:
            got = json.load(f)
    except (OSError, ValueError):
        # A corrupt record must not take the Prize Center down with it: the
        # player loses a tally, not the screen.
        return data
    if isinstance(got, dict):
        data.update({k: v for k, v in got.items() if k in data})
    return data


def store(member_id, data):
    path = path_for(member_id)
    if not path:
        return False
    try:
        os.makedirs(_dir(), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _roll_week(data, now=None):
    """Move the tally onto the current week, discarding a stale one.

    The counters are PER WEEK. Not rolling them is how a player who took two
    lucky cards in March gets paid for them again every week until Christmas.
    """
    week = week_id(now)
    if data.get("week") != week:
        data["week"] = week
        data["takes"] = 0
        data["take_ids"] = []
    return data


# --------------------------------------------------------------------------
# 4. TALLYING, AND PAYING
# --------------------------------------------------------------------------

def record_takes(member_id, card_ids, now=None, say=None):
    """Count the lucky cards in `card_ids` against this week's tally.

    `card_ids` is what one match handed the player -- the cards they TOOK, not
    the cards they hold. Returns how many were lucky.
    """
    lucky = set(pick_lucky(now=now))
    hit = [int(c) for c in card_ids if int(c) in lucky]
    if not hit:
        return 0
    data = _roll_week(load(member_id), now)
    data["takes"] = int(data.get("takes") or 0) + len(hit)
    data["take_ids"] = list(data.get("take_ids") or []) + hit
    store(member_id, data)
    if say:
        say("tm: member %s took %d LUCKY card(s) this week (%s) -- %d total, "
            "worth %d pt at the Prize Center"
            % (member_id, len(hit),
               ", ".join(tm_cardprm.name(c) for c in hit),
               data["takes"], lucky_award(data["takes"])))
    return len(hit)


def pending_lucky(member_id, now=None):
    """What this member is owed for lucky cards, 0 if the week is already paid.

    Pays for the CURRENT week's tally, not last week's, and that is a decision:
    SE's notice is a weekly payout, but our record has no separate "last week's
    takes" slot and inventing one would mean two counters that can disagree. A
    player who takes a lucky card and walks into the Prize Center five minutes
    later gets paid for it, once. Change this and change `_roll_week` with it.
    """
    data = _roll_week(load(member_id), now)
    if data["week"] in (data.get("paid") or []):
        return 0
    return lucky_award(data.get("takes") or 0)


def announce(member_id, now=None, top30=0, rookie=0, say=None):
    """The four `/PP=` numbers -- and PAY them.

    THIS IS THE PAYING CALL. It credits our balance and marks the week paid
    before returning, because the client's OK never reaches us; the pending
    slots we return are what the client will ADD to the balance it was handed,
    so the two agree only if we hand over the OLD balance and the award beside
    it. Returns `(balance, top30, lucky, rookie)` in `/PP=` order.
    """
    data = _roll_week(load(member_id), now)
    already = data["week"] in (data.get("paid") or [])
    lucky = 0 if already else lucky_award(data.get("takes") or 0)
    before = int(data.get("points") or 0)
    # THE RANKING GRANT rides this open (see `grant_ranking`): explicit
    # arguments win, else whatever the weekly publish left pending.
    pending = data.get("pending") or {}
    rank_id = None
    if pending and str(pending.get("week")) not in (data.get("rank_paid") or []):
        rank_id = str(pending.get("week"))
        top30 = int(top30 or 0) or int(pending.get("top30") or 0)
        rookie = int(rookie or 0) or int(pending.get("rookie") or 0)
    total = lucky + max(0, int(top30)) + max(0, int(rookie))
    if total:
        data["points"] = min(BALANCE_MAX, before + total)
        data["acquired"] = int(data.get("acquired") or 0) + total
        if lucky:
            # Keep a year of stamps, not the whole history: this list is only
            # ever asked "is THIS week in it".
            data["paid"] = (data.get("paid") or [])[-51:] + [data["week"]]
        if rank_id is not None:
            data["rank_paid"] = (data.get("rank_paid") or [])[-51:] + [rank_id]
            data["pending"] = {}
        store(member_id, data)
        if say:
            say("tm: member %s PAID %d prize point(s) (lucky %d, top30 %d, "
                "rookie %d) -- balance %d -> %d%s"
                % (member_id, total, lucky, top30, rookie, before,
                   data["points"],
                   " (CAPPED at %d)" % BALANCE_MAX
                   if before + total > BALANCE_MAX else ""))
    return before, max(0, int(top30)), lucky, max(0, int(rookie))


def spend(member_id, points, say=None):
    """Debit a purchase. Refuses to go negative; returns the new balance or None.

    Nothing calls this yet -- the Prize Center's debit is client-side and it
    does not tell us. It is here so that when that arm is read there is one
    place that owns the subtraction.
    """
    data = load(member_id)
    have = int(data.get("points") or 0)
    if points < 0 or have < points:
        if say:
            say("tm: member %s cannot spend %s prize point(s) -- holds %d"
                % (member_id, points, have))
        return None
    data["points"] = have - int(points)
    store(member_id, data)
    return data["points"]


# --------------------------------------------------------------------------
# 5. THE WIRE
# --------------------------------------------------------------------------

def excinit_body(member_id, shop_no=1, now=None, top30=0, rookie=0, say=None):
    """`@ExcInit=` -- the Prize Center's opener, arm rva 0x1054EE.

    ONE `/PP=` AND ONE `/LC=`, PIPE-SEPARATED. `0xAB080`'s index walks `"|"`
    inside a single field's value; four `/PP=` fields in a row would make every
    indexed read return the FIRST one, which is the bug that once looked like
    "the client only accepts card ids 0..3". See the multi-value banner in
    `tetramaster.py`.

        @ExcInit=/N=<shop>/PP=<balance>|<top30>|<lucky>|<rookie>/LC=<a>|<b>|<c>
    """
    bal, t30, lucky, rook = announce(member_id, now=now, top30=top30,
                                     rookie=rookie, say=say)
    ids = pick_lucky(now=now)
    return (b"@ExcInit=/N=%d/PP=%d|%d|%d|%d/LC=%s"
            % (int(shop_no), bal, t30, lucky, rook,
               b"|".join(b"%d" % c for c in ids)))


# --------------------------------------------------------------------------
# 6. SELFTEST
# --------------------------------------------------------------------------

def selftest(say=print):
    """Every assertion here was checked to FAIL with its rule broken."""
    import shutil
    import tempfile
    ok = True

    def check(cond, what):
        if not cond:
            say("FAIL: " + what)
        return bool(cond)

    # -- the ladder is the shipped file, not a retyped one -------------------
    ok &= check(len(RANKING_PRIZE_TOP30) == 30 and len(RANKING_PRIZE_ROOKIE) == 10,
                "RankingPrize.BIN splits 30 + 10")
    ok &= check(RANKING_PRIZE_TOP30[0] == 1000 and RANKING_PRIZE_TOP30[-1] == 50,
                "the Top 30 ladder runs 1000 -> 50")
    ok &= check(list(RANKING_PRIZE_TOP30) == sorted(RANKING_PRIZE_TOP30, reverse=True),
                "the Top 30 ladder must never rise with rank")
    ok &= check(ranking_award(1) == 1000 and ranking_award(30) == 50
                and ranking_award(31) == 0 and ranking_award(0) == 0,
                "ranking_award is 1-based and 0 outside the ladder")
    ok &= check(ranking_award(1, rookie=True) == 300
                and ranking_award(11, rookie=True) == 0,
                "the rookie ladder is ten places, top 300")

    # -- the week agrees with the ranking generator's ------------------------
    # 2011-01-02 was SE's own "next update" day, and it was a Sunday.
    sunday = 1293926400                       # 2011-01-02 00:00:00 UTC
    ok &= check(week_start(sunday) == sunday,
                "a Sunday 00:00 UTC IS a week boundary")
    ok &= check(week_start(sunday + 86399) == sunday
                and week_start(sunday - 1) == sunday - WEEK,
                "the boundary holds across the whole week")
    ok &= check(week_id(sunday + WEEK) == week_id(sunday) + 1,
                "consecutive weeks get consecutive ids")
    ok &= check(last_week(sunday) == week_id(sunday) - 1, "last_week is behind")
    ok &= check(week_start_of(week_id(sunday)) == sunday,
                "week_start_of is the INVERSE of week_id -- `week * WEEK` is "
                "four days early and that is what --cards was printing")
    for _t in (sunday, sunday + 1, sunday + WEEK - 1, sunday + 3 * WEEK + 4242):
        ok &= check(week_start_of(week_id(_t)) == week_start(_t),
                    "week_start_of round-trips at t=%d" % _t)
        ok &= check(time.gmtime(week_start_of(week_id(_t))).tm_wday == 6,
                    "every week must open on a SUNDAY (tm_wday 6), t=%d" % _t)
    try:
        import tmrank
        ok &= check(tmrank.next_update(sunday + 10) == sunday + WEEK,
                    "tmrank's next_update must agree with week_start -- two "
                    "different Sundays is two different games")
    except ImportError:                                      # pragma: no cover
        say("tm: tmrank not importable here; week agreement NOT checked")

    # -- the draw ------------------------------------------------------------
    a, b = pick_lucky(week=2000), pick_lucky(week=2000)
    ok &= check(a == b, "the same week must draw the same three cards")
    ok &= check(len(a) == LUCKY_CARDS == 3 and len(set(a)) == 3,
                "three DISTINCT lucky cards")
    ok &= check(all(0 <= c < tm_cardprm.CARD_COUNT for c in a),
                "every lucky card must be a real card id")
    weeks = [tuple(pick_lucky(week=w)) for w in range(2000, 2104)]
    ok &= check(len(set(weeks)) > 100,
                "two years of draws must not collapse onto a few triples")
    # COVERAGE, against the coupon-collector expectation and not a wish: 104
    # weeks is 312 draws over 250 cards, so ~178 distinct is CORRECT and a
    # threshold of 200 would fail a perfectly uniform draw (it did). What a
    # biased draw cannot do is reach the WHOLE table given enough weeks.
    seen = set()
    for w in range(2000, 3000):
        seen.update(pick_lucky(week=w))
    ok &= check(len(seen) == tm_cardprm.CARD_COUNT,
                "1000 weeks must draw every card at least once -- a draw that "
                "cannot reach a card is a draw with a hole in it (%d of %d)"
                % (len(seen), tm_cardprm.CARD_COUNT))
    ok &= check(len(set(weeks[0]) | set(weeks[1]) | set(weeks[2])) >= 7,
                "consecutive weeks must not keep drawing the same cards")
    os.environ["POL_TM_LUCKY_POOL"] = "1,2,3,4"
    try:
        pooled = pick_lucky(week=2000)
        ok &= check(len(pooled) == 3 and set(pooled) <= {1, 2, 3, 4},
                    "POL_TM_LUCKY_POOL must actually narrow the draw")
    finally:
        del os.environ["POL_TM_LUCKY_POOL"]

    # -- the award -----------------------------------------------------------
    ok &= check(lucky_award(0) == 0 and lucky_award(1) == 10
                and lucky_award(3) == 30 and lucky_award(9) == 30,
                "10 a card, capped at the 30 the client's text promises")

    # -- the record, on a real temp directory --------------------------------
    root = tempfile.mkdtemp(prefix="tmprize-")
    old = os.environ.get("POL_RESOURCE_DIR")
    os.environ["POL_RESOURCE_DIR"] = root
    try:
        now = sunday + 3600                   # Sunday 01:00, week W
        lucky_ids = pick_lucky(now=now)
        other = next(c for c in range(tm_cardprm.CARD_COUNT)
                     if c not in lucky_ids)

        ok &= check(record_takes("m1", [other, other], now=now) == 0,
                    "an ordinary card is not a lucky card")
        ok &= check(pending_lucky("m1", now=now) == 0, "no takes, nothing owed")
        ok &= check(record_takes("m1", [lucky_ids[0], other], now=now) == 1,
                    "one of the week's three counts once")
        ok &= check(pending_lucky("m1", now=now) == 10, "one take is worth 10")
        ok &= check(record_takes("m1", lucky_ids, now=now) == 3, "all three count")
        ok &= check(pending_lucky("m1", now=now) == 30,
                    "four takes are capped at 30")

        # THE ONE THAT MATTERS: announce PAYS, and pays ONCE.
        before = load("m1")["points"]
        bal, t30, lucky, rook = announce("m1", now=now)
        ok &= check((bal, lucky) == (before, 30),
                    "announce hands over the OLD balance beside the award -- "
                    "the client is what adds them")
        ok &= check(load("m1")["points"] == before + 30,
                    "...and credits our own copy in the same breath")
        ok &= check(pending_lucky("m1", now=now) == 0, "a paid week owes nothing")
        ok &= check(announce("m1", now=now)[2] == 0,
                    "ANNOUNCING TWICE MUST NOT PAY TWICE")
        ok &= check(load("m1")["points"] == before + 30,
                    "...and must not move the balance either")
        ok &= check(load("m1")["acquired"] == 30,
                    "'Prize Points Acquired' is a lifetime total")

        # A NEW WEEK RESETS THE TALLY AND OPENS THE FAUCET AGAIN.
        nxt = now + WEEK
        ok &= check(pick_lucky(now=nxt) != pick_lucky(now=now),
                    "a new week draws new cards")
        ok &= check(load("m1")["takes"] == 4
                    and _roll_week(load("m1"), nxt)["takes"] == 0,
                    "the tally is per week and gets rolled, not carried")
        ok &= check(pending_lucky("m1", now=nxt) == 0,
                    "a fresh week starts owing nothing")
        ok &= check(record_takes("m1", [pick_lucky(now=nxt)[0]], now=nxt) == 1
                    and pending_lucky("m1", now=nxt) == 10,
                    "and pays again once something is taken in it")

        # THE CEILING IS THE CLIENT'S, NOT OURS.
        d = load("m2")
        d["points"] = BALANCE_MAX - 5
        store("m2", d)
        announce("m2", now=now, top30=1000)
        ok &= check(load("m2")["points"] == BALANCE_MAX,
                    "the balance saturates FLAT at 99,999, as the client does")

        # THE WIRE.
        body = excinit_body("m3", shop_no=1, now=now, top30=1000, rookie=300)
        ok &= check(body.startswith(b"@ExcInit=/N=1/PP="),
                    "the body is @ExcInit with the shop number first")
        ok &= check(body.count(b"/PP=") == 1 and body.count(b"/LC=") == 1,
                    "ONE /PP= AND ONE /LC= -- repeated keys make every indexed "
                    "read return the first value")

        # THE RANKING FAUCETS: the weekly publish GRANTS, the next Prize
        # Center open PAYS, and neither can pay twice.
        ok &= check(grant_ranking("m4", 2000, top30_rank=3, rookie_rank=None)
                    == (600, 0), "a Top 30 rank 3 grants the ladder's 600")
        ok &= check(grant_ranking("m4", 2000, top30_rank=3) is None,
                    "granting the same publish twice is a no-op")
        ok &= check(load("m4")["pending"].get("top30") == 600
                    and load("m4")["points"] == 0,
                    "a grant is PENDING, not credited, until the Prize Center")
        bal4, t30_4, _l4, rook4 = announce("m4", now=now)
        ok &= check((bal4, t30_4, rook4) == (0, 600, 0),
                    "the Prize Center open hands over the OLD balance and the "
                    "pending Top 30 award beside it")
        ok &= check(load("m4")["points"] == 600 and not load("m4")["pending"],
                    "...credits our copy and clears the grant")
        ok &= check(announce("m4", now=now)[1] == 0
                    and load("m4")["points"] == 600,
                    "a second open must not pay the ranking award again")
        ok &= check(grant_ranking("m4", 2000, top30_rank=1) is None
                    and load("m4")["points"] == 600,
                    "a re-run of a PAID publish cannot grant it again")
        ok &= check(grant_ranking("m4", 2001, rookie_rank=2) == (0, 250)
                    and announce("m4", now=now)[3] == 250
                    and load("m4")["points"] == 850,
                    "the next week's Best Rookie award is a new grant")
        ok &= check(grant_ranking("m5", 2000, top30_rank=31) is None,
                    "an unplaced member gets no grant")
        # --correct (2026-09-13): an UNPAID grant is replaced, or withdrawn
        # when the corrected lists no longer place the member; a PAID one
        # never moves -- the client already added it to its balance.
        ok &= check(grant_ranking("m6", 2002, top30_rank=1, rookie_rank=1)
                    == (1000, 300), "a wrong publish grants m6 #1")
        ok &= check(grant_ranking("m6", 2002, top30_rank=3, replace=True)
                    == (600, 0) and load("m6")["pending"].get("top30") == 600,
                    "--correct replaces an UNPAID grant for the same publish")
        ok &= check(grant_ranking("m6", 2002, top30_rank=3, replace=True)
                    is None, "...and replacing it with itself changes nothing")
        ok &= check(grant_ranking("m6", 2002, replace=True) == (0, 0)
                    and not load("m6")["pending"],
                    "--correct WITHDRAWS an unpaid grant no longer earned")
        ok &= check(grant_ranking("m4", 2001, top30_rank=5, replace=True)
                    is None and load("m4")["points"] == 850,
                    "--correct never touches a PAID publish")
        ok &= check(grant_ranking("m7", 2002, replace=True) is None,
                    "--correct on a member with nothing granted is a no-op")
        pp = body.split(b"/PP=")[1].split(b"/")[0].split(b"|")
        lc = body.split(b"/LC=")[1].split(b"/")[0].split(b"|")
        ok &= check(len(pp) == 4 and len(lc) == 3,
                    "four pipe-separated points, three pipe-separated cards")
        ok &= check([int(x) for x in lc] == pick_lucky(now=now),
                    "the ids on the wire are the week's draw")
        ok &= check((int(pp[1]), int(pp[3])) == (1000, 300),
                    "top30 goes in slot 1 and rookie in slot 3 -- NOT 2 and 3; "
                    "slot 2 is the lucky award (rva 0x10557A -> [esi+0xBE])")
        ok &= check(int(pp[0]) == 0,
                    "a first-time member opens the Prize Center with 0 points")
        ok &= check(load("m3")["points"] == 1300,
                    "...and the announce that built it paid the ranking slots")

        # SPENDING.
        ok &= check(spend("m3", 300) == 1000, "a purchase debits")
        ok &= check(spend("m3", 99999) is None and load("m3")["points"] == 1000,
                    "an unaffordable purchase changes nothing")

        # A CORRUPT RECORD MUST NOT TAKE THE SCREEN DOWN.
        with open(path_for("m4"), "w") as f:
            f.write("{not json")
        ok &= check(load("m4") == blank(),
                    "a corrupt record reads as an empty one")
    finally:
        if old is None:
            os.environ.pop("POL_RESOURCE_DIR", None)
        else:
            os.environ["POL_RESOURCE_DIR"] = old
        shutil.rmtree(root, ignore_errors=True)

    say("tmprize selftest %s" % ("OK" if ok else "FAILED"))
    return ok


if __name__ == "__main__":
    import sys
    if "--cards" in sys.argv:
        # What this week, and the next few, draw -- for the server owner.
        this = week_id()
        for w in range(this - 1, this + 5):
            print("%s week %d (%s UTC .. +7d): %s"
                  % ("*" if w == this else " ", w,
                     time.strftime("%Y-%m-%d %a", time.gmtime(week_start_of(w))),
                     ", ".join("#%d %s" % (c, tm_cardprm.name(c))
                               for c in pick_lucky(week=w))))
        print("  (* = the week running now; ids are what /LC= carries)")
        raise SystemExit(0)
    raise SystemExit(0 if selftest() else 1)
