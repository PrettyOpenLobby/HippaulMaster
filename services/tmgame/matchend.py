"""The end of a match: take lists (@TCList), fractional places, result codes, prize credit and
@ResultData.
"""
from . import careerstats, collection, common, matchstart, pushqueue


def _take_frame(pools, me):
    """The take list in the RECIPIENT's frame: its own pool first.

    WARNING: MEASURED 2026-09-07T15:28Z (41c): the Deck won from ABSOLUTE seat 1 and
    its take screen showed the pools SWAPPED -- its own cards offered as the
    loser's -- so it picked blind (the server still moved the right card,
    the winner's slot 3). The client reads /H= occurrence i and the flat /D<k>= as
    "player i in MY frame" (me = 0), exactly like @VsGameInit and @ResultData;
    a winner in seat 0 never showed it. `POL_TM_TCLIST_ROTATE=0` restores the
    absolute order."""
    if not common._env_int("POL_TM_TCLIST_ROTATE", 1) or not pools:
        return list(pools)
    me = int(me) % len(pools)
    return list(pools[me:]) + list(pools[:me])


def _losers_from(win, pools):
    """The losing seats in the order the WINNER's take screen walks them: the
    other seats in rotated order after itself (identity for two players)."""
    n = len(pools)
    if common._env_int("POL_TM_TCLIST_ROTATE", 1):
        return [(win + k) % n for k in range(1, n) if pools[(win + k) % n]]
    return [s2 for s2 in range(n) if s2 != win and pools[s2]]


def _take_occ0(pools, me, win):
    """Occurrence 0 of every take-list row, per pool, in the RECIPIENT's frame.

    WARNING: IT IS THE SEAT ENTITLED TO TAKE THE CARD. The trade scene's state 0x3A
    (0xC91B9, read 2026-09-07) accepts a target seat only if one of its cards
    carries this byte (hand-slot record +0x1D, where `@TCList /D<k>=` occ 0
    lands) EQUAL to the picker's seat index in the viewer's frame. We sent 0 =
    "the viewer itself": the winner's own client found its targets, every
    LOSER's client (winner = index 1 in its frame) found none, printed "NEXT
    ENEMY" and left the take scene before the pick -- the 2026-09-07 "the loser
    never sees which card was taken". The 09-02 "occ 0 = id makes the cards
    invisible" measurement is the same byte: an id is no seat. `pools` is
    already rotated to `me` first; the winner's own pool keeps 0.
    """
    n = len(pools)
    if n == 0 or not common._env_int("POL_TM_TCLIST_TAKER", 1):
        return [0] * n
    w_r = (int(win) - int(me)) % n if common._env_int("POL_TM_TCLIST_ROTATE", 1) \
        else int(win) % n
    return [0 if p == w_r else w_r for p in range(n)]


def _tclist_body(pools, occ0=None):
    """`@TCList=` -- the win screen's take list. Arm 0x104648, `Recv=TRADELIST`.

    VERIFIED: MEASURED LIVE 2026-08-22T16:49Z, ring clean (store_count=2): a WINNER's
    post-@Ready screen polls exactly (0x43, 26) and (0x43, 35) and hangs
    without them -- a DRAW skips both and flows straight to @Continue. This
    message re-supplies every player's cards for the take screen (the play
    hands were popped as played), into the 156-byte hand-slot records.

    Shape, off the arm:
        /H=      one count PER PLAYER (occurrence i -> player i's slot count,
                 written to [esi+0x19F+48k] at the rotated slot)
        /D<k>=   ONE CARD each, k FLAT across players (player*5+card), each a
                 pipe list read by occurrence:
                   occ 0 -> +0x22A/+0x229   the take screen's NAME field --
                                            see below; was served 0
                   occ 1 -> +0x21A word     the CARD ID (names the ART cad%05d)
                   occ 2 -> +0x214          attack     -+ the arm's own
                   occ 3 -> &3 -> +0x215    TYPE        | CardPrm baseline
                   occ 4 -> +0x216          p. defence  | math at 0x104960
                   occ 5 -> +0x217          m. defence -+ pins these three
                   occ 6 -> +0x219          the arrows

    WARNING: occ 0 IS NOT THE NAME -- it is a VALIDITY/flag field, and a large value
    INVALIDATES the card. Falsified live 2026-09-02: the take screen showed the
    RIGHT card (correct art from occ 1, correct card transferred by @GetSelect)
    under a WRONG name (Chocobo->"Cerberus", Frog->"Fat Chocobo"), so occ 0 = id
    was tried as the name -- and the cards went **INVISIBLE** at the select
    prompt (id 88 etc. is out of this field's valid range). So: occ 0 must stay
    small (0 renders the card), and the WRONG NAME comes from somewhere else
    (occ 1 drives the ART and is correct; the name is NOT occ 1 either, since
    art is right while name is wrong, and the wrong names have no fixed offset
    from the id -- not a shifted string table). The real name field is
    UNIDENTIFIED and needs the TM.dll dump (arm 0x104648), which is not in this
    checkout. `POL_TM_TCLIST_NAME=1` re-enables the falsified occ-0 = id ONLY as
    the A/B that proved it invalid -- do not turn it on.

    `pools` is one list of wire rows (the 8-value `id|atk|type|pdef|mdef|
    power|arrows|x` format everything else uses) per ABSOLUTE seat.
    """
    name_id = common._env_int("POL_TM_TCLIST_NAME", 0)
    body = bytearray(b"@TCList=/H="
                     + b"|".join(b"%d" % len(p) for p in pools))
    k = 0
    for pi, pool in enumerate(pools):
        taker = int((occ0 or [0] * len(pools))[pi])
        for row in pool:
            v = row.split(b"|")
            if len(v) < 8:
                continue
            o0 = v[0] if name_id else b"%d" % taker
            body += b"/D%d=" % k + b"|".join(
                (o0, v[0], v[1], v[2], v[3], v[4], v[6]))
            k += 1
    return bytes(body)


def _placements(scores):
    """`[place x100]` per seat from `[score]` -- **FRACTIONAL** ranking.

    KEY: **THE TIE RULE IS THE CLIENT'S OWN, AND IT IS NOT THE RANKING LISTS'.**
    Measured 2026-08-24 off the result path's average-rank recomputation
    (`0xCC814`), which is the arithmetic this feeds:

        edx = 0
        for i in 0..31:                       ; a 32-slot ring at struct +0x8C
            eax = ring[i] + 2
            eax = eax * 5 * 5                 ; two `lea eax,[eax+eax*4]`
            edx += eax * 2                    ; `lea edx,[edx+eax*2]`
        struct+0x04 = edx >> 5                ; / 32  -> AVERAGE RANK

    i.e. `avg = mean((ring[i] + 2) * 50)`. Read that scale off its own ends:
    `ring[i] == 0` gives **100 = 1.00** and `== 2` gives **200 = 2.00**, so the
    stored byte is `2 * (place - 1)` -- **each unit is HALF a place**, and the
    odd values exist precisely so a DRAW can be recorded. A two-player draw is
    `ring[i] == 1` -> **1.50**, not a shared 1st.

    WARNING: So this is FRACTIONAL (mean) ranking, while `tmrank`'s LISTS use standard
    COMPETITION ranking (`0x175417`: a row whose value equals the previous row's
    inherits its rank). Both are the client's, on two different screens, and an
    earlier version of this function used the ladder's rule here -- which scored
    every draw as a win and made the average, and therefore the TITLE, better
    than it should be.

    Returned **x100** so nothing has to carry halves: seat i's contribution is
    already in the save's own fixed point.
    """
    n = len(scores)
    vals = [int(s or 0) for s in scores]
    out = [100] * n
    for i, v in enumerate(vals):
        better = sum(1 for w in vals if w > v)
        tied = sum(1 for w in vals if w == v)
        # Fractional rank: the mean of the places this seat's tie group spans,
        # i.e. better+1 .. better+tied. x100, and exact for any tie size
        # because (tied - 1) * 50 is a whole number of hundredths.
        out[i] = (better + 1) * 100 + (tied - 1) * 50
    return out


def _result_code(place_x100):
    """The client's own per-match RESULT byte from a fractional place x100.

    VERIFIED: MEASURED at `0xCC5C7..0xCC62E`, the per-opponent accumulator, and it is
    the cleanest confirmation this whole area has: the client walks the other
    seats and adds **0 for a win, 1 for a draw, 2 for a loss** --

        0xCC5CD  cmp cl, [esi]        ; my score vs this opponent's
        0xCC5D1  inc byte [+0x1A1]    ;   EQUAL   -> += 1
        0xCC5DF  jae 0xCC610          ;   >=      -> nothing more (a win is 0)
        0xCC5E7  add cl, 2            ;   <       -> += 2

    -- so the byte is exactly `2 * (fractional place - 1)`, which is what
    `_placements` computes independently from the scores. Two derivations of the
    same number from different ends of the same function.

    That byte is what lands in the result ring (`0xCC806`) and what the streak
    machine branches on (`0xCC64C`), so both consumers take it from here.
    """
    return max(0, (int(place_x100) - 100) // 50)


def _streak_next(current, result):
    """The client's Consecutive Wins counter after one match. SIGNED.

    Ported from `0xCC643..0xCC69C`. One field carries both Playdat labels: a win
    run is positive (`Consecutive Wins`), a loss run negative
    (`Consecutive Losses`), and the screen picks by sign.

        result == 2  ->  0                      0xCC651 `mov word [+0x88], bp`
                                                (ebp is 0 -- 0xCC62C gates the
                                                whole block on `test ebp,ebp`)
        result <  2  ->  1 if current < 0       0xCC663 `test cx,cx / jge`
                         else current + 1       0xCC675 `inc cx`
        result >  2  ->  -1 if current > 0      0xCC68A `mov word [...], 0xFFFF`
                         else current - 1       0xCC695 `dec word [...]`

    WARNING: In a TWO-player match `result == 2` is a loss, and it RESETS to zero
    rather than starting a loss run -- a loss run needs `result > 2`, which only
    a 3-player board can produce. That is the client's behaviour, not a
    simplification of it, and it is why `Consecutive Losses` is nearly dead on a
    two-player server.

    WARNING: Three gates on the update are UNMEASURED (`ebp`, `[ebx+0x197]`,
    `[0x52464CA]` must all be zero). They plausibly exclude observers and
    abandoned matches. We apply the update unconditionally, which can only
    over-count in cases we do not yet know how to detect.
    """
    cur = int(current or 0)
    if result == 2:
        return 0
    if result < 2:
        return 1 if cur < 0 else cur + 1
    return -1 if cur > 0 else cur - 1


def _bump_prize(member_id, amount, why=""):
    """Credit winnings to the ranking prize counters (rank block
    `prize_total` / `prize_week` -- what the Grand Total and Weekly Total
    lists sort on). The weekly job resets `prize_week` at each publish.
    Money itself moves in `_set_money`; this is only the tally."""
    amount = int(amount or 0)
    if amount <= 0 or member_id is None \
            or not common._env_int("POL_TM_RANK_PRIZE", 1):
        return
    try:
        data = collection._collection_load(member_id)
        block = data.setdefault("rank", {})
        block["prize_total"] = max(0, int(block.get("prize_total") or 0)
                                   + amount)
        block["prize_week"] = max(0, int(block.get("prize_week") or 0)
                                  + amount)
        # BIGGEST PRIZE -- `0xCC7BD cmp / 0xCC7C5 mov`, a plain running max over
        # the SAME per-match amount that feeds Grand Total. This is the whole
        # formula; there is nothing else to it.
        block["biggest_prize"] = max(int(block.get("biggest_prize") or 0),
                                     amount)
        collection._collection_store(member_id, data, sync_save=False)
        common._say("tm: member %s prize tally +%d%s (total %d, week %d)"
             % (member_id, amount, (" -- " + why) if why else "",
                block["prize_total"], block["prize_week"]))
        # `prize_total` IS a save field (+0x50, Grand Total). The result path
        # syncs before this settles, so without this call the screen would lag
        # the ledger by exactly one match -- every time.
        careerstats._save_sync_stats(member_id)
    except Exception as e:
        common._say("tm: prize tally NOT recorded for member %s (%s)"
             % (member_id, e))


def _resultdata_body(rand, seats=None, me=0, scores=None, stats=None,
                     prizes=None):
    """`@ResultData=` -- the end of the match. Arm 0x104141, `Recv=RESULTINFO`.

    WARNING: `/P=0` IS FATAL, NOT LEGAL -- RETRACTED 2026-08-21, by a live crash.
    The first end-to-end match delivered `/P=0` and BOTH clients died on the
    result screen. The old reading ("0x104205 `cmp eax,0 / jle 0x105174`
    completes the message") was true of the PARSER and said nothing about the
    scene: the early-out at 0x105174 sets the success flag, and the moment the
    command-13 wait is satisfied the client runs

        0x10F6A5   div ecx      ; [0x5246460] / (word [0x5246464] & 0xFFFF)

    and 0xCBD80 -- called at the scene transition, 0xC7A02 -- repeats the same
    division at 0xCBF69 for non-watchers. The divisor 0x5246464 is [obj+0x84],
    written by exactly ONE producer in the image: the recipient's own `@No`
    section below. On a fresh session it is BSS zero, so `/P=0` is an integer
    divide-by-zero pushed to everyone at the table at once. A message being
    parsed is not a scene surviving it.

    Field map, measured off the arm (own-section writes 0x104282..0x10435B):

        /R= x8   the random table again, into the SAME [0x2B63C4]+0x1A8+i
                 slots `@TurnData` fills (0x1041BE, `cmp edi, 8` at 0x1041D8).
        /P=      the number of `@No<i>=` sections; the loop bound (0x1044EC).
        @No<i>=  i is the seat in the RECIPIENT's own rotated numbering
                 (k = (N - own + i) % N at 0x104254; own = 0 in our copies,
                 same convention as `_vsgame_body`). `/D=` is ONE pipe list,
                 read by occurrence:

                   occ 0 -> [obj+0x04]        dead store; occ 2 overwrites it
                   occ 1 -> [obj+0xCC]        dword; 0xCBD80 max-updates it
                                              into [obj+0x78] -- the Biggest
                                              Prize `max` pattern, so this is
                                              read as THE MATCH PRIZE. Served
                                              from `prizes` (the settled net
                                              gain, computed before the body
                                              is built); reported live 2026-09-02:
                                              "the COM result screen shows the
                                              wager as 0" while the wallet
                                              moved -- delivery, not
                                              computation, one more time.
                                              INFERRED from the max-update; the
                                              first game served a real value
                                              here is the measurement.
                                              POL_TM_RESULT_CC != 0 overrides
                                              (the sweep apparatus it always
                                              was); POL_TM_RESULT_PRIZE=0
                                              restores the flat 0.
                   occ 2 -> [obj+0x180+48k]   the displayed rank slot, AND
                                              [obj+0x04] (0x1042CE / 0x1042E6)
                   occ 3 -> [obj+0x88]        word; career screen prints it
                                              only when > 0 (0x1389D6)
                   occ 4 -> [obj+0x86]        word; no reader found yet
                   occ 5 -> [obj+0x84]        word; **THE DIVISOR -- >= 1**
                   occ 6 -> [obj+0x80]        dword; the dividend

                 Every OTHER player's section is read for occ 2 ONLY
                 (0x10448F). `/T=` is optional (0x104394 skips it over
                 pre-zeroed defaults); `/RN=` is a hex :str (`_str_field`)
                 copied to [obj+0x128].

    WARNING: ALWAYS SEVEN VALUES IN `/D=`. None of the seven parses checks its
    result before storing (the `@VsGameInit` `/R=` trap, one message over): a
    short pipe list stores stack garbage into the remaining slots, the divisor
    included.

    The career screen (STATUS module, 0x138761..0x1389D6) prints [obj+0x78],
    the quotient [obj+0x7C], [obj+0x80], and [obj+0x88]-when-positive -- so
    occ 5/6 read as GAMES PLAYED and a career score total, quotient = the
    average. The NAMES are inferred from those readers; the NUMBERS are ours
    by construction (the csid precedent -- we author the field, the client
    only stores and divides): `_bump_result_stats` tracks real games and the
    real tile totals per member. Rank (occ 0/2) stays the same 0 every other
    message serves (`POL_TM_VSGAME_RA`, `_SHOPINIT_FIELDS`) until rank is a
    real thing on this server; `POL_TM_RESULT_RA` overrides.

    WARNING: The server still does not decide who WON on this message: 0xCBD80
    computes win/lose from the per-seat tile counts (block 0x5246580, stride
    0x30) the match itself filled. What a win PAYS is the house prize
    (`_house_prize`, a COM game), which the client's count-up credits from
    occ 1.

    `POL_TM_RESULT_P=0` restores the pre-crash one-liner, kept ONLY as
    measurement apparatus for the divide itself.
    """
    body = b"@ResultData=/R=" + b"|".join(b"%d" % v for v in rand[:8])
    if not common._env_int("POL_TM_RESULT_P", 1) or not seats:
        return body + b"/P=0"
    seats = list(seats)
    n = len(seats)
    scores = (list(scores or []) + [0] * n)[:n]
    # Same per-recipient rotation as @VsGameInit: the recipient IS seat 0 in
    # its own copy, so its section is @No0 and k = i for every other seat.
    if common._env_int("POL_TM_VSGAME_SEAT0", 1) and me:
        seats = seats[me:] + seats[:me]
        scores = scores[me:] + scores[:me]
        me = 0
    ra_env = common._env_int("POL_TM_RESULT_RA", 0)

    def _seat_rating(mid):
        # occ 0/2 are the result screen's per-player RATING rows ("a bunch
        # of rating scores that are all 0" -- reported live 2026-08-22, first
        # game after the tally went live). Serve each member's real VS.
        # Rating (the same avg-score x100 the ranking lists sort on); a COM
        # seat or a member with no record stays 0. POL_TM_RESULT_RA != 0
        # overrides everyone, kept as the sweep apparatus it always was.
        if ra_env:
            return ra_env
        try:
            import tmrank as _tr
            return int(_tr.stats_of(collection._collection_load(mid)).get("rating") or 0)
        except Exception:
            return 0

    body += b"/P=%d" % n
    for i, (mid, _ident) in enumerate(seats):
        ra = _seat_rating(mid)
        if i == me:
            games, total = (stats or {}).get(pushqueue._push_key(mid),
                                             (1, scores[i]))
            cc = common._env_int("POL_TM_RESULT_CC", 0)
            if not cc and common._env_int("POL_TM_RESULT_PRIZE", 1):
                cc = int((prizes or {}).get(pushqueue._push_key(mid), 0))
            d = (ra, cc, ra, 0, 0,
                 max(1, games), max(0, total))
            body += b"@No%d=/D=" % i + b"|".join(b"%d" % v for v in d)
            body += b"/RN=" + matchstart._vsgame_player_name(mid, i)
        else:
            d = (ra, 0, ra, 0, 0, 1, 0)
            body += b"@No%d=/D=" % i + b"|".join(b"%d" % v for v in d)
    return body
