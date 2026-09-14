#!/usr/bin/env python3
"""Build Tetra Master auction EXHIBIT records -- `U/g/TM0_EXHIBITLIST`.

ONE RECORD IS 0xC0 = 192 BYTES, and that is not a guess from a hex dump: three
independent readings agree on it.
  * `sqMgAccpReadExhibitList` (TM.dll rva 0x1A46A0) asks for `n*3<<6` = n*192;
  * `sqMgAccpExhibitRequestCheck` (0x1A4140) copies 0x30 dwords = 192 bytes out;
  * `responders._fetch_len` has served these lists at a 0xC0 stride since
    2026-08-16, measured the hard way (padded to 668, the screen ERRORS OUT).
The `0x48` in older notes is the BID list's stride (`sqMgAccpReadBidList`,
0x1A4A70, `n*9<<3`) and was wrongly generalised to both.

THE LAYOUT BELOW IS READ OFF `0x1A5650`, the `<ES>` REPLY unpacker, which fills
a 192-byte struct one tag at a time.

WARNING: **AND THE FILE RECORD IS NOT THAT STRUCT -- at least not for the strings.**
Every NUMERIC field lines up on screen (`<AI> <SP> <BI> <CM>` and all of
`<II>`), but markers at the reply struct's string offsets (+0x00 `<EI>`, +0x58
`<CB>`) rendered as empty and "-", while the LIST-ROW renderers take addresses
at **+0x18** (`0x1244BA` -> `0x19AFB0`) and **+0x70** (`0x122080`, pushed with
a string id). So do not assume "reply layout == file layout" the way this file
originally did; `--strprobe` is the experiment that settles it.

    +0x00  <EI>  IDENT[0x38]    +0x50  <ET>  u8    hours
           name at +0x18 = SELLER
    +0x38  <AI>  u32  auction   +0x51  <AM>  u8    auto-auction mode
    +0x3C  <SP>  u32  price     +0x52  <AC>  u8
    +0x40  <BI>  u32  min bid   +0x58  <CB>  IDENT[0x38]
                                       name at +0x70 = BIDDER
    +0x44  <ED>  u32            +0x90  <CM>  u32
    +0x48  <NC>  u32            +0x94  <CD>  u32
    +0x4C  <LC>  u32            +0x98  <BC>  u32
                                +0xA0  <II>  32B, the card  -> 0xC0, exactly

OK: **RENDERED LIVE 2026-08-20** -- one record served, "Cards for Sale" drew it,
and the screen settled five questions at once:

    Offense 12    <- <II> u8[0]      Type Magic  <- <II> u8[2] = 1
    P defense 5   <- <II> u8[1]      M defense 13 <- <II> u8[3]
    Bid range 500 <- <BI> (+0x40)    Currently 0  <- <CM> (+0x90)

  * `<II>` ROUND-TRIPS at `+0xA0` -- the card drew as Wyerd with its own stats.
  * **THE TRANSPOSITION IS CONFIRMED BY RENDERING**, not just by comparison:
    the screen puts 5 in P defense and 1 in Type, which is `<II>` order
    attack / physDEF / TYPE / magicdef, NOT `CardPrm` order. And **type 1 is
    "Magic"** -- the first value of that enum we have seen named.
  * `<BI>` is the screen's **"Bid range"**; `<CM>` is **"Currently"**.
  * WARNING: **`<ET>` DOES NOT DRIVE "Time left"** -- it was 97 and the row said
    **0h**. WARNING: This bullet then guessed `<ED>` and that was WRONG too;
    OK: it is **`<NC>` (+0x48)**, an end timestamp -- see the `LISTED_AT` /
    `ENDS_AT` block below. `<ET>` is only the duration the client SENDS.
  * WARNING: **`<EI>` AND `<CB>` ARE NOT PLAIN DISPLAY NAMES** -- markers at +0x00 and
    +0x58 rendered as empty and `-`. OK: **RESOLVED by `--strprobe`:** they are
    0x38-byte IDENTITY STRUCTS and the name is at **+0x18 inside** each, so the
    seller is at +0x18 and the bidder at +0x70. See STR_PROBE below.

**PROBE 2 (`--probe`, same evening) -- distinct values in all six unnamed
fields. Mostly a NEGATIVE result, and worth reading before running a third:**

    sent  ED=1787421600  NC=111  LC=222  AC=33  CM=9999  CD=444  BC=55

  * `<CM>` = **"Currently"** confirmed -- the row read 9999.
  * WARNING: **`<ED>` DOES NOT DRIVE "Time left" EITHER.** A far-future timestamp
    still rendered **0h**.
  * WARNING: **`<NC> <LC> <AC> <CD> <BC>` ARE NOT ON THIS SCREEN** -- five distinct
    values, not one appeared.
    OK: **BOTH BULLETS HAVE THE SAME EXPLANATION, and the probe caused it:**
    "Time left" IS `<NC>`, and this probe set `<NC>`=111 -- a 1970 timestamp,
    so the clamp at `0x12223A` pinned the column to 0h. The one field that
    WAS on screen is the one the probe made invisible. A distinctive value is
    only a good probe when the field is not interpreted; for a timestamp, 111
    and 0 are the same answer.
  * the bidder field went from `-` to **BLANK** when `<CM>` became non-zero.
    So it IS gated on there being a bid -- but the name still did not come out,
    with `CBMARK` sitting at +0x58. **`<CB>` is not a plain ASCII display
    name**, and `<EI>` never rendered at all.

WARNING: **STOP BLACK-BOX PROBING HERE.** Two rounds bought five fields and then five
straight misses; the list row shows a SUBSET and the rest cannot be reached
this way. The remaining questions -- what fills "Time left", where the seller
and bidder names come from, and what the five invisible fields do -- want the
ROW RENDERER read out of `TM.dll`, which is this project's oldest lesson
(open the function, not just the address). A third value-guessing round is the
expensive way to learn nothing.

WARNING: ITERATING IS CHEAP: the fixture is a DATA file and `<SN>` lives in
`polpro.json` (mtime-reloaded), so changing the row needs neither a restart nor
a dropped session -- unlike the `responders.py` change that first enabled it.

WARNING: `<II>` IS ECHOED VERBATIM AND MUST BE. It is the player's own 32-byte
collection entry (`0x12D997` does `mov ecx,8 / rep movsd` straight off the
selected card), and two of its fields are still unidentified. We do not need to
understand them to store and return them -- and we must not "normalise" them.

    python tm_exhibit.py --from-log authserv.log --out list.bin
    python tm_exhibit.py --demo --out list.bin        # the Wyerd listing
"""
import argparse
import re
import struct
import sys

# The docstring and the warnings carry non-ASCII, and a Windows console is
# cp1252 -- without this the tool DIES on its own success message. `tmxref.py`
# does the same for the same reason.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REC = 0xC0
STR_LEN = 0x38

#: The `<II>` sub-struct, from the reader 0x1A54B0 and its inverse 0x1A58D0:
#: four u32, then four u16 at +0x10, then eight u8 at +0x18 = 0x20.
II_BLOCKS = ((4, "<I", 4), (4, "<H", 2), (8, "<B", 1))


def pack_ii(vals):
    """The 16 wire values of an `<II>` group as its 32 bytes."""
    if len(vals) != 16:
        raise ValueError("<II> takes 16 values, got %d" % len(vals))
    out, i = bytearray(), 0
    for count, fmt, _w in II_BLOCKS:
        for k in range(count):
            v = vals[i + k]
            try:
                out += struct.pack(fmt, v)
            except struct.error:
                raise ValueError(
                    "<II> value %d = %d does not fit %s -- either the source is "
                    "not an <II> group or the block layout is wrong "
                    "(re-read 0x1A54B0)" % (i + k, v, fmt))
        i += count
    assert len(out) == 0x20, len(out)
    return bytes(out)


#: An identity field is 0x38 bytes and the NAME sits at +0x18 inside it -- the
#: same shape as TM's message record (ids at +0x08/+0x10, nick at +0x18).
#: Measured: markers at +0x18 and +0x70 rendered as the seller and the bidder,
#: while markers at the field bases +0x00 and +0x58 rendered as nothing.
IDENT_LEN = 0x38
IDENT_NAME_OFF = 0x18


def _ident_field(name):
    """One 0x38-byte identity field carrying `name` at +0x18.

    The ID half (+0x00..+0x17) is left zero: we do not know its layout, and the
    two screens that read this record take the NAME and nothing else. If a
    later screen needs the ids, that is the half to decode -- do not assume it
    is empty just because zeros render fine here.
    """
    f = bytearray(IDENT_LEN)
    b = name.encode("latin1", "replace")[:IDENT_LEN - IDENT_NAME_OFF - 1]
    f[IDENT_NAME_OFF:IDENT_NAME_OFF + len(b)] = b
    return bytes(f)


#: `--strprobe`: WHERE ARE THE NAME STRINGS ACTUALLY? `<EI>`/`<CB>` at +0x00 and
#: +0x58 came from the `<ES>` REPLY unpacker (0x1A5650) and were assumed to
#: describe the FILE record too -- but markers there rendered as empty and "-".
#: The list-row renderers instead take ADDRESSES at +0x18 (0x1244BA, passed to
#: 0x19AFB0) and +0x70 (0x122080, pushed with a string id), which is what code
#: does with a string. So the reply struct and the file record may simply not
#: share a layout for the string half, even though every NUMERIC field lines up.
#:
#: OK: **RESULT: `S70MARK` RENDERED AS THE BIDDER** (2026-08-20). So the bidder
#: name is at record **+0x70**, not at `<CB>`'s +0x58.
#:
#: AND THE TWO OFFSETS EXPLAIN EACH OTHER: 0x70 = 0x58 + 0x18, and 0x18 =
#: 0x00 + 0x18. So `<EI>` (+0x00) and `<CB>` (+0x58) are not bare char arrays --
#: they are IDENTITY STRUCTS with the display name at **+0x18 inside each**.
#: That is the same shape as TM's own message record (ids at +0x08/+0x10,
#: nick at +0x18), so it is a house style rather than a coincidence, and it also
#: explains why writing a bare name at +0x00 and +0x58 rendered nothing: those
#: offsets are the struct's ID half, not its name.
#:
#: OK: **PREDICTED AND CONFIRMED, same live run: `S18MARK` RENDERED AS THE
#: SELLER.** The model holds -- two 0x38-byte identity structs at +0x00 and
#: +0x58, each with its display name at +0x18 inside. That is why `--exhibitor`
#: and `--bidder` now write to +0x18 and +0x70.
#:
#: Five distinct markers at every candidate offset settle it in ONE render.
#: Sizes are chosen so none overlaps the next: +0x14 has only 4 bytes before
#: +0x18, and +0x18 runs to <AI> at +0x38.
STR_PROBE = ((0x00, b"S00"), (0x14, b"S14"), (0x18, b"S18MARK"),
             (0x58, b"S58MARK"), (0x70, b"S70MARK"))


def record(ii, auction_id=1, sp=0, bi=0, et=0, am=0, ei="", cb="",
           ed=0, nc=0, lc=0, ac=0, cm=0, cd=0, bc=0, strprobe=False):
    """One 0xC0 exhibit record. `ii` is the 16 wire values of the `<II>` group."""
    r = bytearray(REC)
    r[0x00:0x38] = _ident_field(ei)      # name lands at +0x18 -> SELLER
    struct.pack_into("<IIIIII", r, 0x38, auction_id, sp, bi, ed, nc, lc)
    r[0x50], r[0x51], r[0x52] = et & 0xFF, am & 0xFF, ac & 0xFF
    r[0x58:0x90] = _ident_field(cb)      # name lands at +0x70 -> BIDDER
    struct.pack_into("<III", r, 0x90, cm, cd, bc)
    r[0xA0:0xC0] = pack_ii(ii)
    if strprobe:
        # AFTER the named writes, so the markers win wherever they collide with
        # the <EI>/<CB> fields this tool has been placing until now.
        for off, mark in STR_PROBE:
            r[off:off + len(mark) + 1] = mark + b"\x00"
    assert len(r) == REC, len(r)
    return bytes(r)


#: `polpro A <- <ER>(...) <SP>(n) <BI>(n) <ET>(n) <AM>(n) <II>(v,...)`
_ER = re.compile(
    r"polpro A <- <ER>\(.*?\)"
    r"(?=.*<SP>\((\d+)\))(?=.*<BI>\((\d+)\))"
    r"(?=.*<ET>\((\d+)\))(?=.*<AM>\((\d+)\))"
    r".*<II>\(([^)]*)\)")


def from_log(fh, exhibitor=""):
    """Every `<ER>` in an authserv.log, as records -- the listing the player
    actually attempted, turned into the row it should have produced."""
    out = []
    for line in fh:
        m = _ER.search(line)
        if not m:
            continue
        sp, bi, et, am, ii = m.groups()
        out.append(record([int(x) for x in ii.split(",")],
                          auction_id=len(out) + 1, sp=int(sp), bi=int(bi),
                          et=int(et), am=int(am), ei=exhibitor))
    return out


#: The 6th live listing, 2026-08-20T17:15:22Z -- Wyerd (id 12) at its own sell
#: price. Kept so the probe is reproducible without a log to hand.
DEMO_II = [12, 43, 0, 0, 0, 1, 322, 0, 12, 5, 1, 13, 3, 2, 0, 21]

#: `--probe`: one DISTINCT value per unnamed field, so a single render names
#: them all -- the trick that turned the stat columns from inference into fact.
#: Values are deliberately unmistakable and mutually unconfusable.
#:
#: `<ED>` is the interesting one. "Time left" read 0h with `<ED>`=0 while
#: `<ET>`=97 was ignored, so `<ED>` is what that column reads -- but not whether
#: it is HOURS REMAINING or an END TIME. A far-future unix timestamp separates
#: them in one shot: an end time renders as a sane "time left", an hours count
#: renders as an absurd one, and a different epoch renders as neither.
#:
#: `<CM>` is non-zero for a second reason beyond confirming "Currently": if the
#: bidder name is GATED on there being a bid, this is what makes `<CB>` appear.
PROBE = dict(ed=1787421600,   # 2026-08-22T18:00:00Z, +48h from the probe build
             nc=111, lc=222, ac=33, cm=9999, cd=444, bc=55)

#: OK: "TIME LEFT" IS `<NC>` (+0x48), SOLVED 2026-08-20 at `0x12222C`:
#:     esi = row[+0x48] ; esi -= now ; if esi <= 0 -> 0
#:     hours = esi * 0x91A2B3C5 >> 32 >> 0xB          (i.e. / 3600)
#:     days  = hours / 24  -> if days >= 1 the row uses string id [0x2FF500+0x9C0]
#:                            else the hours one at +0x9CC
#: so it is an END TIMESTAMP in seconds, clamped at zero, shown as days once it
#: exceeds 24h. Probe 2 sent `<NC>`=111 -- an instant in 1970 -- which is exactly
#: why "Time left" read 0h, and why `<ET>` and `<ED>` both looked innocent.
#:
#: `<ED>` (+0x44) and `<LC>` (+0x4C) are ALSO timestamps: `0x1221CF` and
#: `0x122215` hand both to the same date formatter `[0x24DFE8]+0xAD0` that
#: `<NC>` goes through at `0x1221FE`. Which calendar field each one fills on
#: screen is still unread -- three dates, three unlabelled columns.
#:
#: `--listing` builds a row that is LIVE for 48 hours from the timestamps below,
#: which is the check that the epoch really is unix seconds.
LISTED_AT = 1787250600   # 2026-08-20T18:30:00Z
ENDS_AT   = 1787423400   # 2026-08-22T18:30:00Z  (+48h -> should read "2 days")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-log", help="authserv.log to lift <ER> listings from")
    ap.add_argument("--demo", action="store_true", help="one record, the Wyerd listing")
    ap.add_argument("--probe", action="store_true",
                    help="--demo plus a distinct value in every unnamed field")
    ap.add_argument("--listing", action="store_true",
                    help="a plausible LIVE listing: 48h left, real names")
    ap.add_argument("--strprobe", action="store_true",
                    help="markers at every candidate STRING offset (see STR_PROBE)")
    ap.add_argument("--exhibitor", default="", help="<EI> string (meaning INFERRED)")
    ap.add_argument("--bidder", default="", help="<CB> string (meaning INFERRED)")
    ap.add_argument("--out", help="write the list file here")
    ap.add_argument("--last", action="store_true", help="with --from-log, keep only the last")
    args = ap.parse_args()

    if args.listing:
        recs = [record(DEMO_II, auction_id=1, sp=43, bi=500, et=48, am=2,
                       ed=LISTED_AT, nc=ENDS_AT, lc=LISTED_AT,
                       ei=args.exhibitor or 'TGCY2623',
                       cb=args.bidder)]
    elif args.demo or args.probe or args.strprobe:
        extra = dict(PROBE) if args.probe else {}
        recs = [record(DEMO_II, auction_id=1, sp=43, bi=500, et=97, am=2,
                       ei=args.exhibitor, cb=args.bidder,
                       strprobe=args.strprobe, **extra)]
    elif args.from_log:
        with open(args.from_log, "r", encoding="utf-8", errors="replace") as f:
            recs = from_log(f, args.exhibitor)
        if args.last and recs:
            recs = recs[-1:]
    else:
        ap.error("give --demo or --from-log")

    blob = b"".join(recs)
    print("%d record(s), %d bytes (%d x 0x%X)" % (len(recs), len(blob), len(recs), REC))
    if len(blob) % REC:
        print("!! not a whole number of records", file=sys.stderr)
        return 1
    for i, r in enumerate(recs):
        ai, sp, bi = struct.unpack_from("<III", r, 0x38)
        cid = struct.unpack_from("<I", r, 0xA0)[0]
        print("  [%d] <AI>=%d <SP>=%d <BI>=%d <ET>=%d <AM>=%d card id %d"
              % (i, ai, sp, bi, r[0x50], r[0x51], cid))
    if args.out:
        with open(args.out, "wb") as f:
            f.write(blob)
        print("wrote %s" % args.out)
        print("WARNING: `<SN>` in config/polpro.json MUST equal %d, or the reader asks "
              "for a different length than we serve." % len(recs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
