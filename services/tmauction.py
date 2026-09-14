"""Tetra Master's auction: the 0xC0 exhibit record, and the listing store.

THE STORE IS THE SERVED FILE. A member's listings live in exactly one place --
the blob `responders._resource_file("U/g/TM0_EXHIBITLIST")` already serves over
lobby `3:0` -- so the `<SN>` we promise on the auth band and the bytes the lobby
band hands over are read from the SAME file by the same code. That is not
tidiness, it is the fix for POL-5135's shape: a client told "1 record" and given
4 bytes waits for the rest for ever, and the two halves are answered by two
different CONTAINERS, so nothing but a shared file can keep them honest. The
ranking list needed exactly this fix on 2026-08-20; see `_tm_rank_reply`.

RECORD LAYOUT -- 0xC0 = 192 bytes, every field measured, most of them confirmed
on screen 2026-08-20:

    +0x00  <EI>  IDENT[0x38]   seller   -- name at +0x18 INSIDE the struct
    +0x38  <AI>  u32           auction id      (client echoes it on <BH>)
    +0x3C  <SP>  u32           start price
    +0x40  <BI>  u32           minimum raise   -> screen "Bid range"
    +0x44  <ED>  u32           timestamp       (date-formatted, column unknown)
    +0x48  <NC>  u32           END timestamp   -> screen "Time left"
    +0x4C  <LC>  u32           timestamp       (date-formatted, column unknown)
    +0x50  <ET>  u8            duration the client asked for
    +0x51  <AM>  u8            RELIST COUNT (0..3) -- see below
    +0x52  <AC>  u8            ? never rendered
    +0x58  <CB>  IDENT[0x38]   bidder   -- name at +0x70
    +0x90  <CM>  u32           current bid     -> screen "Currently"
    +0x94  <CD>  u32           ? never rendered
    +0x98  <BC>  u32           ? never rendered
    +0xA0  <II>  32B           the card, VERBATIM from the client -> 0xC0 exactly

WARNING: THREE THINGS THIS FILE MUST NOT DO, each of which was measured the hard way:

  * **Do not touch `<II>`.** It is the player's own collection entry, copied
    byte for byte off the selected card (`0x12D997`, `mov ecx,8 / rep movsd`).
    Two of its 16 fields are still unidentified. Store and return it; do not
    normalise, reorder or rebuild it.
  * **Do not write a name at +0x00 or +0x58.** Those are the ID halves of two
    identity structs; the display name is at +0x18 INSIDE each. Bare names at
    the bases render as nothing, which is measured, not theorised.
  * **Do not let `<SN>` and the file disagree.** `count()` and `blob()` read one
    file; keep it that way.

WARNING: AND THE TIME FIELDS ARE INTERPRETED, so a "distinctive" probe value is not a
probe here: "Time left" is `(<NC> - now) / 3600` CLAMPED AT ZERO, so any past
timestamp renders identically to zero. A probe once set `<NC>`=111 and concluded
the field was not on screen at all.

WARNING: **NEVER ACCEPT THE CLIENT'S `03:02` WRITE OF THIS PATH.** The client writes
`U/g/TM0_EXHIBITLIST` and `U/g/TM0_BIDLIST` back up after reading them -- a
cache write-back, visible in `lobby.log` as "not a storable prefix (O/m/) --
captured, NOT stored". That refusal is now LOAD-BEARING: this file is the
authoritative store, and honouring the write would let any client clobber every
listing it holds with its own copy. It was harmless when we served a fixture.
It is not harmless now.

WARNING: **THE PC CLIENT NEVER READS `DI`/`BI` -- DO NOT TRY TO DECODE THEM FROM
`TM.dll`.** It loads 968 bytes into `0x297C20` and discards them. Three checks
agree: that buffer has EXACTLY ONE reference in the whole image (the `push`
that loads it), the literal `0x3C8` appears EXACTLY ONCE (no copy, no loop
bound, no comparison), and `sqMgCpLoadPlayerSaveDataCheck` returns only a
status. There is no parser to read the format off.

WARNING: There is precedent for the split: `responders.py` calls kind 0 (`RM`)
"unreachable, not uncertain" **read off this same PC `TM.dll`**, and the PS2
build then fetched it. The decrypted PS2 module is not in the tree (only
`TMaster.pex.enc`), so that route needs the console-bound decrypt first.

OK: **SETTLEMENT ON PC IS A MESSAGE PROTOCOL, AND IT IS DECODED.** The Check Out
scene (rva ~`0x133000`-`0x136000`) waits on exactly TWO commands, found by
listing `0x101A10` waiters -- the same wait-for-command family as the rest of
the shop:

    0x13498B  cmd 0x20 -> arm 0x1060B4  @Data  Recv=AUCMONEY   /M=
    0x134F97  cmd 0x21 -> arm 0x10613D  @Card  Recv=AUCCARDS   /E= /S= /C= + @N<i>=/D=

So the two things Check Out collects -- money and cards -- arrive as MESSAGES,
which is why the `DI`/`BI` files are loaded and ignored on this build.

VERIFIED: **AND `AUCCARDS` IS THE CARD SHOP'S OWN `@Card=` SHAPE**, so no new record
format is needed: `0x106200` formats `"@N%d"` in a loop over `/S=` and parses
each one's `/D=`, exactly the `@N<i>=/D=` records already decoded for the shop and
`tetramaster.py` already builds. `/E=` gates the whole list -- `0x1061BC` bails
on `<= 0` -- and is stored as a WORD at `[esi+0xf0]`.

WARNING: The arm gates on the sender id (`[esi+0x28]`/`[esi+0x2C]`) and the shop byte
(`[esi+0x30]`) before it reads a field, the same three gates SHOPBUY/SHOPQUIT
use -- so echoing the request's 8-hex header satisfies them for free, and
getting them wrong looks like the message never arrived.

**THE PRICE LIST IS FIVE BANDS AND WE HAVE NEVER SERVED ONE.** The auction menu
offers All / Cheap / Affordable / Expensive / Exorbitant Cards, each with its
own count, greyed at zero -- that is the BROWSE half, and it is what `<SI>`'s
twelve criteria values are for. Opening the menu sends nothing (measured
2026-08-20), so the counts are the client's own state; the query goes out when a
band is chosen. `<IO>`/`<IB>` select "my sales"/"my bids" and send those
criteria all-zero, which is why they are the only two shapes we have ever seen.
"""
import json
import os
import struct
import time

REC = 0xC0
IDENT_LEN = 0x38
IDENT_NAME_OFF = 0x18

#: offset, struct code -- the scalar half of the record.
_SCALARS = (
    ("AI", 0x38, "<I"), ("SP", 0x3C, "<I"), ("BI", 0x40, "<I"),
    ("ED", 0x44, "<I"), ("NC", 0x48, "<I"), ("LC", 0x4C, "<I"),
    ("ET", 0x50, "<B"), ("AM", 0x51, "<B"), ("AC", 0x52, "<B"),
    ("CM", 0x90, "<I"), ("CD", 0x94, "<I"), ("BC", 0x98, "<I"),
)
_EI_OFF, _CB_OFF, _II_OFF = 0x00, 0x58, 0xA0

#: `<II>`: four u32, then four u16 at +0x10, then eight u8 at +0x18 (reader
#: TM.dll 0x1A54B0, writer 0x1A58D0 -- exact inverses).
_II_BLOCKS = ((4, "<I", 4), (4, "<H", 2), (8, "<B", 1))


def pack_ii(vals):
    """The 16 wire values of an `<II>` group as its 32 bytes."""
    if len(vals) != 16:
        raise ValueError("<II> takes 16 values, got %d" % len(vals))
    out, i = bytearray(), 0
    for count, fmt, _w in _II_BLOCKS:
        for k in range(count):
            out += struct.pack(fmt, vals[i + k])
        i += count
    return bytes(out)


def unpack_ii(blob):
    """The 32 bytes back as 16 wire values, so a stored row can be re-served."""
    out, off = [], 0
    for count, fmt, width in _II_BLOCKS:
        for _ in range(count):
            out.append(struct.unpack_from(fmt, blob, off)[0])
            off += width
    return out


def _ident(name):
    """A 0x38-byte identity field with `name` at +0x18.

    The ID half is left zero: its layout is unread, and the two screens that
    render this record take the name and nothing else. Do not read the zeros as
    evidence the half is unused.
    """
    f = bytearray(IDENT_LEN)
    b = (name or "").encode("latin1", "replace")[:IDENT_LEN - IDENT_NAME_OFF - 1]
    f[IDENT_NAME_OFF:IDENT_NAME_OFF + len(b)] = b
    return bytes(f)


def _ident_name(blob, off):
    raw = blob[off + IDENT_NAME_OFF:off + IDENT_LEN]
    return raw.split(b"\x00", 1)[0].decode("latin1", "replace")


def build_record(ii, seller="", bidder="", **kw):
    """One 0xC0 record. `ii` is the 16 wire values of the client's `<II>`."""
    r = bytearray(REC)
    r[_EI_OFF:_EI_OFF + IDENT_LEN] = _ident(seller)
    r[_CB_OFF:_CB_OFF + IDENT_LEN] = _ident(bidder)
    for name, off, fmt in _SCALARS:
        struct.pack_into(fmt, r, off, int(kw.get(name.lower(), 0)))
    r[_II_OFF:_II_OFF + 0x20] = pack_ii(ii)
    return bytes(r)


def read_record(blob, i=0):
    """Record `i` of a list blob as a dict, for re-serving or for a reply."""
    b = blob[i * REC:(i + 1) * REC]
    if len(b) != REC:
        raise ValueError("record %d is short (%d bytes)" % (i, len(b)))
    d = {name.lower(): struct.unpack_from(fmt, b, off)[0]
         for name, off, fmt in _SCALARS}
    d["seller"] = _ident_name(b, _EI_OFF)
    d["bidder"] = _ident_name(b, _CB_OFF)
    d["ii"] = unpack_ii(b[_II_OFF:_II_OFF + 0x20])
    return d


def count(blob):
    """Records in a list blob -- the number `<SN>` must carry."""
    return len(blob) // REC if blob else 0


def duration_seconds(et):
    """`<ET>` as seconds. The client sends the duration it asked for; the screen
    reads `<NC>`, so this is the bridge between the two.

    OK: HOURS, CONFIRMED ON SCREEN 2026-08-20. Two listings went out at `<ET>`
    121 and 145; a tester reported the rendered "Time left" as accurate for
    what they had picked. Since the screen reads `<NC>` and `<NC>` is built here
    as `now + ET*3600`, an agreeing countdown exercises the whole chain -- the
    unit, the epoch, and the `/3600` -> `/24` truncation at `0x12222C`.
    """
    try:
        et = int(et)
    except (TypeError, ValueError):
        et = 0
    return max(1, et) * 3600


def new_listing(ii, seller, sp, bi, et, am, auction_id, now=None):
    """The record for a fresh `<ER>`, timestamps filled in.

    `<ED>` and `<LC>` are both date-formatted somewhere and both get the listing
    time; which calendar column each fills is unread, so they are not guessed
    apart. `<NC>` is the one that matters -- it drives "Time left".
    """
    now = int(now if now is not None else time.time())
    return build_record(
        ii, seller=seller, bidder="",
        ai=auction_id, sp=sp, bi=bi,
        ed=now, nc=now + duration_seconds(et), lc=now,
        et=min(255, int(et or 0)), am=am, ac=0, cm=0, cd=0, bc=0)


def es_groups(rec):
    """The `<ES>` success reply for a stored record, as polpro groups.

    The lead group picks the handler (`0x1A3EEE` routes on the FIRST group's
    tag), and `0x1A5650` then unpacks the rest BY TAG into the caller's
    192-byte buffer -- so order beyond the lead does not matter, but the lead
    must be `<ES>` and nothing else.

    WARNING: `<EI>`/`<CB>` go out as PLAIN STRINGS here, not identity structs: the
    reply unpacker writes them with the string reader `0x1A5550` to the result
    struct's +0x00/+0x58, which is a different consumer from the list row.
    """
    g = [("ES", [str(rec["ai"])])]
    for name, _off, _fmt in _SCALARS:
        g.append((name, [str(rec[name.lower()])]))
    g.append(("EI", [rec.get("seller", "")]))
    g.append(("CB", [rec.get("bidder", "")]))
    g.append(("II", [str(v) for v in rec["ii"]]))
    return g


#: The Price List's five rows, in `b/g/TM0AucData` order -- which is menu order,
#: confirmed by serving 11/22/33/44/55 and reading them straight down the screen.
#:
#: THE RANGES ARE THE CLIENT'S, not ours: each band sends its own `<MP>(lo,hi)`
#: and we filter on it. Measured 2026-08-20 by clicking the rows:
#:     Cheap      <MP>(0,1000)       19:26:09Z
#:     Affordable <MP>(1001,2000)    19:31:54Z
#:     Expensive  <MP>(2001,5000)    19:32:37Z
#:     Exorbitant <MP>(5001,99999999) 19:34:53Z
#: WARNING: ALL FOUR ARE NOW MEASURED, and the top one is BOUNDED: 99999999, the same
#: eight-digit money cap `CoPrm.BIN`'s last title row uses. The guess
#: here was `None` for "no upper bound" -- directionally right, wrong in fact,
#: and it would have silently mis-counted anything above the cap. "All" carries
#: no `<MP>` at all, which is why its range is (None, None).
#:
#: WARNING: AND THESE ARE FOR COUNTING ONLY. The FILTER always uses the range the
#: request carried, never this table: if SE's client and this table ever
#: disagree, the client is right and the counts are what is wrong.
BANDS = (
    ("All",        None, None),
    ("Cheap",      0,    1000),
    ("Affordable", 1001, 2000),
    ("Expensive",  2001, 5000),
    ("Exorbitant", 5001, 99999999),
)


def band_counts(blob):
    """The five Price List counts for a list blob, in `b/g/TM0AucData` order."""
    return [count(blob) if lo is None and hi is None
            else count(filter_band(blob, lo, hi))
            for _name, lo, hi in BANDS]


def asking_price(rec):
    """What a listing "costs" for the Price List bands.

    WARNING: AN ASSUMPTION, and the one to revisit if a band's count disagrees with
    the list it opens: the current bid if there is one, else the start price.
    That is what the row itself shows -- "Currently" is `<CM>` and falls back to
    nothing until somebody bids -- but the client sends us the RANGE and lets us
    do the filtering, so the choice of field is ours and is not measured.
    """
    return rec["cm"] or rec["sp"]


def in_band(rec, lo, hi):
    """Is this listing inside a `<MP>(lo,hi)` price band?

    Bounds are treated as INCLUSIVE. `<MP>(0,1000)` for "Cheap Cards" is the
    only range measured so far, and 0 as a lower bound only makes sense
    inclusive; the upper bound is a guess in the same direction. A listing
    priced exactly on a boundary is the case that would prove it.
    """
    p = asking_price(rec)
    return (lo is None or p >= lo) and (hi is None or p <= hi)


def filter_band(blob, lo, hi):
    """The rows of `blob` inside a price band, re-joined. Order is preserved."""
    keep = [blob[i * REC:(i + 1) * REC] for i in range(count(blob))
            if in_band(read_record(blob, i), lo, hi)]
    return b"".join(keep)


def max_auction_id(blob):
    """The highest `<AI>` in a list blob, or 0.

    WARNING: `<AI>` MUST BE GLOBALLY UNIQUE, not per member. It is the only thing a
    `<BH>` or a `<BR>` carries to say WHICH auction, and once a row can reach a
    player through the BROWSE list -- everybody's listings in one file -- a
    per-member id cannot identify one. Minting from the max across every store
    is self-healing: it needs no counter file to get out of step with the rows.
    """
    return max((read_record(blob, i)["ai"] for i in range(count(blob))),
               default=0)


#: THE BID HISTORY RECORD -- 0x48 = 72 bytes, a DIFFERENT stride from the 0xC0
#: exhibit record. Read off the client, not guessed:
#:   * `sqMgAccpReadBidList` (0x1A4A70) asks for `n*9<<3` = n*72;
#:   * the row getter `0x131A40` is `base + i*72`;
#:   * `0x1319A0` bubble-sorts the array in place with `rep movsd` of 0x12 dwords.
#: The renderer (`0x1253A6`, `0x1254D6`) touches exactly three fields, and
#: `+0x00..0x17` / `+0x44..0x47` it never reads at all.
BID_REC = 0x48
BID_NAME_OFF = 0x18     #: passed to the text drawer 0x19AEB0 -- a string, drawn
BID_AMOUNT_OFF = 0x3C   #: money, comma-formatted; ALSO the ascending sort key
BID_TIME_OFF = 0x40     #: handed to the date formatter [0x24DFE8]+0xAD0


#: OUR bookkeeping, in space the client never reads. The renderer touches only
#: +0x18, +0x3C and +0x40; +0x00..0x17 and +0x44..0x47 it never looks at. So the
#: bidder's MEMBER ID goes at +0x00, which is what makes "Cards Bid On" answerable
#: without matching on display names -- names collide, get renamed, and are not
#: identity. WARNING: Rows written before this existed carry 0 here; `bid_is_member`
#: falls back to the name for exactly those.
BID_MEMBER_OFF = 0x00


def build_bid(name, amount, when, member_id=0):
    """One 0x48 bid-history row.

    WARNING: The name goes in as a BARE STRING at +0x18, not as an identity struct.
    That is not an inconsistency with the exhibit record: there the renderer
    reads +0x18 INSIDE a 0x38-byte struct based at +0x00, and here it reads
    +0x18 of the record itself. Same offset, different base -- and the bid row
    has no room for a 0x38 struct before its money field at +0x3C anyway.
    """
    r = bytearray(BID_REC)
    b = (name or "").encode("latin1", "replace")[:BID_AMOUNT_OFF - BID_NAME_OFF - 1]
    r[BID_NAME_OFF:BID_NAME_OFF + len(b)] = b
    struct.pack_into("<I", r, BID_AMOUNT_OFF, int(amount) & 0xFFFFFFFF)
    struct.pack_into("<I", r, BID_TIME_OFF, int(when) & 0xFFFFFFFF)
    struct.pack_into("<I", r, BID_MEMBER_OFF, int(member_id or 0) & 0xFFFFFFFF)
    return bytes(r)


def read_bid(blob, i=0):
    b = blob[i * BID_REC:(i + 1) * BID_REC]
    if len(b) != BID_REC:
        raise ValueError("bid %d is short (%d bytes)" % (i, len(b)))
    name = b[BID_NAME_OFF:BID_AMOUNT_OFF].split(b"\x00", 1)[0]
    return {"name": name.decode("latin1", "replace"),
            "amount": struct.unpack_from("<I", b, BID_AMOUNT_OFF)[0],
            "when": struct.unpack_from("<I", b, BID_TIME_OFF)[0],
            "member": struct.unpack_from("<I", b, BID_MEMBER_OFF)[0]}


def bid_is_member(bid, member_id, name):
    """Did `member_id` place this bid?

    Prefers the stored member id and falls back to the display name, because
    bids recorded before BID_MEMBER_OFF existed carry 0 there. The fallback is
    deliberately narrow -- it only applies when the id is absent -- so a rename
    cannot silently reassign somebody else's bid.
    """
    if bid.get("member"):
        return bid["member"] == member_id
    return bool(name) and bid["name"] == name


def bid_count(blob):
    """Rows in a bid-history blob -- the number `<SN>` must carry for `<HS>`."""
    return len(blob) // BID_REC if blob else 0


def sort_bids(blob):
    """Bids ascending by amount -- the order the client sorts into anyway.

    `0x1319A0` bubble-sorts the array on `+0x3C` after loading it, so serving
    them sorted changes nothing the player sees. It is done here so that the
    file, the count and the screen all agree on an order, which makes a
    mismatch a diff rather than a puzzle.
    """
    rows = sorted((blob[i * BID_REC:(i + 1) * BID_REC]
                   for i in range(bid_count(blob))),
                  key=lambda r: struct.unpack_from("<I", r, BID_AMOUNT_OFF)[0])
    return b"".join(rows)


#: OK: `<AM>` IS A RELIST COUNT -- how many times a listing re-lists if it does
#: not sell, before it goes back to the player as UNSOLD. Straight off the
#: menu's own explanatory text, and confirmed on the wire: a listing made with
#: the option set to 2 arrived as `<AM>`(2) (auction 6, 20:35:16Z).
#:
#: WARNING: AND A RETRACTION OF MY OWN, WORTH KEEPING: this was read as a BITMASK an
#: hour earlier, from the `or` at `0x13326B`:
#:     mov eax,[esi+0x188] ; mov ecx,[esp+8] ; or eax,ecx ; mov [esi+0x188],eax
#: The `or` is real -- but `0x1324E1` ZEROES the field on form reset, and OR-ing
#: into zero is indistinguishable from assignment. The instruction supported
#: both readings and I picked one. What settled it was the menu text, not the
#: disassembly. **An `or` into a field that is always cleared first proves
#: nothing about bit-ness.**
#:
#: THE END-OF-AUCTION RULE, now fully specified:
#:     <NC> passes
#:       bids  (<BC> > 0) -> SOLD: the winner PAYS at Check Out, and Messenger
#:                           sends the notice (the Check Out menu text)
#:       no bids, <AM> > 0 -> relist with <AM>-1 and a fresh <NC>
#:       no bids, <AM> = 0 -> back to the player, UNSOLD
#:
#: VERIFIED: THE RELIST HALF NEEDS NOTHING WE DO NOT HAVE -- it rewrites `<NC>` and
#: decrements `<AM>` in a record we already own. The SOLD and UNSOLD halves need
#: the 968-byte `DI`/`BI` layout, which is not decoded (see the module
#: docstring), because that is how a settled auction reaches the player.


def am_options(am):
    """`<AM>` as the set of bits it carries -- e.g. 3 -> (1, 2)."""
    return tuple(1 << i for i in range(8) if int(am or 0) & (1 << i))


def expired(rec, now):
    """Has this listing passed its `<NC>` end time?"""
    return int(rec["nc"]) and int(rec["nc"]) <= int(now)


def relist(rec_bytes, now):
    """A listing re-listed once: fresh `<NC>`, `<AM>` decremented, bid cleared.

    The menu calls `<AM>` "the amount of times an item will be relisted if it
    doesn't sell before it returns to the player as unsold", so a relist SPENDS
    one and the count is what terminates the loop.

    `<CM>`, `<BC>` and the `<CB>` name are reset because the new listing has no
    bids -- leaving a stale bidder would show the seller a bid nobody placed on
    a listing that is running again. `<II>`, `<SP>`, `<BI>`, `<ET>` and `<AI>`
    all survive: it is the SAME card at the same terms, and keeping `<AI>`
    keeps any bid history already attached to it addressable.
    """
    r = bytearray(rec_bytes)
    d = read_record(bytes(r))
    struct.pack_into("<I", r, 0x48, int(now) + duration_seconds(d["et"]))  # <NC>
    struct.pack_into("<I", r, 0x44, int(now))                             # <ED>
    struct.pack_into("<I", r, 0x4C, int(now))                             # <LC>
    r[0x51] = max(0, int(d["am"]) - 1)                                    # <AM>
    struct.pack_into("<I", r, 0x90, 0)                                    # <CM>
    struct.pack_into("<I", r, 0x98, 0)                                    # <BC>
    r[_CB_OFF:_CB_OFF + IDENT_LEN] = _ident("")                           # <CB>
    return bytes(r)


def min_bid(rec):
    """The least a bid may be: current bid + the minimum raise.

    Measured twice on the client's own default: a listing showing
    "Currently 9999 / Bid range 500" offered 10,499, and auction 1 with
    `<CM>`=0 / `<BI>`=50 produced `<BM>`(50).

    WARNING: `<SP>` TAKES NO PART, which is worth noticing rather than smoothing over:
    auction 1 was listed at `<SP>`=287 and the client still offered 50. So
    either `<SP>` is not a reserve price, or the client does not enforce one.
    Do not "fix" this by folding `<SP>` in -- that would reject bids the client
    itself proposes, which is the worst kind of disagreement.
    """
    return rec["cm"] + rec["bi"]


def apply_bid(rec_bytes, bidder, amount, bid_count=1):
    """A listing with the bid applied: `<CM>`, the `<CB>` name, and `<BC>`.

    OK: `<BC>` IS THE BID COUNT AND IT GATES THE BIDDER COLUMN -- MEASURED at
    `0x12205E`, not inferred:

        mov edx, [esi+0x90]        ; <CM>, drawn as "Currently"
        cmp dword [esi+0x98], ebx  ; <BC> vs 0
        jle 0x12208E               ; <= 0 -> the else branch, which draws "-"
        lea eax, [esi+0x70]        ; > 0 -> draw the BIDDER NAME
        push 0x131

    So the name at +0x70 is only reached when `<BC>` > 0, which is exactly why
    a listing with a real bid, a non-zero `<CM>` and a correct name still
    rendered "-". It was first INFERRED from a render diff (`--strprobe` had
    `<BC>`=55 and the name drew; a live listing with `<BC>`=0 did not) and then
    confirmed by opening the function -- the order this project keeps relearning.

    WARNING: `<CD>` (+0x94) and `<AC>` (+0x52) are STILL UNREAD. Neither has an
    absolute reference on the selected-row copy and neither turned up in the
    list-row renderers, so whatever reads them is elsewhere -- Check Out is the
    obvious place to look, since that is where a settled auction is handled.

    Returns new bytes; `<II>` and every untouched field survive byte-identical
    because only these three are rewritten.
    """
    r = bytearray(rec_bytes)
    struct.pack_into("<I", r, 0x90, int(amount) & 0xFFFFFFFF)   # <CM> "Currently"
    struct.pack_into("<I", r, 0x98, int(bid_count) & 0xFFFFFFFF)  # <BC> bid count
    r[_CB_OFF:_CB_OFF + IDENT_LEN] = _ident(bidder)             # name -> +0x70
    return bytes(r)


def bs_groups(rec):
    """The `<BS>` success reply -- `<ES>`'s groups with the bid arm leading.

    Same unpacker (`0x1A5650`) fills the same 192-byte struct for both; only
    the LEAD group selects which gate is satisfied (`0x1A3EEE` routes on the
    first tag), so this differs from `es_groups` by exactly that tag.
    """
    return [("BS", [str(rec["ai"])])] + es_groups(rec)[1:]


# ---------------------------------------------------------------- pending ---
# WHAT A SETTLED AUCTION OWES, per member, until Check Out collects it.
#
# This is OUR bookkeeping, not a client format, so it is JSON: nothing parses it
# but us, and a readable file is worth more than a packed one when somebody has
# to work out why a player is owed a card.
#
# WARNING: IT IS WHAT MAKES DELETING A LISTING SAFE. The sweep held sold and unsold
# rows in place precisely because dropping one would destroy the card it holds.
# A listing may only be removed AFTER its card and money have landed here --
# write pending first, remove second, and on any failure leave the listing
# alone. The worst case then is a listing that settles twice, which shows up as
# a duplicate a player can report; the alternative is a card that silently
# ceases to exist.

RESOURCE_DIR = os.environ.get("POL_RESOURCE_DIR", "/data/resources")


def pending_file(member):
    return os.path.join(RESOURCE_DIR, "auction-pending-%s.json" % member)


def pending(member):
    """`{"money", "cards", "won"}` for a member.

    `cards` are RETURNED cards (unsold, ride Check Out's cards-A = "Returned
    Card"); `won` are cards WON at auction (ride cards-B = "Successful Bid").
    They are two Check Out sections with two different labels, so they cannot
    share a list -- a won card in `cards` renders as "returned" (measured).
    """
    try:
        with open(pending_file(member), "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"money": 0, "cards": [], "won": [], "refund": 0}
    return {"money": int(d.get("money") or 0),
            "cards": [list(c) for c in d.get("cards") or [] if len(c) == 16],
            "won": [list(c) for c in d.get("won") or [] if len(c) == 16],
            "refund": int(d.get("refund") or 0)}


def add_pending(member, money=0, card=None, won=None, refund=0):
    """Credit a member money (sale proceeds, Check Out money-A "Collecting
    Payment"), a RETURNED card (cards-A), a WON card (cards-B), and/or a bid
    REFUND (money-B "Bid Refund" -- gil held on a bid that was then outbid).
    Returns the new pending dict.

    Raises on a write failure -- the caller MUST NOT remove the listing/hold if
    this does not land.
    """
    d = pending(member)
    d["money"] = int(d["money"]) + int(money or 0)
    d["refund"] = int(d.get("refund") or 0) + int(refund or 0)
    if card is not None:
        d["cards"].append([int(v) for v in card])
    if won is not None:
        d["won"].append([int(v) for v in won])
    tmp = pending_file(member) + ".tmp"
    os.makedirs(RESOURCE_DIR, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, pending_file(member))
    return d


def clear_pending(member, money=False, cards=False, won=False, refund=False):
    """Drop what Check Out has just collected. Selective, because proceeds,
    returned cards, won cards and bid refunds are four different messages and
    any may fail."""
    d = pending(member)
    if money:
        d["money"] = 0
    if cards:
        d["cards"] = []
    if won:
        d["won"] = []
    if refund:
        d["refund"] = 0
    tmp = pending_file(member) + ".tmp"
    os.makedirs(RESOURCE_DIR, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, pending_file(member))
    return d


def card_row_from_ii(ii):
    """The 8 `/D=` values of an `@N<i>=` record, from a stored `<II>`.

    AUCCARDS reuses the CARD SHOP's record shape (arm `0x10613D` formats
    `"@N%d"` over `/S=` and parses each one's `/D=`), and the shop decode reads that row
    as: id, attack, type, physdef, magicdef, then three more.

    WARNING: `<II>` IS NOT IN THAT ORDER. Its u8 block is attack, physDEF, TYPE,
    magicdef -- 1 and 2 transposed, measured on three cards and confirmed on
    screen. Getting this wrong swaps every card's type with its defence, which
    renders as a plausible card and is therefore the kind of bug nobody spots.
    """
    u8 = ii[8:16]
    return [ii[0], u8[0], u8[2], u8[1], u8[3], u8[4], u8[5], u8[6]]


def enabled():
    return os.environ.get("POL_TM_AUCTION", "1") == "1"
