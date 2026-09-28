"""Tournaments on the lobby band: the event zone, its room list, the Event List and the missions
mask.
"""
import os
import struct
import time
import tetramaster
from . import corenames, templates, zones


def _tm_event_active():
    """True when a Tetra Master event has been DECLARED, i.e. a real time window
    is configured.

    This is the single signal that turns the event on. It reads the SAME env the
    countdown uses (`POL_TM_EVENT_START` / `POL_TM_EVENT_END`, whose service-side
    default is -1/-1 = "no event", matched in `tetramaster._eventtime_fields`),
    so the ranking fixtures and the `@EventTimeReqCheck` window can never
    disagree: the server owner sets the window and both come on; unset, the event
    resources stay all-zero = empty, which is prod's current behaviour, so
    shipping the fixtures changes nothing on its own.

    WARNING: UNMEASURED. The event branch has never opened on our server.
    The first proof this ever worked is `b/g/TM0EventDataList` appearing
    in authserv.log at all.
    """
    for k in ("POL_TM_EVENT_START", "POL_TM_EVENT_END"):
        v = os.environ.get(k)
        if v is None:
            continue
        try:
            if int(v, 0) != -1:
                return True
        except ValueError:
            pass
    return False
#: member id -> the zone id (= slot) its event zone was last served under.
_EVENT_ZONE_ID = {}


def _tm_event_test_member():
    """True when this session's member is in POL_TM_EVENT_ZONE_MEMBERS."""
    spec = os.environ.get("POL_TM_EVENT_ZONE_MEMBERS", "").strip()
    if not spec:
        return False
    allowed = {s.strip() for s in spec.split(",") if s.strip()}
    return "*" in allowed or str(corenames._session_get("member_id") or "") in allowed


#: THE MISSIONS. `b/g/TM0EventDataList` +0x12FC is a u32 mask (bit t turns on
#: mission type t; the first three set bits are Missions 1-3) and +0x1300 + t is
#: type t's count. Types: 0 combos, 1 perfect wins, 2 firsts in a row, 3
#: Rotating Block flips, 4 ties, 5 act first and place 1st, 6 Chance Block
#: flips, 7 wins over a Defense Up card (not counted). POL_TM_EVENT_MISSIONS is
#: `type:count,...`; the default is three we can tell apart on screen.
_EVD_MASK_OFF, _EVD_COUNT_OFF = 0x12FC, 0x1300


def _tm_event_missions(data):
    buf = bytearray(data.ljust(_EVD_COUNT_OFF + 8, b"\x00"))
    mask = 0
    try:
        active = tetramaster.event_missions()
    except Exception:
        active = [(1, 1), (2, 2), (4, 1)]
    for t, n in active:
        mask |= 1 << t
        buf[_EVD_COUNT_OFF + t] = n
    struct.pack_into("<I", buf, _EVD_MASK_OFF, mask)
    return bytes(buf[:max(len(data), _EVD_COUNT_OFF + 8)])


def _zl_event_zone(path, data):
    if path != "b/g/ZL":
        return data
    spec = os.environ.get("POL_TM_EVENT_ZONE_MEMBERS", "").strip()
    if not spec:
        return data
    member = str(corenames._session_get("member_id") or "")
    allowed = {s.strip() for s in spec.split(",") if s.strip()}
    if "*" not in allowed and member not in allowed:
        return data
    if len(data) < zones._ZL_HDR + zones._ZL_REC:
        return data
    buf = bytearray(data)
    n = min(struct.unpack_from("<I", buf, zones._ZL_COUNT_OFF)[0],
            (len(buf) - zones._ZL_HDR) // zones._ZL_REC)
    if n < 1 or n >= zones._ZL_MAX:
        return data
    names = [bytes(buf[zones._ZL_HDR + i * zones._ZL_REC + zones._ZL_F_NAME:
                       zones._ZL_HDR + i * zones._ZL_REC + zones._ZL_F_HOST]) for i in range(n)]
    # Only a TM-shaped list (`EN` + three letters), and never twice.
    if not all(nm[:2] == b"EN" and len(nm) > 5 for nm in names):
        return data
    if any(nm[4:5] != b"A" for nm in names):
        return data
    # WARNING: THE ZONE ID IS THE ZONE'S OWN SLOT. The client dials
    # ZLrecord[zone_id].host, so an id past the populated rows dials an empty
    # slot: TRM-8196-37130, live 2026-09-26 with id 9 at slot 2.
    zid = n
    if any(buf[zones._ZL_HDR + i * zones._ZL_REC + zones._ZL_F_ID] == zid for i in range(n)):
        corenames.log("lobby", f"  3:0 {path!r}: event zone NOT added -- zone id {zid} "
                     f"(its slot) is already used by another row")
        return data
    _EVENT_ZONE_ID[member] = zid
    title = os.environ.get("POL_TM_EVENT_ZONE_NAME", "Tournament Hall")
    name = (b"ENAAB" + title.encode("latin-1", "replace"))[:zones._ZL_F_HOST - zones._ZL_F_NAME - 1]
    src = zones._ZL_HDR
    dst = zones._ZL_HDR + n * zones._ZL_REC
    buf[dst:dst + zones._ZL_REC] = buf[src:src + zones._ZL_REC]
    struct.pack_into("<II", buf, dst, 0, 0)             # players, rooms
    buf[dst + zones._ZL_F_NAME:dst + zones._ZL_F_HOST] = name.ljust(zones._ZL_F_HOST - zones._ZL_F_NAME, b"\x00")
    buf[dst + zones._ZL_F_ID] = zid
    struct.pack_into("<I", buf, zones._ZL_COUNT_OFF, n + 1)
    corenames.log("lobby", f"  3:0 {path!r}: EVENT ZONE added for member {member}: "
                 f"{name.decode('latin-1')!r} id {zid} (zone {n + 1} of {n + 1})")
    return bytes(buf)


#: THE EVENT ZONE'S ROOM LIST. Entering an event zone still loads the zone's
#: normal `b/g/RL%03d` (CEventSelect -> CGetEventListWithError -> CIRCOpen ->
#: CGetRoomList), and the Event List is those rooms JOINED to `TM0EventList`:
#: a row matches a record whose +0x10 is the zone name and +0x30 the room name
#: (static reading of the PS2 client). No such file exists for our event zone
#: id, so serve ONE room cloned from this subject's zone-0 list, renamed. Room
#: id, name and channel below are OURS and unmeasured; only the list's shape is
#: the client's.
_RL_HDR, _RL_REC, _RL_COUNT_OFF = 0x48, 200, 0x40
_RL_F_NAME, _RL_F_NAME_END, _RL_F_CHAN, _RL_F_CHAN_END = 0x2A, 0x86, 0xB8, 0xC8


#: THE EVENT LIST ROW. `b/g/TM0EventList` (0x2C08 B = 8 + 32 * 0x160) as the
#: PS2 20040908 reader CGetEventHelp 0x344D10 uses it: count u32
#: at +0x04, records from +0x08. A room row lights up only when a record's
#: +0x10 equals the zone name and +0x30 the room name; +0x00 is the mode (0 =
#: event, 1 = table room); +0x08 / +0x0C are second counts the window shows as
#: h:m:s (Start / End Time); +0x50 is the text it draws. +0x04..+0x07 are four
#: u8s the window also shows, UNMEASURED: we send the date as yy, mm, dd, 0.
_EVL_TOTAL, _EVL_HDR, _EVL_REC = 0x2C08, 0x08, 0x160


def _tm_event_info():
    try:
        return tetramaster.event_info()
    except Exception:
        return {}


def _tm_event_room_name():
    """The Event List's Event Name column IS the event room's name, and the
    TM0EventList record keys on it: the calendar event's name, so the list
    reads "Holiday Cup" on Dec 24. POL_TM_EVENT_ROOM_NAME overrides."""
    return (os.environ.get("POL_TM_EVENT_ROOM_NAME")
            or str(_tm_event_info().get("name") or "Chocobo Cup"))[:30]


def _tm_event_list(path, n):
    if path != "b/g/TM0EventList":
        return None
    spec = os.environ.get("POL_TM_EVENT_ZONE_MEMBERS", "").strip()
    if not spec:
        return None
    member = str(corenames._session_get("member_id") or "")
    allowed = {s.strip() for s in spec.split(",") if s.strip()}
    if "*" not in allowed and member not in allowed:
        return None

    def secs(name, default):
        v = os.environ.get(name, default)
        try:
            h, m = (int(x) for x in v.split(":")[:2])
            return h * 3600 + m * 60
        except ValueError:
            return 0

    buf = bytearray(_EVL_TOTAL)
    struct.pack_into("<I", buf, 0x04, 1)
    r = _EVL_HDR
    struct.pack_into("<I", buf, r + 0x00, 0)                 # mode: event
    # The window the countdown uses (tetramaster.event_window). The date's
    # month and day are 0-BASED: 26/9/26 drew "2026.10.27" live 2026-09-26.
    try:
        ws, we = tetramaster.event_window()
    except Exception:
        ws, we = time.time(), time.time() + 7200
    t = time.gmtime(ws)
    buf[r + 0x04:r + 0x08] = bytes((t.tm_year % 100, t.tm_mon - 1,
                                    t.tm_mday - 1, 0))
    struct.pack_into("<II", buf, r + 0x08, int(ws % 86400), int(we % 86400))
    zone = os.environ.get("POL_TM_EVENT_ZONE_NAME", "Tournament Hall")
    room = _tm_event_room_name()
    # +0x50 is the ONLY string the reader copies (0x396C0): the Event Guide
    # text. About 60 characters fit a line (live 2026-09-26: one long line was
    # cut off), so lines break at '|' -> "\n", as the game's own .BIN text
    # does; whether this box honours it is the live test. The Event Name
    # column is the RL ROOM name, not this.
    title = (os.environ.get("POL_TM_EVENT_TITLE") or _tm_event_info().get("guide")
             or "Win matches to move your chocobo up the track!|"
                "Clear the three missions for extra prizes.").replace("|", "\n")
    for off, width, text in ((0x10, 0x20, zone), (0x30, 0x20, room),
                             (0x50, _EVL_REC - 0x50, title)):
        buf[r + off:r + off + width] = text.encode(
            "latin-1", "replace")[:width - 1].ljust(width, b"\x00")
    corenames.log("lobby", f"  3:0 {path!r}: EVENT LIST for member {member}: {title!r} "
                 f"in {zone!r}/{room!r} ")
    return bytes(buf[:n]).ljust(n, b"\x00")


def _tm_event_room_list(path, n, subject):
    if not (path.startswith("b/g/RL") and path[6:].isdigit()):
        return None
    spec = os.environ.get("POL_TM_EVENT_ZONE_MEMBERS", "").strip()
    if not spec:
        return None
    member = str(corenames._session_get("member_id") or "")
    allowed = {s.strip() for s in spec.split(",") if s.strip()}
    if "*" not in allowed and member not in allowed:
        return None
    # The id `_zl_event_zone` gave this member's event zone (its slot). The
    # zone list is always fetched before a room list, in this same process.
    zid = _EVENT_ZONE_ID.get(member)
    if zid is None or int(path[6:], 10) != zid or zid == 0:
        return None
    # RAW zone-0 list, not `_resource_blob`: its live-count patch records a
    # b/g/RL fetch as ENTERING that zone, and this player is entering ours.
    base = b""
    try:
        with open(corenames._resource_read_file("b/g/RL000", subject), "rb") as f:
            base = f.read()
    except OSError:
        base = templates._tm_template_blob("b/g/RL000") or b""
    try:
        zones._note_zone_presence(zid)
    except Exception as exc:
        corenames.log("lobby", f"  3:0 {path!r}: zone presence not noted ({exc!r})")
    if len(base) < _RL_HDR + _RL_REC or struct.unpack_from(
            "<I", base, _RL_COUNT_OFF)[0] < 1:
        corenames.log("lobby", f"  3:0 {path!r}: event room list NOT built -- no zone-0 "
                     f"room to clone")
        return None
    buf = bytearray(len(base))
    buf[:_RL_HDR] = base[:_RL_HDR]
    buf[_RL_HDR:_RL_HDR + _RL_REC] = base[_RL_HDR:_RL_HDR + _RL_REC]
    struct.pack_into("<I", buf, _RL_COUNT_OFF, 1)
    r = _RL_HDR
    try:
        rid = int(os.environ.get("POL_TM_EVENT_ROOM_ID", "81"), 0)
    except ValueError:
        rid = 81
    struct.pack_into("<I", buf, r, rid)
    room = _tm_event_room_name()
    width = _RL_F_NAME_END - _RL_F_NAME
    buf[r + _RL_F_NAME:r + _RL_F_NAME_END] = room.encode(
        "latin-1", "replace")[:width - 1].ljust(width, b"\x00")
    chan = b"#TM0E%03d" % (rid % 1000)
    buf[r + _RL_F_CHAN:r + _RL_F_CHAN_END] = chan.ljust(
        _RL_F_CHAN_END - _RL_F_CHAN, b"\x00")
    corenames.log("lobby", f"  3:0 {path!r}: EVENT ROOM LIST for member {member}: one room "
                 f"{room!r} id {rid} {chan.decode()} (cloned from b/g/RL000)")
    return bytes(buf[:n]).ljust(n, b"\x00")
