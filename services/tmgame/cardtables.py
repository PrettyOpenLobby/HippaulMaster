"""The client's parameter tables read from services/tmdata: CardPrm, card names, PackPrm, CardPri,
and the card level.
"""
import os
import struct
from . import collection, common, deps, protocol


#: WARNING: THE SALE PRICE IS **NOT MEASURED**, AND IT IS THE ONE NUMBER HERE THAT IS
#: NOT. `@Sell=` is fire-and-forget: TM.dll carries two command tables, one
#: space-terminated for messages it RECEIVES (`@EQuit`, `@Card`, `@CardSelect`)
#: and one `=`-terminated for what it SENDS (`@Buy=`, `@Quit=`, `@Sell=`), and
#: there is no `@ESell` anywhere in the image. So the client prices the card
#: itself, updates its own money, and only tells us WHICH card went. It never
#: reports the balance -- `@Save=` carries the table settings (`@Tab=`/`@Tet=`,
#: 11r) and `@PutData=` / `@Data=` have never been seen on a wire.
#:
#: So the server has to price it, and until somebody sells one card and reads
#: the money off the screen, any rule here is invented. A flat rate is the least
#: pretending: a formula would imply we know the shape.
#:
#: WARNING: PINNING IT TAKES ONE SALE. `_sell_cards` logs the card and the balance
#: either side; compare the on-screen money before and after, then set
#: `POL_TM_SELL_PRICE` -- or, if the delta varies by card, say so and this
#: becomes a function of the CardPrm row instead of a constant.
#:
#: NOTE: AND THERE IS A HYPOTHESIS WORTH TESTING FIRST, because if it holds the
#: price stops being ours to guess at all. SHOPINIT carries `/CP=` and `/SP=`,
#: two fields whose NAMES are measured and whose VALUES never were (see the
#: banner above `MSG_SHOPINIT`). **The server sends both.** If `SP` is the Sell
#: Price and `CP` the Card Price, then we DICTATE what the client pays and pays
#: out, and server and client agree by construction rather than by luck.
#:
#: The prediction is sharp and costs one sale: we currently send `/SP=0`. If SP
#: is the sell price, a card sells for NOTHING and the player's money does not
#: move. So --
#:
#:     money did NOT change on sale  ->  SP is (very likely) the price, and
#:                                       POL_TM_SHOP_SP is the real knob; set
#:                                       POL_TM_SELL_PRICE to the same number
#:     money went UP by X            ->  SP is not it; X is the price, and
#:                                       POL_TM_SELL_PRICE=X is exact
#:
#: Either answer closes this. Do not wire `@Buy=` to debit until it is settled:
#: pack prices are unknown, the Pauper's Pack is FREE, and a wrong debit desyncs
#: money in the direction the player notices most.
#: VERIFIED: THE SELL PRICE, SOLVED 2026-08-20 -- it is a LADDER LOOKUP, not a constant.
#:
#: A tester read three prices off the shop screen and the wire gave the cards:
#:
#:     15|14|0|10|12|**2**  ->  43        49|54|1|43|13|**5**  ->  437
#:     Cinna (level 4)      ->  218
#:
#: `data/CardPri.BIN` is a 25-entry u32 table after a 16-byte header:
#:
#:     0, 25, 50, 100, 250, 500, 750, 1000, 1500, 2500, 5000, 10000, | 2500, ...
#:
#: and the client's own pricing routine is at **0xB3840**: it loads the table
#: pointer from the global at rva 0x2B6368 (filled by the `CardPri.BIN` loader at
#: 0x188CBF), indexes it with `[ebx+5]` -- the card's LEVEL, which is exactly the
#: last field `@Sell=` sends -- and scales the result with float math built from
#: 1.0 (0x205DB8), 0.5 (0x2049F8) and 0.25 (0x204528):
#:
#:     0xB3877  mov al, [ebx+5]           the level
#:     0xB3892  mov edx, [ecx + eax*4]    base = ladder[level]
#:     0xB38DB  call 0x1E2790             float -> int
#:
#: All three prices are `floor(ladder[level] * 7/8)` exactly:
#:
#:     50 * 0.875 = 43.75  -> 43        250 * 0.875 = 218.75 -> 218
#:     500 * 0.875 = 437.5 -> 437
#:
#: VERIFIED: AND THE MULTIPLIER IS THE CARD **TYPE**, in eighths. Solved 2026-08-20
#: from six sold cards spanning all four types -- a tester read the prices
#: off the shop screen and selling put each card's full row on the wire:
#:
#:     id   atk type pdef mdef lvl   ladder   price    mult
#:     15    14   0    10   12   2       50      43    7/8
#:    102    65   0    56   40   6      750     656    7/8
#:     49    54   1    43   13   5      500     437    7/8
#:    234     5   1    50  100  15      500     437    7/8   [Magic Urn]
#:    161   100   2    90   99  21    10000   10000    8/8   [Braska]
#:    158    97   3    89   95  24    17500   19687    9/8   [Masked Rikku]
#:
#:     multiplier = (7 + max(0, type - 1)) / 8
#:
#: WARNING: AND THE STATS DO NOT MATTER, which is what makes this a rule rather than a
#: fit. Cards 15 (14/10/12) and 102 (65/56/40) have wildly different stats, are
#: both type 0, and are both 7/8; 49 (54/43/13) and 234 (5/50/100) likewise are
#: both type 1 and both 7/8. Only the type moves it. Those are Tetra Master's
#: P/M/X/A types, and the rarer X and A sell for more.
#:
#: WARNING: TYPES 4+ ARE UNSEEN. `@VsGameInit`'s parser bounds the type at 4
#: (tmsave's TYPE_MAX, `0x5091383 cmp byte [edi+1], 4 / jae`), so a type-4 card
#: would be 10/8 under this rule -- untested. POL_TM_SELL_NUM forces a numerator.
#:
#: WARNING: Three earlier readings were wrong before this: a flat 100, then "7/8
#: always" (fitted to four same-type cards), then "the stats are in the
#: multiplier". The type only became visible once cards of types 2 and 3 were
#: actually SOLD -- reading the price alone never carried the stats.
#: VERIFIED: THE CARD TABLE, DECODED 2026-08-20. `data/CardPrm.BIN` is 12-byte records
#: from offset 16, indexed by CARD ID:
#:
#:     byte 0 = attack   1 = TYPE   2 = phys def   3 = mag def   5 = LEVEL
#:
#: Validated against all six sold cards -- byte 5 reproduces their levels (2, 5,
#: 6, 15, 21, 24) and byte 1 their types exactly. 269 real records; the rest of
#: the 480 are padding with nonsense types.
#:
#: WARNING: AND IT SETTLES THE TYPE-4 QUESTION: the real records carry ONLY types
#: 0 (145), 1 (91), 2 (21) and 3 (12). There is no type 4, so
#: `(7 + max(0, type-1))/8` covers every card that exists.
#:
#: Why ship it: the wire row carries type and level, but a card that arrives
#: WITHOUT them used to fall back to a flat price and silently desync the
#: wallet. With this table an id is enough.
_CARDPRM = None


#: *** THE CARD LEVEL, PORTED FROM THE CLIENT'S OWN 0xB9040 (2026-09-08). ***
#:
#: WARNING: THIS IS THE NUMBER THE VS. COM OPPONENT UNLOCK IS GATED ON, and this
#: server was overwriting it with a ZERO on every VS. COM open -- see
#: `_comgame_body`. The client keeps it live: SEVEN sites call `0xB9040` and
#: store the result into `word [0x52463E0]` (struct +0x00) during play
#: (0xCB652, 0xDA648, 0xEAEFD, 0xEFFD3, 0xF0075, 0x117ABC, 0x12D046), and the
#: save parser derives it again at load (0x10147F). So "the client only knows
#: this at launch" is FALSE -- it recomputes constantly, and the only thing
#: that made it stale was our own message.
#:
#: The algorithm, read instruction for instruction at 0xB90E6..0xB9192 over the
#: collection's 16-byte memory rows (attack +0, type +1, pdef +2, mdef +3,
#: grade +4, arrows +5, id word +6, deck slot +0xE -- the save loader's own
#: copy order at 0x1013A3..0x1013CB):
#:
#:     total = 0
#:     for each row:
#:         if id >= CardPrm count:            skip entirely      (0xB90F6)
#:         if this id has not been seen yet:                     (0xB90FE)
#:             total += the HIGHEST grade among all copies of it (0xB9112 scans
#:                      the rest of the collection keeping the max, 0xB9131)
#:             mark the id seen
#:         if type > 1:  total += type - 1                       (0xB9146)
#:         if this ARROW MASK has not been seen yet:             (0xB915F)
#:             total += 5; mark the mask seen                    (0xB9171)
#:     return total as a WORD                                    (0xB9192)
#:
#: KEY: So it rewards BREADTH, not duplicates: one copy of each card (its best
#: grade), a bonus for the rarer types, and five per distinct arrow pattern.
#: The dedupe table is per CARD ID and the arrow table is 256 bytes indexed by
#: the mask byte (0xB90A5 zeroes 0x100 bytes), so both are exact.
#:
#: WARNING: The type bonus is per ROW, not per distinct id -- 0xB9143 sits AFTER the
#: dedupe block's join, so a second copy of a type-3 card still pays its 2.
def card_level(rows):
    """The player's CARD LEVEL from their collection -- the client's 0xB9040.

    `rows` are the stored 8-value rows `[id, atk, type, pdef, mdef, grade,
    arrows, deck slot]`. Returns the same u16 the client computes, so the two
    can be compared directly (and so `_comgame_body` can stop sending 0).
    """
    best = {}
    for r in rows or ():
        try:
            cid = int(r[0])
            grade = int(r[5])
        except (TypeError, ValueError, IndexError):
            continue
        if not 0 <= cid < protocol.CARDPRM_RECORDS:
            continue
        if grade > best.get(cid, -1):
            best[cid] = grade
    total = sum(best.values())
    seen_arrows = set()
    for r in rows or ():
        try:
            cid = int(r[0])
        except (TypeError, ValueError, IndexError):
            continue
        if not 0 <= cid < protocol.CARDPRM_RECORDS:
            continue
        try:
            ctype = int(r[2])
        except (TypeError, ValueError, IndexError):
            ctype = 0
        if ctype > 1:
            total += ctype - 1
        try:
            arrows = int(r[6]) & 0xFF
        except (TypeError, ValueError, IndexError):
            continue
        if arrows not in seen_arrows:
            seen_arrows.add(arrows)
            total += 5
    return max(0, min(0xFFFF, total))


def card_level_of(member_id):
    """`card_level` for one member's stored collection."""
    try:
        data = collection._collection_load(member_id)
        rows = data.get("cards")
        return card_level(rows if isinstance(rows, list) else [])
    except Exception as exc:
        common._say("tm: card level unavailable for member %s (%r) -- sending 0, which "
             "LOCKS every VS. COM opponent above the first threshold"
             % (member_id, exc))
        return 0



def _cardprm(card_id):
    """(type, level) for a card id from `CardPrm.BIN`, or (None, None)."""
    global _CARDPRM
    if _CARDPRM is None:
        _CARDPRM = {}
        try:
            here = os.path.dirname(os.path.abspath(deps.FACADE_FILE))
            with open(os.path.join(here, "tmdata", "CardPrm.BIN"), "rb") as f:
                blob = f.read()
            H, R = 16, 12
            for cid in range((len(blob) - H) // R):
                row = blob[H + cid * R:H + cid * R + 6]
                if row[1] <= 3:                 # a real record, not padding
                    _CARDPRM[cid] = (row[1], row[5])
        except Exception as exc:
            common._say("tm: CardPrm.BIN unreadable (%r) -- prices fall back to the "
                 "wire row alone" % (exc,))
    try:
        return _CARDPRM.get(int(card_id), (None, None))
    except (TypeError, ValueError):
        return (None, None)


#: card id -> ENGLISH name (0=Goblin, 21=Cerberus, 75=Cinna, 87=Viltgance).
#: The name ordinal IS the card id. WARNING: The shipped `CardPrm.BIN` is the JAPANESE
#: build (its stats feed `_cardprm`/`_sell_price` and are load-bearing, so it is
#: NOT swapped) -- its name pool is Japanese, wrong for the US Viewer. The US
#: English names ride alongside as `tmdata/card_names_en.txt`, extracted from the
#: US install tree's CardPrm.BIN; the BIN pool is only a last-ditch fallback.
_CARDNAMES = None


def card_name(card_id):
    """The card's US English display name, or "" if unknown -- callers must
    tolerate empty (a notice that cannot name the card still has to send)."""
    global _CARDNAMES
    if _CARDNAMES is None:
        _CARDNAMES = []
        here = os.path.dirname(os.path.abspath(deps.FACADE_FILE))
        try:
            with open(os.path.join(here, "tmdata", "card_names_en.txt"),
                      "r", encoding="utf-8") as f:
                _CARDNAMES = [ln.rstrip("\n") for ln in f]
        except OSError:
            # Last resort: the BIN's own pool (Japanese in the shipped file).
            try:
                with open(os.path.join(here, "tmdata", "CardPrm.BIN"), "rb") as f:
                    blob = f.read()
                pool = struct.unpack_from("<I", blob, 0)[0]
                if 0 < pool < len(blob):
                    _CARDNAMES = [s.decode("cp932", "replace")
                                  for s in blob[pool:].split(b"\x00")]
                common._say("tm: card_names_en.txt missing -- names fall back to the "
                     "BIN pool (Japanese in the shipped build)")
            except Exception as exc:
                common._say("tm: card names unreadable (%r) -- notices go unnamed"
                     % (exc,))
    try:
        cid = int(card_id)
    except (TypeError, ValueError):
        return ""
    if 0 <= cid < len(_CARDNAMES):
        name = _CARDNAMES[cid]
        return "" if name in ("(null)", "") else name
    return ""


#: VERIFIED: PACK PRICES, DECODED 2026-08-20 -- `data/PackPrm.BIN`.
#:
#:     header: u32 size(4020), u32 COUNT(77), u32 1026, u32 checksum
#:     77 records of **52 bytes** from offset 16
#:       record +0  read as a card mask in the first pass; it is not one
#:       record +4  the PRICE, u32
#:
#: 16 + 77*52 = 4020, exactly the size the header declares, which is what
#: confirms the stride. The first six records are the shop's ladder:
#:
#:     No=0      0   <- the FREE Pauper's Pack, and its own record says so
#:     No=1   1000       No=2   1500      No=3   9000
#:     No=4  40000       No=5 100000
#:
#: WARNING: AND `@Buy=/No=<n>` INDEXES THIS TABLE DIRECTLY, validated against the
#: a tester's own purchases: they bought `/No=3` and reported paying **9000**,
#: and `/No=0` (the free pack) is the one that costs nothing. Records beyond the
#: shop ladder are mostly 0 -- prize and event packs, not purchasable.
#:
#: WARNING: THE CLIENT DEBITS ITSELF, exactly as it prices sales itself, so our number
#: has to MATCH rather than lead. `_sell_price`'s banner explains why that
#: matters: `_collection_to_save` writes OUR balance into the save the client
#: loads, so a wrong debit eventually overwrites the right one.
#:
#: VERIFIED: THE REST OF THE RECORD WENT THE SAME WAY, 2026-08-20 -- see the banner
#: above `_draw_pack` for the decode and the evidence. The whole 52 bytes:
#:
#:     +0x00 u32   a packed pair, (K << 20) | 0xFFFFF, K = 4..21. NOT the pool
#:                 -- the low 20 bits are 0xFFFFF in all 77 records, and read
#:                 as a level mask it has a counterexample (see `_draw_pack`).
#:     +0x04 u32   PRICE (gold; the Prize Center's records price in points)
#:     +0x08 u8    KIND: 0 = not for sale (COM decks, Starter's), 1 = shop
#:                 pack, 2 = Pauper's, 3 = rank_1, 4 = Blitzball, 5 = Prize
#:                 Center card. Read off which records carry which name; the
#:                 grouping is exact but the labels are inference.
#:     +0x09 u8    CARDS the pack yields (1 for every single-card record)
#:     +0x0C..0x1B 4 x (level, group, category, cumulative-weight) -- THE POOL
#:     +0x1C u16   unread          +0x1E u16  unread (150 in 75 of 77 records)
#:     +0x20 u16   a CARD ID for the Prize Center's 30 records, else 9999
#:     +0x24..     five u32, 9999 each -- unread
_PACKPRM = None


def _packprm():
    """`PackPrm.BIN` parsed into one dict per record; [] if unreadable."""
    global _PACKPRM
    if _PACKPRM is None:
        _PACKPRM = []
        try:
            here = os.path.dirname(os.path.abspath(deps.FACADE_FILE))
            with open(os.path.join(here, "tmdata", "PackPrm.BIN"), "rb") as f:
                blob = f.read()
            n = struct.unpack_from("<I", blob, 4)[0]
            H, R = 16, 52
            if 0 < n <= 512 and len(blob) >= H + R * n:
                for i in range(n):
                    o = H + R * i
                    _PACKPRM.append({
                        "raw0": struct.unpack_from("<I", blob, o)[0],
                        "price": struct.unpack_from("<I", blob, o + 4)[0],
                        "kind": blob[o + 8],
                        "cards": blob[o + 9],
                        "draw": [tuple(blob[o + 12 + 4 * e:o + 16 + 4 * e])
                                 for e in range(4)],
                        "card": struct.unpack_from("<H", blob, o + 0x20)[0],
                    })
        except Exception as exc:
            common._say("tm: PackPrm.BIN unreadable (%r) -- packs stay free and draw "
                 "from the whole table" % (exc,))
    return _PACKPRM


def _pack_rec(no):
    """The record `@Buy=/No=<no>` names, or None if there is no such pack."""
    try:
        i = int(no)
    except (TypeError, ValueError):
        return None
    table = _packprm()
    return table[i] if 0 <= i < len(table) else None


def _pack_price(no):
    """What pack `/No=<no>` costs, or None if the table has no such record."""
    rec = _pack_rec(no)
    return None if rec is None else rec["price"]


_CARDPRI = None


def _cardpri_table():
    """The 25-entry price ladder from `CardPri.BIN`, or [] if it is missing."""
    global _CARDPRI
    if _CARDPRI is None:
        _CARDPRI = []
        try:
            here = os.path.dirname(os.path.abspath(deps.FACADE_FILE))
            with open(os.path.join(here, "tmdata", "CardPri.BIN"), "rb") as f:
                blob = f.read()
            n = struct.unpack_from("<I", blob, 4)[0]
            if 0 < n <= 256 and len(blob) >= 16 + 4 * n:
                _CARDPRI = [struct.unpack_from("<I", blob, 16 + 4 * i)[0]
                            for i in range(n)]
        except Exception as exc:
            common._say("tm: CardPri.BIN unreadable (%r) -- sell prices fall back to "
                 "POL_TM_SELL_PRICE" % (exc,))
    return _CARDPRI
