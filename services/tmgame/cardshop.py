"""The card shop: its door handshakes, packs and what a draw yields, the buy reply, and selling or
discarding cards.
"""
import os
import random
import tm_cardprm
import tmbattle
import re
from .deps import tmroll
from . import cardtables, collection, common, protocol, purse


#: WARNING: MULTI-VALUE FIELDS ARE PIPE-SEPARATED, NOT REPEATED. `0xAB080(name, dest,
#: index, out)` finds the field, takes its value up to the next `/` or `@`
#: (char set `"/@"` at 0x51B3950), and then walks `index` occurrences of the
#: SEPARATOR `"|"` (0x51B2B78) INSIDE that one value. So the card block is
#:
#:     @N0=/D=<id>|<atk>|<type>|<pdef>|<mdef>|<b4>|<b5>|<b6>
#:
#: and NOT eight `/D=` fields in a row.
#:
#: THIS WAS THE BUG BEHIND "the client only accepts card ids 0..3", and it cost
#: four live purchases and two wrong theories (degenerate stats, a four-slot
#: cap). Writing `/D=a/D=b/D=c...` made every indexed read fall back to the
#: FIRST value -- the card id -- so the type check `cmp idx2, 4 ; jge discard`
#: was being applied to the CARD ID. Ids 0..3 passed it and everything else was
#: silently dropped, which looked exactly like an id ceiling of 4.
#:
#: The live dump killed that theory for good: `[0x528F4FC]` reads **250** and
#: the CardPrm rows in memory match the file byte for byte, so the id gate was
#: never the one firing. Measure before theorising (a live card-state probe).

#: LEAVING THE SHOP -- and NOT answering this is what dropped both clients.
#: Live 2026-08-16: after a purchase the player backs out, the client sends
#: `@Quit=/No=1` as `B2001701` (code 0xB2, **msgid 0x17**, shop 1) and blocks in
#: `0x101A10(0x17)`. We were silent, it waited, and ~75 s later the session went
#: `resumed channel closed` and the game errored out. The shop LOOKED finished
#: because the purchase had already succeeded; the failure is on the way out.
#:
#: Arm 0x105A97 handles msgid 0x17 and accepts two keys:
#:   `@Event`  -- the featured-pack event (Cardsh.BIN 71, "celebrate X's #1 rank
#:                by selling X's Pack"); fields /N= /RA= /M= /CP= /S= /RT=.
#:   `@EQuit`  -- 0x105BA3, the plain exit. Same two equality gates as SHOPBUY
#:                (sender id vs shop[+0x28/+0x2C], msg[+0x3D0] vs shop[+0x30]),
#:                logs `---->Recv=SHOPQUIT`, then reads ONE field: `/D=`, which
#:                **must be 0** -- 0x105C29 `cmp eax,ebx ; jne 0x105878` takes
#:                the failure path on anything else.
#: Echoing the request header satisfies both gates, as with SHOPBUY.

#: THE CARD SHOP ENTRY, and it is the same destination-id mechanism as `/Shm=`.
#: Measured 2026-08-16 in the unpacked `TM.dll`; the screen errors out with no
#: reply at all, which is what silence looks like from the client side.
#:
#:   handler 0x88E06:  find("@ShEnter")                <- a map KEY, so `=`
#:                     tm_field_get_int("/EN=", v, 0, &n)   <- the ONLY field
#:                     if (n > 0):
#:                         [0x5242958] = sender id lo
#:                         [0x524295c] = sender id hi
#:
#: `0x5242958/5c` is `tm_send`'s FALLBACK destination (0x820EC, the third branch
#: of the gate that silently ate `@Init=`). So
#: this exchange is how the client learns which peer to address card-shop traffic
#: to, and it takes the id from the SENDER of our reply -- nothing for us to put
#: in a field.
#:
#: WARNING: THE CODE IS 0xB1 (177), AND 0xB2 IS A DIFFERENT MESSAGE. The first cut served
#: 0xB2 by inferring it from `push 0xb2` at the send site; the client ignored it
#: and the screen sat loading, exactly as it does with no reply at all. Reading
#: the handler settled it -- it polls TWO codes in order:
#:
#:   0x88D95  push 0xb2 ; push 0x13 ; call 0x827f0   <- tried FIRST. If THAT is
#:            found it sets [0x5242958/5c] straight from the message and never
#:            looks at @ShEnter, so 0xB2 carries no text body.
#:   0x88DD4  push 0xb1 ; call 0x825a0               <- then the store lookup
#:            whose record is searched for "@ShEnter".
#:
#: So `@ShEnter=` rides on **0xB1**; the `push 0xb2` at the send site was this
#: first poll, not a drain. `POL_TM_SH_CODE` remains for A/B-ing 0xB2 (which
#: would take the other branch and may be the "shop ready" push).
def _sh_enabled():
    return os.environ.get("POL_TM_SH", "1") == "1"


def _sh_code():
    try:
        return int(os.environ.get("POL_TM_SH_CODE", str(protocol.MSG_SHENTER)), 0)
    except ValueError:
        return protocol.MSG_SHENTER


def _sh_en():
    """`/EN=` must be > 0; the handler bails at `jle` otherwise."""
    try:
        return int(os.environ.get("POL_TM_SH_EN", "1"), 0)
    except ValueError:
        return 1


def _cv_enabled():
    return os.environ.get("POL_TM_CV", "1") == "1"


def _cv_en():
    """THE PRIZE CENTER endpoint handshake -- `@CvReq=` -> `@CvEnter=/EN=<n>`.

    Observed live 2026-08-16 when a tester opened Rankings -> Prize Center:
    the client sends `@CvReq=/NN=.../HID=0/Dm=0/Vol=0/CN=...` on code 112 and we
    were silent, which is the same 75-second dead wait `@Quit=` was.

    NOT GUESSED. The parser at TM.dll rva 0x8B490 is `@ShEnter`'s (0x88DF0)
    instruction for instruction with one literal changed -- match `@CvEnter` in
    the body, read `/EN=` with the same extractor, bail at `jle` unless it is
    **> 0**, then take OUR message's sender id into [0x2B2958]/[0x2B295C] as the
    endpoint. So this is the shop handshake wearing a different name, and the
    same `/EN=1` that has been working there all day is the right answer here.

    WARNING: That global is shared: it is the same one the card-service directory
    search at 0x86044 compares entries against, so these endpoint handshakes
    move state the whole game reads. One at a time.
    """
    try:
        return int(os.environ.get("POL_TM_CV_EN", "1"), 0)
    except ValueError:
        return 1


def _shopinit_enabled():
    return os.environ.get("POL_TM_SHOPINIT", "1") == "1"


def _shopbuy_enabled():
    return os.environ.get("POL_TM_SHOPBUY", "1") == "1"


def _shopquit_enabled():
    return os.environ.get("POL_TM_SHOPQUIT", "1") == "1"


def _cardent_enabled():
    return os.environ.get("POL_TM_CARDENT", "1") == "1"


def _cardent_ans():
    """`/Ans=` must be > 0; 0x8BA52 `jle` skips the endpoint store otherwise."""
    try:
        return int(os.environ.get("POL_TM_CARDENT_ANS", "1"), 0)
    except ValueError:
        return 1


def _shopquit_d():
    """`/D=` -- 0 is the only value that continues; anything else fails out."""
    try:
        return int(os.environ.get("POL_TM_SHOP_QUIT_D", "0"), 0)
    except ValueError:
        return 0


#: A pack is a RANDOM DRAW, not a fixed list. SE's shop sells a permanent
#: catalogue -- `data/PackPrm.BIN` names Pauper's / Beginner's / Standard /
#: Collector's / Hero's / Starter's / COM's Lv.1-3 packs plus a `rank_1 Pack`
#: and per-character packs -- so a pack is a PRODUCT that stays on sale, and
#: what varies is the cards it yields. The frozen five-card list this replaced
#: was scaffolding to prove the wire format, never intended behaviour.
#:
#: `POL_TM_SHOP_CARDS` still pins an exact list (`no:d1..d7`, comma-separated)
#: for A/B testing; leave it unset for a real draw. `POL_TM_SHOP_SEED` makes a
#: draw reproducible when you are bisecting the unmeasured bytes.
_PACK_SIZE = 5              # cards per pack; also the minimum needed to play

#: WARNING: DIAGNOSTIC, 2026-08-16 -- REMOVE ONCE THE CEILING IS KNOWN.
#: Three live purchases say the client accepts a card id only below a small
#: ceiling: ids 1,2,3,4,5 -> it kept 1,2,3; two draws of random ids 34..226 ->
#: it kept NOTHING, while still printing "Acquired" for every card (the message
#: prints before the discard at 0x106093). The gate is
#: `cmp id, [0x528F4FC] ; jge discard`, and 0x528F4FC is written only by the
#: BIN loader's third argument when it loads CardPrm.BIN -- which ought to be
#: 250, so the static reading and the observed behaviour disagree.
#: `POL_TM_SHOP_LADDER=n` serves ids 0..n-1 with their real CardPrm stats;
#: however many come back IS the ceiling, in one purchase.
def _ladder():
    try:
        return max(0, int(os.environ.get("POL_TM_SHOP_LADDER", "0"), 0))
    except ValueError:
        return 0


def _ladder_base():
    """First id of the ladder. Rung 2 of the diagnostic: the first ladder (0..7)
    came back as ids 0,1,2,3, which reads as "card id must be < 4" -- but
    0x528F4FC is the CardPrm COUNT (0x128143 uses it as the loop bound over the
    array at 0x5246364), so an id ceiling of 4 is nonsense. The competing
    explanation is a TOTAL CARD CAP of four, which fits the same evidence.
    Serving a ladder that starts ABOVE 4 tells them apart in one purchase:
    4 cards back = a total cap, 0 back = a genuine id ceiling."""
    try:
        return max(0, int(os.environ.get("POL_TM_SHOP_LADDER_BASE", "10"), 0))
    except ValueError:
        return 0


#: VERIFIED: WHAT A PACK CONTAINS -- DECODED 2026-08-20 FROM `data/PackPrm.BIN`.
#:
#: Each 52-byte record carries FOUR draw rows at +0x0C, each four bytes:
#:
#:     (LEVEL, GROUP, CATEGORY, CUMULATIVE WEIGHT out of 255)
#:
#: One row is chosen per card: roll 0..254 and take the FIRST row whose
#: cumulative weight exceeds the roll, then pick uniformly from the cards that
#: row admits. The three filters are columns of `CardPrm.BIN`, and 0xFF (level)
#: or 0 (group, category) means "any" -- 0xFF never appears in the other two
#: slots in any of the 77 records, which is what makes the convention readable.
#:
#:     LEVEL     CardPrm byte 5, already the sell-price index (`_sell_price`)
#:     GROUP     CardPrm byte 6: 0..3 are broad classes, 4..29 are near-unique
#:               marker groups (22 = Mace of Zeus, 19 = Dragon's Hair, ...)
#:     CATEGORY  CardPrm byte 7: 1 monsters (109), 2 summons (19), 3 weapons
#:               (28), 4 ships (10), 5 mascots (9), 6 places (2), 7 characters
#:               (65), 8 oddities (8)
#:
#: WHY THIS IS THE READING AND NOT A SHAPE THAT HAPPENS TO FIT. The file names
#: its own records, and every name states what its rows resolve to:
#:
#:     Summoner's Pack     -> category 2, and the pool is the 12 summons
#:     Warrior's Pack       -> category 3 at levels 3/4/5, all weapons
#:     Voyager's Pack       -> category 4, the airships
#:     FFIX Celebrity Pack -> category 7 at levels 6/7, the characters
#:     FFX Blitzball Ed.   -> group 1, which is Datto/Letty/Jassu/Botta/Keepa
#:     Cid (COM deck)      -> category 4, the man who builds airships
#:     the weapon merchant -> category 3
#:     Steiner             -> group 23 = Excalibur II, group 5 = Beatrix
#:     Quina/Freya/Amarant -> groups 20/19/21 = Gastro Fork / Dragon's Hair /
#:                            Rune Claws, each that character's own weapon
#:     Beginner's/Hero's   -> plain level bands, 1/2/3/5 and 7/8/9/10
#:
#: Nine independent name-to-pool matches, and NO row in the whole file resolves
#: to an empty pool. The same record's +0x20 holds a card id for the 30 Prize
#: Center records, and those 30 rise monotonically in card LEVEL as their point
#: cost rises (13,13,13,14,15,15,15,16,16,16,16,...,22,23,23,24) -- an
#: independent confirmation of the level column, off a different field.
#:
#: WARNING: AND IT RETRACTS THE MASK. The 2026-08-20 pass read record +0x00 as
#: a 24-bit level mask and could not make the sanity check fit (the free pack
#: appeared to mask off a bit the 1000-gold pack allowed). It is not a pool
#: field at all: its low 20 bits are 0xFFFFF in every one of the 77 records,
#: and read as a level mask it has a counterexample -- Cid's deck (+0 =
#: 0x008FFFFF, bit 21 clear) has a draw row that asks for level 21 outright,
#: which is the Airship. The pool never needed it. What K = raw0 >> 20 (4..21)
#: means is still unknown; it is NOT price order, NOT pack size, and NOT the
#: highest level reachable. Nothing reads it here, and nothing should until it
#: is measured.
#:
#: WARNING: AND THE PLANNED MEASUREMENT IS RETIRED, NOT PASSED. The cheap route on
#: the table was "buy one pack repeatedly and record which levels appear". It
#: could never have worked from here: OUR server draws the cards, so the levels
#: a purchase yields are the levels we chose. The oracle was always the shipped
#: file. What a purchase CAN still falsify is this decode -- if a Beginner's Pack
#: ever hands over a card outside levels 1/2/3/5, the reading is wrong.
def _pack_pool(level, group, cat):
    """The card ids one draw row admits. Cached: 77 records x 4 rows at most."""
    key = (level, group, cat)
    pool = _POOL_CACHE.get(key)
    if pool is None:
        pool = []
        for no in range(tm_cardprm.CARD_COUNT):
            row = tm_cardprm.row(no)
            if level != 0xFF and row[5] != level:
                continue
            if group and row[6] != group:
                continue
            if cat and row[7] != cat:
                continue
            pool.append(no)
        _POOL_CACHE[key] = pool
    return pool


_POOL_CACHE = {}


def _pack_size(rec):
    """How many cards this pack yields -- the record's own +0x09 byte.

    The flat 5 this replaced was scaffolding; the field is the file's own,
    and it is 1 in all 31 single-card records, which is what identifies it.

    VERIFIED: AND THE CLIENT HAS ROOM FOR IT -- measured 2026-08-20 off the SHOPBUY
    parse loop at rva 0x105EF4, because raising a count SE never sent us is
    exactly the kind of unverified change that breaks a working screen:

        0x105EF4  eax = /S=          the count we send
        0x105F07  word [esi+0xF0] = 0        the staging count, zeroed
        0x105F50  edi = (staging + 0x4B9) << 4 + esi     the record slot
        0x10608A  inc word [esi+0xF0]        ...only for a card it KEPT
        0x10609F  cmp ebp, eax / jl          the loop runs exactly /S= times

    **There is no cap in that loop** -- it writes as many records as we send, so
    the only real bound is where the staging array (0x524AF70) runs into
    something else. The nearest global referenced above it is 0x524B0B0, at
    +0x140 = **20 records**, so ten is comfortably inside and five never tested
    the edge. `POL_TM_PACK_SIZE` still forces a count.

    WARNING: It changes what is DRAWN, not what is kept. Shop
    state 6 (0x17961D) copies ONE record, from staging slot 0, and zeroes the
    rest -- so `cards[0]` is the card the player actually gets, and the other
    nine are animation. That is also why the two range gates in the loop matter
    here: a card the client DISCARDS (id >= CardPrm count at 0x105F78, or type
    >= 4 at 0x105FBD) does not increment the staging count, so a discarded
    `cards[0]` would silently hand the player our second card while we recorded
    the first. Every id we draw comes out of CardPrm, so neither gate can fire.
    """
    env = os.environ.get("POL_TM_PACK_SIZE")
    if env:
        try:
            return max(1, min(64, int(env, 0)))
        except ValueError:
            pass
    if rec and 0 < rec["cards"] <= 64:
        return rec["cards"]
    return _PACK_SIZE


def _card_arrows(rnd, base):
    """Wire slot 6 for one newly-issued card: its ARROW MASK.

    WARNING: THIS SERVER WAS SENDING THE CARD'S LEVEL HERE, AND IT IS THE REASON A
    WHOLE TEN-TURN MATCH PRODUCED ONE BATTLE. Slot 6 is `rec+0x0D`, which the
    client bit-tests per direction in the renderer (0xB4D0D) and in the
    adjacency engine (0xD3760); we were filling it with `CardPrm` byte 5, the
    sell-price level. The two consequences are both visible on screen:

      * a level-0 card has NO arrows and can never take anything, and
      * levels cluster at 1-3 bits with a maximum of 5, against the client's
        own distribution of 0-8 centred on 3-4 (`ARROW_COUNT_WEIGHTS`), so
        every card we ever dealt was arrow-poor.

    WARNING: THIS CHANGES CARDS ALREADY IN PLAYERS' COLLECTIONS -- or rather, it does
    not: a collection stores the eight-value ROW, so cards granted before this
    keep the level in slot 6 until they are replaced. That is deliberate. The
    alternative is rewriting saved rows, and a migration that edits a player's
    cards to fix a server bug is exactly the class of change this project has
    been burned by. New cards are right; old ones stay as issued.

    `POL_TM_CARD_ARROWS=0` restores the old byte for an A/B.
    """
    if not common._env_int("POL_TM_CARD_ARROWS", 1):
        return base[5]
    return tmbattle.draw_arrows(rnd)


def _draw_pack(no=None, n=None):
    """The cards pack `/No=<no>` yields, each carrying the stats the CLIENT'S
    OWN CardPrm row says that card should have.

    WARNING: REAL PACKS ALMOST CERTAINLY ROLLED, AND THAT ROLL IS NOT RECOVERABLE.
    Every card of a given id we hand out is byte-identical, because we mirror
    the CardPrm row exactly; SE's server presumably spread the three stat bytes
    around that baseline, which is what makes two copies of the same card worth
    different amounts. Checked 2026-08-18 (stopgap audit): the distribution is
    SERVER-SIDE by construction -- the client only ever RENDERS a record against
    the baseline (0xB9410 computes about ((2*v - base)/base + K1) * K2 per
    stat), it never generates one -- so no amount of reading TM.dll or
    TMaster.pex can produce it. There is no oracle for this in anything we hold.

    So this is a deliberate stop, not an unexamined one: `POL_TM_SHOP_JITTER`
    (percent, default 0) exists to spread the bytes, and it stays at 0 because
    picking a spread would be inventing a game rule and calling it recovery.
    Nominal stats are at least a defensible reading of the baseline. If a period
    source ever shows two same-id cards with different stats, the spread between
    them is the measurement, and this is the knob.

    TM.dll 0xB9410 renders a card by measuring the record we send AGAINST the
    CardPrm baseline for the same id -- about `((2*v - base)/base + K1) * K2`
    per stat. A record of placeholder 1s against a baseline of 6..90 is a
    degenerate card, which is why the first draws drew flat and skipped the
    acquire animation. Mirroring the row puts every stat at its nominal value.
    """
    rungs = _ladder()
    if rungs:
        # Ladder mode: ids 0..rungs-1 in order, real stats, nothing random --
        # except the arrows, which are a per-INSTANCE property and have no
        # nominal value to serve (see `_card_arrows`).
        out = []
        base_id = _ladder_base()
        lad = random.Random(os.environ.get("POL_TM_SHOP_SEED") or None)
        for card in range(base_id, min(base_id + rungs, tm_cardprm.CARD_COUNT)):
            b = tm_cardprm.row(card)
            out.append([card, b[0], b[1], b[2], b[3], b[4],
                        _card_arrows(lad, b), collection._new_card_slot(b[6])])
        return out

    rnd = random.Random(os.environ.get("POL_TM_SHOP_SEED") or None)
    try:
        jitter = max(0, min(100, int(os.environ.get("POL_TM_SHOP_JITTER", "0"), 0)))
    except ValueError:
        jitter = 0

    def vary(v):
        if not jitter or v <= 0:
            return v
        lo = max(1, v - v * jitter // 100)
        hi = min(255, v + v * jitter // 100)
        return rnd.randint(lo, hi)

    rec = cardtables._pack_rec(no)
    ids = []
    for _ in range(n or _pack_size(rec)):
        pool = None
        if rec:
            roll = rnd.randint(0, 254)
            for level, group, cat, cum in rec["draw"]:
                if cum > roll:
                    pool = _pack_pool(level, group, cat)
                    if not pool:
                        # WARNING: ONE ROW IN THE SHIPPED FILE ASKS FOR CARDS THAT DO
                        # NOT EXIST, and this is OUR repair of it, not SE's
                        # behaviour. Record 27 (Quina's COM deck, kind 0, never
                        # sold) wants level 1 + group 3 + category 5, and the
                        # four cards in that group and category are Chocobo,
                        # Fat Chocobo, Mog and Frog at levels 3/6/5/3. It is the
                        # ONLY empty row in all 308 -- `_selftest_packs` pins
                        # that -- so the level is what gives, and the group and
                        # category, which are what name the pack, are kept.
                        pool = _pack_pool(0xFF, group, cat)
                        common._say("tm: pack /No=%s row (level %d, group %d, "
                             "category %d) selects no card that exists -- drew "
                             "on group+category alone (%d cards)"
                             % (no, level, group, cat, len(pool)))
                    break
            if not pool and rec["card"] < tm_cardprm.CARD_COUNT:
                # A Prize Center record: no live draw row, one named card.
                pool = [rec["card"]]
        if not pool:
            # No record, or one whose rows cover no roll. SAY SO -- drawing from
            # the whole table silently is exactly the bug this replaced.
            common._say("tm: pack /No=%s -- no draw row covers this roll; falling back "
                 "to the whole card table" % (no,))
            pool = list(range(tm_cardprm.CARD_COUNT))
        ids.append(rnd.choice(pool))

    roll = tmroll is not None and common._env_int("POL_TM_CARD_ROLL", 1)
    out = []
    for card in ids:
        base = tm_cardprm.row(card)
        # /D= order: idx1 attack, idx2 type, idx3 pdef, idx4 mdef, idx5 the
        # derived power byte, idx6 THE ARROWS, idx7 -> [rec+0x16], unread.
        if roll:
            # WARNING: THE MEASURED PER-INSTANCE ROLL -- the client's OWN (TM.dll
            # 0xB2CF0 / PS2 TMaster.pex 0x2b1c80, byte-identical): each combat
            # stat strictly below its CardPrm ceiling, so the card arrives with
            # room to GROW through use instead of pre-maxed. This ends the
            # universal-MAX bug (serving the ceiling made every card read MAX).
            # It REPLACES the old `POL_TM_SHOP_JITTER` stop, which predated
            # finding the roll and served nominal because "picking a spread would
            # be inventing a rule" -- the client's roll IS the rule now.
            # POL_TM_CARD_ROLL=0 restores nominal (all-maxed).
            atk = tmroll.roll_stat(rnd, base[0])
            pdf = tmroll.roll_stat(rnd, base[2])
            mdf = tmroll.roll_stat(rnd, base[3])
        else:
            atk, pdf, mdf = vary(base[0]), vary(base[2]), vary(base[3])
        out.append([card, atk, base[1], pdf, mdf,
                    tmbattle.power_byte(base[4], atk, pdf, mdf,
                                        base[0], base[2], base[3]),
                    _card_arrows(rnd, base), collection._new_card_slot(base[6])])
    return out


def _shopbuy_cards(no=None):
    """[(cardno, d1..d7)] for pack `/No=<no>`, filtered to what the client will
    accept. `no` is None only when a caller has no purchase in hand, and then
    the draw has no record to work from and says so."""
    pinned = os.environ.get("POL_TM_SHOP_CARDS")
    if not pinned:
        return _draw_pack(no)
    out = []
    for item in pinned.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            vals = [int(x, 0) for x in item.split(":")]
        except ValueError:
            continue
        if len(vals) != 8:
            continue
        # The client drops a card whose number is >= the CardPrm table or whose
        # type byte is >= 4. Dropping it here too keeps the log honest about how
        # many cards the player should actually receive.
        # idx 0 is the card number, idx 2 the type -- the client's only two range
        # checks. (This read vals[3] until 2026-08-16; harmless with the old
        # hardcoded list, wrong for anything else.)
        if not (0 <= vals[0] < protocol.CARDPRM_RECORDS) or not (0 <= vals[2] < 4):
            continue
        out.append(vals)
    return out


def _card_body(header, cards):
    """`@Card=` for a list of 8-value card records, under an existing header."""
    body = bytearray(header + b"@Card=/S=%d" % len(cards))
    for i, v in enumerate(cards):
        # ONE `/D=` per card, values PIPE-SEPARATED -- see the banner below.
        body += b"@N%d=/D=%s" % (i, b"|".join(b"%d" % x for x in v))
    return bytes(body)


def _shopbuy_body(header, member_id=None, no=None):
    """The SHOPBUY answer. `header` is the request's 8 hex chars, echoed so the
    sender-id and shop-number equalities at 0x105EA9/0x105ECD both hold. `no` is
    the `/No=` the player bought -- it selects the pack's own draw table, so
    losing it here is what made every pack draw from all 250 cards."""
    cards = _shopbuy_cards(no)
    # RECORD WHAT WE GRANTED. This is the authoritative half of persistence --
    # these are our own bytes, not a reading of the client's. WARNING: The client keeps
    # only `cards[0]` and only if the player completes the acquire, so the pack
    # is held as an OFFER and credited when `@Get=` says it was taken. See
    # `_ACQUIRED_PER_PACK` for the disassembly that measures both halves.
    if collection._record_on_grant():
        collection._collection_add_cards(member_id, cards)
    else:
        collection._collection_offer(member_id, cards)
    return _card_body(header, cards)


#: Field defaults for the SHOPINIT body. Names measured, values NOT -- see the
#: banner above `MSG_SHOPINIT`. Each is `POL_TM_SHOP_<FIELD>`.
_SHOPINIT_FIELDS = (("N", 1), ("RA", 0), ("M", 10000), ("CP", 0), ("S", 1), ("SP", 0))


def _sell_price(card=None):
    """What one card sells for. See the banner above: ladder[level] * 7/8."""
    forced = os.environ.get("POL_TM_SELL_PRICE")
    if forced:
        try:
            return max(0, int(forced, 0))
        except ValueError:
            pass
    tbl = cardtables._cardpri_table()
    # The level is the LAST field the client sends for the card -- index 5 on the
    # `@Sell=` wire row, which `_sell_cards` keeps in the row it hands us.
    lvl = typ = None
    if card is not None:
        # WARNING: THE LEVEL IS THE CARD TABLE'S, BY ID -- NOT THE WIRE'S SIXTH VALUE
        # (2026-09-07, 41i). The client prices at 0xB3840 with `ebx` = the
        # CardPrm ROW -- `[ebx]`, `[ebx+2]`, `[ebx+3]` are the baseline stats a
        # few lines up -- so `[ebx+5]` is CardPrm byte 5 looked up by the card's
        # ID. The sixth value the client SENDS is struct+5 = save rec+7 = what
        # this server stored in row slot 6: the table level until `422bc0c4`,
        # the ARROW MASK since. They were equal on every card of the 08-20
        # measurement, which is the only reason reading the wire ever worked;
        # a Fat Chocobo with arrows 20 then sold for ladder[20] * 7/8 = 6562
        # while the client's own shop showed ladder[6] * 7/8 = 656.
        # `POL_TM_SELL_TABLE_LEVEL=0` restores the wire read for an A/B.
        try:
            t2, l2 = cardtables._cardprm(card[0])
        except (TypeError, IndexError):
            t2 = l2 = None
        if common._env_int("POL_TM_SELL_TABLE_LEVEL", 1) and l2 is not None:
            lvl = l2
        else:
            try:
                lvl = int(card[5])
            except (TypeError, ValueError, IndexError):
                lvl = l2
        try:
            typ = int(card[2])
        except (TypeError, ValueError, IndexError):
            typ = t2
        if typ is None:
            typ = t2
    if tbl and lvl is not None and 0 <= lvl < len(tbl):
        # THE MULTIPLIER IS THE CARD TYPE, in eighths. See the banner above.
        num = common._env_int("POL_TM_SELL_NUM", 0) or (7 + max(0, (typ or 0) - 1))
        den = common._env_int("POL_TM_SELL_DEN", 8) or 8
        return max(0, (tbl[lvl] * num) // den)
    # No level (or no table): say so rather than inventing a number silently.
    if card is not None:
        common._say("tm:   WARNING: no level for %r -- falling back to a flat price, which "
             "WILL desync the wallet" % (card,))
    try:
        return max(0, int(os.environ.get("POL_TM_SELL_PRICE", "100"), 0))
    except ValueError:
        return 100


#: How many leading fields identify a card. The wire and the stored row agree on
#: id, attack, type, phys def and magic def and then DIVERGE -- measured
#: 2026-08-18 off one real sale:
#:
#:     stored  [220, 55, 0, 78, 80, 19, 18, 2]
#:     @Sell=   220| 55| 0| 78| 80|     18
#:
#: the wire's sixth value is the stored row's SEVENTH. So matching the whole row
#: would never hit. Five is what both sides mean the same way, and an id plus
#: four rolled stats is specific enough that a collision is two genuinely
#: identical cards, where removing either is the same answer.
_CARD_KEY_FIELDS = 5

#: One card entry on the wire: `@N0=/D=...`, `@0=/D=...` -- the prefix
#: differs between what `_card_body` builds and what `@Sell=` sends, so it
#: is matched loosely and only the pipe-separated values are taken.
_CARD_ENTRY_RE = re.compile(rb"@[A-Za-z]*\d+=/D=([0-9|]+)")


def _parse_card_list(cmd):
    """`@<i>=/D=a|b|c|...` -> a list of rows, each `[a, b, c, ...]`.

    The shape `_card_body` builds and both `@Sell=` and `@Decks=` send back. Any
    `/C=` count is deliberately not trusted: the entries are what we act on, and
    a count that disagrees is worth logging rather than obeying.
    """
    out = []
    for m in _CARD_ENTRY_RE.finditer(cmd):
        try:
            out.append([int(x) for x in m.group(1).split(b"|") if x != b""])
        except ValueError:
            continue
    return out


def _erase_cards(member_id, cmd):
    """Drop discarded cards from the collection. No money changes hands.

    The mirror of `_sell_cards` minus the credit -- which also makes the pair a
    useful control: a discard removes a card and moves nothing else, so if a
    balance ever shifts on a discard, the money path is wrong and not the card
    path. Returns None: nothing is waiting for an answer.
    """
    if member_id is None or not common._env_int("POL_TM_ERASE", 1):
        return None
    gone = _parse_card_list(cmd)
    if not gone:
        common._say("tm: member %s @Erase= carried no card list -- %r"
             % (member_id, cmd[:120]))
        return None
    mode = re.search(rb"/Mode=(-?\d+)", cmd or b"")
    data = collection._collection_load(member_id)
    have = data.get("cards")
    have = [list(c) for c in have] if isinstance(have, list) else []
    missed = []
    for card in gone:
        key = card[:_CARD_KEY_FIELDS]
        for i, row in enumerate(have):
            if row[:_CARD_KEY_FIELDS] == key:
                have.pop(i)
                break
        else:
            missed.append(key)
    data["cards"] = have
    collection._collection_store(member_id, data)
    common._say("tm: member %s DISCARDED %d card(s) %s (Mode=%s) -- %d card(s) left, "
         "money unchanged at %d"
         % (member_id, len(gone), [list(c) for c in gone],
            mode.group(1).decode() if mode else "?", len(have),
            purse.money_of(member_id)))
    if missed:
        common._say("tm: member %s discarded %d card(s) we had NO record of: %s -- the "
             "collection had drifted from the client's"
             % (member_id, len(missed), missed))
    collection._collection_to_save(member_id, have)
    return None


def _sell_cards(member_id, cmd):
    """`@Sell=` -- the player sold cards. Drop them and credit the money.

    Returns None always: there is nothing to answer (see `_sell_price`). What
    matters is that the collection and the balance both move, because the client
    has ALREADY moved its own and will be shown ours on the next `@ShReq=`.
    """
    if member_id is None or os.environ.get("POL_TM_SELL", "1") != "1":
        return None
    sold = _parse_card_list(cmd)
    if not sold:
        common._say("tm: member %s sent @Sell= with no /D= entries -- %r"
              % (member_id, cmd[:80]))
        return None
    data = collection._collection_load(member_id)
    have = data.get("cards")
    have = [list(c) for c in have] if isinstance(have, list) else []
    # WARNING: PRICE EACH CARD, FROM THE CARD. `_sell_price()` used to be called with
    # NO ARGUMENT here, so it could never reach the ladder and every sale paid
    # the flat fallback -- which is how member 3 ended up 57 gold ahead of its
    # own client while the pricing code was already correct.
    #
    # WARNING: And it must be the WIRE row, not the stored one: the level lives at
    # index 5 and `_CARD_KEY_FIELDS` is 5, so the identity prefix drops exactly
    # the field the price depends on. `SOLD ... [[102, 65, 0, 56, 40]]` in the
    # log was the truncated key, and that truncation was the bug.
    gained, missed, prices = 0, [], []
    for card in sold:
        key = card[:_CARD_KEY_FIELDS]
        for i, row in enumerate(have):
            if row[:_CARD_KEY_FIELDS] == key:
                have.pop(i)
                break
        else:
            # NOT AN ERROR TO SWALLOW. The client sold something we never
            # recorded -- a card from a save we did not write, or a grant we
            # missed. Credit it anyway (the player really did sell it) and say
            # so, because a collection drifting from the client's is exactly
            # what the save work exists to stop.
            missed.append(key)
        _p = _sell_price(card)
        prices.append(_p)
        gained += _p
    data["cards"] = have
    was = purse.money_of(member_id)
    data["money"] = max(0, was + gained)
    collection._collection_store(member_id, data)
    common._say("tm: member %s SOLD %d card(s) %s -- money %d -> %d (+%d: %s), "
          "%d card(s) left"
          % (member_id, len(sold), [list(c) for c in sold], was,
             data["money"], gained,
             " + ".join("lvl %s=%d" % (c[5] if len(c) > 5 else "?", pr)
                        for c, pr in zip(sold, prices)) or "nothing",
             len(have)))
    if missed:
        common._say("tm: member %s sold %d card(s) we had NO record of: %s -- credited "
              "anyway; the collection had drifted from the client's"
              % (member_id, len(missed), missed))
    # The price is MEASURED now (four screen readings, one of them an
    # out-of-sample prediction): floor(CardPri[level] * 7/8). What is still
    # fitted is the 7/8 -- 0xB3840 folds card fields into that multiplier -- so
    # keep checking the screen, and a price that is NOT 7/8 of a CardPri entry
    # falsifies the multiplier alone. See the banner above `_sell_price`.
    if any(len(c) <= 5 for c in sold):
        common._say("tm: WARNING: a sold card arrived with no level field -- it was priced by "
              "the flat fallback and WILL desync the wallet. Wire row: %r"
              % ([list(c) for c in sold],))
    return None
