"""tmroom.py -- the LIVE room roster behind `b/g/PTL`, for Tetra Master and Janhourou.

WHY THIS EXISTS, AND WHAT IT REPLACES

`b/g/PTL` is the member+table list a client reads while it is standing in a room.
Until now this server served a **hand-authored file** built by `tools/tmptl.py`,
with one member baked into it. Two players in one room therefore could not see
each other, one of them could not sit down at a table, and the fix each time was
to hand-edit the blob. That is not how the original works, and the client's own
source says so.

THE ORIGINAL, read off SE's decompiled sqMg (`cp.c`), which BOTH games link.
Class L is a **sequenced delta
stream**:

  * `b/g/PTL` is a server-generated SNAPSHOT, and its `+0x40` is the SEQUENCE
    NUMBER that snapshot represents -- `cp__002fb560` opens with
    `ctx+0xa4 := ptl+0x40` before copying members from `+0x50` and tables from
    `+0x5850`.
  * the client sends `<DR>(N)` -- its own current sequence -- to the room game
    manager every **2 seconds** while `LobbyState == 15` (`cp__002fb218`).
  * the server pushes numbered deltas; `cp__002fb290` applies them by tag:
    `PD` 0x13 member add/update (88 B), `PC` 0x14 member REMOVE by id,
    `TD` 0x15 table add/update (104 B), `DE` 0x20 = `PD`.
  * a client more than 50 behind full-reloads the snapshot instead.

THIS MODULE IS THE SNAPSHOT HALF, AND IT IS DELIBERATELY THE WHOLE OF STEP ONE.
It keeps the per-room member records the clients themselves send, and builds the
member half of `b/g/PTL` from them. **It does not send deltas yet** -- the
framing of the update message (`cp__002fae98` reads a count from `<DN>` and walks
`(sequence, payload)` pairs from value index 2) is NOT pinned, and inventing it is
exactly the habit this module exists to end. The snapshot alone is correct-but-slow: the
client re-fetches `b/g/PTL` every few seconds anyway, so a roster that changes
now actually reaches everybody. When the delta framing is measured, the sequence
this module already maintains is what those deltas number.

TWO CONTAINERS, ONE ROSTER. The records arrive on the AUTH band (`authsess`) and
`b/g/PTL` is served on the LOBBY band (`login`) -- different processes. So this
uses the same owner-guarded snapshot-file idiom as `responders._publish_rooms`:
only the process that MUTATES writes, a pure reader can never clobber it with its
own empty view.
"""
import json
import os
import struct
import threading

#: Container geometry -- measured, and identical in `tools/tmptl.py`, which
#: derived it from the PC `<DE>` serialiser at 0x1A3340 while `cp.c`'s parser
#: `lb__002f9ab8` gives the same map from the PS2 side. Two independent reads.
TOTAL = 49232
SERIAL_OFF = 0x40           # THE SEQUENCE NUMBER (cp__002fb560: ctx+0xa4 := this)
MEMBER_COUNT_OFF = 0x44
TABLE_COUNT_OFF = 0x48
MEMBER_OFF = 0x50
MEMBER_REC = 88             # 0x58
MEMBER_SLOTS = 256          # 0x50 + 256*88 == 0x5850 exactly
TABLE_OFF = 0x5850
TABLE_REC = 104             # 0x68
TABLE_SLOTS = 256           # what the CONTAINER holds: 0x5850 + 256*104 == 49232
#: ...but what the CONSUMER reads is 64 -- `0x1A00 // 104`, the same ceiling
#: `janlobby.PTL_TABLE_MAX` and `tmptl.TABLE_MAX` derive independently. A row
#: past this is in the file and on no screen, so appending one is not a bug we
#: get to discover later.
TABLE_MAX = 0x1A00 // TABLE_REC

#: `<DE>` / `<PD>` carry ELEVEN values, and `lb__002f9ab8` scatters them into the
#: 88-byte record like this. The left column is the value index on the wire; the
#: right is the struct offset. Read straight off the parser -- sub-field 9 really
#: does land at +0x08 and sub-field 1 at +0x18, which is why the order looks
#: arbitrary and must not be "tidied".
#:
#:      idx  offset  width  what
#:        0  0x00    u64    the member id -- the CLIENT SENDS 0 and the server fills it
#:        1  0x18    u32
#:        2  0x26    u8
#:        3  0x24    u16
#:        4  0x28    char[16]
#:        5  0x27    u8     2 = in the room, 0 = leaving
#:        6  0x10    u64    a unix timestamp -- DECIMAL TEXT on the wire, an
#:                          8-byte LITTLE-ENDIAN INTEGER in the record. The one
#:                          field whose wire and struct forms differ, and the
#:                          one this module got wrong first: `tools/tmptl.py`
#:                          packs `<Q` here (line 529) and the served file reads
#:                          `f4ce836a00000000` = 1786399988, a plain unix time.
#:                          Storing the ASCII would put "17870300" in the struct.
#:        7  0x1C    u32
#:        8  0x20    u32
#:        9  0x08    u64    the ROOM id
#:       10  0x38    char[32]  name[15] + 16 letters (the name-plus-status block)
_FIELDS = (
    (0, 0x00, "u64"), (1, 0x18, "u32"), (2, 0x26, "u8"), (3, 0x24, "u16"),
    (4, 0x28, "s16"), (5, 0x27, "u8"), (6, 0x10, "t64"), (7, 0x1C, "u32"),
    (8, 0x20, "u32"), (9, 0x08, "u64"), (10, 0x38, "s32"),
)
_NVALUES = 11


def _as_int(s):
    """A wire value -> int. `0x`-prefixed is hex (ids and room ids arrive that
    way); everything else is decimal. Junk reads as 0 rather than raising: this
    runs on live client bytes and one bad field must not lose the whole roster."""
    s = (s or "").strip()
    try:
        return int(s, 16) if s[:2].lower() == "0x" else int(s or "0")
    except ValueError:
        return 0


def encode_member(values):
    """The eleven `<DE>`/`<PD>` values -> the 88-byte record, per `lb__002f9ab8`."""
    vals = list(values) + [""] * (_NVALUES - len(values))
    rec = bytearray(MEMBER_REC)
    for idx, off, kind in _FIELDS:
        v = vals[idx]
        if kind in ("u64", "t64"):
            struct.pack_into("<Q", rec, off, _as_int(v) & 0xFFFFFFFFFFFFFFFF)
        elif kind == "u32":
            struct.pack_into("<I", rec, off, _as_int(v) & 0xFFFFFFFF)
        elif kind == "u16":
            struct.pack_into("<H", rec, off, _as_int(v) & 0xFFFF)
        elif kind == "u8":
            rec[off] = _as_int(v) & 0xFF
        else:                                   # s8 / s16 / s32 -- a fixed field
            n = {"s8": 8, "s16": 16, "s32": 32}[kind]
            raw = v.encode("latin1", "replace")[:n]
            rec[off:off + n] = raw.ljust(n, b"\x00")
    return bytes(rec)


def decode_member(rec):
    """The inverse, so a stored record can be checked against a served blob.
    Strings come back NUL-trimmed; the timestamp field is text in the record."""
    out = [""] * _NVALUES
    for idx, off, kind in _FIELDS:
        if kind == "u64":
            out[idx] = "0x%016X" % struct.unpack_from("<Q", rec, off)[0]
        elif kind == "t64":
            # back out as DECIMAL, which is how the client spells it
            out[idx] = str(struct.unpack_from("<Q", rec, off)[0])
        elif kind == "u32":
            out[idx] = str(struct.unpack_from("<I", rec, off)[0])
        elif kind == "u16":
            out[idx] = str(struct.unpack_from("<H", rec, off)[0])
        elif kind == "u8":
            out[idx] = str(rec[off])
        else:
            n = {"s8": 8, "s16": 16, "s32": 32}[kind]
            out[idx] = rec[off:off + n].split(b"\x00")[0].decode("latin1", "replace")
    return out


# --- the TABLE record ------------------------------------------------------
#
# THE OTHER HALF OF THE SNAPSHOT, and it is measured from the SAME decompile the
# member half came from -- `cp__002fb290`'s **tag 0x15** (`TD`) arm calls
# `lb__002f9e10`, which is the table twin of `lb__002f9ab8`. Read straight off
# it (`param_1` is `undefined8 *`, so `param_1 + n` is byte offset `n*8`):
#
#      idx  offset  width      what
#        0  0x18    char[13]   the table NAME (`mgStrCopyLim(param_1+3, .., 0xd)`)
#        1  0x10    u32
#        2  0x00    u64        the table ID -- hex text on the wire, through
#                              `lb__002f8d38`, exactly like a member id
#        3  0x14    u8         the STATE byte (7 = "Open Table", 2 = "Playing")
#        4  0x08    u32        CAPACITY   -- 0x76338 refuses a table whose
#                              seated >= 8, so this pair is seats and its ceiling
#        5  0x0C    u32        SEATED
#        6  0x28    char[64]   the settings block (`mgStrCopyLim(.., 0x40)`)
#
# WARNING: THIS IS THE TWO-INDEPENDENT-READS STANDARD, ACTUALLY MET. `tools/tmptl.py`
# derived the same seven offsets from the PC serialiser at `0x1A3340` and the
# panel reader at `0x66D10`; the map above comes from the PS2 parser. They agree
# field for field, which is why `selftest` asserts our encoder against a blob
# tmptl authored rather than against our own output.
#
# WARNING: A `TD` IS MATCHED BY **NAME**, NOT BY ID. `cp__002fb290` hands
# `lb__002f9f18` the parsed record's `+0x18` and that function walks the live
# table array comparing `entry + 0x18`, allocating a new slot (`lb__002f9dc8`)
# only when nothing matches. The member arm matches on the id (`lb__002f9c28`)
# -- the two halves genuinely differ, and using the id here would append a
# duplicate row on every update instead of editing the one on screen.
_TABLE_FIELDS = (
    (0, 0x18, "s13"), (1, 0x10, "u32"), (2, 0x00, "u64"), (3, 0x14, "u8"),
    (4, 0x08, "u32"), (5, 0x0C, "u32"), (6, 0x28, "s64"),
)
_NTABLEVALUES = 7
TABLE_NAME_OFF = 0x18           # the identity, per lb__002f9f18
TABLE_STATE_OFF = 0x14
TABLE_CAP_OFF = 0x08
TABLE_SEATED_OFF = 0x0C


def encode_table(values):
    """The seven `TD` values -> the 104-byte table record, per `lb__002f9e10`."""
    vals = list(values) + [""] * (_NTABLEVALUES - len(values))
    rec = bytearray(TABLE_REC)
    for idx, off, kind in _TABLE_FIELDS:
        v = vals[idx]
        if kind == "u64":
            struct.pack_into("<Q", rec, off, _as_int(v) & 0xFFFFFFFFFFFFFFFF)
        elif kind == "u32":
            struct.pack_into("<I", rec, off, _as_int(v) & 0xFFFFFFFF)
        elif kind == "u8":
            rec[off] = _as_int(v) & 0xFF
        else:
            n = {"s13": 13, "s64": 64}[kind]
            raw = v.encode("latin1", "replace")[:n]
            rec[off:off + n] = raw.ljust(n, b"\x00")
    return bytes(rec)


def decode_table(rec):
    """The inverse, so a fixture's rows can be read back out as `TD` values."""
    out = [""] * _NTABLEVALUES
    for idx, off, kind in _TABLE_FIELDS:
        if kind == "u64":
            out[idx] = "0x%016X" % struct.unpack_from("<Q", rec, off)[0]
        elif kind == "u32":
            out[idx] = str(struct.unpack_from("<I", rec, off)[0])
        elif kind == "u8":
            out[idx] = str(rec[off])
        else:
            n = {"s13": 13, "s64": 64}[kind]
            out[idx] = rec[off:off + n].split(b"\x00")[0].decode("latin1", "replace")
    return out


#: The block's 64-bit occupancy id, and the settings letters, live in
#: `+0x28`. Both use TM's house encoding: base-16 digits as 'A'..'P', MOST
#: SIGNIFICANT FIRST.
#:
#: WARNING: THIS LIVES IN `services/` BECAUSE `tools/` IS NOT MOUNTED IN THE
#: CONTAINER. `tetramaster` imported `tools/tmptl.py` for exactly this and the
#: import failed on prod -- silently, because the caller fell back to "leave the
#: block as authored". So the card-level write and the occupancy write were both
#: NO-OPS in production while passing every test on the host, which is the
#: "fixed but never ran" shape this project keeps paying for.
BLOCK_LETTER_MAX = 16


def letters(value, n=3):
    """`value` -> `n` base-16 letters, most significant first."""
    if value < 0 or value >= 16 ** n:
        raise ValueError("%d does not fit in %d letter-encoded nibbles (max %d)"
                         % (value, n, 16 ** n - 1))
    return "".join(chr(ord("A") + ((value >> (4 * (n - 1 - i))) & 0xF))
                   for i in range(n)).encode("ascii")


def unletters(raw):
    """The inverse -- what the client reconstructs from those letters."""
    v = 0
    for ch in (raw.decode("latin1") if isinstance(raw, bytes) else raw):
        v = (v << 4) | ((ord(ch) - ord("A")) & 0xF)
    return v


def client_reads_id(block16):
    """Model `0x66DBB`..`0x66F0F` VERBATIM -- the client's own four-group unpack.

    Kept as a separate function from `unletters` ON PURPOSE. The two are derived
    from different things (one from the encoding, one from the disassembly) and
    `selftest` asserts they agree, which is what actually pins the nibble order.
    Collapsing them into one would make that assertion prove nothing.
    """
    n = [(b if isinstance(b, int) else ord(b)) - ord("A") for b in block16]
    if len(n) != 16:
        raise ValueError("the occupancy block is 16 letters, got %d" % len(n))

    def quad(a, b, c, d):               # the (<<4, <<8, <<12, <<0) pattern
        return (n[a] << 4) + (n[b] << 8) + (n[c] << 12) + n[d]

    v = quad(10, 9, 8, 11) * 0x10000                # 0x66DCC -> bits 16..31
    v |= quad(6, 5, 4, 7) * 0x100000000             # 0x66E1C -> bits 32..47
    v |= quad(2, 1, 0, 3) * 0x1000000000000         # 0x66E6C -> bits 48..63
    v |= quad(14, 13, 12, 15)                       # 0x66EBF -> bits  0..15
    return v & 0xFFFFFFFFFFFFFFFF


def table_name(values):
    """A table's identity, as `lb__002f9f18` compares it."""
    return (list(values) + [""] * _NTABLEVALUES)[0]


# --- the roster ------------------------------------------------------------
#
# `{chan: {"seq": n, "members": {member_id: [11 values]}}}`. Keyed by member id
# because that is what `cp__002fb290`'s `PC` arm removes by, and what
# `lb__002f9c28` looks a record up by when a `PD` delta arrives.

_LOCK = threading.RLock()
_RECORDS = {}          # member_id -> the 11 values, as sent
_ROOMS_SEQ = {}        # room id  -> its update sequence
_GUIDS = {}            # member_id -> the id the client calls itself
_NAMES = {}            # member_id -> the handle name to DRAW (see note_name)
_DELTAS = {}           # room id  -> list of [seq, tag, values], oldest first
_TABLES = {}           # room id  -> {table NAME: [7 values]}  -- see note_table
_SEATS = {}            # room id  -> {table INDEX: list of [member, ident]}
_CONFIRMED = {}        # room id  -> [table index, ..] whose owner confirmed
_PEERS = {}            # room id  -> the ROOM PEER GUID, see note_room_peer
_POLIDS = {}           # member_id -> Tetra Master's POL-ID, see note_pol_id
#: room id -> {table INDEX (str): {field: value}} -- a table's FULL rule set, the
#: fourteen `@Tet=`/`@Tab=` fields its owner last chose. See `note_table_rules`.
_RULES = {}
_OWNER = [False]
_FILE = os.environ.get(
    "POL_TM_ROSTER_FILE",
    os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "tm-roster.json"))
_CACHE = {"mtime": -1.0, "data": {}}


#: Have we seeded memory from the file yet? See `_adopt`.
_ADOPTED = [False]


def _adopt():
    """Seed the in-memory maps from the file BEFORE this process starts writing it.

    WARNING: THIS IS A RESTART WIPE, AND IT DESTROYED A LIVE RESERVATION IN FRONT OF
    THE OWNING PROCESS. `_publish` sets `_OWNER[0] = True` and from that moment every
    reader serves this process's own dicts and every write dumps them over the
    file. Nothing ever loaded them back, so a restarted container came up with
    EMPTY maps and the first `note_member` -- which happens within seconds,
    because clients re-send their records constantly -- published that emptiness
    over everything the previous process had.

    Member records survived that only because the clients re-send them. **Table
    rows do not: only the server knows them**, so every restart silently erased
    every reservation in every room.

    Measured 2026-08-20: `#TM0R001` table 1 published "1 seated" at sequence 9;
    after a deploy restart the file held `"tables": {}` at sequence 11 with no
    release delta ever sent. Live testing saw exactly what that implies -- one client
    still showed table 1 as reserved (empty with yellow glow icon), the other
    showed it as empty: the client that had received the seated delta kept
    drawing it, and the one that re-read afterwards got the authored empty row.

    WARNING: the room registry's own history warns that a wipe like this reads as
    split-brain and is not. It is not split-brain here either: one process, one
    restart, no second writer needed. **Check StartedAt before reaching for a
    concurrency story** -- that warning was earned there and it applies to this
    sibling file too.

    `setdefault`, never overwrite: anything this process has already learned is
    newer than the file.
    """
    if _ADOPTED[0]:
        return
    _ADOPTED[0] = True
    disk = _read_file() or {}
    for mem, key in ((_RECORDS, "records"), (_ROOMS_SEQ, "seq"),
                     (_GUIDS, "guids"), (_DELTAS, "deltas"),
                     (_NAMES, "names"), (_TABLES, "tables"),
                     (_PEERS, "peers"), (_POLIDS, "polids"),
                     (_SEATS, "seats"), (_RULES, "rules"),
                     (_CONFIRMED, "confirmed")):
        for k, v in (disk.get(key) or {}).items():
            mem.setdefault(k, v)


def _publish():
    """Hand the roster to the other container. Never raises -- a snapshot must
    not be able to break a room entry."""
    # NEVER take ownership of a file we have not read. See `_adopt`.
    if not _ADOPTED[0]:
        _adopt()
    _OWNER[0] = True
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"records": _RECORDS, "seq": _ROOMS_SEQ,
                       "guids": _GUIDS, "deltas": _DELTAS,
                       "names": _NAMES, "tables": _TABLES,
                       "peers": _PEERS, "polids": _POLIDS,
                       "seats": _SEATS, "rules": _RULES,
                       "confirmed": _CONFIRMED}, f)
        os.replace(tmp, _FILE)                  # atomic
        _CACHE["mtime"] = -1.0
    except OSError:
        pass


def _live_records():
    if _OWNER[0]:
        with _LOCK:
            return json.loads(json.dumps(_RECORDS))
    return (_read_file() or {}).get("records", {})


def _live_seq():
    if _OWNER[0]:
        with _LOCK:
            return dict(_ROOMS_SEQ)
    return (_read_file() or {}).get("seq", {})


def _live_guids():
    if _OWNER[0]:
        with _LOCK:
            return dict(_GUIDS)
    return (_read_file() or {}).get("guids", {})


def _live_deltas():
    if _OWNER[0]:
        with _LOCK:
            return json.loads(json.dumps(_DELTAS))
    return (_read_file() or {}).get("deltas", {})


def _live_peers():
    if _OWNER[0]:
        with _LOCK:
            return dict(_PEERS)
    return (_read_file() or {}).get("peers", {})


def _live_tables():
    if _OWNER[0]:
        with _LOCK:
            return json.loads(json.dumps(_TABLES))
    return (_read_file() or {}).get("tables", {})


def _live_rules():
    if _OWNER[0]:
        with _LOCK:
            return json.loads(json.dumps(_RULES))
    return (_read_file() or {}).get("rules", {})


def note_table_rules(chan, index, fields):
    """A table's FULL rule set -- the fourteen `@Tet=`/`@Tab=` fields.

    WARNING: WHY THIS IS NOT JUST THE TABLE ROW. Only `lu`/`ll` have a home in the
    104-byte record's `+0x28` block (`tetramaster._settings_block`); the other
    twelve fields have nowhere to live in it. But the CLIENT wants all of them:
    the settings dialog can be told to seed a page from THE TABLE'S rules rather
    than the player's own preset, and the only channel that carries them is code
    **0x13**, which `tetramaster.table_info_body` builds out of exactly this set.

    WARNING: AND IT HAD TO SURVIVE A RESTART. This lived in a module-level dict in
    `tetramaster` (`_TABLE_SETTINGS`), so it was empty in a freshly started
    container and stayed empty until some table's owner reconfigured it. Every
    dialog opened from one of the sites that hard-codes "use the table's rules"
    then found the info record unpopulated -- `0x873A0` fails its `+0x30 == 2`
    gate, copies nothing, and `0x6BC61` answers by displaying a message and
    dropping to state 0x64 instead of opening. Same family as the live report of
    "hanging at getting table info".

    Kept here rather than in `tetramaster` for the reason `note_room_peer` is:
    it is per-ROOM state and BOTH CONTAINERS have to agree on it.

    Returns True if anything changed, matching `note_table`.
    """
    room = "0x%016X" % room_id_for(chan)
    key = str(int(index))
    # WARNING: NOT EVERY FIELD IS A NUMBER. `pw` is the PASSWORD's own characters
    # (`tetramaster.TABLE_SETTING_TEXT_KEYS`), and `int()` on it either raises
    # -- taking the whole rules store down with it, silently, for any table
    # anyone put a password on -- or truncates a hex secret at its first letter
    # into a different password. Numbers are coerced, text is kept verbatim,
    # and anything that is neither is dropped rather than guessed at.
    clean = {}
    for k, v in dict(fields or {}).items():
        k = str(k)
        if k in ("Tbl", "Game"):          # page selectors, not rules
            continue
        if isinstance(v, str) and not v.lstrip("-").isdigit():
            clean[k] = v
            continue
        try:
            clean[k] = int(v)
        except (TypeError, ValueError):
            clean[k] = v
    with _LOCK:
        if not _ADOPTED[0]:
            _adopt()
        cur = (_RULES.setdefault(room, {})).get(key)
        if cur == clean:
            return False
        _RULES[room][key] = clean
    _publish()
    return True


def table_rules(chan, index):
    """`{field: value}` for one table, or None if nobody has set them."""
    room = "0x%016X" % room_id_for(chan)
    got = ((_live_rules() or {}).get(room) or {}).get(str(int(index)))
    return dict(got) if got else None


def _live_names():
    if _OWNER[0]:
        with _LOCK:
            return dict(_NAMES)
    return (_read_file() or {}).get("names", {})


def _live_pol_ids():
    if _OWNER[0]:
        with _LOCK:
            return dict(_POLIDS)
    return (_read_file() or {}).get("polids", {})


def note_name(member_id, name):
    """The handle name to DRAW for this member. Returns True if it changed.

    WARNING: THE CLIENT SENDS THIS FIELD EMPTY AND THE SERVER IS MEANT TO FILL IT.
    Measured on the wire 2026-08-18, a real room entry:

        <DE>(0x0000000000000000,0,0,0,,2,1787090558,7274505,0,
             0x0000002000000001,Lex            DAA0AAAJABAADIAB)
                              ^^ value 4, EMPTY

    Value 4 is `+0x28`, and `+0x28` is the name the room screen DRAWS --
    `roomwin__ZoneListPage_0033e2c0` passes `record + 0x28` to the row's ZoneName
    widget. Value 10 (`+0x38`) is the name-plus-letter-block, and the
    pane uses it for its GATES (`+0x38` non-zero, `+0x47` a length letter in
    1..15) and for the Member Info fields -- not for the row's text.

    That is the whole of "the member list has an entry but the name is
    invisible": the gates pass on +0x38, so the ROW draws; the text comes from
    +0x28, which nobody had filled. Value 0 has the same shape of story -- the
    client sends 0 for itself and the server fills the guid -- so a second
    server-filled field is the expected pattern, not a surprise.

    A client cannot supply this: it does not know the other players' handles. We
    do, from the account row.
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return False
    name = (name or "").strip()[:15]
    if not mid or not name:
        return False
    with _LOCK:
        if _NAMES.get(str(mid)) == name:
            return False
        _NAMES[str(mid)] = name
    _publish()
    return True


def name_of(member_id):
    """The handle name to draw for this member, or "" if we have never learned
    one. Empty is honest: it is exactly what the client sent us."""
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return ""
    return str((_live_names() or {}).get(str(mid), ""))


def member_by_name(name):
    """The member id (as a string) whose handle is `name`, or "" if unknown
    or AMBIGUOUS -- two members sharing a handle must not swap winnings.

    Added for the auction sweep: bid rows recorded before the member id
    reached the recorder carry member=0, and a pending credit keyed by NAME
    lands in a file the Check Out door (which looks up by member id) can
    never read."""
    if not name:
        return ""
    hits = [mid for mid, n in (_live_names() or {}).items()
            if str(n) == str(name)]
    return hits[0] if len(hits) == 1 else ""


#: Has this process already recovered the published POL-IDs? See `_seed_pol_ids`.
_POLIDS_SEEDED = [False]


def _seed_pol_ids():
    """Recover the published POL-IDs into memory, once per process.

    WARNING: WITHOUT THIS, EVERY RESTART SILENTLY FORGETS WHO PEOPLE ARE. `_publish`
    dumps the in-memory maps and takes ownership (`_OWNER[0] = True`), and a
    fresh process starts with all of them EMPTY -- so the first publish after a
    restart writes an empty `polids` straight over the good data on disk.

    Records survive that because every client re-sends `<DE>`/`<PD>` constantly;
    a POL-ID does not, because `@Init=/NN=` arrives **once per launch**. So a
    member who launched before the restart is anonymous until they relaunch the
    game, and `_chat_roster_rows` correctly-but-uselessly omits them.

    Measured 2026-08-20: member 9's POL-ID was captured at 15:47, lost to a
    bounce, and at 17:32 both players were served a roster containing only member
    6 -- so the one player who WAS listed was listed to himself, and his client
    hid the row as "me". An empty member list, from an identity we already knew
    and threw away.

    Only fills GAPS: anything this process has learned since starting wins, so a
    stale file can never overwrite a live capture.
    """
    if _POLIDS_SEEDED[0]:
        return
    _POLIDS_SEEDED[0] = True
    try:
        published = (_read_file() or {}).get("polids") or {}
    except Exception:
        return
    with _LOCK:
        for key, val in published.items():
            _POLIDS.setdefault(str(key), val)


def note_pol_id(member_id, hexid):
    """Tetra Master's **POL-ID** for this member -- the 64-bit id the client
    calls itself by -- kept as the CLIENT'S OWN 16-hex STRING.

    VERIFIED: THIS IS THE ID THE CHAT ROSTER'S `/NN=` IS MATCHED AGAINST, and it is a
    different id from `note_guid`'s. Read off TM.dll (base 0x04F90000):

        0x87F74/0x87F8B   `@Init=/NN=` is formatted from [0x5242918]/[0x524291C]
        0x14BC50          those two are copied to [0x5245308]/[0x524530C]
        0x0AFE3           the chat sidebar compares each roster row's id pair
                          against [0x5245308]/[0x524530C] and SKIPS the match

    So the row a client recognises as itself is the one whose `/NN=` equals what
    that client sent us in `@Init=/NN=`, and every other row is somebody else.
    `0x81930` prints the same pair as **`POL-ID = %d`**, which is the name used
    here; TM's *other* self-id, `GAME-ID` (rva 0x294BD4, from `@Init=/GID=`), is
    what `@VsGameInit`'s `/ID=` is matched against and is NOT interchangeable.

    WARNING: **STORED VERBATIM, NOT PARSED.** The client formats it with the 16-digit
    hex writer at 0xACDA0 and parses it back with the accumulator at 0xAB72E; if
    we echo its own characters, the round trip is its own inverse and no reading
    of the nibble order can be wrong. Parsing it to an int and re-printing is the
    one way to get this wrong for free.

    WARNING: AND IT IS NOT `note_guid`'s VALUE. That one is the lobby-band 44-bit
    guid + 6-bit slot the member RECORD's `+0x00` carries, and `note_guid`
    raises if handed this id -- see its banner. Both are true at once for the
    same person; they are read by different subsystems.
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return False
    text = (hexid or "")
    if isinstance(text, bytes):
        text = text.decode("ascii", "replace")
    # THE WIRE FORM AS IT COMES, from either source: `@Init=/NN=` is bare hex
    # and class-L `<PC>` arrives through polpro as `(0xAB12CD56EB0F5932)`,
    # parentheses and all. Normalising here means neither caller has to know.
    text = text.strip().strip("()").strip().upper()
    if text.startswith("0X"):
        text = text[2:]
    if not mid or not text or len(text) > 16:
        return False
    if any(c not in "0123456789ABCDEF" for c in text):
        return False
    text = text.rjust(16, "0")
    if text == "0" * 16:
        return False                            # the client's "I do not know"
    _seed_pol_ids()
    with _LOCK:
        if _POLIDS.get(str(mid)) == text:
            return False
        _POLIDS[str(mid)] = text
    _publish()
    return True


def pol_id_of(member_id):
    """This member's POL-ID as the 16-hex string to put on the wire, or "" if
    they have never sent an `@Init=`. Empty is honest: a made-up id matches
    nobody's self-check and would put a stranger's name on the row."""
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return ""
    _seed_pol_ids()
    return str((_live_pol_ids() or {}).get(str(mid), ""))


def _read_file():
    try:
        mtime = os.stat(_FILE).st_mtime
    except OSError:
        return {}
    if mtime != _CACHE["mtime"]:
        try:
            with open(_FILE, "r", encoding="utf-8") as f:
                _CACHE["data"] = json.load(f) or {}
            _CACHE["mtime"] = mtime
        except (OSError, ValueError):
            return _CACHE["data"]               # torn write: last good view stands
    return _CACHE["data"]


#: How many deltas we keep per room. `cp__002fae98` full-reloads a client more
#: than **50** behind (`iVar2 < DAT_003fb884 - 0x32`), so anything older than that
#: can never be applied and keeping it would only grow the published file. The
#: margin above 50 is so a client sitting exactly on the boundary is still served
#: from the log rather than bounced to a reload it does not need.
DELTA_WINDOW = 64

#: `cp__002fae98`'s own constant. A client this far behind is TOLD to reload
#: rather than fed a chain it will throw away.
RELOAD_BEHIND = 0x32


def _bump(room, tag, values):
    """Advance a room's sequence and record the delta that caused it.

    WARNING: CALL WITH `_LOCK` HELD. The sequence and the log entry have to move
    together: a client that reads a sequence with no matching delta sits waiting
    for one that will never arrive, which is exactly the stall a missing delta produces.

    `tag` is the delta command the client will apply -- `PD` (add/update, the
    88-byte record) or `PC` (remove by id). `cp__002fb290` is the switch that
    consumes them.
    """
    seq = _ROOMS_SEQ.get(room, 0) + 1
    _ROOMS_SEQ[room] = seq
    log = _DELTAS.setdefault(room, [])
    log.append([seq, tag, list(values)])
    del log[:-DELTA_WINDOW]
    return seq


def deltas_after(chan, have):
    """`(seq, tag, values)` the client at sequence `have` still needs, or None.

    None means **we cannot serve this client from the log** and it should be told
    to reload the snapshot instead -- either it is further behind than we kept, or
    it is claiming a sequence we never issued (which is what a client holding a
    blob from a previous process looks like). An empty list means it is CURRENT.

    WARNING: THE CLIENT APPLIES STRICTLY IN ORDER. `cp__002fb560`'s drain takes a queued
    slot only when `slot.seq == ctx.seq + 1`, so a gap is not a delay, it is a
    permanent stall. Hence the contiguity check rather than a simple filter.
    """
    room = "0x%016X" % room_id_for(chan)
    cur = int((_live_seq() or {}).get(room, 0))
    try:
        have = int(have)
    except (TypeError, ValueError):
        return None
    if have == cur:
        return []                       # nothing to say
    if have > cur or have < cur - RELOAD_BEHIND:
        return None                     # ahead of us, or past the reload gate
    log = (_live_deltas() or {}).get(room) or []
    want = [d for d in log if int(d[0]) > have]
    if len(want) != cur - have:
        return None                     # we do not hold the whole chain
    for i, d in enumerate(want):        # ...and it must be contiguous
        if int(d[0]) != have + 1 + i:
            return None
    return [(int(d[0]), str(d[1]), list(d[2])) for d in want]


def dd_groups(deltas):
    """The `<DD>` update message for `deltas`, as polpro groups.

    THE FRAMING IS `cp__002fae98` + `mg__002f7d00`, and the indices are GROUP
    indices, not value indices (an earlier reading as value indices was wrong):

        group 0   <DD>   the command; `lb__002f99f8` reads group 0's code to
                         route the message, and nothing reads its value
        group 1   <DN>   the pair COUNT -- located BY TAG (`mg__002f80e8(msg,
                         0x1c)`), so its position is convention, not a rule
        group 2   <DC>   sequence of the first delta   (`mg__002f80a8(msg, 2)`)
        group 3   <PD>   its payload, a WHOLE GROUP    (`lb__002f88f8(.., 3)`)
        group 4/5, 6/7   the next pairs; the loop steps both indices by 2

    WARNING: THE SEQUENCE GROUP'S TAG IS NEVER READ. `mg__002f80a8` takes a group index
    and returns that group's value 0 as an integer without looking at its code.
    `DC` is chosen because it is the class-L delta family's one unclaimed tag
    (DR 25, DD 26, DO 27, DN 28, **DC 29**) -- a convention we picked, and the
    client cannot tell us we picked wrong.

    WARNING: AND NO ESCAPING IS INVOLVED. An earlier reading took `lb__002f88f8` as copying "the
    payload value's raw bytes", which made the delta a message nested inside a
    value and raised the escape-set question that blocked this work. It is not:
    `mg__002f7d00` SKIPS N GROUPS and `mg__002f78f0` walks to the group's own
    terminator, so the payload is a sibling group at the top level and is copied
    verbatim, `<PD>` through its closing 0x07. Escaping (a BACKSLASH before an
    0x06 or an 0x07) is a value-level facility that deltas never reach.
    """
    groups = [("DD", ["0"]), ("DN", [str(len(deltas))])]
    for seq, tag, values in deltas:
        groups.append(("DC", [str(int(seq))]))
        groups.append((str(tag), [str(v) for v in values]))
    return groups


#: *** THREE IDS NAME ONE PLAYER, AND THEY ARE NOT INTERCHANGEABLE. ***
#: This cost more time than anything else on this track, because picking the
#: wrong one makes a row VANISH instead of raising:
#:
#:   accounts.member.id      OURS. A small integer, a database row. It exists on
#:                           the server and nowhere else, and no client can
#:                           resolve it. Serving it in a member record put ids in
#:                           the list that nothing could look up.
#:   handle.client_guid      THE LOBBY BAND'S. 44-bit guid + 6-bit SLOT
#:                           learned from `u/account`'s
#:                           fetch subject. **This is what the member record's
#:                           `+0x00` wants** -- it is what the working placeholder
#:                           carried (0x000000860FB3E2A2).
#:   @Init=/NN= and <PC>     TETRA MASTER'S OWN, a full 64-bit value
#:                           (0xAB12CD56EB0F5932). Right for `@GameM=/ID=` and the
#:                           `@GameML` reply, which the client DROPS if the echoed
#:                           `/ID=` does not match. Wrong for the member record.
#:
#: THE SHAPES TELL THEM APART, which is what `is_lobby_band_id` is for. A
#: lobby-band id's top 20 bits are zero because the value is a 44-bit guid in the
#: low bits and a 6-bit slot above it; a Tetra Master id fills all 64. So the
#: 64-bit id fed to the record gives `>> 44` = 0xB62DC, a "slot" that is not a
#: slot, and the row is dropped somewhere downstream with nothing logged.

#: `>> 44` must land inside the 6-bit slot field.
LOBBY_ID_MAX = (1 << 50) - 1


def is_lobby_band_id(value):
    """Could `value` be a lobby-band self id (44-bit guid + 6-bit slot)?

    A shape test, not a lookup: it cannot tell a wrong id from a right one, only
    a wrong KIND of id from a plausible one. That is still the difference between
    a logged rejection and a row that silently never draws.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return False
    return 0 < v <= LOBBY_ID_MAX


def note_guid(member_id, guid):
    """What a client calls ITSELF, learned from class-L `<PC>` or `@Init=/NN=`.

    WARNING: THE RECORD ID IS A GUID, NOT OUR MEMBER ROW ID. `@GameM=/ID=<u64>` is the
    client asking about a table using the id it knows itself by, and `@GameML`'s
    parser DROPS the whole reply if the echoed `/ID=` does not match
    (TM.dll 0x838E3). Serving our own small integers put ids in the member
    list that no client could resolve. `tools/tmptl.py --from-log` had this right
    all along -- it takes the id from `@GameM=/ID=` or `<PI>`.

    Measured: `<PC>(0xAB12CD56EB0F5932)` and `@Init=/NN=AB12CD56EB0F5932` from
    the same client agree, so either is a source.
    """
    try:
        mid, g = int(member_id), int(guid)
    except (TypeError, ValueError):
        return False
    if not mid or not g:
        return False
    # WARNING: REFUSE THE WRONG KIND OF ID RATHER THAN STORE IT. Value 0 of the member
    # record is the LOBBY-BAND self id; Tetra Master's own 64-bit id has been
    # offered here twice, and storing it does not fail -- the row simply stops
    # drawing, with nothing in any log to say why. See the banner above.
    if not is_lobby_band_id(g):
        raise ValueError(
            "0x%X is not a lobby-band self id (>> 44 = 0x%X is not a 6-bit "
            "slot) -- this looks like Tetra Master's 64-bit @Init=/NN= id, "
            "which the member record's +0x00 must not carry" % (g, g >> 44))
    with _LOCK:
        if _GUIDS.get(str(mid)) == g:
            return False
        _GUIDS[str(mid)] = g
    _publish()
    return True


def guid_of(member_id):
    """The client's own id for this member, or our row id if it has not said."""
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return 0
    return int((_live_guids() or {}).get(str(mid), mid))


#: Members we have already complained about, so a 2 s poll does not fill the log.
_GUID_WARNED = set()


def guid_is_placeholder(member_id):
    """True while we are serving OUR row id because no lobby-band id is known.

    WARNING: THIS IS THE UNVERIFIED HALF OF THE MEMBER-PANE FIX AND IT MUST BE VISIBLE.
    `note_member` writes `guid_of(member_id) or member_id` into the record's
    `+0x00`, and the fallback arm is reached whenever `handle.client_guid` is
    NULL -- which it is for most handles, because it is only learned when that
    client fetches `u/account` and we see the subject. A row carrying our row id
    is well-SHAPED (`is_lobby_band_id` says yes) and still meaningless to every
    client, so nothing downstream can catch it. The only defence is saying so.

    Callers should log once per member; `warn_once` is the latch for that.
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return False
    return str(mid) not in (_live_guids() or {})


def warn_once(member_id):
    """True the FIRST time it is asked about a member, so a poll cannot spam."""
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return False
    if mid in _GUID_WARNED:
        return False
    _GUID_WARNED.add(mid)
    return True


def note_member(member_id, values, chan=None):
    """A `<DE>` or `<PD>` arrived. Both are the same delta in `cp__002fb290`
    (0x20 and 0x13 share the arm), so both land here.

    WARNING: **KEYED BY MEMBER, NOT BY ROOM, AND IT MUST BE.** The first version looked
    the sender's room up in the IRC registry and dropped the record if it found
    none -- which threw away every `<DE>`, the most important record of all,
    because **`<DE>` ARRIVES BEFORE THE JOIN**. Measured 2026-08-18: `<DE>` at
    07:36:25 and the `JOIN #TM0R001` 1.5 s later; `janlobby` had already recorded
    the same ordering for Janhourou ("THE FETCH PRECEDES THE JOIN"). The record
    carries its own room at value 9, so it never needed the registry.

    WARNING: **PRESENCE COMES FROM THE RECORD, NOT FROM THE SOCKET.** Value 5 is the
    client's own in-room flag -- `2 = in the room, 0 = leaving` -- so the
    client says when it goes. Keying presence to IRC membership instead made
    people blink out of each other's lists every time an auth session churned,
    which is exactly what live testing showed.

    `member_id` is OURS; the id written into value 0 is the client's own guid
    (`guid_of`), because that is what the client can resolve.
    """
    vals = [("" if v is None else str(v)) for v in values][:_NVALUES]
    vals += [""] * (_NVALUES - len(vals))
    # THE GUID, not our row id -- see `note_guid`. Falls back to the row id only
    # while the client has not yet said what it calls itself.
    vals[0] = "0x%016X" % (guid_of(member_id) or int(member_id))
    # ...AND THE NAME, for the same reason and from the same kind of source: the
    # client sends value 4 EMPTY and cannot do otherwise -- it does not know who
    # anybody is. `+0x28` is what the room screen draws, so a record that
    # keeps the client's blank is a row with no name on it, which is exactly the
    # symptom. See `note_name`. Whatever the client did send is preferred, on the
    # principle that a client telling us something beats our guess about it.
    vals[4] = (vals[4] or name_of(member_id))[:15]
    # SEATED MEMBERS CARRY STATE 3 -- so the delta stream teaches the owner's
    # Start-Game gate without a re-fetch. See `_mark_status`; `_member_seated`
    # reads the same persisted seat list `build_ptl` marks from, so the two
    # paths agree by construction. A non-seated member keeps what it sent.
    vals[10] = _mark_status(vals[10], _member_seated(member_id))
    key = str(int(member_id))
    with _LOCK:
        if _RECORDS.get(key) == vals:
            return False                        # an idle re-send changes nothing
        was = _RECORDS.get(key)
        _RECORDS[key] = vals
        room = _room_key(vals)
        # A MOVE IS A REMOVAL SOMEWHERE ELSE. If this record names a different
        # room than the one we held, the people still standing in the OLD room
        # have to be told this member left it -- their snapshot has the row and
        # nothing else would ever take it out. `PC` removes by id
        # (`cp__002fb290` case 0x14), which is why value 0 is what it carries.
        if was is not None and _room_key(was) != room:
            _bump(_room_key(was), "PC", [vals[0]])
        # WARNING: `stat = 0` IS A DEPARTURE, AND A DELTA STREAM HAS TO SAY SO WITH
        # `PC`. `members()` filters a zero-stat record out of the SNAPSHOT, but a
        # client that already holds the row applies whatever we send: a `PD`
        # carrying stat 0 would update the row and leave it drawn. The two halves
        # have to agree or a player who leaves stays on screen until a reload.
        _bump(room, "PC" if _as_int(vals[5]) == 0 else "PD",
              [vals[0]] if _as_int(vals[5]) == 0 else vals)
    _publish()
    return True


def _room_key(vals):
    """The room a record belongs to -- its own value 9, normalised."""
    return "0x%016X" % _as_int(vals[9] if len(vals) > 9 else 0)


def forget_member(member_id):
    """`PC` -- the member is gone. Returns True if we held anything."""
    key = str(int(member_id))
    with _LOCK:
        vals = _RECORDS.pop(key, None)
        if vals is None:
            return False
        _bump(_room_key(vals), "PC", [vals[0]])
    _publish()
    return True


def fixture_table(index):
    """The shipped fixture's row for `#TM0T%03d`, as 7 `TD` values, or None.

    The authored row is the STARTING POINT for a live update -- a table keeps
    its id, capacity and name, and only what actually changed is overwritten.
    Building a row from scratch instead would silently drop whichever field the
    caller forgot, and `0x1A1552` drops a row with no name at `+0x18`.
    """
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    want = "#TM0T%03d" % idx
    try:
        import tmfixtures
        blob = tmfixtures.ptl()
    except Exception:
        return None
    if len(blob) < TOTAL:
        return None
    n = max(0, min(struct.unpack_from("<i", blob, TABLE_COUNT_OFF)[0], TABLE_SLOTS))
    for i in range(n):
        vals = decode_table(blob[TABLE_OFF + i * TABLE_REC:
                                 TABLE_OFF + (i + 1) * TABLE_REC])
        if table_name(vals) == want:
            return vals
    return None


def note_room_peer(chan, guid):
    """The room's own TM0 peer guid -- what every class-L `<DR>` is addressed to.

    IT IS THE ORIGIN A TABLE PEER IS INDEXED AGAINST (`tetramaster.
    table_index_for_peer`), and it is the ONE piece the auth band needs that only
    a nick on that band carries. Kept here rather than in `tetramaster` because
    it is per-ROOM state and both containers have to agree on it, which is what
    this module's snapshot is for.

    WARNING: It is also the value `responders` records as `b/g/PTL`'s SUBJECT and had
    written down as unexplained. Same number, two bands.
    """
    try:
        guid = int(guid)
    except (TypeError, ValueError):
        return False
    if not guid:
        return False
    room = "0x%016X" % room_id_for(chan)
    with _LOCK:
        if _PEERS.get(room) == guid:
            return False
        _PEERS[room] = guid
    _publish()
    return True


def room_peer(chan):
    """The room's peer guid, or None -- see `note_room_peer`."""
    v = (_live_peers() or {}).get("0x%016X" % room_id_for(chan))
    return int(v) if v else None


def _say(msg):
    """Flushed diagnostic through the same file the band uses.

    Same shape as `tetramaster._say`, for the same two reasons its banner
    records: `authsess` does not set PYTHONUNBUFFERED, and its stdout reaches
    `docker logs` on no container that matters -- so a bare print here is not
    an observable. `responders` is only imported at call time and only used
    when it is ALREADY loaded (the serving process); standalone runs (the
    selftests, `tmtables.py` outside a band) fall back to a flushed print.
    """
    try:
        import titles
        if titles.core.bound:
            titles.core.log("authserv", msg)
            return
    except Exception:
        pass
    try:
        print(msg, flush=True)
    except Exception:
        pass


def note_table(chan, values):
    """A table's live state -- the twin of `note_member`, and `TD` is its delta.

    WARNING: KEYED BY NAME, because that is the identity `lb__002f9f18` compares when a
    `TD` arrives (`entry + 0x18`). Using the id here would append a duplicate row
    on every update instead of editing the one already on screen. See the
    `_TABLE_FIELDS` banner.

    Returns True if anything actually changed, so an idle re-send neither bumps
    the sequence nor puts a no-op delta in the log -- the same contract
    `note_member` keeps, and for the same reason: a client that sees the sequence
    move expects a delta that says something.
    """
    vals = [("" if v is None else str(v)) for v in values][:_NTABLEVALUES]
    vals += [""] * (_NTABLEVALUES - len(vals))
    name = table_name(vals)
    if name.startswith("#TM0T")             and os.environ.get("POL_TM_TABLE_ID_REWRITE", "1") == "1":
        # ONE ID, TWO PATHS. The snapshot rewrites a colliding id on the way out;
        # if the delta stream did not, a client that applied a `TD` would hold a
        # different id from one that re-fetched, and only one of them would draw
        # the tile as its own. See `canonical_table_id` -- and the PRE-FOLD
        # (room-shared) id is rewritten too, same as `_normalise_table_ids`,
        # so a stored legacy row heals the moment anything republishes it.
        try:
            tno = int(name[5:])
            rn = _room_no(chan)
            cur = _as_int(vals[2])
            # `round_trips` is the 2026-08-22 addition: a stored row whose id
            # cannot survive the nick (the old `0x0021<room>` high half) is as
            # broken as a legacy pre-fold one, and heals the same way.
            if cur < TABLE_ID_MIN or (rn and cur == canonical_table_id(tno - 1, 0))                     or (rn and not round_trips(cur)):
                vals[2] = "0x%016X" % canonical_table_id(tno - 1, rn)
        except ValueError:
            pass
    if not name:
        # `0x1A1552` gates on the first BYTE at +0x18 and drops the row, and
        # `lb__002f9f18` has nothing to match on -- a nameless table cannot be
        # addressed by either side, so storing one only hides the bug.
        _say("tm: note_table %s REFUSED a nameless row -- nothing stored" % chan)
        return False
    room = "0x%016X" % room_id_for(chan)
    _adopt()                    # diff against what is really out there
    with _LOCK:
        have = _TABLES.setdefault(room, {})
        if have.get(name) == vals:
            # THREE-OUTCOME LOGGING: the
            # no-op and the change must print DIFFERENT lines, or neither can
            # be told from "never ran" -- the success-only-log trap.
            _say("tm: note_table %s %s: no-op (identical row already published)"
                 % (chan, name))
            return False
        have[name] = vals
        _bump(room, "TD", vals)
        seq = _ROOMS_SEQ.get(room)
    _publish()
    # The decoded truth of what just went on the wire, so authserv.log alone
    # can answer "what did we serve at the minute of the screenshot".
    blk = vals[6] or ""
    cnt = occ = None
    try:
        if len(blk) >= 30:
            cnt = unletters(blk[4:6])
            occ = unletters(blk[14:30])
    except Exception:
        pass
    _say("tm: note_table %s %s PUBLISHED seq %s: state=%s id=%s cap=%s "
         "seated=%s block-count=%s occ-id=%s"
         % (chan, name, seq, vals[3], vals[2], vals[4], vals[5],
            cnt, ("0x%X" % occ) if occ is not None else occ))
    return True


#: *** THE SEAT LIST HAS TO SURVIVE WHATEVER THE ROW SURVIVES. ***
#:
#: WARNING: `_adopt`'s banner records that a restart used to erase every reservation,
#: because only the server knew the table rows. Persisting `_TABLES` fixed that
#: -- AND CREATED ITS EXACT INVERSE, which is what live testing saw on
#: 2026-08-20T19:25: "no one has done anything with the table since the clients
#: restarted", one client's tile saying `Join!` and the other's `Setting Up`.
#:
#: `tetramaster._SEATED` -- WHO is sitting at each table -- was a plain module
#: dict, never written to this file and never read back. So after an authsess
#: restart the published row still said `state 1, seated 1, <occupancy id>` and
#: the process behind it knew about nobody:
#:
#:     tm-roster.json  "#TM0T001": [.., "1", "8", "1", "BAAAAB..LGCNMI.."]
#:     _SEATED         {}
#:
#: and every consequence follows from that one gap. `release_seats` iterates
#: `_SEATED`, so the table can never be freed. `_push_owner_change` needs a seat
#: to inherit, so a handoff can never fire. `_seat_at_table` reads an empty list,
#: so the next joiner is offered a fresh reservation on a table the row says is
#: taken. The reservation becomes permanent and unowned.
#:
#: WARNING: STORED, NOT DERIVED. The row carries one id (`block[14..29]`) and a count,
#: so a second player's member id is simply not in it -- reconstructing the list
#: from the row would invent one half and lose the other, and a seat list that
#: disagrees with the row is the drift `release_seats`' own banner is about. The
#: two halves are written together and read back together, which is the only
#: arrangement that cannot desynchronise.
def note_seats(chan, seats_by_index):
    """Persist who is sitting at each table in `chan`. Returns True if changed.

    Deliberately NOT a delta and NOT a sequence bump: no client ever sees this.
    It is server-side bookkeeping that has to outlive the process, and bumping
    the room for it would send every client a `<DD>` describing nothing.
    """
    room = "0x%016X" % room_id_for(chan)
    clean = {}
    for idx, seats in (seats_by_index or {}).items():
        rows = [[int(m), int(i or 0)] for m, i in (seats or []) if m]
        if rows:
            clean[str(int(idx))] = rows
    _adopt()
    with _LOCK:
        if _SEATS.get(room, {}) == clean:
            return False
        if clean:
            _SEATS[room] = clean
        else:
            _SEATS.pop(room, None)
    _publish()
    return True


def note_table_confirmed(chan, index):
    """The owner pressed Confirm (0x14) for this table. SHARED, because the
    process-local `tetramaster._TABLE_CONFIRMED` is wiped by every deploy -- and
    the "Setting up" launch freeze locks every control on every seated client,
    so a wiped flag bricks the table with the players inside it (measured
    23:52:31: the hold fired for a confirm sent four minutes earlier, across one
    restart). WARNING: NOT inferred from stored rules: `@Save=` (0x24) also stores
    rules, and the selftest's "no match before the owner confirms" caught that
    shortcut before it shipped."""
    room = "0x%016X" % room_id_for(chan)
    _adopt()
    with _LOCK:
        have = _CONFIRMED.setdefault(room, [])
        if int(index) in [int(i) for i in have]:
            return False
        have.append(int(index))
    _publish()
    return True


def table_confirmed(chan, index):
    """Did an owner Confirm this table, as recorded by ANY process."""
    room = "0x%016X" % room_id_for(chan)
    try:
        return int(index) in [int(i) for i in
                              ((_live_confirmed() or {}).get(room) or [])]
    except (TypeError, ValueError):
        return False


def clear_table_confirmed(chan, index):
    """The table emptied: the NEXT owner must confirm for themselves, or their
    guests get announced into an open dialog -- the @GameNG= strand."""
    room = "0x%016X" % room_id_for(chan)
    _adopt()
    with _LOCK:
        have = _CONFIRMED.get(room) or []
        keep = [i for i in have if int(i) != int(index)]
        if len(keep) == len(have):
            return False
        if keep:
            _CONFIRMED[room] = keep
        else:
            _CONFIRMED.pop(room, None)
    _publish()
    return True


def _live_confirmed():
    if _OWNER[0]:
        with _LOCK:
            return {k: list(v) for k, v in _CONFIRMED.items()}
    return (_read_file() or {}).get("confirmed", {})


def seats(chan):
    """`{table index: [(member_id, ident), ..]}` for `chan`, from wherever it lives."""
    room = "0x%016X" % room_id_for(chan)
    out = {}
    for idx, rows in ((_live_seats() or {}).get(room) or {}).items():
        try:
            out[int(idx)] = [(int(m), int(i or 0)) for m, i in rows]
        except (TypeError, ValueError):
            continue
    return out


def _live_seats():
    if _OWNER[0]:
        with _LOCK:
            return json.loads(json.dumps(_SEATS))
    return (_read_file() or {}).get("seats", {})


def tables(chan):
    """`[(name, [7 values]), ...]` this room has LIVE state for, name order.

    Empty is the normal case and means "nothing has happened to a table here" --
    `build_ptl` then serves the authored fixture's table half untouched, which is
    the behaviour every room had before this existed.
    """
    room = "0x%016X" % room_id_for(chan)
    return sorted(((_live_tables() or {}).get(room) or {}).items())


def sequence(chan):
    """The room's current sequence -- what `+0x40` is stamped with, and what an
    up-to-date client echoes back on `<DR>`."""
    return int((_live_seq() or {}).get("0x%016X" % room_id_for(chan), 0))


def members(chan):
    """`[(member_id, [11 values]), ...]` for one room, in a stable order.

    A record whose value 5 is 0 is a member who told us they are LEAVING, and it
    is filtered here rather than deleted, so a late duplicate cannot resurrect
    them.
    """
    want = "0x%016X" % room_id_for(chan)
    out = []
    for k, vals in (_live_records() or {}).items():
        if _room_key(vals) != want:
            continue
        if _as_int(vals[5]) == 0:
            continue
        out.append((int(k), vals))
    return sorted(out, key=lambda kv: kv[0])


def room_of(member_id):
    """The room a member's own record puts them in, or None."""
    vals = (_live_records() or {}).get(str(int(member_id)))
    if not vals or _as_int(vals[5]) == 0:
        return None
    rid = _as_int(vals[9])
    return "#TM0R%03d" % (rid & 0xFFFFFFFF) if rid else None


def why_no_room(member_id):
    """Why `room_of` said None, in words. For log lines, never for logic.

    WARNING: EXISTS BECAUSE `_seat_at_table` BAILED SILENTLY. A reservation whose
    member has no room is dropped with no answer and no line, and from the
    outside that is indistinguishable from "the code never ran" -- which is the
    rule this project keeps re-learning. The live
    report was "player 2 can join a reserved table and it prompts them to enter
    reservation settings and effectively creates a new reservation": that is a
    client whose seat this server never recorded.
    """
    try:
        key = str(int(member_id))
    except (TypeError, ValueError):
        return "member id %r is not an int" % (member_id,)
    vals = (_live_records() or {}).get(key)
    if not vals:
        return ("no member record at all -- nothing has been learned about "
                "member %s this session (a record arrives on <DE>/<PD>, or is "
                "synthesised from the room registry by synth_member)" % key)
    if _as_int(vals[5]) == 0:
        return ("member %s has a record but its IN-ROOM flag (value 5) is 0 -- "
                "the registry does not think they are in a room" % key)
    if not _as_int(vals[9]):
        return "member %s is in-room but carries room id 0 (value 9)" % key
    return "member %s resolves fine -- room_of did not fail here" % key


def synth_member(chan, member_id, name, room_id=0):
    """A record for somebody who IS in the room but has not sent one.

    WARNING: THIS IS NOT DECORATION, IT IS THE FIX FOR THE FIRST VERSION OF THIS MODULE.
    Driving the roster only off `<DE>`/`<PD>` meant a player the server KNOWS is
    in the channel stayed invisible until their client happened to send a record
    -- measured 2026-08-18 with `rooms-live.json` holding two people and
    `tm-roster.json` holding one. Presence is a fact about the room registry; the
    record is what decorates it.

    Only the fields the client cannot do without are filled: a non-zero id (a zero
    one is an EMPTY SLOT at 0x1A14E9 and the row is dropped), the room id, the
    in-room flag, and the name block -- whose FIRST letter is the name's length
    (`0x66A39` reads `+0x47`, subtracts 'A', and displays that many bytes).
    """
    name = (name or "")[:15]
    stats = chr(ord("A") + len(name)) + "AA0AAABABAABIAB"
    # WARNING: VALUE 4 IS THE NAME AGAIN, AND IT IS A SECOND SCREEN'S COPY OF IT. This
    # was blank until 2026-08-18, which diverged from every real client: the
    # reference record in `selftest` -- bytes a live client sent -- carries
    # the same name in BOTH value 4 (+0x28) and value 10 (+0x38).
    #
    # It matters because the two are read by DIFFERENT screens. Tetra Master's
    # PC member pane reads `+0x38` (0x66984 gates on `[ebp-0x12]`), but
    # Janhourou's room screen draws `+0x28`: `roomwin__ZoneListPage_0033e2c0`
    # walks the same 0x58-stride array and calls
    # `pfgdraw__0036ad80(.., ZoneName, iVar4 + 0x28)`. A synthesised row was
    # therefore nameless on Jan's screen while looking correct on TM's, and
    # this module serves both.
    return ["0x%016X" % (guid_of(member_id) or int(member_id)),
            "0", "0", "0", name, "2",
            "0", "0", "0", "0x%016X" % int(room_id or room_id_for(chan)),
            name.ljust(15) + stats]


def room_id_for(chan):
    """`#TM0R001` -> `0x0000002000000001`. Measured on two values -- room 1 here
    and room 7 (`<DE>` carried `0x0000002000000007` for `#TM0R007`) -- and
    only ever a FALLBACK: a stored record's own value 9 is preferred, because that
    came from the client."""
    digits = "".join(c for c in str(chan) if c.isdigit())
    return (0x20 << 32) | (int(digits[-3:]) if digits else 1)


#: A PTL member row's 16-letter STATUS BLOCK is the LAST 16 chars of value[10]
#: (name field 15 wide + status 16 = 31). block[8] is the player's STATE and
#: block[9] a FLAG, both letters (A=0..P=15). SE's server sets a SEATED member's
#: state to 3 ('D') and flag to 1 ('B'); the owner's "Start Game" (Lobby.BIN 149)
#: ungreys only when 0x5C0D0 finds every reserved player at the table carrying
#: exactly that -- block[8]=='D' AND block[9]=='B'. Our
#: client self-reports 'A' (state 0), so without this the button never ungreys
#: and the match can only ever fire from our headcount substitute -- which is
#: what forecloses the third seat.
STATUS_BLOCK = 16
STATUS_STATE = 8                #: block[8] -- 'D' (3) when seated, else as sent
STATUS_FLAG = 9                 #: block[9] -- 'B' (1) when seated


def _member_seated(member_id):
    """Is this member seated at any table in any room? Reads the persisted seat
    list, so both containers agree and it survives a restart."""
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return False
    for tabs in (_live_seats() or {}).values():
        for rows in (tabs or {}).values():
            for m, _i in (rows or ()):
                try:
                    if int(m) == mid:
                        return True
                except (TypeError, ValueError):
                    continue
    return False


def remark_seated(member_id):
    """Re-store a member's record so its seated status re-marks and a PD goes
    out NOW -- called when a seat is placed or released, so the owner's
    Start-Game gate re-evaluates without waiting for the client's next re-send."""
    key = str(int(member_id))
    with _LOCK:
        vals = _RECORDS.get(key)
    if not vals:
        return False
    return note_member(member_id, list(vals))


def _mark_status(v10, seated):
    """value[10] with the status block's STATE set from seat membership.

    WARNING: THE SERVER OWNS block[8], BOTH WAYS. The client always self-reports 'A'
    (state 0), so forcing 'D' when seated and 'A' when not
    is deterministic and REVERTS cleanly -- an earlier "only force when seated"
    left our own 'D' stuck after the seat was released, because there was no
    original 'A' left to fall back to. block[9] (the flag) is set to 'B' when
    seated and left alone otherwise (it is already 'B' in every record).
    Malformed (too-short) strings pass through. `POL_TM_SEATED_STATUS=0` off.
    """
    if not isinstance(v10, str) or len(v10) < STATUS_BLOCK:
        return v10
    if os.environ.get("POL_TM_SEATED_STATUS", "1") != "1":
        return v10
    head, block = v10[:-STATUS_BLOCK], list(v10[-STATUS_BLOCK:])
    block[STATUS_STATE] = "D" if seated else "A"
    if seated:
        block[STATUS_FLAG] = "B"
    return head + "".join(block)


#: The client's own row identity in its OWN member list. Measured live
#: 2026-08-21 (tools/readself.py): the member-sidebar self-guard reads the
#: client's own id from polcore [0x383541c]/[0x3835420] (cft +0x2f8/+0x2f4) as
#: **(guid=0, slot=1)** = 0x0000000100000000. The `1` is NOT any server id --
#: for the client under test member_id=6, member_no=0, handle=13, client_guid
#: 0x860FB3E2A2, yet the self-id is 0x100000000 -- it is the slot the CLIENT
#: assigns its OWN row (it numbers itself 1 in its own view). This is a member-
#: SLOT id space, distinct from the POL guid space the group list uses
#: (the group master's self-identity = raw client_guid, a DIFFERENT record).
#: Serving `client_guid`/`guid_of` in value 0 is why the row the client draws
#: for itself stopped equalling its self-id, so "You cannot display the member
#: menu by selecting your player name" never fires and you can friend-request
#: yourself. WARNING: CONSTANT slot-1 is the SOLO-tested value; a two-client test is
#: still owed to prove self stays slot 1 when others are present (if not, this
#: becomes the member's position/join slot).
SELF_ROW_SLOT = 1


def self_row_value0(member_id=None):
    """The value-0 to serve for the RECIPIENT's own member row (guid=0, slot 1).

    Per-recipient: only the fetcher's own row gets this; everyone else keeps
    their real guid. `member_id` is accepted for call-site symmetry but the value
    is the client's self-slot, not a server id. `POL_TM_SELF_ROW_ID=0` reverts to
    the old `guid_of` value for an A/B.
    """
    return "0x%016X" % ((SELF_ROW_SLOT << 32) & 0xFFFFFFFFFFFFFFFF)


def rewrite_self_deltas(deltas, member_id):
    """Apply the member-number self-id to the RECIPIENT's own rows in a delta run.

    `build_ptl` fixes the snapshot, but member records re-send on their own timer
    (2 s .. 90 min), so a later `<PD>` carrying
    the recipient's own row would re-stamp it with `guid_of` and re-break the
    self-guard within seconds. This rewrites just those rows, matched by the
    value 0 we stamped for that member (`guid_of`), so it also catches the `<PC>`
    remove-by-id. Everyone else's deltas pass through untouched.
    """
    if not deltas or os.environ.get("POL_TM_SELF_ROW_ID", "0") != "1":
        return deltas
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return deltas
    mine = ("0x%016X" % ((guid_of(mid) or mid) & 0xFFFFFFFFFFFFFFFF)).lower()
    want = self_row_value0(mid)
    out, changed = [], False
    for entry in deltas:
        seq, tag, vals = entry[0], entry[1], list(entry[2])
        if vals and str(vals[0]).lower() == mine:
            vals[0] = want
            changed = True
        out.append([seq, tag, vals])
    return out if changed else deltas


def _value0_mode():
    return (os.environ.get("POL_TM_VALUE0", "polid") or "polid").lower()


def served_value0(member_id, current):
    """The value-0 to SERVE for a member's row -- THE ROSTER'S KEY ID.

    WARNING: THE START-GAME GATE AND THE MEMBERS PANE BOTH DIED ON THIS FIELD
    (live, 2026-08-21 ~13:3xZ: Start Game greyed, hover = Lobby.BIN 344
    "Conditions to start the game have not been met"; members pane empty with
    a byte-correct @GameML on the wire). The client's room member list
    [0x51DF120] is keyed by whatever we put here; `@GameML`'s /MLID is the
    POL-ID; the gate 0x5C0D0 looks each /MLID up in that list and NOT FOUND
    leaves conditions unmet. We served the LOBBY GUID -- the id-space mismatch
    the members-panel analysis predicted. `polid` mode serves the member's TM
    POL-ID; a member with no POL-ID yet keeps their current value (they have
    not @Init'd the game band and cannot be a reserved player).
    `POL_TM_VALUE0=guid` reverts wholesale.

    WARNING: KNOWN COUPLING: the polcore room-member sidebar reads the same field
    (self-guard/friend-add) -- flows that are
    already open bugs; watch them in the A/B rather than pre-blocking this.
    """
    if _value0_mode() != "polid":
        return current
    pid = pol_id_of(member_id)
    if not pid:
        return current
    try:
        return "0x%016X" % (int(str(pid), 16) & 0xFFFFFFFFFFFFFFFF)
    except (TypeError, ValueError):
        return current


def rewrite_value0_deltas(deltas):
    """Serve-time value-0 rewrite for the DELTA stream.

    MUST agree with `build_ptl`'s snapshot rewrite or a client holds two rows
    per member (PD matched by id). Maps by integer value so a `<PC>` whose
    member record is already popped still rewrites -- the map is built from
    the persisted guid/polid stores, not just the live records.
    """
    if not deltas or _value0_mode() != "polid":
        return deltas
    remap = {}
    for mid_str, pid in (_live_pol_ids() or {}).items():
        try:
            mid = int(mid_str)
            new = "0x%016X" % (int(str(pid), 16) & 0xFFFFFFFFFFFFFFFF)
        except (TypeError, ValueError):
            continue
        g = guid_of(mid)
        if g:
            remap[int(g) & 0xFFFFFFFFFFFFFFFF] = new
        remap[mid] = new                      # a record keyed by the bare id
    if not remap:
        return deltas
    out, changed = [], False
    for entry in deltas:
        seq, tag, vals = entry[0], entry[1], list(entry[2])
        if vals:
            new = remap.get(_as_int(vals[0]))
            if new and str(vals[0]) != new:
                vals[0] = new
                changed = True
        out.append([seq, tag, vals])
    return out if changed else deltas


def build_ptl(chan, base, present=None, self_member=None):
    """`base` (an authored blob, for its TABLE half) + the live roster -> the blob
    to serve.

    The table LIST stays authored -- which tables exist is server-defined content
    -- but a table's live STATE is not, and used to be frozen into the fixture
    alongside it. That was the whole of "a reservation is invisible to everyone
    else in the room" (reported 2026-08-20): `+0x0C` (seated) and `+0x14` (the
    state byte) never moved, so every other player kept reading "Open Table, 0
    seated" no matter what anybody did. `note_table` holds the live rows and they
    are overlaid here, matched BY NAME exactly as `lb__002f9f18` matches a `TD`.

    Returns `base` unchanged if the roster knows nothing about `chan` AND no
    table here has live state, so a room we have never seen degrades to exactly
    the old behaviour.
    """
    _rn = _room_no(chan)
    # WHO IS SEATED AT A TABLE HERE -- the set whose member records must carry
    # status 3 (block[8]='D') so the owner's "Start Game" can ungrey. tmroom
    # owns the seat list, so this needs no cross-module hand-off.
    seated = set()
    for _idx, _rows in (seats(chan) or {}).items():
        for _m, _i in _rows:
            seated.add(int(_m))
    stored = dict(members(chan))
    live_tables = dict(tables(chan))
    if present is None:
        rows = sorted(stored.items())
    else:
        # THE ROOM REGISTRY IS THE AUTHORITY ON WHO IS PRESENT. A stored record
        # decorates a row; it does not create or keep one. So somebody who has
        # not sent `<DE>` yet still appears, and somebody who has LEFT drops out
        # without needing a separate removal path to have fired.
        # WARNING: A UNION, NOT A FILTER. `present` ADDS people the IRC registry knows
        # and we hold no record for; it must NEVER remove one whose own record
        # says `stat = 2`. Letting it filter put the volatile view back in charge
        # by the back door -- measured 2026-08-18: the roster held two records,
        # both in-room, and the served blob carried ONE, because the registry in
        # the `login` container had lost the other to session churn. That is the
        # same fault `note_member`'s banner describes, one layer further out.
        rows = sorted(stored.items())
        room_id = 0
        for _, vals in rows:
            room_id = _as_int(vals[9]) or room_id
        # WARNING: `have` MUST GROW AS WE GO. It used to be computed once, so a member
        # appearing TWICE in `present` produced TWO rows -- measured 2026-08-20:
        # the room registry held `UA4XX8PKP` twice after a reconnect (members=3
        # for two people) and the member pane drew the same member twice. The
        # duplicate in the registry is its own bug; this is the one that turned
        # it into something on screen, and a snapshot builder should be immune to
        # a repeated input regardless.
        have = {int(mid) for mid, _ in rows}
        for mid, name in sorted(present):
            if not mid or int(mid) in have:
                continue                     # no member bound, or already known
            have.add(int(mid))
            rows.append((int(mid), synth_member(chan, mid, name, room_id)))
        rows.sort()
    blob = bytearray(base.ljust(TOTAL, b"\x00")[:TOTAL])
    # BEFORE the early-out on purpose: a never-used room still gets the id
    # repair (states are served exactly as authored -- see EMPTY_TABLE_STATE).
    _normalise_tables(blob, _rn)
    if not rows and not live_tables:
        return bytes(blob)
    n = min(len(rows), MEMBER_SLOTS)
    for i, (mid, vals) in enumerate(rows[:n]):
        vals = list(vals)
        vals[10] = _mark_status(vals[10], int(mid) in seated)
        # THE ROSTER KEY ID -- see served_value0: the Start-Game gate and the
        # members pane look members up by this field, as a POL-ID.
        vals[0] = served_value0(int(mid), vals[0])
        # THE RECIPIENT'S OWN ROW USES THE MEMBER-NUMBER SELF-ID, NOT A GUID.
        # Measured live 2026-08-21 (tools/readself.py): the member-sidebar
        # self-guard (polcore cft +0x2f4/+0x2f8) holds the client's own identity
        # as (guid=0, member number in the high dword) -- 0x100000000 for member
        # 1 -- NOT client_guid. Stamping client_guid here is why "You cannot
        # display the member menu by selecting your player name" regressed when
        # the live roster began overwriting the row the client had drawn itself.
        # Per-recipient: only the row that IS the fetcher changes; everyone else
        # still sees this member by their real guid, so nobody else is affected.
        if (self_member is not None and int(mid) == int(self_member)
                and os.environ.get("POL_TM_SELF_ROW_ID", "0") == "1"):
            vals[0] = self_row_value0(mid)
        rec = encode_member(vals)
        blob[MEMBER_OFF + i * MEMBER_REC:MEMBER_OFF + (i + 1) * MEMBER_REC] = rec
    # Clear the slots the previous roster used, or a member who left stays drawn.
    for i in range(n, MEMBER_SLOTS):
        off = MEMBER_OFF + i * MEMBER_REC
        if blob[off:off + MEMBER_REC] == bytes(MEMBER_REC):
            break
        blob[off:off + MEMBER_REC] = bytes(MEMBER_REC)
    struct.pack_into("<i", blob, MEMBER_COUNT_OFF, n)
    _apply_tables(blob, live_tables)
    # AFTER the overlay: a LIVE row carries whatever id it was stored with, and a
    # row stored before this landed still says 1. The snapshot and any `TD` we
    # send have to agree, so `note_table` normalises on the way in as well.
    _normalise_tables(blob, _rn)
    struct.pack_into("<I", blob, SERIAL_OFF, sequence(chan) & 0xFFFFFFFF)
    return bytes(blob)


#: What an EMPTY table's `+0x14` must say: **0**, and this is the THIRD and final
#: swing of a value that has flip-flopped twice. It is settled by a LIVE end-to-end
#: read (live, 2026-08-21), which outranks both earlier arguments.
#:
#: THE HISTORY, because the next person WILL find the old banners in git:
#:  * `6151fa3c` set it 7 -> 0, reasoning that `state > 6` has no dispatch arm.
#:  * `ac56bd72` retracted that on a MEASUREMENT: after the heal TD flipped a free
#:    table 7 -> 0, "Setting up" appeared on it. The conclusion drawn was that the
#:    state-0 arm renders through `cmp eax, ebp` -- client state -- while 7 renders
#:    the pre-stored display code 0 deterministically.
#:  * 2026-08-21 restores 0. The whole-function read (`0x671DD`-`0x67395`) says
#:    states 0 and 7 render BYTE-IDENTICALLY for a free row that is NOT yours, and
#:    diverge only on the MY-TABLE arm -- which was firing wrongly when `ac56bd72`
#:    was measured, because every room's table 1 carried the same id
#:    (`0x2100001001`). `_normalise_table_ids`'s room fold fixed that separately,
#:    and with it gone the symptom does not reproduce: a tester drove free
#:    tables at state 0 across rooms and they render as they did at 7.
#:
#: WHY IT HAD TO MOVE: state 0 is the ONLY state that reaches Table Menu variant 0
#: (`0x45ABC`), and variant 0 is the only arm that can ungrey **VS. COM**. Every
#: other state takes variants 1-4, which grey the row with help code 0x13 =
#: Lobby.BIN 428, "This command cannot be executed" -- the symptom reported for as
#: long as VS. COM has existed on this server.
#:
#: AND THE RESERVATION RISK WAS MEASURED, NOT ASSUMED. `tools/tmptl.py`'s
#: round-three sweep had recorded Lobby.BIN 337 ("You cannot make a reservation
#: with current table status") at states 0/1/2/3, which would have made this flip
#: catastrophic. It does NOT fire: a state-0 table reserves normally (live,
#: 2026-08-21). That sweep's 337 belonged to something else.
#:
#: WARNING: A RESERVED table that sits in "Setting up" forever is a DIFFERENT bug and is
#: still open -- state 1's owner tile arm `0x672E4` reads the reservation count
#: `[0x51def2c]` (= `screenobj+0x108`) and renders code 2 while it is 0. Tables
#: authored 7 hit the identical arm once reserved, so do not read that symptom as
#: this constant.
#:
#: WARNING: KEEP IN STEP WITH `tools/tmptl.py`'s `FREE_TABLE_STATE`. This constant is
#: the FALLBACK for a table with no fixture row; the authored value in
#: `services/tmdata/b_g_PTL.bin` is what actually ships, and `fixture_table` is
#: what the heal restores. They must not disagree.
EMPTY_TABLE_STATE = 0

#: The highest state with its own DISPATCH arm (the 7-entry jump table at rva
#: 0x674C4). Above it the pre-stored display code 0 stands -- the free-table
#: rendering, not an error path. Kept for the tile map; nothing normalises against it any more.
MAX_TABLE_STATE = 6


#: *** A TABLE ID OF 1 IS NOT AN ID, IT IS A NUMBER THE CLIENT ALREADY HAS. ***
#:
#: Read out of the client 2026-08-20, and it is the SHARED TAIL of the tile
#: switch -- every arm reaches it, and `state > 6` jumps straight into it:
#:
#:     0x736D  cmp [0x52461D0], edi        ; "the table I am at", lo half
#:     0x7373  jne 0x7395                  ; not mine -> the normal render
#:     0x7375  mov edx, [esp+0x30]         ; table id, hi half
#:     0x7379  mov eax, [0x52461D4]        ; "the table I am at", hi half
#:             ...match -> the MY-TABLE treatment, i.e. the yellow icon
#:
#: So the yellow "you own this table" is `+0x00 == [0x52461D0/4]` and nothing
#: else. The fixture authors `#TM0T001` = **1**, `002` = 2, `003` = 3, and table
#: 1 is the only tile that has ever shown the icon -- in a room never used
#: before, for a client that had reserved nothing. A 1 is exactly what a
#: zeroed, defaulted or 1-based "current table" slot holds, and every other
#: identity in this protocol is a full 64-bit value: see the three-ids banner
#: below, whose whole point is that THE SHAPES TELL THEM APART.
#:
#: WARNING: THE EXPERIMENT RAN AND THE COLLISION THEORY IS REFUTED (2026-08-20).
#: With `#TM0T001` served as `0x0000002100001001`, a tester walked three
#: rooms and got THREE DIFFERENT TILES -- "Setting up", "Empty", and the yellow
#: icon. And `#TM0R002/003/004/006/007` serve a **byte-identical** table half:
#: measured by running this module's own `build_ptl` against prod's data, five
#: rooms, no differing byte. Only `#TM0R001` differs at all (it holds a live
#: reservation on table 2).
#:
#: One byte-set cannot produce three renders. **So the tile is decided by CLIENT
#: state, not by anything we serve**, and `[0x52461D0/4]` -- "the table I am at"
#: -- is carried across rooms by the client and is not being set from our data.
#: The id was never the reason.
#:
#: WARNING: SO DO NOT READ THIS BLOCK AS THE EXPLANATION OF A FIXED BUG. It is kept
#: because a 64-bit id field holding the literal 1 is wrong on its own terms --
#: it is the one value in the whole record that a partially-initialised client
#: slot can equal, and the three-ids banner below is this file's standing lesson
#: about exactly that. But it fixed nothing that was reported, and the next
#: person to look at the yellow icon should start at `0x52461D0`, in a live
#: process, and not here. `POL_TM_TABLE_ID_REWRITE=0` restores the authored
#: 1/2/3 if that read shows the id matters after all.
#:
#: Safe to change: the only reader of `+0x00` anywhere in this tree is
#: `tetramaster._announce_match`, and only under `POL_TM_MATCH_TBLID=record`,
#: which is not the default ("peer", the table peer's own guid).
TABLE_ID_MAGIC = 0x0000002100000000
TABLE_ID_MIN = 0x10000          #: below this, an id is small enough to collide

#: *** THE CLIENT'S ID KEY -- WHY A TABLE ID IS NOT OURS TO CHOOSE FREELY. ***
#:
#: VERIFIED: MEASURED AND CLOSED 2026-08-22. Every 64-bit id in this protocol exists in
#: TWO spaces and the client converts between them with ONE constant:
#:
#:     peer_guid  =  (app_id ^ K)  &  0xFF_FFFF_FFFF      <- what the NICK carries
#:     app_id     =   peer_guid ^ K                       <- what the client
#:                                                           reconstructs on receive
#:
#: `K` is the client-local key `polcore` mangles guids with;
#: TM.dll applies it on both sides (`0x19FAC0` out, `0x19FB00` back, both
#: `polcore [+0x4e4] a^b^c` with `[+0x580]` = getK). The IRC NICK is 8 base-36
#: digits, so **only the LOW 40 BITS of `app_id ^ K` survive the wire** -- bits
#: 40..63 are lost and are restored from `K`. Therefore:
#:
#:     an id round-trips  <=>  (id ^ K) >> 40 == 0
#:
#: A MEMBER id always does: the CLIENT minted it (`@Init=/NN=`), so it is `K ^
#: something small` by construction. A TABLE id is ours, and the old
#: `0x0021<room>` high half is NOT in that coset -- so the client reconstructs
#: `0xAB12CD01_00001001` for a table we published as `0x00210001_00001001`.
#: `TM.dll 0x86ED0` compares the two 32-bit halves SEPARATELY: the low half
#: matches, the high half does not, `record+0x30` never reaches 2, and "Change
#: Table Settings" sits on "Retrieving table info..." until it times out. That
#: is the whole bug -- see `canonical_table_id`.
#:
#: VERIFIED: AND IT RETIRES THE "ALLOCATION ERA" WHACK-A-MOLE. `table_index_for_peer`
#: records a table peer class that "drifts F -> E -> D, one step per
#: re-allocation/restart". It never drifted: the class byte is just
#: `(app_hi ^ K_hi) & 0xFF`, so it changed when WE changed the table id's high
#: half. Pre-room-fold ids (`0x00000021....`) give `0x21 ^ 0xD0 = 0xF1` and
#: post-fold (`0x00210001....`) gives `0x01 ^ 0xD0 = 0xD1` -- exactly the two
#: "eras" that were measured live and blamed on the client.
#:
#: DERIVATION (no debugger, nothing guessed): the client tells us its own app id
#: in `@Init=/NN=`, and its login NICK folds to that id's wire form, so
#:     K = tm_self_id ^ base36(polid_for_nick(nick))
#: The default below was derived that way from TWO different clients in the same
#: capture (`AB12CD31DC3F63B6`/`UF8TOQDTX` and
#: `AB12CD56EB0F5932`/`UA4XX8PKP`) and they agree to the bit; it also
#: reproduces every peer nick ever measured -- the room peer `UKXDBA266`
#: (0xF0E4BCBB91), table 1 `UE5BRFVCJ` (0xD1E4BCAB91) and table 2 `UE5BRFYDE`
#: (0xD1E4BC9B92). `tetramaster`'s `/Shm=` banner recorded this same value as
#: "ONE OPEN VARIABLE" on 2026-08-16 and never connected it to the table id.
#:
#: WARNING: IT IS A DEFAULT, NOT AN ASSUMPTION. `learn_client_key` re-derives it from
#: any live (self id, POL ID) pair and says so; a disagreement is LOGGED, not
#: swallowed, because a `K` that is really per-install would show up here first.
CLIENT_KEY_DEFAULT = 0xAB12CDD0E4BCBB90
#: The nick carries 8 base-36 digits; 40 bits always fit (2**40 < 36**8).
CLIENT_ID_BITS = 40
_CLIENT_KEY_LEARNED = None
_CLIENT_KEY_SOURCE = "compiled default"


def client_key():
    """The client's id key `K`. Env override, else what we learned live, else
    the measured default -- in that order, so a server owner can always pin it."""
    raw = os.environ.get("POL_TM_CLIENT_KEY")
    if raw:
        try:
            return int(raw, 0) & 0xFFFFFFFFFFFFFFFF
        except ValueError:
            pass
    if _CLIENT_KEY_LEARNED is not None:
        return _CLIENT_KEY_LEARNED
    return CLIENT_KEY_DEFAULT


def wire_guid(app_id, key=None):
    """The 40-bit value a NICK can carry for `app_id` -- what the client's peer
    guid for it will be."""
    k = client_key() if key is None else key
    return (int(app_id) ^ k) & ((1 << CLIENT_ID_BITS) - 1)


def app_id_for_guid(guid, key=None):
    """The inverse: what the client reconstructs when a message arrives from a
    peer whose nick folds to `guid`."""
    k = client_key() if key is None else key
    return (int(guid) ^ k) & 0xFFFFFFFFFFFFFFFF


def round_trips(app_id, key=None):
    """Can this id survive the wire? True iff its top 24 bits agree with `K`."""
    k = client_key() if key is None else key
    return ((int(app_id) ^ k) >> CLIENT_ID_BITS) == 0


def learn_client_key(self_id, polid_fold):
    """Re-derive `K` from one live pair and record whether it AGREES.

    `self_id` is the client's own 64-bit app id (`@Init=/NN=`, `<PC>`), as an
    int or its 16-hex string. `polid_fold` is the base-36 fold of that member's
    POL ID -- i.e. `tetramaster.peer_guid(their login nick)`.

    Three outcomes, all logged: AGREES with what we are using, DISAGREES (loud
    -- the compiled default would then be wrong for this install), or the first
    value we have ever had. Returns the key in use afterwards.
    """
    global _CLIENT_KEY_LEARNED, _CLIENT_KEY_SOURCE
    try:
        sid = int(self_id, 16) if isinstance(self_id, str) else int(self_id)
        fold = int(polid_fold)
    except (TypeError, ValueError):
        return client_key()
    if not sid or not fold:
        return client_key()
    derived = (sid ^ fold) & 0xFFFFFFFFFFFFFFFF
    cur = client_key()
    if _CLIENT_KEY_LEARNED is None:
        _CLIENT_KEY_LEARNED = derived
        _CLIENT_KEY_SOURCE = "derived live from self id 0x%016X" % sid
        if derived == cur:
            _say("tm: client key K = 0x%016X -- derived live and AGREES with "
                 "the %s" % (derived, "compiled default"))
        else:
            _say("tm: WARNING: client key K DISAGREES with the compiled default: live "
                 "0x%016X vs 0x%016X (self id 0x%016X). Using the LIVE value; "
                 "table ids now follow it." % (derived, cur, sid))
    elif derived != _CLIENT_KEY_LEARNED:
        _say("tm: WARNING: client key K is NOT constant: 0x%016X (self id 0x%016X) vs "
             "0x%016X already learned (%s). Keeping the first; a per-session K "
             "means table ids cannot be authored ahead of the session."
             % (derived, sid, _CLIENT_KEY_LEARNED, _CLIENT_KEY_SOURCE))
    return client_key()


def _room_no(chan):
    """`#TM0R003` -> 3, or 0 for anything that is not a room channel."""
    try:
        cn = chan.decode("latin1") if isinstance(chan, bytes) else (chan or "")
        return int(cn[5:]) if cn.startswith("#TM0R") else 0
    except (ValueError, AttributeError):
        return 0


def canonical_table_id(index, room_no=0):
    """A 64-bit id for table `index` in room `room_no`, both halves non-trivial.

    The client compares the two 32-bit halves separately, so an id distinctive
    only in the high half would still match on the first compare. `0x21` marks
    it as ours in a hexdump; the low half carries the table number twice so a
    wrong index is visible; and the ROOM number rides the high half so a
    my-table slot set in one room cannot match another room's tile of the same
    index (live, 2026-08-21: the yellow/Setting-up tile followed a player
    across rooms because every room's table 1 carried the same id).

    room_no 0 keeps the pre-fold value, which the standalone selftests assert.

    WARNING: THE HIGH HALF IS NOT OURS TO CHOOSE -- FIXED 2026-08-22, and this is the
    "Change Table Settings hangs on Retrieving table info..." bug. See the
    `CLIENT_KEY_DEFAULT` banner for the full derivation. In one line: the nick
    that carries an id on the wire holds only the LOW 40 BITS of `id ^ K`, so a
    published id whose top 24 bits are not `K`'s comes back to the client as a
    DIFFERENT id. `0x00210000|room` is not in that coset, so `TM.dll 0x86ED0`
    compared `0xAB12CD01_00001001` (reconstructed) against `0x00210001_00001001`
    (published), missed on the high half, and never set `record+0x30 = 2`.
    The high half is therefore `K_hi ^ room_no`, which makes `id ^ K` exactly
    `(room_no << 32) | (low ^ K_lo)` -- under 2**40 for any room < 256, so it
    round-trips to the bit.
    THE LOW HALF IS DELIBERATELY UNCHANGED. It still carries the table number
    twice, so every peer guid's low 32 bits are byte-identical to what they were
    before this change and `table_index_for_peer`'s indexing is untouched. Only
    the class byte moves (`room_no` instead of `0xD<room>`), which
    `_is_table_class`'s low-nibble rule already accepts.
    `POL_TM_TABLE_ID_KEYFOLD=0` restores the old high half for an A/B.
    """
    n = int(index) + 1
    if not room_no:
        return TABLE_ID_MAGIC | (n << 12) | n
    low = ((n << 12) | n) & 0xFFFFFFFF
    if os.environ.get("POL_TM_TABLE_ID_KEYFOLD", "1") != "1":
        return ((0x00210000 | (int(room_no) & 0xFFFF)) << 32) | low
    hi = ((client_key() >> 32) ^ (int(room_no) & 0xFF)) & 0xFFFFFFFF
    return (hi << 32) | low


def _normalise_table_ids(blob, room_no=0):
    """Replace any table id small enough to collide with the client's own slot,
    folding the room number in (see `canonical_table_id`).

    WARNING: THE FOLD NEVER APPLIED UNTIL 2026-08-21: the fixture authors PRE-FOLD
    canonical ids (>= TABLE_ID_MIN), so the `continue` below skipped every
    playable row and all rooms served table 1 as the SAME id -- measured live
    (probe + roster audit): the client's
    my-reservation global held 0x0000002100001001 and drew the yellow tile in
    a freshly entered room, because that id IS every room's table 1. A
    pre-fold id is now detected BY ITS OWN SHAPE (low 12 bits carry the table
    number; `canonical_table_id(n-1, 0)` must reproduce it exactly) and folded
    for this room -- by table NUMBER, not array index, because the fixture
    puts service rows first. Service rows ('E'/'F', real ids) never match the
    shape and are untouched."""
    if os.environ.get("POL_TM_TABLE_ID_REWRITE", "1") != "1":
        return
    n = max(0, min(struct.unpack_from("<i", blob, TABLE_COUNT_OFF)[0], TABLE_SLOTS))
    for i in range(n):
        off = TABLE_OFF + i * TABLE_REC
        cur = struct.unpack_from("<Q", blob, off)[0]
        if cur < TABLE_ID_MIN:
            struct.pack_into("<Q", blob, off, canonical_table_id(i, room_no))
            continue
        tno = cur & 0xFFF
        if room_no and tno and cur == canonical_table_id(tno - 1, 0):
            struct.pack_into("<Q", blob, off,
                             canonical_table_id(tno - 1, room_no))
            continue
        # WARNING: AND HEAL AN ID THAT CANNOT SURVIVE THE WIRE (2026-08-22). A row
        # stored by an older build carries the `0x0021<room>` high half, which
        # the client reconstructs as something else entirely -- see
        # `canonical_table_id`. Detect it BY THE PROPERTY THAT MATTERS (does it
        # round-trip) rather than by any one legacy shape, so this also heals
        # whatever the next wrong high half would have been. The low half still
        # has to look like one of ours, or a service row would be rewritten.
        if room_no and tno and not round_trips(cur)                 and (cur & 0xFFFFFFFF) == (((tno << 12) | tno) & 0xFFFFFFFF):
            struct.pack_into("<Q", blob, off,
                             canonical_table_id(tno - 1, room_no))


#: The room byte folded into table ids served when the fetcher's room is
#: UNKNOWN. Real rooms are 1..7; 0xFF cannot collide, still round-trips
#: (`canonical_table_id`'s fold covers any room < 256), and reads as "no room"
#: in a hexdump.
NEUTRAL_ROOM = 0xFF


def fold_unknown_tables(blob):
    """Round-tripping table ids for a blob served to a client whose ROOM WE DO
    NOT KNOW -- the deterministic first-VS.-COM-attempt failure (2026-08-22).

    The fixture authors PRE-FOLD ids (`0x0021...`), and every heal arm in
    `_normalise_table_ids` is gated on a nonzero room -- so the unknown-room
    serve path (`_ptl_unknown_base`) handed the client ids that CANNOT SURVIVE
    THE WIRE: a nick carries only the low 40 bits of `id ^ K`, and
    `0x2100001001` is not in `K`'s coset. Measured live 14:49:17Z: the
    client's first COM attempt after launch addressed `UK6T6V3TZ` -- which
    folds back to app id `AB12CD21_00001001`, missed its own stored
    `00000021_00001001` on the high half, and died into "could not play
    against the computer" + `@GameExit=`; the retry (room known by then, ids
    room-folded) worked. Same truncation class as the Change Table Settings
    hang. Service rows ('E'/'F') fail every shape test and are untouched,
    exactly as in the per-room heal.
    """
    _normalise_table_ids(blob, NEUTRAL_ROOM)


#: The row-kind letter at a table record's `+0x28[0]`, which is what the client
#: SELECTS SERVICE ROWS BY. Read straight off the shipped fixture rather than
#: guessed: `#TM0COM` carries 'E', `#TM0CARD` carries 'F', and every play table
#: `#TM0T0nn` carries 'A'. `_normalise_table_ids` already relies on this ("Service
#: rows ('E'/'F', real ids) never match the shape and are untouched").
#:
#: KEY: **AND 'D' IS THE TRADE ROW, WHICH WE HAVE NEVER SERVED.** Measured live
#: 2026-08-24 with the pol-shim board-tick probe: the trade board (TM.dll tick
#: 0x15590) opens, and its state 0 walks this exact array -- `0x521BB70`, stride
#: 0x68, count `[0x524290C]` -- for a record whose `+0x28` byte is `'D'` (0x44,
#: `cmp ecx,0x44` at 0x15706), takes that row's id64 from `+0x00` into
#: `[esi+0x1A8/0x1AC]`, and on finding NONE jumps to state 0x12C:
#:
#:     BOARD TICK #1   state=0x0    row=0000000000000000
#:     BOARD TICK #2   state=0x12C  row=0000000000000000   <- no row
#:     ... 182 ticks parked ...
#:     BOARD TICK #184 state=0xC8                          <- the error dialog
#:
#: That is the whole failure, and it explains every symptom this chase produced:
#: the board dies in its first two frames, so it never reaches state 1 and the
#: client has therefore NEVER emitted `@TrID=`/`@TrEnt=` on the wire -- which is
#: why nothing we served (card lists, the go flag, the game band) could matter,
#: and why no push timed off `@TrAns` could ever be early enough.
#:
#: It also explains why SEATING both players changed nothing: `_mark_status`
#: writes 'D' into the MEMBER record's `value[10]` status block at index 8, a
#: different table entirely. And it matches the observation that the game
#: offers a trade without a seat -- the 'D' row is a SERVICE ROW like COM and the
#: card shop, not a reservation.
TRADE_ROW_KIND = "D"
TRADE_ROW_NAME = "#TM0TRADE"


def _ensure_trade_table(blob, room_no=0):
    """Append the 'D' (trade) service row if this room's list lacks one.

    Modelled on the rows SE shipped rather than invented: the block is the kind
    letter followed by filler exactly as `#TM0COM` authors `EAAAAAAAAAAAAAAA`,
    and the id is COPIED FROM AN EXISTING SERVICE ROW -- `#TM0COM` and
    `#TM0CARD` already share one id (`000000384EA5822C`), so a service row's id
    is evidently the world/shop id and not a per-row value. Copying it also
    means the id round-trips through the nick's 40-bit `id ^ K` window by
    construction, which a hand-picked constant would not
    (`canonical_table_id`'s banner -- an id whose top 24 bits are not K's comes
    back to the client as a DIFFERENT id, and that is a bug we have already paid
    for once).

    Idempotent: a list that already has a 'D' row is left exactly alone, so this
    is a no-op the moment the fixture itself grows one.
    POL_TM_TRADE_TABLE=0 disables.
    """
    if os.environ.get("POL_TM_TRADE_TABLE", "1") != "1":
        return
    try:
        n = struct.unpack_from("<i", blob, TABLE_COUNT_OFF)[0]
    except struct.error:
        return
    n = max(0, min(n, TABLE_SLOTS))
    if n >= TABLE_SLOTS:
        return                                  # no room to add one
    svc_id = 0
    for i in range(n):
        off = TABLE_OFF + i * TABLE_REC
        kind = blob[off + 0x28:off + 0x29].decode("latin1", "replace")
        if kind == TRADE_ROW_KIND:
            return                              # already present, nothing to do
        # 'E'/'F' are the shipped service rows; borrow their id.
        if kind in ("E", "F") and not svc_id:
            svc_id = struct.unpack_from("<Q", blob, off)[0]
    if not svc_id:
        return          # no service row to model on -- author nothing over a guess
    off = TABLE_OFF + n * TABLE_REC
    rec = bytearray(TABLE_REC)
    struct.pack_into("<Q", rec, 0x00, svc_id)                    # the id state 0 takes
    rec[0x14] = 0                                                # state, as COM/CARD
    name = TRADE_ROW_NAME.encode("latin1", "replace")[:13]
    rec[0x18:0x18 + 13] = name.ljust(13, bytes([0]))
    block = (TRADE_ROW_KIND + "A" * (BLOCK_LETTER_MAX - 1)).encode("latin1")
    rec[0x28:0x28 + BLOCK_LETTER_MAX] = block
    blob[off:off + TABLE_REC] = rec
    struct.pack_into("<i", blob, TABLE_COUNT_OFF, n + 1)


def _normalise_tables(blob, room_no=0):
    """The id repair, at both exits of `build_ptl`.

    WARNING: THE STATE REPAIR THAT RAN BESIDE THIS IS RETRACTED -- see
    `EMPTY_TABLE_STATE`'s banner: authored 7 is the client's DETERMINISTIC empty
    tile (display code 0 is pre-stored and `state > 6` skips the only
    `mov ecx, 2`), and "repairing" it to 0 handed free tables the state-0 arm's
    `cmp eax, ebp` -- a render that varies with client state, measured as
    "Setting up" appearing on a free table moments after the heal TD (22:18).
    States are served exactly as authored or as live rows carry them.
    """
    _normalise_table_ids(blob, room_no)
    # AFTER the id repair, so the appended row is never rewritten by it.
    _ensure_trade_table(blob, room_no)


def _normalise_table_states(blob):
    """RETRACTED -- a deliberate no-op, kept so its callers' history greps.

    This forced state 7 -> 0 for empty tables and shipped in 6151fa3c. Measured
    consequence (22:18): free tables began drawing "Setting up"
    NONDETERMINISTICALLY, because the state-0 arm renders through `cmp eax, ebp`
    while state 7 renders the pre-stored display code 0 -- the deterministic
    empty tile SE authored. See `EMPTY_TABLE_STATE`. The reconcile now restores
    the AUTHORED state instead.
    """


def _apply_tables(blob, live):
    """Overlay `live` (name -> 7 values) on the fixture's table half, in place.

    WARNING: MATCH BY NAME, APPEND ONLY WHAT IS GENUINELY NEW. The snapshot has to
    agree with what a `TD` delta would do to a client that is already up to date,
    or the two paths disagree and a reload silently changes the screen -- which
    is indistinguishable from a protocol bug, and is how this subsystem has burnt
    days before. `lb__002f9f18` matches on `+0x18` and only then allocates a new
    slot, so this does the same.
    """
    if not live:
        return
    authored = max(0, struct.unpack_from("<i", blob, TABLE_COUNT_OFF)[0])
    authored = min(authored, TABLE_SLOTS)
    seen = {}
    for i in range(authored):
        off = TABLE_OFF + i * TABLE_REC
        nm = blob[off + TABLE_NAME_OFF:off + TABLE_NAME_OFF + 13]
        seen[nm.split(b"\x00")[0].decode("latin1", "replace")] = i
    n = authored
    for name, vals in sorted(live.items()):
        i = seen.get(name)
        if i is None:
            if n >= TABLE_MAX:
                continue                    # past what the client's array reads
            i, n = n, n + 1
        off = TABLE_OFF + i * TABLE_REC
        blob[off:off + TABLE_REC] = encode_table(vals)
    struct.pack_into("<i", blob, TABLE_COUNT_OFF, n)


def selftest():
    """Assert the field map against a blob `tools/tmptl.py` built, not against
    our own output -- the two encoders were derived from different binaries (the
    PC serialiser 0x1A3340 and the PS2 parser `lb__002f9ab8`) and a disagreement
    between them is exactly the bug worth catching.

    WARNING: IT WRITES NOTHING REAL, AND THAT IS A FIX, NOT A PRECAUTION. This
    function calls `note_member`/`note_table`, both of which end in `_publish()`
    -- so running it inside a LIVE container overwrote prod's `tm-roster.json`
    with fixture rows: a phantom `#TM0T099`, `#TM0T001` marked Playing, the real
    member records gone and the room sequence reset to 2. Measured on prod
    2026-08-20T00:49Z, by me, doing exactly that to "check the deploy". The
    hazard was always there in the member half; the table half only made it
    bigger. `_FILE` is redirected to a scratch path for the duration and the
    module state is restored on the way out, so a selftest can no longer reach
    the snapshot two containers share.
    """
    ok = True
    _saved = (_FILE, _OWNER[0], dict(_RECORDS), dict(_ROOMS_SEQ), dict(_GUIDS),
              dict(_NAMES), json.loads(json.dumps(_DELTAS)),
              json.loads(json.dumps(_TABLES)), dict(_PEERS),
              json.loads(json.dumps(_RULES)),
              # WARNING: _SEATS/_CONFIRMED/_POLIDS were MISSING from this sandbox
              # until 2026-08-21 -- a selftest that seated anybody would have
              # leaked it into the live process.
              json.loads(json.dumps(_SEATS)),
              json.loads(json.dumps(_CONFIRMED)), dict(_POLIDS))
    try:
        return _selftest_body()
    finally:
        globals()["_FILE"] = _saved[0]
        _OWNER[0] = _saved[1]
        for d, v in ((_RECORDS, _saved[2]), (_ROOMS_SEQ, _saved[3]),
                     (_GUIDS, _saved[4]), (_NAMES, _saved[5]),
                     (_DELTAS, _saved[6]), (_TABLES, _saved[7]),
                     (_PEERS, _saved[8]), (_RULES, _saved[9]),
                     (_SEATS, _saved[10]), (_CONFIRMED, _saved[11]),
                     (_POLIDS, _saved[12])):
            d.clear(); d.update(v)
        _CACHE["mtime"] = -1.0


def _selftest_body():
    """The assertions themselves. Called only through `selftest`, which is what
    guarantees the scratch `_FILE` and the state restore above."""
    import tempfile
    globals()["_FILE"] = os.path.join(tempfile.mkdtemp(prefix="tmroom-selftest-"),
                                      "tm-roster.json")
    ok = True

    # member[0] of a real served b/g/PTL, tmptl-authored: a test member in room 1.
    REF = bytes.fromhex(
        "a2e2b30f860000000100000020000000f4ce836a00000000000000000100000000000000"
        "000000024c6170746f70546573743200000000004c6170746f70546573743220202020"
        "4c41413041414142414241414249414200")
    if len(REF) != MEMBER_REC:
        print("FAIL: reference record is %d B, not %d" % (len(REF), MEMBER_REC))
        return 1

    vals = decode_member(REF)
    print("decoded:", vals)
    # The identity fields have to come back as the wire spells them.
    if vals[0] != "0x000000860FB3E2A2":
        print("FAIL: value 0 (the id) decoded %r" % vals[0]); ok = False
    if vals[9] != "0x0000002000000001":
        print("FAIL: value 9 (the room id) decoded %r" % vals[9]); ok = False
    if vals[10] != "LaptopTest2    LAA0AAABABAABIAB":
        print("FAIL: value 10 (name+stats) decoded %r" % vals[10]); ok = False
    if vals[5] != "2":
        print("FAIL: value 5 (in-room flag) decoded %r" % vals[5]); ok = False

    # DECODE -> ENCODE must be the identity on tmptl's own bytes. This is the
    # assertion that actually pins the map: a field read at the wrong offset
    # survives a round trip through our own code and dies here.
    again = encode_member(vals)
    if again != REF:
        print("FAIL: re-encode differs from tmptl's record")
        for i in range(0, MEMBER_REC, 8):
            if again[i:i+8] != REF[i:i+8]:
                print("  +0x%02X  tmptl %s  ours %s"
                      % (i, REF[i:i+8].hex(), again[i:i+8].hex()))
        ok = False

    # The live <DE> off the wire (authserv.log 2026-08-18T05:14:27Z) must encode
    # to a record with the id WE assign, not the zero the client sent.
    LIVE = ["0x0000000000000000", "0", "0", "0", "", "2", "1787030064",
            "2359297", "0", "0x0000002000000001",
            "LaptopTest2    LAA0AAABABAABIAB"]
    _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
    _OWNER[0] = True
    if not note_member(0x860FB3E2A2, LIVE):
        print("FAIL: the first note_member must report a change"); ok = False
    if note_member(0x860FB3E2A2, LIVE):
        print("FAIL: an identical re-send must NOT bump the sequence"); ok = False
    seq_one = sequence("#TM0R001")
    note_member(0xAB12CEB20D9067C4,
                ["0", "0", "0", "0", "", "2", "1787030064", "0", "0",
                 "0x0000002000000001", "Lex            DAA0AAABABAADIAB"])
    if sequence("#TM0R001") != seq_one + 1:
        print("FAIL: a second member must advance the sequence"); ok = False

    # WARNING: THE ROUND TRIP ABOVE CANNOT CATCH A WRONG *WIDTH*, and did not: value 6
    # was first coded as an 8-byte string, which survives decode->encode intact
    # and still writes ASCII into the struct when a real wire value arrives. So
    # assert the LIVE encode's numeric fields directly.
    live_rec = encode_member(LIVE[:0] + ["0x%016X" % 0x860FB3E2A2] + LIVE[1:])
    if struct.unpack_from("<Q", live_rec, 0x10)[0] != 1787030064:
        print("FAIL: +0x10 must be the unix time as a u64, got %r"
              % (live_rec[0x10:0x18].hex(),)); ok = False
    if struct.unpack_from("<Q", live_rec, 0x08)[0] != 0x0000002000000001:
        print("FAIL: +0x08 must be the room id"); ok = False
    if struct.unpack_from("<I", live_rec, 0x1C)[0] != 2359297:
        print("FAIL: +0x1C must be value 7"); ok = False
    if live_rec[0x27] != 2:
        print("FAIL: +0x27 must be the in-room flag"); ok = False

    blob = build_ptl("#TM0R001", bytes(TOTAL))
    if struct.unpack_from("<i", blob, MEMBER_COUNT_OFF)[0] != 2:
        print("FAIL: member count is not 2"); ok = False
    if struct.unpack_from("<I", blob, SERIAL_OFF)[0] != sequence("#TM0R001"):
        print("FAIL: +0x40 must carry the room sequence"); ok = False
    # WARNING: A ZERO ID IS AN EMPTY SLOT (0x1A14E9) -- the client sends 0 for itself and
    # a roster that stored it verbatim would serve an invisible row.
    first = blob[MEMBER_OFF:MEMBER_OFF + MEMBER_REC]
    if struct.unpack_from("<Q", first, 0)[0] == 0:
        print("FAIL: served record 0 has a zero id"); ok = False
    if decode_member(first)[9] != "0x0000002000000001":
        print("FAIL: the served record lost its room id"); ok = False

    # A member who leaves must vacate their slot, not linger.
    forget_member(0x860FB3E2A2)
    blob2 = build_ptl("#TM0R001", blob)
    if struct.unpack_from("<i", blob2, MEMBER_COUNT_OFF)[0] != 1:
        print("FAIL: member count after a part is not 1"); ok = False
    if decode_member(blob2[MEMBER_OFF + MEMBER_REC:
                           MEMBER_OFF + 2 * MEMBER_REC])[0] != "0x%016X" % 0:
        print("FAIL: the vacated slot was not cleared"); ok = False

    # An unknown room must degrade to the authored blob, byte for byte APART
    # FROM the table-state repair -- see `_normalise_table_states`. Asserted as
    # "nothing but +0x14 moved" rather than relaxed to "close enough", because
    # the whole value of this check is that an untouched room is not a
    # regression for everyone who never goes near a table.
    def _repairable():
        """Byte offsets `_normalise_tables` is allowed to move: the 8-byte id
        per table, nothing else. The STATE left this set when the state repair
        was retracted (see EMPTY_TABLE_STATE) -- authored states now ship
        verbatim, and a state byte moving in an untouched room is a bug again."""
        out = set()
        for t in range(TABLE_SLOTS):
            o = TABLE_OFF + t * TABLE_REC
            out.update(range(o, o + 8))
        return out

    degraded = build_ptl("#TM0R999", REF)
    diff = [i for i in range(len(REF)) if REF[i] != degraded[i]]
    off_state = _repairable()
    if not set(diff) <= off_state:
        print("FAIL: an unknown room changed something other than a table state:"
              " %r" % (sorted(set(diff) - off_state)[:8],)); ok = False
    for t in range(4):
        o = TABLE_OFF + t * TABLE_REC
        if struct.unpack_from("<Q", degraded, o)[0] < TABLE_ID_MIN:
            print("FAIL: table %d still carries an id small enough to collide "
                  "with the client's own current-table slot" % t)
            ok = False

    # --- THE DELTA STREAM --------------------------------------------
    # Three members joining, one leaving, from an empty room -- then every
    # `<DR>(N)` a client could plausibly send.
    _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
    for i, who in enumerate(("Lex", "LaptopTest2", "Third")):
        note_member(0x100 + i, ["0", "0", "0", "0", "", "2", "1787030064", "0",
                                "0", "0x0000002000000001",
                                who.ljust(15) + chr(ord("A") + len(who))
                                + "AA0AAABABAABIAB"])
    cur = sequence("#TM0R001")
    if cur != 3:
        print("FAIL: three joins must be three sequences, got %r" % cur); ok = False
    if deltas_after("#TM0R001", cur) != []:
        print("FAIL: a CURRENT client must be told nothing"); ok = False
    if deltas_after("#TM0R001", cur + 1) is not None:
        print("FAIL: a client AHEAD of us must be sent to the snapshot"); ok = False
    if deltas_after("#TM0R001", cur - RELOAD_BEHIND - 1) is not None:
        print("FAIL: past the reload gate must be sent to the snapshot"); ok = False
    d = deltas_after("#TM0R001", 1)
    if not d or [x[0] for x in d] != [2, 3] or [x[1] for x in d] != ["PD", "PD"]:
        print("FAIL: <DR>(1) must yield PD 2 and PD 3, got %r" % (d,)); ok = False

    # A DEPARTURE IS `PC`, NOT A `PD` WITH stat 0 -- the client applies what it
    # is sent, and a PD would leave the row drawn.
    note_member(0x101, ["0", "0", "0", "0", "", "0", "1787030064", "0", "0",
                        "0x0000002000000001", "LaptopTest2    LAA0AAABABAABIAB"])
    d = deltas_after("#TM0R001", cur)
    if not d or d[0][1] != "PC":
        print("FAIL: a stat-0 record must become a PC delta, got %r" % (d,)); ok = False
    if d and len(d[0][2]) != 1:
        print("FAIL: a PC delta carries the id ALONE, got %r" % (d[0][2],)); ok = False

    # The `<DD>` message: <DD> <DN>count then (sequence, payload) GROUP pairs, so
    # the sequence is at group 2 and its payload at group 3 -- the indices
    # `cp__002fae98` walks.
    g = dd_groups(deltas_after("#TM0R001", 1))
    if [t for t, _ in g[:2]] != ["DD", "DN"]:
        print("FAIL: <DD> then <DN> must lead the message, got %r" % (g[:2],)); ok = False
    if g[1][1] != [str((len(g) - 2) // 2)]:
        print("FAIL: <DN> must count the PAIRS, got %r" % (g[1][1],)); ok = False
    if g[2][0] != "DC" or g[3][0] not in ("PD", "PC", "TD", "DE"):
        print("FAIL: group 2 must be the sequence and group 3 the payload, "
              "got %r" % (g[2:4],)); ok = False
    if [int(v[0]) for t, v in g[2::2]] != [int(x[0]) for x in
                                           deltas_after("#TM0R001", 1)]:
        print("FAIL: the sequences in the message are not the deltas'"); ok = False

    # THE RELOAD GATE HAS TO BE THE THING THAT REFUSES, not an accident of how
    # much log we happen to hold. Build a chain LONGER than the gate, so a client
    # past it could otherwise be served in full.
    _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
    for i in range(DELTA_WINDOW + 8):
        note_member(0x200, ["0", "0", "0", str(i), "", "2", "1787030064", "0",
                            "0", "0x0000002000000001",
                            "Lex            DAA0AAABABAABIAB"])
    cur = sequence("#TM0R001")
    if len(_DELTAS["0x0000002000000001"]) <= RELOAD_BEHIND:
        print("FAIL: the fixture is too short to test the reload gate"); ok = False
    # THE LOG IS BOUNDED. Past the reload gate the old entries can never be
    # applied, so keeping them would only grow the file this state is published in.
    if len(_DELTAS["0x0000002000000001"]) > DELTA_WINDOW:
        print("FAIL: the delta log grew past DELTA_WINDOW (%d)"
              % len(_DELTAS["0x0000002000000001"])); ok = False
    if deltas_after("#TM0R001", cur - RELOAD_BEHIND) is None:
        print("FAIL: exactly at the gate must still be served from the log"); ok = False
    if deltas_after("#TM0R001", cur - RELOAD_BEHIND - 1) is not None:
        print("FAIL: past the gate must be sent to the snapshot even when the "
              "log could serve it"); ok = False

    # A GAP IS A PERMANENT STALL, so a log we cannot serve contiguously must
    # refuse rather than send a chain the client will drop on the floor.
    room = "0x0000002000000001"
    full = list(_DELTAS[room])
    _DELTAS[room] = [d for d in full if int(d[0]) != cur - 2]
    if deltas_after("#TM0R001", cur - 4) is not None:
        print("FAIL: a hole in the log must fall back to the snapshot"); ok = False
    # ...and a log missing its TAIL is a hole the contiguity walk cannot see,
    # because what it holds IS contiguous. Only the count catches this one.
    _DELTAS[room] = [d for d in full if int(d[0]) != cur]
    if deltas_after("#TM0R001", cur - 3) is not None:
        print("FAIL: a log short of the current sequence must fall back"); ok = False
    # ...and a log of the RIGHT LENGTH can still be out of order, which the count
    # cannot see either. The client applies only at `seq + 1`, so this stalls it.
    dup = [list(d) for d in full]
    for d in dup:
        if int(d[0]) == cur - 2:
            d[0] = cur - 3
    _DELTAS[room] = dup
    if deltas_after("#TM0R001", cur - 4) is not None:
        print("FAIL: a log with a duplicated sequence must fall back"); ok = False
    _DELTAS[room] = full

    # A MOVE IS A REMOVAL FROM THE ROOM LEFT BEHIND. Nobody else would ever take
    # the row out of the old room's list.
    _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
    mover = ["0", "0", "0", "0", "", "2", "1787030064", "0", "0",
             "0x0000002000000001", "Lex            DAA0AAABABAABIAB"]
    note_member(0x300, mover)
    note_member(0x300, mover[:9] + ["0x0000002000000002", mover[10]])
    left = deltas_after("#TM0R001", 0)
    if not left or left[-1][1] != "PC":
        print("FAIL: moving room must leave a PC behind, got %r" % (left,)); ok = False
    joined = deltas_after("#TM0R002", 0)
    if not joined or joined[-1][1] != "PD":
        print("FAIL: moving room must PD into the new one, got %r" % (joined,))
        ok = False

    # --- THE NAME THE ROW DRAWS (value 4 / +0x28) -----------------------------
    # The client sends this field EMPTY and cannot do otherwise. If we keep its
    # blank, the row draws with no name on it -- measured on two live clients.
    _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
    _NAMES.clear()
    WIRE = ["0x0000000000000000", "0", "0", "0", "", "2", "1787090558",
            "7274505", "0", "0x0000002000000001",
            "Lex            DAA0AAAJABAADIAB"]
    note_member(0x600, WIRE)
    if _RECORDS["1536"][4] != "":
        print("FAIL: with no name known, value 4 must stay as the client sent it")
        ok = False
    note_name(0x600, "Lex")
    note_member(0x600, WIRE)
    if _RECORDS["1536"][4] != "Lex":
        print("FAIL: value 4 must carry the handle name, got %r"
              % _RECORDS["1536"][4]); ok = False
    rec = encode_member(_RECORDS["1536"])
    if rec[0x28:0x2B] != b"Lex":
        print("FAIL: +0x28 must hold the drawn name, got %r" % rec[0x28:0x38])
        ok = False
    if rec[0x38:0x3B] != b"Lex" or rec[0x47:0x48] != b"D":
        print("FAIL: +0x38/+0x47 (the pane's GATES) must be untouched"); ok = False
    # A name the client DID send wins -- it knows itself better than we do.
    note_member(0x601, WIRE[:4] + ["TheirOwn"] + WIRE[5:])
    note_name(0x601, "Ours")
    note_member(0x601, WIRE[:4] + ["TheirOwn"] + WIRE[5:])
    if _RECORDS["1537"][4] != "TheirOwn":
        print("FAIL: a name the client sent must not be overwritten, got %r"
              % _RECORDS["1537"][4]); ok = False
    _NAMES.clear()

    # --- THE THREE IDS ---------------------------------------------------------
    # A wrong-KIND id must be refused, not stored: it is the one mistake on this
    # track whose only symptom is a row that never draws.
    _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
    # A SYNTHESISED ROW MUST CARRY THE NAME WHERE A REAL CLIENT PUTS IT -- both
    # places, because two different screens read two different fields.
    syn = synth_member("#TM0R001", 0x500, "Lex")
    if syn[4] != "Lex":
        print("FAIL: synth value 4 (+0x28, Jan's room screen) is %r" % syn[4])
        ok = False
    if syn[10][:15].strip() != "Lex":
        print("FAIL: synth value 10 (+0x38, TM's member pane) is %r" % syn[10])
        ok = False

    if not is_lobby_band_id(0x000000860FB3E2A2):
        print("FAIL: the working lobby-band id must be accepted"); ok = False
    if is_lobby_band_id(0xAB12CD56EB0F5932):
        print("FAIL: Tetra Master's 64-bit id must NOT pass as a lobby-band id")
        ok = False
    if is_lobby_band_id(0):
        print("FAIL: zero is an EMPTY SLOT, not an id"); ok = False
    try:
        note_guid(0x400, 0xAB12CD56EB0F5932)
        print("FAIL: note_guid must REFUSE Tetra Master's own id"); ok = False
    except ValueError:
        pass
    if not guid_is_placeholder(0x400):
        print("FAIL: a member with no client_guid must report as a placeholder")
        ok = False
    if not note_guid(0x400, 0x000000860FB3E2A2):
        print("FAIL: a well-shaped lobby-band id must be accepted"); ok = False
    if guid_is_placeholder(0x400):
        print("FAIL: a member WITH a client_guid must not report as one"); ok = False
    if not warn_once(0x401) or warn_once(0x401):
        print("FAIL: warn_once must latch"); ok = False

    # --- ZONE OCCUPANCY -------------------------------------------------------
    # A zone's population is its rooms PLUS whoever is standing in it without
    # having joined one -- and each person counted once.
    zl = bytearray(ZL_TOTAL); struct.pack_into("<I", zl, ZL_COUNT_OFF, 1)
    zl[ZL_HDR + ZL_F_ZONEID] = 0
    rl = bytearray(RL_TOTAL); struct.pack_into("<I", rl, RL_COUNT_OFF, 2)
    for i, c in enumerate((b"#TM0R001", b"#TM0R002")):
        o = RL_HDR + i * RL_REC + RL_F_CHANNEL
        rl[o:o + len(c)] = c
    head = {"#TM0R001": 2, "#TM0R002": 0}
    in_rooms = {"#TM0R001": {0x101, 0x102}, "#TM0R002": set()}

    def zone_players(**kw):
        out = patch_zone_list(bytes(zl), head, {0: bytes(rl)}, **kw)
        return struct.unpack_from("<I", out, ZL_HDR + ZL_F_PLAYERS)[0]

    if zone_players() != 2:
        print("FAIL: with no zone data the count must be the room sum"); ok = False
    if zone_players(zone_members={0: {0x109}}, room_members=in_rooms) != 3:
        print("FAIL: somebody in the zone but in no room must be counted"); ok = False
    if zone_players(zone_members={0: {0x101}}, room_members=in_rooms) != 2:
        print("FAIL: somebody in a room must NOT be counted twice"); ok = False
    if struct.unpack_from("<I", patch_zone_list(bytes(zl), head, {0: bytes(rl)}),
                          ZL_HDR + ZL_F_ROOMS)[0] != 2:
        print("FAIL: the zone's room count is wrong"); ok = False

    # TWO ZONES SHARING ONE `+0x3C` -- Tetra Master's real blob, where both rows
    # say zone 0. One person must be counted ONCE, not once per row.
    zl2 = bytearray(ZL_TOTAL); struct.pack_into("<I", zl2, ZL_COUNT_OFF, 2)
    zl2[ZL_HDR + ZL_F_ZONEID] = 0
    zl2[ZL_HDR + ZL_REC + ZL_F_ZONEID] = 0
    out2 = patch_zone_list(bytes(zl2), head, {0: bytes(rl)},
                           zone_members={0: {0x109}}, room_members=in_rooms)
    got = [struct.unpack_from("<I", out2, ZL_HDR + i * ZL_REC + ZL_F_PLAYERS)[0]
           for i in range(2)]
    if got != [3, 0]:
        print("FAIL: colliding zone ids must not repeat the population -- one "
              "player entering raised EVERY zone by one; got %r" % (got,))
        ok = False
    rooms2 = [struct.unpack_from("<I", out2, ZL_HDR + i * ZL_REC + ZL_F_ROOMS)[0]
              for i in range(2)]
    if rooms2 != [2, 2]:
        print("FAIL: both rows still name the same rooms, got %r" % (rooms2,))
        ok = False

    # THE DIAL TARGET IS THE SERVING HOST'S, NEVER THE AUTHOR'S. The fixture
    # rows carry the dev box's LAN address in +0x2C and the client dials the
    # field literally (prod, 2026-08-19: SYN_SENT 127.0.0.1 -> TRM-8196).
    zl3 = bytearray(ZL_TOTAL); struct.pack_into("<I", zl3, ZL_COUNT_OFF, 2)
    o0, o1 = ZL_HDR + ZL_F_HOST, ZL_HDR + ZL_REC + ZL_F_HOST
    zl3[o0:o0 + 9] = b"192.0.2.1"
    out3, rep3 = patch_zone_hosts(bytes(zl3), "127.0.0.1")
    if bytes(out3[o0:o0 + 16]).split(b"\x00")[0] != b"127.0.0.1":
        print("FAIL: an authored dial target must become the serving host's")
        ok = False
    if bytes(out3[o1:o1 + 16]).strip(b"\x00"):
        print("FAIL: an EMPTY host row is unused, not ours to invent"); ok = False
    if rep3 != ["192.0.2.1"]:
        print("FAIL: the patch must report what it replaced, got %r" % (rep3,))
        ok = False
    if patch_zone_hosts(out3, "127.0.0.1")[1]:
        print("FAIL: re-patching an already-correct blob must report nothing")
        ok = False
    try:
        patch_zone_hosts(bytes(zl3), "a-name-longer-than-the-field.example.com")
        print("FAIL: a host that cannot fit the 16B field must be refused")
        ok = False
    except ValueError:
        pass

    # --- THE TABLE HALF ----------------------------------------------------
    # Asserted against the SHIPPED FIXTURE, whose bytes `tools/tmptl.py` wrote
    # from the PC serialiser at 0x1A3340 while the codec here was read off the
    # PS2 parser `lb__002f9e10`. Two binaries, one layout -- a disagreement
    # between them is exactly the bug worth catching, and asserting our encoder
    # against our own decoder would catch nothing at all.
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import tmfixtures
        fixture = tmfixtures.ptl()
    except Exception as exc:
        print("FAIL: tmfixtures.ptl() raised %r" % (exc,))
        fixture = None
    if fixture is None or len(fixture) < TOTAL:
        print("FAIL: the built b/g/PTL is short -- an all-zero PTL means NO "
              "TABLES draw"); ok = False
    else:
        # WARNING: THE TRADE SERVICE ROW IS OFF FOR THE OVERLAY ASSERTIONS BELOW, and
        # it must be. They pin the authored-vs-live contract -- "a live row EDITS
        # the authored one", "an untouched room is byte-identical to the fixture"
        # -- by counting rows and diffing bytes, and appending a service row
        # shifts both. Turning it off here keeps those assertions testing what
        # they were written to test; the row gets its OWN assertion afterwards,
        # so it is proven rather than merely excused.
        _tt_keep = os.environ.get("POL_TM_TRADE_TABLE")
        os.environ["POL_TM_TRADE_TABLE"] = "0"
        nt = struct.unpack_from("<i", fixture, TABLE_COUNT_OFF)[0]
        if nt < 1:
            print("FAIL: the shipped fixture declares %d tables" % nt); ok = False
        for i in range(max(nt, 0)):
            o = TABLE_OFF + i * TABLE_REC
            rec = fixture[o:o + TABLE_REC]
            if encode_table(decode_table(rec)) != rec:
                print("FAIL: table %d does not survive decode->encode; the PS2 "
                      "map and tmptl's PC map disagree" % i)
                ok = False

        # A LIVE ROW EDITS THE AUTHORED ONE; it does not append a second.
        _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
        _TABLES.clear(); _OWNER[0] = True
        first = decode_table(fixture[TABLE_OFF:TABLE_OFF + TABLE_REC])
        busy = list(first)
        busy[5] = "1"                       # seated
        busy[3] = "2"                       # state -> Playing
        if not note_table("#TM0R001", busy):
            print("FAIL: the first note_table must report a change"); ok = False
        if note_table("#TM0R001", busy):
            print("FAIL: an identical re-send must NOT bump the sequence")
            ok = False
        built = build_ptl("#TM0R001", fixture)
        got = struct.unpack_from("<i", built, TABLE_COUNT_OFF)[0]
        if got != nt:
            print("FAIL: a live update to an AUTHORED table changed the count "
                  "%d -> %d; it must EDIT the row, as lb__002f9f18 does"
                  % (nt, got))
            ok = False
        back = decode_table(built[TABLE_OFF:TABLE_OFF + TABLE_REC])
        if back[5] != "1" or back[3] != "2":
            print("FAIL: the live seated/state never reached the blob: %r"
                  % (back,))
            ok = False
        rest = slice(TABLE_OFF + TABLE_REC, None)
        moved = [i for i in range(TABLE_OFF + TABLE_REC, len(fixture))
                 if fixture[i] != built[i]]
        off_state = _repairable()
        if not set(moved) <= off_state:      # the state/id repair is allowed
            print("FAIL: updating one table disturbed the others"); ok = False

        # ...and a table we have never heard of DOES allocate a slot.
        note_table("#TM0R001",
                   ["#TM0T099", "1", "0x00000000000000FF", "7", "8", "0", ""])
        built = build_ptl("#TM0R001", fixture)
        if struct.unpack_from("<i", built, TABLE_COUNT_OFF)[0] != nt + 1:
            print("FAIL: an unknown table name must allocate a new slot")
            ok = False

        # THE DELTA THE CLIENT WILL APPLY. `cp__002fb290` routes on the TAG, so
        # a table update riding `PD` would be applied as an 88-byte MEMBER.
        d = deltas_after("#TM0R001", 0)
        if not d or [x[1] for x in d] != ["TD", "TD"]:
            print("FAIL: table updates must be TD deltas, got %r"
                  % ([x[1] for x in d] if d else d,)); ok = False
        elif len(d[0][2]) != _NTABLEVALUES:
            print("FAIL: a TD carries %d values, lb__002f9e10 reads %d"
                  % (len(d[0][2]), _NTABLEVALUES)); ok = False

        # THE NIBBLE ORDER OF THE OCCUPANCY ID. `letters` comes from the
        # encoding, `client_reads_id` from the DISASSEMBLY (0x66DBB's four
        # groups and their __allmul multipliers). Asserting one against the
        # other is what pins the order; asserting either against itself would
        # prove nothing. Edges included because 0 is the value that
        # short-circuits the state-1 arm to the empty tile.
        for v in (0, 1, 0xF, 0x1234567890ABCDEF, 0xAB12CD56EB0F5932,
                  0xFFFFFFFFFFFFFFFF):
            enc = letters(v, 16)
            if client_reads_id(enc) != v:
                print("FAIL: the client would read %016X as %016X"
                      % (v, client_reads_id(enc))); ok = False
            if unletters(enc) != v:
                print("FAIL: unletters(%016X) round trip" % v); ok = False
        if client_reads_id(b"A" * 16) != 0:
            print("FAIL: an all-'A' block MUST read 0 -- that is the value the "
                  "state-1 arm short-circuits on"); ok = False

        # ...AND IT MUST AGREE WITH THE AUTHORING TOOL. Different file, derived
        # separately; a divergence would author blobs the server then contradicts.
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(here, os.pardir, "tools"))
            import tmptl as _tmptl
        except ImportError:
            print("note: tools/tmptl.py not importable here (it is NOT mounted "
                  "in the container -- which is why this encoding lives in "
                  "services/); skipping the cross-check")
        else:
            if _tmptl.letters(0xAB12CD56EB0F5932, 16) != letters(0xAB12CD56EB0F5932, 16):
                print("FAIL: tmptl.letters and tmroom.letters disagree"); ok = False

        # WARNING: THE BLOCK MUST STAY NUL-TERMINATED INSIDE THE RECORD. `+0x28`
        # runs to the very end of the 104 bytes, so a 64-character block leaves
        # the client's `mgStrCopyLim(rec+0x28, src, 0x40)` an unterminated
        # string -- and that is not theory: it killed BOTH players' clients the
        # instant the first such row was published (2026-08-20). Asserted on the
        # record, not on the string, because the string is not where it hurts.
        for nletters in (49, 63, 64):
            probe = list(first)
            probe[6] = "A" * nletters
            rec = encode_table(probe)
            tail = rec[TABLE_OFF and 0x28 + nletters:0x68] if nletters < 64 else b""
            if nletters < 64 and not (len(tail) and set(tail) == {0}):
                print("FAIL: a %d-letter block must leave NULs to +0x68, got %r"
                      % (nletters, tail)); ok = False
            if nletters == 64 and rec[0x28:0x68].count(b"\x00") == 0:
                # This is the shape that crashed the client. The encoder cannot
                # refuse it (a caller may legitimately fill the field), so the
                # assertion lives with the CALLERS -- see tetramaster._block_fit
                # -- and this line exists to say so out loud rather than to pass.
                pass

        # A REPEATED MEMBER IN `present` MUST DRAW ONCE. See build_ptl.
        _RECORDS.clear(); _ROOMS_SEQ.clear(); _DELTAS.clear(); _TABLES.clear()
        dup = [(6, "LaptopTest2"), (6, "LaptopTest2"), (3, "Lex")]
        blob = build_ptl("#TM0R001", fixture, present=dup)
        n = struct.unpack_from("<i", blob, MEMBER_COUNT_OFF)[0]
        if n != 2:
            print("FAIL: %d rows from a `present` naming two people three times "
                  "-- a repeated member must draw once" % n)
            ok = False
        _TABLES.clear()

        # A ROOM WITH NO LIVE TABLES IS BYTE-IDENTICAL TO THE FIXTURE -- the
        # assertion that keeps this whole feature from being a regression for
        # every room nobody has touched.
        _TABLES.clear()
        untouched = build_ptl("#TM0R002", fixture)
        off_state = _repairable()
        if not {i for i in range(len(fixture)) if fixture[i] != untouched[i]}                 <= off_state:
            print("FAIL: an untouched room must serve the fixture unchanged")
            ok = False
        _TABLES.clear()

        # --- THE 'D' TRADE SERVICE ROW ------------------------------------
        # The trade board's state 0 (TM.dll 0x15590) scans this array for a
        # record whose +0x28 byte is 'D' and takes its id64; with no such row it
        # goes straight to state 0x12C and errors, which is measured live and is
        # why a trade has never once got past the accept. See TRADE_ROW_KIND.
        if _tt_keep is None:
            os.environ.pop("POL_TM_TRADE_TABLE", None)
        else:
            os.environ["POL_TM_TRADE_TABLE"] = _tt_keep
        _TABLES.clear()
        with_trade = build_ptl("#TM0R002", fixture)
        n_tr = struct.unpack_from("<i", with_trade, TABLE_COUNT_OFF)[0]
        rows = [(i, chr(with_trade[TABLE_OFF + i * TABLE_REC + 0x28]))
                for i in range(n_tr)]
        d_rows = [i for i, k in rows if k == TRADE_ROW_KIND]
        if len(d_rows) != 1:
            print("FAIL: exactly one 'D' trade row must be served, got %d"
                  % len(d_rows)); ok = False
        elif n_tr != nt + 1:
            print("FAIL: the trade row must APPEND (count %d -> %d, got %d)"
                  % (nt, nt + 1, n_tr)); ok = False
        else:
            _o = TABLE_OFF + d_rows[0] * TABLE_REC
            _id = struct.unpack_from("<Q", with_trade, _o)[0]
            # It must carry a real service id -- state 1 sends this as
            # `@TrID=/NN=<id as 16 hex>`, and a zero or colliding id is the
            # bug canonical_table_id's banner already cost us once.
            if _id < TABLE_ID_MIN:
                print("FAIL: the trade row's id %#x is too small to survive the "
                      "wire (see canonical_table_id)" % _id); ok = False
            if with_trade[_o + 0x28:_o + 0x28 + BLOCK_LETTER_MAX] !=                     (TRADE_ROW_KIND + "A" * (BLOCK_LETTER_MAX - 1)).encode("latin1"):
                print("FAIL: the trade row's block must be the kind letter plus "
                      "filler, as #TM0COM authors 'EAAAAAAAAAAAAAAA'"); ok = False
        # ...and it must be IDEMPOTENT: re-serving a blob that already has one
        # must not stack a second (build_ptl runs on every fetch).
        again = build_ptl("#TM0R002", bytes(with_trade))
        if struct.unpack_from("<i", again, TABLE_COUNT_OFF)[0] != n_tr:
            print("FAIL: a second build_ptl added another trade row"); ok = False
        _TABLES.clear()

    _RECORDS.clear(); _ROOMS_SEQ.clear(); _GUIDS.clear(); _DELTAS.clear()
    _TABLES.clear()
    _OWNER[0] = False
    ok = _selftest_client_key() and ok
    print("\n%s" % ("selftest OK" if ok else "SELFTEST FAILED"))
    return 0 if ok else 1


def _selftest_client_key():
    """THE ID KEY AND THE ROUND TRIP -- the "Change Table Settings" invariant.

    Pinned against LIVE VALUES, not against this module's own arithmetic. The
    two (self id, login nick) pairs and the three peer guids below were read off
    the 2026-08-22 wire (authserv.log, two clients in `#TM0R001`). If a future
    edit puts a table id back outside the key's coset, this fails here instead
    of as a player sitting on "Retrieving table info..." until it times out.
    """
    fails = []

    def want(cond, msg):
        if not cond:
            fails.append(msg)

    # MEASURED: two different clients in one capture must yield the SAME key.
    pairs = [(0xAB12CD31DC3F63B6, 0x00E13883D826),      # client A / UF8TOQDTX
             (0xAB12CD56EB0F5932, 0x00860FB3E2A2)]      # client B / UA4XX8PKP
    keys = {sid ^ fold for sid, fold in pairs}
    want(keys == {CLIENT_KEY_DEFAULT},
         "the two measured (self id, nick fold) pairs must both give "
         "CLIENT_KEY_DEFAULT, got %s" % sorted(hex(k) for k in keys))

    # MEASURED: that key reproduces every peer guid ever captured.
    for guid, app, what in (
            (0x00F0E4BCBB91, 0x0000002000000001, "room peer UKXDBA266"),
            (0x00D1E4BCAB91, 0x0021000100001001, "table 1 peer UE5BRFVCJ"),
            (0x00D1E4BC9B92, 0x0021000100002002, "table 2 peer UE5BRFYDE")):
        got = wire_guid(app, CLIENT_KEY_DEFAULT)
        want(got == guid, "%s: expected wire guid %#014x, got %#014x"
                          % (what, guid, got))

    # THE BUG ITSELF, stated as an assertion so it cannot come back silently.
    want(not round_trips(0x0021000100001001, CLIENT_KEY_DEFAULT),
         "the legacy table id must be RECOGNISED as un-round-trippable, or the "
         "heal never fires")
    want(app_id_for_guid(0x00D1E4BCAB91, CLIENT_KEY_DEFAULT)
         == 0xAB12CD0100001001,
         "the legacy id must reconstruct as 0xAB12CD0100001001 -- the measured "
         "high-half mismatch TM.dll 0x86ED0 rejects")

    if os.environ.get("POL_TM_TABLE_ID_KEYFOLD", "1") == "1":
        # THE FIX: every authored table id in every room survives the wire.
        bad = [(r, i + 1) for r in range(1, 33) for i in range(64)
               if not round_trips(canonical_table_id(i, r))]
        want(not bad, "%d authored table id(s) cannot survive the wire, first "
                      "%s" % (len(bad), bad[:3]))
        # ...and the peer LOW half is byte-identical to the old scheme, which is
        # what `tetramaster.table_index_for_peer` indexes on.
        klow = CLIENT_KEY_DEFAULT & 0xFFFFFFFF
        for r in (1, 3):
            for i in range(3):
                got = wire_guid(canonical_table_id(i, r)) & 0xFFFFFFFF
                exp = klow ^ (((i + 1) << 12) | (i + 1))
                want(got == exp,
                     "room %d table %d peer low half MOVED: %#010x != %#010x -- "
                     "peer indexing would break" % (r, i + 1, got, exp))

    # THE UNKNOWN-ROOM SERVE MUST ROUND-TRIP TOO -- the deterministic
    # first-VS.-COM-attempt failure (2026-08-22, live: peer UK6T6V3TZ =
    # the fixture's raw 0x2100001001 folding back wrong). The shipped
    # fixture's TABLE rows must come out of `fold_unknown_tables`
    # round-trippable; its SERVICE rows ('E'/'F') must be untouched.
    try:
        import tmfixtures
        _fx = bytearray(tmfixtures.ptl())
        _svc_before = [struct.unpack_from("<Q", _fx, TABLE_OFF + i * TABLE_REC)[0]
                       for i in range(2)]
        fold_unknown_tables(_fx)
        _nt = struct.unpack_from("<i", _fx, TABLE_COUNT_OFF)[0]
        _bad = []
        for i in range(min(_nt, TABLE_SLOTS)):
            tid = struct.unpack_from("<Q", _fx, TABLE_OFF + i * TABLE_REC)[0]
            if i < 2:
                want(tid == _svc_before[i],
                     "fold_unknown_tables must not touch service row %d" % i)
            elif not round_trips(tid):
                _bad.append((i, tid))
        want(not _bad, "unknown-room fold left %d table id(s) un-round-"
                       "trippable, first %s" % (len(_bad), _bad[:2]))
    except FileNotFoundError:
        pass

    for msg in fails:
        print("FAIL: %s" % msg)
    print("  client key K = %#018x (%s) -- table ids round trip: %s"
          % (client_key(), _CLIENT_KEY_SOURCE, "no" if fails else "yes"))
    return not fails


# --- THE ZONE AND ROOM LISTS -- live player/room counts ----------------------
#
# The zone and room screens have shown PLACEHOLDER counts for as long as they
# have rendered, because both blobs are authored files and nothing updated the
# numbers in them.
#
# THE FIELD MAP IS JANHOUROU'S, AND IT IS CONFIRMED ON TETRA MASTER. Both games
# link sqMg and both containers are byte-identical -- `b/g/ZL` is 2120 = 0x48 +
# 32*0x40 with its count at +0x40, `b/g/RL%03d` is 51272 = 0x48 + 256*200 with
# its count at +0x40 -- and `services/janlobby.py` derived the FIELDS from
# decompiled source (`zoneselw.c`, `Copy_Zone_Information`, SE's own names
# "BodyCount", "RoomCount", "RoomPlayerNum", "TableNum"). `tools/tmzonelist.py`
# guessed three of them and says so ("a FIRST CUT ... non-zero placeholders").
#
# WARNING: THE CROSS-CHECK THAT SETTLES IT, measured 2026-08-18 rather than argued:
# the TM zone records we serve carry `+0x3C = 0`, and the client fetches
# **`b/g/RL000`** -- eight times, and nothing else. `+0x3C` is therefore the zone
# id that names `b/g/RL%03d`, exactly as `janlobby.ZL_F_ID` says and exactly what
# `tmzonelist.py` called an opaque flag byte.
#
# WARNING: WE PATCH, WE DO NOT REBUILD. Only the COUNT fields are written; names, ids,
# channels, the rules block and the gates stay exactly as authored. TM's rooms
# render today with `RL_F_GATE` at 0 where Jan's map demands 1, so rebuilding
# from Jan's template would "fix" a field that is already working -- and that is
# this file's standing lesson about changing what you have not measured.

ZL_TOTAL, ZL_HDR, ZL_REC, ZL_COUNT_OFF = 2120, 0x48, 0x40, 0x40
ZL_F_PLAYERS, ZL_F_ROOMS, ZL_F_ZONEID = 0x00, 0x04, 0x3C
ZL_F_HOST = 0x2C                # 16B NUL-terminated ASCII, per janlobby.ZL_F_HOST

RL_TOTAL, RL_HDR, RL_REC, RL_COUNT_OFF = 51272, 0x48, 200, 0x40
RL_F_ROOMID, RL_F_PLAYERS, RL_F_OPENTABLES, RL_F_CHANNEL = 0x00, 0x10, 0x86, 0xB8
RL_CHANNEL_LEN = 13

#: SE's "TableNum" -- the room's TOTAL table count, u32. From `janlobby`, which
#: took it from decompiled source (`zoneselw.c`, `Copy_Zone_Information`); the
#: sibling `RL_F_OPENTABLES` at 0x86 is the s8 "Open Tables" column, biased +0x20.
RL_F_TABLES = 0x18

#: WARNING: NEITHER FIELD IS CONFIRMED ON TETRA MASTER, and the numbers on screen say
#: so. Reported 2026-08-20: the room list draws **15** tables for the first room
#: and **16** for the rest. The authored bytes are `0x18 = 1` for room 1 and 0
#: for the others, and `0x86 = 0x00` throughout -- so 15/16 comes from NEITHER
#: offset, and the client is reading something we have not identified.
#:
#: What we DO know is that we have never written either field, so whatever is on
#: that screen is authored placeholder. Writing SE's own named fields from live
#: data is the smallest step that can distinguish the two possibilities, and it
#: is reversible: if the column moves, the offsets are right and the numbers are
#: now true; if it does not, the offsets are wrong for TM and the read site has
#: to come out of `TM.dll` before anything else is written.
#:
#: WARNING: DO NOT WIDEN THIS INTO A REBUILD. `patch_room_list` writes COUNT fields
#: only. TM's rooms render today with `RL_F_GATE` at 0 where Jan's map demands 1,
#: so rebuilding from Jan's template would "fix" a field that is already working
#: -- the standing lesson at the top of this section.


def _room_channels(rl_blob):
    """`[(index, channel)]` from an authored room list."""
    if not rl_blob or len(rl_blob) < RL_TOTAL:
        return []
    n = min(struct.unpack_from("<I", rl_blob, RL_COUNT_OFF)[0],
            (RL_TOTAL - RL_HDR) // RL_REC)
    out = []
    for i in range(n):
        o = RL_HDR + i * RL_REC + RL_F_CHANNEL
        chan = rl_blob[o:o + RL_CHANNEL_LEN].split(b"\x00")[0]
        out.append((i, chan.decode("latin1", "replace")))
    return out


def patch_room_list(blob, headcount, tables_by_room=None):
    """Write each room's LIVE player and table counts into an authored `b/g/RL%03d`.

    `headcount` is `{channel: n}`. Rooms the caller knows nothing about keep the
    authored value rather than being zeroed -- an unknown room is not an empty
    one, and blanking it would be a worse lie than the placeholder.

    `tables_by_room` is `{channel: (total, open)}` and is optional for the same
    reason: a caller that cannot say loses nothing and the authored bytes stand.
    See `RL_F_TABLES` -- those two offsets are SE's names via `janlobby` and are
    NOT yet confirmed on Tetra Master.
    """
    if not blob or len(blob) < RL_TOTAL:
        return blob
    tables_by_room = tables_by_room or {}
    buf = bytearray(blob)
    for i, chan in _room_channels(blob):
        o = RL_HDR + i * RL_REC
        if chan in headcount:
            struct.pack_into("<I", buf, o + RL_F_PLAYERS,
                             int(headcount[chan]) & 0xFFFFFFFF)
        if chan in tables_by_room:
            total, free = tables_by_room[chan]
            struct.pack_into("<I", buf, o + RL_F_TABLES, int(total) & 0xFFFFFFFF)
            # s8 biased by +0x20, per janlobby.RL_F_OPENTABLES. Clamped rather
            # than masked: a count that wrapped would read as a huge negative and
            # tell us nothing about whether the offset is even right.
            buf[o + RL_F_OPENTABLES] = (0x20 + max(0, min(int(free), 0x5F))) & 0xFF
    return bytes(buf)


def room_table_counts(chan, base, live=None):
    """`(total, open)` tables for `chan`, from the same rows `build_ptl` serves.

    ONE SOURCE, so the room list and the table screen cannot disagree -- a room
    advertising three tables that opens onto four is the kind of split this
    subsystem keeps producing.

    WARNING: THE CARD SHOP IS NOT A TABLE. `#TM0CARD` sits in the same array as
    `#TM0T001..003` and the fixture counts four rows; counting it would advertise
    a table nobody can sit at. Only `#TM0T###` names are tables.
    """
    if not base or len(base) < TOTAL:
        return None
    live = dict(live if live is not None else tables(chan))
    n = max(0, min(struct.unpack_from("<i", base, TABLE_COUNT_OFF)[0], TABLE_SLOTS))
    total = free = 0
    for i in range(n):
        off = TABLE_OFF + i * TABLE_REC
        row = decode_table(base[off:off + TABLE_REC])
        name = row[0]
        if not name.startswith("#TM0T"):
            continue                        # the card shop, not a table
        total += 1
        row = live.get(name, row)
        if not _as_int(row[5]):             # +0x0C, seated
            free += 1
    for name, row in sorted(live.items()):  # live rows the fixture never had
        if name.startswith("#TM0T") and name not in {
                decode_table(base[TABLE_OFF + i * TABLE_REC:
                                  TABLE_OFF + (i + 1) * TABLE_REC])[0]
                for i in range(n)}:
            total += 1
            if not _as_int(row[5]):
                free += 1
    return total, free


def patch_zone_list(blob, headcount, room_lists, zone_members=None,
                    room_members=None):
    """Write each zone's LIVE player and room counts into an authored `b/g/ZL`.

    `room_lists` is `{zone_id: rl_blob}` -- a zone's population starts as the sum
    over the rooms its OWN list names, which is the only definition that survives
    two zones pointing at one room list (which is what we serve today).

    WARNING: BUT THE ROOM SUM IS NOT THE ZONE'S POPULATION, and that is the live report
    "joining a zone does not move the count". A player standing on the ROOM LIST
    has joined no IRC channel, so they are in no room, in no headcount, and in
    nothing this function could see. `zone_members` is `{zone_id: {member_id}}`
    for exactly those people -- whoever is IN the zone by some other measure --
    and `room_members` is `{channel: {member_id}}` so somebody who walked on
    THROUGH into a room is not counted twice.

    Both default to empty, in which case this computes precisely what it computed
    before: a caller that cannot say who is where loses nothing.
    """
    if not blob or len(blob) < ZL_TOTAL:
        return blob
    zone_members = zone_members or {}
    room_members = room_members or {}
    buf = bytearray(blob)
    n = min(struct.unpack_from("<I", blob, ZL_COUNT_OFF)[0],
            (ZL_TOTAL - ZL_HDR) // ZL_REC)
    # WARNING: TWO ZONES CAN CARRY THE SAME `+0x3C`, AND TETRA MASTER'S DO -- both its
    # rows say zone 0, which is why the client only ever fetches `b/g/RL000`.
    # Without this guard every row resolves to the SAME room list and the same
    # standing players, so one person entering a zone raised the count of EVERY
    # zone by one. Measured 2026-08-18 in live testing, and it is the population
    # half that made it visible: the room sums had the same fault all along and
    # nobody saw it, because they were zero.
    #
    # A person is counted ONCE, by the FIRST row claiming their zone id. Which
    # row that is is arbitrary when the ids collide -- we genuinely cannot tell
    # the two apart, since the client's zone-entry event names the room LIST and
    # both zones name the same one -- but the TOTAL across the list is then
    # right, and "one zone has them" beats "every zone has them".
    claimed = set()
    for i in range(n):
        o = ZL_HDR + i * ZL_REC
        zid = blob[o + ZL_F_ZONEID]
        rl = room_lists.get(zid)
        if rl is None:
            continue
        chans = _room_channels(rl)
        struct.pack_into("<I", buf, o + ZL_F_ROOMS, len(chans))
        if zid in claimed:
            # An earlier row already holds this id's population. Zero, not a
            # repeat: repeating it is the inflation this guard exists to stop.
            struct.pack_into("<I", buf, o + ZL_F_PLAYERS, 0)
            continue
        claimed.add(zid)
        players = sum(int(headcount.get(c, 0)) for _, c in chans)
        # ...PLUS whoever is in the zone but in none of its rooms. A UNION: the
        # rooms already counted anyone who is in one, identity and all.
        in_rooms = set()
        for _, c in chans:
            in_rooms |= set(room_members.get(c) or ())
        players += len(set(zone_members.get(zid) or ()) - in_rooms)
        struct.pack_into("<I", buf, o + ZL_F_PLAYERS, players)
    return bytes(buf)


def patch_zone_hosts(blob, host):
    """Every populated zone row's dial target (+0x2C) becomes `host`.

    `ZL_F_HOST` is the address the client dials for the zone's game connection,
    and it dials the field LITERALLY (same layout janlobby derived from SE's
    `zoneselw.c`). The authored fixtures carry the address of the box they were
    CAPTURED on -- dev's LAN IP -- so serving them from any other machine sends
    every client somewhere that is not there. Measured on prod 2026-08-19: both
    testers sat in SYN_SENT to 127.0.0.1:51241 and errored TRM-8196-37130
    ("Error occurred while connecting to the server"). The serving host is the
    only value that is ever right, so it is enforced here at serve time and the
    fixture keeps its provenance.

    Rows whose host field is EMPTY are unused rows, not ours to invent -- they
    stay empty. Returns `(patched, replaced)` where `replaced` lists the
    distinct values actually rewritten; empty when the blob already dials
    `host` (dev, where the fixture's address IS the serving host).
    """
    width = ZL_F_ZONEID - ZL_F_HOST                       # 16, NUL-terminated
    data = host.encode("ascii")
    if not data or len(data) >= width:
        raise ValueError(f"zone host {host!r} does not fit the {width}B field")
    if not blob or len(blob) < ZL_TOTAL:
        return blob, []
    buf = bytearray(blob)
    n = min(struct.unpack_from("<I", blob, ZL_COUNT_OFF)[0],
            (ZL_TOTAL - ZL_HDR) // ZL_REC)
    replaced = []
    for i in range(n):
        o = ZL_HDR + i * ZL_REC + ZL_F_HOST
        cur = bytes(buf[o:o + width]).split(b"\x00")[0]
        if not cur or cur == data:
            continue
        buf[o:o + width] = data.ljust(width, b"\x00")
        val = cur.decode("ascii", "replace")
        if val not in replaced:
            replaced.append(val)
    return bytes(buf), replaced


if __name__ == "__main__":
    # WARNING: AT THE END OF THE FILE ON PURPOSE. `selftest` covers the zone and room
    # lists, whose constants are defined below the roster half -- run from the
    # middle of the module it fails with a NameError on `ZL_TOTAL`.
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__.splitlines()[0])
