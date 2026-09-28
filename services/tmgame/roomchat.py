"""Room chat (@Chat): the roster arm, its live toggle file and the serial it carries."""
import os
from . import common, protocol, pushqueue


#: ROOM CHAT IS THREE ARMS OF ONE CODE, AND THE MSGID PICKS WHICH.
#: TM.dll's chat manager (the object at `parent+0x1A0`) dispatches every code
#: 0x42 message in the store on the header's msgid field -- rva 0x07E3FF,
#: `a9780(4)`:
#:
#:     cmp eax, 4 / je  0x07E42F    arm A -- the MEMBER LIST, rebuilt wholesale
#:     cmp eax, 6 / jle bail        5, 6 and anything below 4 are dropped
#:     cmp eax, 8 / jle 0x07E8A9    arm B -- a chat LINE; appends it and lights
#:                                  the new-message indicator
#:
#: and msgid **1** is what the client SENDS to ask for arm A's data. The sender
#: is 0x07F540: it stamps `[esp+0x3d0]=0x42`, `[esp+0x3d4]=1` and writes
#: `@Chat=/NN=<POL-ID>/CN=<name>/HID=/Dm=/Vol=` -- with `/NN=` and `/CN=` read
#: from globals ([0x5224BD8]/[0x5224BDC] and [0x5224BE0]) that NOTHING in the
#: image ever writes, which is why every one of them on the wire reads
#: `/NN=0000000000000000/CN=`. The server is meant to fill them in, exactly as
#: it fills `<DE>`'s name and `@Init=/Shm=`. Twenty went unanswered in the
#: 2026-08-20 session.
#:
#: WARNING: AND THE ANSWER IS POLLED FOR, NOT PUSHED AT. The screen pump at 0x0ACD0 is
#: a four-state machine over `[obj+0x64F0]`; state 0 sends the msgid-1 request
#: (0x0AD1C, the arm taken when `[obj+0x18C] == 1`) and states 2 and 3 ask the
#: message store `0x07DFD0(0x42, 4)` on every tick before dispatching. So the
#: answer may arrive whenever -- it sits in the store until the pump looks --
#: but it is only ever LOOKED for while that screen is up. That is why the
#: "rebroadcast a chat line as msgid 4" experiment logged no `find("@Chat")`
#: at all: it was answered into a state that does
#: not poll for msgid 4, not dropped by the chat manager.
MSG_CHAT = 0x42
CHAT_MSGID_WHO = 1              # request  `@Chat=/NN=../CN=../HID=../Dm=../Vol=..`
CHAT_MSGID_ROSTER = 4           # reply    `@Chat=/Num=..|..(/C=)(/NN=)(/CN=)`
CHAT_MSGID_LINE = 8             # a chat line, both directions


def _chat_roster_enabled():
    """OFF by default. WARNING: **A MSGID-4 THAT FAILS ARM A'S GATE IS NEVER ERASED,
    AND IT BLOCKS EVERY LATER CODE-0x42 MESSAGE.**

    Measured 2026-08-20, live, and it cost a tester a working chat indicator:

        13:37  roster served to members 3 and 6
        14:12  chat lines relayed, NO INDICATOR on the receiver
        14:18  roster served again
        15:47  member 6 relaunches Tetra Master -- the store dies with the process
        15:55  indicator WORKS again

    The mechanism is in the bail paths. `0x07E852` (the gate at 0x07E4B0 failing)
    and `0x07E828` (the gather loop finding no part) both `inc esi ; jmp` OUT
    **without calling the erase at 0x81F60** -- only the "@Chat not found" path
    (0x07E855) erases. The dispatcher at 0x07E36C takes the FIRST code-0x42
    message in the store on every tick and arm A ends the function, so one
    un-erased msgid-4 parks permanently in front of the msgid-8 chat lines. The
    store is a TM.dll global (0x5228250) that survives room changes and dies only
    with the process, which is why a relaunch is what cleared it.

    So this stays off until the gate is PROVEN to pass. The suspect is the one
    thing the chat reading flags as never verified: which of `0xAB080`'s
    output pointers takes the value and which takes the found-flag. If `/Num=`
    does not land in the slot 0x07E4A9 reads, the gate compares the shop byte
    against stack garbage and fails on a number we never sent.
    """
    if _chat_control_says() is not None:
        return _chat_control_says()
    return os.environ.get("POL_TM_CHAT_ROSTER", "0") != "0"


#: The live toggle for the roster, RE-READ ON EVERY REQUEST.
#:
#: VERIFIED: THE POINT IS THAT TURNING IT OFF COSTS ONE COMMAND, NOT A DEPLOY. This
#: reply carries a measured hazard -- a msgid-4 that fails arm A's gate is never
#: erased and parks in front of every later code-0x42 message, taking the chat
#: indicator with it until the player relaunches (see `_chat_roster_enabled`).
#: An env knob cannot be changed without recreating the container, which is far
#: too slow to abort a live test with somebody sitting in a room. The teach
#: control file already established this pattern in this module ("re-read per
#: line, so this is tunable to zero without a restart"); this is the same thing
#: for the same reason.
#:
#:     echo 1 > /data/tm_chat_roster.txt      # answer the next request
#:     echo 0 > /data/tm_chat_roster.txt      # stop, takes effect immediately
#:     rm      /data/tm_chat_roster.txt       # fall back to POL_TM_CHAT_ROSTER
#:
#: Absent or unreadable means "no opinion", so the env default still governs and
#: a missing file can never turn the feature ON.
_CHAT_CONTROL_FILE = os.environ.get(
    "POL_TM_CHAT_ROSTER_FILE",
    os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "tm_chat_roster.txt"))


def _chat_control_says():
    """True/False from the control file, or None if it has nothing to say."""
    try:
        with open(_CHAT_CONTROL_FILE) as f:
            text = f.read().strip()
    except OSError:
        return None
    if not text:
        return None
    return text.split()[0].strip().lower() not in ("0", "off", "no", "false")


#: member_id -> the last `/C=` we served them. See `_chat_next_serial`.
_CHAT_SERIAL = {}


def _chat_next_serial(member_id):
    """The next `/C=` for this member -- a serial that only ever goes UP.

    Arm A treats `/C=` as the identity of one roster SNAPSHOT. Having taken the
    first code-0x42 message out of the store, it walks the store for the rest of
    that snapshot's parts (0x07E5A9 parses each candidate's `/C=`) and:

        equal    -> a part of my snapshot: keep it, erase the original
        greater  -> a NEWER snapshot: skip it, it is not mine
        lesser   -> STALE: erase it unread   (0x07E5C4)

    so a serial that rises per snapshot is what lets a client that fell behind
    throw away what it missed instead of drawing it. A constant would do, right
    up until two snapshots sit in the store at once.
    """
    key = pushqueue._push_key(member_id)
    nxt = (_CHAT_SERIAL.get(key, 0) % 0x7FFFFFFE) + 1
    _CHAT_SERIAL[key] = nxt
    return nxt


def _chat_name(mid, vals):
    """The name to draw on one roster row, as arm A's 16-byte slot wants it.

    The consumer at 0x0AFF3 copies at most 0x10 bytes out of the node
    (`0x07ED50(i, 0, 0x10)`), so 15 characters plus a terminator is the budget.
    `/`, `@` and `|` are the field grammar's own delimiters -- a name carrying
    one would end the value early and shift every later row by one -- so they
    are dropped rather than escaped: this grammar has no escape.
    """
    name = ""
    if vals is not None and len(vals) > 10:
        name = (vals[10] or "")[:15].strip()
    if not name and vals is not None and len(vals) > 4:
        name = (vals[4] or "").strip()
    if not name:
        try:
            import tmroom
            name = tmroom.name_of(mid)
        except Exception:
            name = ""
    name = "".join(c for c in str(name)
                   if c not in "/@|" and 0x20 <= ord(c) < 0x7F)
    return name.strip()[:15].encode("latin1", "replace")


def _chat_roster_rows(member_id):
    """`[(mid, pol_id_bytes, name_bytes), ...]` for this player's room, or None
    if we cannot say which room they are in.

    WARNING: A MEMBER WITH NO POL-ID IS SKIPPED, NOT INVENTED. `/NN=` is matched by
    every receiving client against its own id (0x0AFE3, and see
    `tmroom.note_pol_id`), and it is the id the row carries into whatever the
    player does with that row next. A placeholder would be a row nobody can
    resolve and that its own owner does not recognise as itself -- so it is left
    out, and said out loud instead of being papered over.
    """
    try:
        import tmroom
    except ImportError:
        return None
    try:
        chan = tmroom.room_of(member_id)
    except Exception as exc:
        common._say("tm: chat roster -- no room for member %s (%r)" % (member_id, exc))
        return None
    if not chan:
        return None
    rows, missing = [], []
    for mid, vals in tmroom.members(chan):
        ident = tmroom.pol_id_of(mid)
        if not ident:
            missing.append(mid)
            continue
        rows.append((mid, ident.encode("ascii"), _chat_name(mid, vals)))
    if missing:
        common._say("tm: chat roster for %s omits member(s) %s -- no POL-ID yet "
             "(it is learned from @Init=/NN=; a client that has not sent one "
             "this session cannot be put on a row anyone could match)"
             % (chan, ", ".join(str(m) for m in missing)))
    return rows


def _chat_roster_body(member_id):
    """The `@Chat=` member list for arm A, or None to stay silent.

    Read off the builder at 0x07E5C0-0x07E71B rather than invented:

        0x07E620  erase the second list (the container at `+0x62AC`)
        0x07E674  find "@Chat"
        0x07E68F  `/Num=` occurrence **1** -- the ENTRY COUNT in this message
        0x07E6A1  loop that many times:
        0x07E6B1      `/NN=` occurrence i  (0xAB470: up to 16 hex digits into a
                                            u64 -- low half to node+0x18, high
                                            half to node+0x1C)
        0x07E6C6      `/CN=` occurrence i  (0xAAD60: the text, node+0x0C)
        0x07E6DE      append the node
        0x07E709  `mov eax,[ebp+0x62b4] ; mov [ebp+0x62b8],eax` -- PUBLISH the
                  count, which is the bound every accessor checks

    Two things about the shape are load-bearing and neither is obvious:

    * **`/Num=` occurrence 0 is a different number from occurrence 1.**
      Occurrence 0 is the highest PART index of a multi-message roster (the
      gather loop at 0x07E4E8 runs `0..occ0` INCLUSIVE, into a 17-slot array of
      0x3E8-byte buffers); occurrence 1 is how many entries this part carries.
      We send one message, so occurrence 0 is 0.
    * **the header's shop byte must EQUAL `/Num=` occurrence 0** -- 0x07E4B0
      compares `a9780(6)` against it and bails on `jne`. It is the "this is the
      last part, reassemble now" trigger, so with a single message both are 0.

    And occurrences are PIPE-separated under one key, not repeated keys: the
    ordinal indexes into a `|` list (see `_vsgame_body`'s banner).
    """
    rows = _chat_roster_rows(member_id)
    if rows is None:
        common._say("tm: member %s asked who is in the chat and we do not know which "
             "room they are in -- silent" % (member_id,))
        return None
    parts = 0                                   # one message: parts 0..0
    body = b"@Chat=/Num=%d|%d/C=%d" % (parts, len(rows),
                                       _chat_next_serial(member_id))
    if rows:
        body += b"/NN=" + b"|".join(r[1] for r in rows)
        # NOTE: DELIBERATELY NOT CHANGED, AND FLAGGED. This `/CN=` is parsed by
        # **0xAAD60** (this docstring says so, at 0x07E6C6) -- the same function
        # measured 2026-08-20 to be a HEX DECODER, not a text extractor; see
        # `_str_field`, where the in-match `/CN=` was fixed after a tester
        # photographed a name rendering as `z` followed by squares. If the chat
        # roster's names are squares too, `_str_field(...)` around each row here
        # is the whole fix. It is left alone because this screen is another
        # track's active work and nobody has reported it broken -- shipping an
        # unmeasured change to a screen nobody has seen is the habit this
        # subsystem's ledger exists to stop.
        body += b"/CN=" + b"|".join(r[2] for r in rows)
    header = protocol.encode_code(MSG_CHAT | (CHAT_MSGID_ROSTER << 16)
                         | ((parts & 0xFF) << 24))
    common._say("tm: member %s chat roster -- %d row(s): %s"
         % (member_id, len(rows),
            ", ".join("%s=%s" % (r[2].decode("latin1"), r[1].decode("ascii"))
                      for r in rows) or "(none)"))
    return header + body
