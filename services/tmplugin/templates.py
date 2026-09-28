"""The authored lobby blobs shipped with the tree, and how one is found."""
import struct
import tetramaster
import tmfixtures
import tmroom
from . import corenames, events, fetches, zones


def _tm_template_blob(path):
    """The authored Tetra Master lobby blob shipped WITH THE TREE, or None.

    `b/g/ZL` and `b/g/RL%03d` are per-member resources, but their CONTENT is a
    server-authored fixture -- the zone and room tables -- that on dev only
    existed as loose per-subject files in `data/resources/` (gitignored). Prod
    had none, so every fetch fell through to the all-zero "no data stored yet"
    reply and the Tetra Master zone list rendered EMPTY (reported live
    2026-08-19T01:16, three refetches then @Quit). The canonical blobs now ship
    in `services/tmdata/`, which reaches prod like any code change, and a
    member with no stored copy is served the template -- with the live counts
    patched in on the way out, same as a stored file.

    WARNING: `b/g/PTL` IS THE SAME BUG ONE PATH OVER, and it is why prod had NO TABLES
    (reported and measured 2026-08-19/20: `3:0 'b/g/PTL': serving 49236B (all
    zero ...)`, so `+0x48` -- the TABLE COUNT -- read 0 and the table screen drew
    nothing). Its member half is generated from the live roster, but its TABLE
    half is authored content, and the only copy of it was dev's gitignored
    `data/resources/1.b_g_PTL.bin`. `tools/tmptl.py --template` writes the
    canonical one, with NO members baked in -- the fixture answers every member
    who has no stored blob, so a baked row would be a phantom player in every
    room, and `_ptl_with_live_roster` fills that half in anyway.

    WARNING: AND `U/g/TM0_RANKLIST` IS THE SAME BUG A THIRD TIME (2026-08-20). The
    zero row lived ONLY in dev's gitignored `data/resources/`, so prod could
    never serve it -- and the ranking list is worse than the zone list, because
    its LENGTH is derived from the file: with nothing stored, `_lobby_paylen`
    answered 4 bytes to a reader asking for 232+4, which is POL-5135's shape.
    The canonical 232B zero row now ships in `services/tmdata/` like the rest.

    WARNING: IT IS A ZERO ROW, NOT AN EMPTY LIST, AND THAT IS DELIBERATE -- see the
    `<LN>`=1 note in `polpro.json`: the client's rankings scene bails on a row
    count of 0 before it ever reads the file (TM.dll rva 0x1750A8 `jle`).
    """
    # WARNING: `b/g/TM0AucData` IS THE SAME BUG A FOURTH TIME, and the sharpest one:
    # nothing was MISSING, we served the honest empty answer and the honest
    # empty answer DISABLES THE FEATURE. It is 20 bytes = five u32 counts, one
    # per Price List band (All / Cheap / Affordable / Expensive / Exorbitant),
    # copied straight into the client's count array at `0x11EA5D`:
    #     mov ecx, [eax + 0x52378A8] ; mov [eax + 0x527EEC0], ecx ; cmp eax,5/jl
    # A zero count GREYS its row, so with all five zero the browse half of the
    # auction cannot be entered at all -- and the client therefore never sends
    # the browse query, which is why only `<SI>+<IO>` and `<SI>+<IB>` have ever
    # been seen on the wire. Exactly the `<LN>`(0) teardown one list over: a
    # legal zero is not always a usable zero.
    # THE EVENT RESOURCES ship WITH THE TREE like the rest, but stay INERT until
    # an event is DECLARED. With no window configured the client reads Start/End
    # = -1/-1 ("no event"), so serving a populated ranking then would be a
    # phantom event -- a stranger ("Fox") sitting atop everyone's event board.
    # `_tm_event_active()` ties the fixture to the same window the countdown
    # uses: set POL_TM_EVENT_START/END and the ranking AND the countdown turn on
    # together; leave them unset and this returns None = prod's current all-zero
    # empty list. The member fixture is keyed to ONE winner id (regenerate with
    # `tools/tmevent.py --winner`), so only that member places top-3 and opens
    # the Event Shop; everyone else just sees the board.
    # b/g/TM0EventList is THE SCHEDULE/GATE -- the ONLY one of the three the
    # *reachable* loader reads (pump 0x39590, reader 0x8B5D0, from 0x0395BD),
    # before the event scenes ever touch DataList/MemberList. Ship + gate it the
    # same way; tools/tmevent.py --out-list builds it (layout in that file's
    # build_list). Until an event is declared this returns None = the all-zero
    # "no event" answer, exactly like the other two.
    # BUILT, NOT SHIPPED (2026-09-14): every blob below comes out of
    # services/tmfixtures.py -- the layouts the client's readers dictate plus
    # this server's own zone/room/table content. The live patchers on the way
    # out are unchanged.
    if path == "b/g/TM0EventMemberList" and events._tm_event_test_member():
        # THE RANKING, FROM PLAY: tetramaster scores each tournament game into
        # tmeventstate; the client reads its own row on re-entry (score ->
        # status chars 14-15 -> everyone's board, mission ticks).
        try:
            import tmeventstate
            ws, _we = tetramaster.event_window()
            blob = tmeventstate.member_list_blob(ws, tmroom.pol_id_of,
                                                 tmroom.name_of)
            corenames.log("lobby", f"  3:0 {path!r}: tournament standings, "
                         f"{struct.unpack_from('<I', blob, 4)[0]} row(s)")
            return blob
        except Exception as exc:
            corenames.log("lobby", f"  3:0 {path!r}: standings not built ({exc!r})")
    if path in ("b/g/TM0EventList", "b/g/TM0EventDataList", "b/g/TM0EventMemberList"):
        # The tournament members (POL_TM_EVENT_ZONE_MEMBERS) also get the DATA
        # list: the event room loader needs its count >= 1 (+0x54) and the
        # fixture carries exactly one record.
        if not events._tm_event_active() and not (
                path == "b/g/TM0EventDataList" and events._tm_event_test_member()):
            return None
        data = tmfixtures.template(path)
        if data is not None and path == "b/g/TM0EventDataList"                 and events._tm_event_test_member():
            data = events._tm_event_missions(data)
        return data
    if path == fetches._EXHIBIT_LIST_PATH:
        return None                         # per-member store only; no fixture
    data = tmfixtures.template(path)
    if data is None:
        return None
    if path == "b/g/ZL":
        data = zones._zl_with_live_host(data)
    elif path.startswith("b/g/RL"):
        data = zones._rl_name_ps2(data)
    return data
