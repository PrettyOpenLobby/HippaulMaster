"""Declared lengths of Tetra Master's resource fetches (FETCH_PATHLEN) and resource_length."""
import os
import struct
import tmauction
import tmfixtures
import tmrank
import tmroom
from . import auction, corenames, rankings, templates


#: DECLARED LENGTHS THAT END WHERE THE CONTENT ENDS. The zone's room list is a
#: 51272-byte buffer and the room's table list 49232, but a console over a
#: 1280-MTU link drops the tail of a reply that size and parks its reader
#: (measured 2026-08-19: `expect=51272 got=43226`). The live service declared
#: both short - `b/g/RL000=1476` (0x48 + 7 rooms * 200 + the 4-byte trailer)
#: and `b/g/PTL=24588` (0x5850 + 19 table rows * 104 + 4: the 18 authored
#: tables and the trade service row) - and the client accepts a short declare
#: (confirmed live 2026-08-19). The room list length follows the room count
#: this server authors; the table list one is the proven constant.
PTL_DECLARED = 0x5850 + 19 * 104 + 4


# ---------------------------------------------------------------------------
# resource lengths, templates and the live patchers (the lobby band)
# ---------------------------------------------------------------------------
#: Tetra Master AUCTION list files. Variable-length (`<SN>` records of 0xC0),
#: so they get NO entry in _FETCH_PATHLEN -- they are served at their own size,
#: unpadded, next to the mail branch. Both list screens read through the
#: EXHIBIT reader regardless of which one the menu says.
_AUCTION_LIST_PATHS = ("U/g/TM0_EXHIBITLIST", "U/g/TM0_BIDLIST", "U/g/TM0_AUCLIST")

#: The EXHIBIT list specifically -- the one with a shipped fixture, because the
#: `<SN>` that drives its length lives in `polpro.json` and is therefore the
#: SAME for every member (the spec is keyed on the request's shape, not on who
#: sent it). So a non-zero `<SN>` with a per-member store is POL-5135 waiting to
#: happen: the member who listed something is served 192B and everyone else is
#: served 4B against a reader asking for 192, and hangs. Shipping the fixture
#: makes "no stored copy" mean ONE record rather than none, so the declared
#: length and `<SN>` agree for every member, present and future.
#:
#: WARNING: THE FIXTURE AND `SI+IO`'s `<SN>` ARE ONE NUMBER IN TWO FILES. Change one
#: and you must change the other -- exactly the coupling the `TM0:RR`/`<LN>`
#: note warns about, and `tools/tm_exhibit.py` prints the required `<SN>` when
#: it writes a list.
_EXHIBIT_LIST_PATH = "U/g/TM0_EXHIBITLIST"
_AUCTION_LIST_REC = 0xC0


#: Declared payload lengths of Tetra Master's resource fetches, merged into
#: the core's table. Each +4 is the 03:00 trailer. See responders._FETCH_PATHLEN
#: for the rule and the reader-side measurements these came from.
FETCH_PATHLEN = {
    # Tetra Master default-data files, read via sqMgCpReadFile (which fetches over
    # this same 03:00 opcode). Lengths are the reader's `a3` arg, measured in
    # TMaster.pex: TM0SML/TM0SML2 at 0x003aad48/0x003aae48 = 776; TM0IML at
    # 0x003aaf18 = 400. Serving the fallback frame length (664) was a mismatch the
    # client rejects. +4 = the 03:00 trailer.
    "b/g/TM0SML": 776 + 4,
    "b/g/TM0SML2": 776 + 4,
    "b/g/TM0IML": 400 + 4,
    # More default-data files in the same family (TMaster.pex read sites): TM0CQL
    # 1544 (0x003ab018), TM0CVML 392 (0x003ab118). Not yet observed on the wire but
    # measured so they're ready.
    "b/g/TM0CQL": 1544 + 4,
    "b/g/TM0CVML": 392 + 4,
    # U/g/TM0DataFile = the PLAYER SAVE (sqMgCpLoadPlayerSaveData). Length 12328,
    # measured at TMaster.pex 0x003a6dd0 (a2 = 12328 = 0x3028); dest buf 0x0059c360,
    # parsed at 0x002db878. We were serving the 664 fallback -- a massive short read
    # the client hangs/aborts on. This is the current wall (savestate slot1: buffer
    # all-zero, both TM0IML+TM0DataFile fetched, then error 037092).
    "U/g/TM0DataFile": 12328 + 4,
    # THE MULTIPLAYER LOBBY CHAIN, measured 2026-08-15 off a live click. The menu
    # item "player vs. player" fetches `b/g/ZL` and we answered the 664 fallback --
    # a 3x short read. SE's own symbols name the whole chain (TMaster.pex):
    # sqMgCpLoadZoneList -> sqMgCpLoadRoomList -> sqMgCpEnterRoom2 -> sqMgCpEnterTable.
    #   b/g/ZL     2120 (0x848), a2 at TMaster.pex 0x00410784, dest buf 0x0058f2c0,
    #              re-stored as the recorded length at 0x004107cc.
    # b/g/TM0RkData -- fetched by RANKINGS once the <RF> reply lets it get that
    # far (first seen 2026-08-17, on the 668 fallback). 28 bytes: the call at
    # TM.dll rva 0x8AFE2 passes it to sqMgCpReadFile (0x1A1AA0, a pure forwarder
    # to sqMgReadFileOffset 0x19F5B0) as `(path, 0x2923AC, 0x1C, ...)`.
    #
    # ARGUMENT SLOT CONFIRMED against a reader whose length is known
    # independently: sqMgAccpReadExhibitList (0x1A4700) pushes `(n*3)<<6` =
    # n*0xC0 into that same third slot. So 0x1C is the length, not a flag.
    "b/g/TM0RkData": 28 + 4,
    # The four remaining `b/g/` reads in TM.dll, read off the SAME third argument
    # slot the entry above validates. None of them has
    # ever been requested -- the event branch has never opened on our server --
    # so these exist so that the FIRST time one is, it is answered at the client's
    # own buffer size instead of the 668 fallback. Getting this wrong is not a
    # visible error: an under-declared 3:0 reply leaves the client waiting for
    # ever with most of the data already delivered (see the "large 3:0 replies
    # truncate" trap), which reads as "the screen hung", not as "bad length".
    #
    #   0x8B5D0  b/g/TM0EventList        (path, 0x52136D0, 0x2C08, ...)
    #   0x8C670  b/g/TM0EventDataList    (path, 0x52095E8, 0x1308, ...)
    #   0x8C700  b/g/TM0EventMemberList  (path, 0x52223C8, 0x2808, ...)
    #   0x8B0B0  b/g/TM0AucData          (path, 0x52378A8, 0x0014, ...)
    #
    # An all-zero body is a well-formed EMPTY list for all four: every consumer
    # reads a count first (EventList at +0x04, EventDataList at +0x54,
    # EventMemberList at +0x04) and loops zero times on 0.
    "b/g/TM0EventList": 11272 + 4,
    "b/g/TM0EventDataList": 4872 + 4,
    "b/g/TM0EventMemberList": 10248 + 4,
    "b/g/TM0AucData": 20 + 4,
    # THE FOUR `u/g/TM0_xx` PATHS, READ OUT OF TM.dll 2026-08-17. These were the
    # last paths still logging "has NO measured length" and falling back to 668 --
    # the POL-5135 shape, on a reader that checks its trailer at the length IT
    # asked for.
    #
    # They are not literals in the image, which is why a string search never found
    # them: the builder at `0x0512f7d0` formats `u/g/%s_%s` (VA 0x051d5d08) from
    # the tag `"TM0"` (0x051b232c) and a two-letter code chosen by a jump table at
    # `0x0512f818` over its `kind` argument --
    #
    #     kind 0 -> "RM"   kind 1 -> "PM"   kind 2 -> "DI"   kind 3 -> "BI"
    #
    # (the `"PM"` pointer is 0x051b1a70, which is ALSO the AM/PM time literal --
    # the compiler pooled the identical 4-byte constant. It is not a clue about
    # meaning.)
    #
    # The length is the READER's, not the builder's, and there are two readers:
    #
    #     0x050159c0   len 0x80  = 128    called at 0x050c260c with ebx=1 -> PM
    #     0x05015a30   len 0x3c8 = 968    called at 0x050c26d0 push 3   -> BI
    #                                     called at 0x050c2785 push 2   -> DI
    #
    # +4 for the 03:00 trailer, exactly as every other entry here.
    "u/g/TM0_PM": 128 + 4,
    "u/g/TM0_DI": 968 + 4,
    "u/g/TM0_BI": 968 + 4,
    # OK: RM (kind 0) IS SETTLED, AND THE ANSWER IS "NOTHING EVER FETCHES IT".
    # This was the one guessed entry in the table, marked because the file's
    # convention is that lengths are read off the caller. Read off
    # TM.dll.unpacked 2026-08-18 (stopgap audit), and the whole family closes:
    #
    #   the PATH BUILDER 0x0512F7D0 switches kind 0..3 to the suffix, via the
    #   jump table at 0x0512F818 -- 0 'RM', 1 'PM', 2 'DI', 3 'BI'.
    #
    #   it has EXACTLY TWO CALLERS, and they are the two readers, each with a
    #   length that is hardcoded and does NOT vary with kind:
    #       0x050159C0   push 0x80  = 128   dest 0x052162D8
    #       0x05015A30   push 0x3C8 = 968   dest 0x05227C20
    #
    #   and those readers have exactly THREE call sites between them:
    #       0x050C260C -> 0x050159C0 with ebx, and `mov ebx, 1` is at the
    #                     function's entry (0x050C25E3) and is never reassigned
    #       0x050C26D0 -> 0x05015A30 with a literal 3   (BI)
    #       0x050C2785 -> 0x05015A30 with a literal 2   (DI)
    #
    # So every reachable path is PM, BI or DI. **Kind 0 is never passed by
    # anything in the module**, which means `u/g/TM0_RM` is a suffix the client
    # can spell and never asks for -- this entry is unreachable, not uncertain.
    # 132 is kept because RM shares its table with PM, whose reader is the
    # 128-byte one, so if a future build ever does call kind 0 that is the
    # length it would take. It cannot be wrong today because it cannot be used.
    "u/g/TM0_RM": 128 + 4,
}


def resource_length(path):
    """The declared 03:00 payload length for a Tetra Master resource whose
    length is not a constant (the variable-length lists, the PS2 build's
    ranking header), or None for every other path.
    """
    if path == "b/g/PTL":
        return PTL_DECLARED
    if path.startswith("b/g/RL") and path[6:].isdigit():
        rl = tmfixtures.template(path)
        if rl is not None:
            n = struct.unpack_from("<I", rl, tmroom.RL_COUNT_OFF)[0]
            return tmroom.RL_HDR + n * tmroom.RL_REC + 4
    if path == auction._AUCTION_BIDHIST_PATH:
        # WARNING: A DIFFERENT STRIDE FROM THE LISTS BELOW -- 0x48, not 0xC0.
        # `sqMgAccpReadBidList` (0x1A4A70) asks for `n*9<<3`, where the
        # exhibit reader asks for `n*3<<6`. Serving this path through the
        # 0xC0 branch would declare 192 bytes per row to a reader expecting
        # 72 and hang it on the difference, which is why the bid history
        # cannot share `U/g/TM0_BIDLIST` with `<SI>+<IB>`.
        try:
            have = os.path.getsize(corenames._resource_read_file(path))
        except OSError:
            have = 0
        if have:
            rec = tmauction.BID_REC if tmauction is not None else 0x48
            if have % rec:
                corenames.log("lobby", f"  3:0 {path!r}: {have}B is NOT a whole "
                             f"number of 0x{rec:X} bid records -- the "
                             f"reader checksums at a record boundary and "
                             f"will reject it.")
            corenames.log("lobby", f"  3:0 {path!r}: serving its OWN {have}B "
                         f"({have // rec} bid(s) of 0x{rec:X}) + 4 trailer, "
                         f"unpadded. WARNING: Must match the `<SN>` sent with "
                         f"`<HS>` on the auth band.")
            return have + 4
        corenames.log("lobby", f"  3:0 {path!r}: no bids stored -- serving the EMPTY "
                     f"history (0 records + 4 trailer), which pairs with "
                     f"`<SN>`(0).")
        return 4
    if path in _AUCTION_LIST_PATHS:
        # THE AUCTION LISTS ARE VARIABLE-LENGTH, so they are served at their
        # own size like a message -- NEVER padded to this opcode's default.
        #
        # The reader asks for `count * 0xC0` (sqMgAccpReadExhibitList, TM.dll
        # RVA 0x1A46A0: offset = index*0xC0, length = count*0xC0) where count
        # is the `<SN>` we sent, so no constant in _FETCH_PATHLEN can be right
        # for more than one list length.
        #
        # WARNING: MEASURED THE HARD WAY (2026-08-16): padded to 668 this screen
        # ERRORS OUT -- and it only appeared to work beforehand because an
        # all-zero placeholder made the trailer-over-668 and the client's
        # checksum-over-192 accidentally agree at zero. The instant the file
        # held real bytes, both list screens broke. That is POL-5135's cause
        # exactly, one opcode over: the reader checks the trailer at the
        # length IT asked for and finds our padding there instead.
        try:
            have = os.path.getsize(corenames._resource_read_file(path))
        except OSError:
            have = 0
        if have:
            if have % 0xC0:
                corenames.log("lobby", f"  3:0 {path!r}: {have}B is NOT a whole number "
                             f"of 0xC0 records -- the reader will checksum at "
                             f"a record boundary and reject it.")
            corenames.log("lobby", f"  3:0 {path!r}: serving its OWN {have}B "
                         f"({have // 0xC0} record(s) of 0xC0) + 4 trailer, "
                         f"unpadded -- see the POL-5135 note above.")
            return have + 4
        # WARNING: NOTHING STORED FALLS BACK TO THE SHIPPED FIXTURE, exactly as
        # the ranking branch below does and for the same POL-5135 reason.
        # `_resource_blob` consults `_tm_template_blob` on this same path,
        # so declaring its length here keeps the two bands agreeing by
        # construction rather than by two constants that must be edited
        # together. Only the EXHIBIT list has a fixture; the bid list
        # returns None here and falls through to the empty answer below.
        tmpl = templates._tm_template_blob(path)
        if tmpl:
            corenames.log("lobby", f"  3:0 {path!r}: nothing stored -- serving the "
                         f"shipped {len(tmpl)}B "
                         f"({len(tmpl) // _AUCTION_LIST_REC} record(s) of "
                         f"0x{_AUCTION_LIST_REC:X}) + 4 trailer, unpadded. "
                         f"WARNING: This count must equal the `<SN>` in "
                         f"polpro.json's SI+IO entry.")
            return len(tmpl) + 4
        # WARNING: AN EMPTY LIST IS A REAL ANSWER, AND IT HAS TO BE EXPRESSIBLE.
        # Falling through from here reaches this opcode's PADDED default,
        # which the note above measured as the thing that errors these two
        # screens out -- so "this player has no bids" could not be said at
        # all, and the only reachable states were "a listing" or "an error".
        # That is why a probe blob sat here: it was the only shape that
        # rendered. Zero records + the 4-byte trailer is what `<SN>`(0)
        # asks for, on exactly the unpadded terms a non-empty list uses.
        corenames.log("lobby", f"  3:0 {path!r}: nothing stored -- serving the EMPTY "
                     f"list (0 records + 4 trailer, unpadded). This is the "
                     f"honest answer while no auction state exists; it pairs "
                     f"with `SN`=0 in polpro.json.")
        return 4
    if tmrank.is_list_path(path):
        # THE RANKING LIST, on the same unpadded terms as the auction lists
        # above and for the same measured reason -- the reader's length is
        # `rows * 232`, not this opcode's default. See `_RANK_LIST_PATH`.
        try:
            have = os.path.getsize(corenames._resource_read_file(path))
        except OSError:
            have = 0
        if have:
            if have % rankings._RANK_LIST_REC:
                corenames.log("lobby", f"  3:0 {path!r}: {have}B is NOT a whole number "
                             f"of {rankings._RANK_LIST_REC}B records -- the reader "
                             f"checksums at a record boundary and will "
                             f"reject it.")
            corenames.log("lobby", f"  3:0 {path!r}: serving its OWN {have}B "
                         f"({have // rankings._RANK_LIST_REC} row(s) of "
                         f"{rankings._RANK_LIST_REC}) + 4 trailer, unpadded. WARNING: The "
                         f"row count must match the `<LN>` in polpro.json's "
                         f"TM0:RR entry, or the reader asks for a different "
                         f"length than the file holds.")
            return have + 4
        # WARNING: NOTHING STORED FALLS BACK TO THE SHIPPED TEMPLATE, NOT TO 4.
        # Answering the empty list here was measured wrong on 2026-08-20:
        # `<LN>`(0) makes the client's rankings scene tear itself down at
        # TM.dll rva 0x1750A8 (`cmp [listid*4 + 0x52846F0], 0` / `jle`) and
        # return to the menu WITH NO ERROR -- it never reaches the read, so
        # a legal zero-length payload was never the question. `<LN>` is back
        # to 1 and this serves the 232B zero row `_resource_blob` will hand
        # out, whether that is dev's stored copy or the shipped fixture.
        #
        # `_rank_list_blob` IS THE SAME FUNCTION `_tm_rank_reply` COUNTED
        # ROWS WITH, which is what keeps the length we declare here and the
        # `<LN>` we promised on the other band the same number: the
        # published tally if the job has run, the shipped fixture if not.
        tmpl = rankings._rank_list_blob(path)
        if tmpl:
            corenames.log("lobby", f"  3:0 {path!r}: nothing stored -- serving the "
                         f"published/shipped {len(tmpl)}B "
                         f"({len(tmpl) // rankings._RANK_LIST_REC} row(s)) + 4 "
                         f"trailer, unpadded.")
            return len(tmpl) + 4
        corenames.log("lobby", f"  3:0 {path!r}: nothing stored AND no shipped "
                     f"template -- serving 0 rows + 4 trailer. WARNING: The client "
                     f"will only survive this if polpro.json's TM0:RR says "
                     f"`LN`=0, and a zero count closes the screen.")
        return 4
    if (tmrank is not None and path == tmrank.RKDATA
            and corenames._peer_is_ps2()
            and os.environ.get("POL_TM_RKDATA_PS2", "1") == "1"):
        # THE CONSOLE ASKS FOR 24, NOT THE PC's 28. Serving 28 + 4 is
        # what `sqMgReadFileCheck` rejected with -8250, rendered on
        # screen as `8250-37069` ("Error occurred while retrieving
        # rankings data"); measured 2026-09-09T01:22:56Z against the
        # console's own log ring. `tmrank.to_ps2` carries the
        # disassembly. +4 is this opcode's trailer, as everywhere else
        # in this table.
        n = tmrank.RKDATA_LEN_PS2 + 4
        corenames.log("lobby", f"  3:0 {path!r}: PS2 build -- serving {n} "
                     f"({tmrank.RKDATA_LEN_PS2} + 4 trailer), not the "
                     f"PC's {FETCH_PATHLEN.get(path)}")
        return n
    return None
