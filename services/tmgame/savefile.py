"""Writing the player save (U/g/TM0DataFile): byte and field patches, the guild byte and @Opt=
profile fields.
"""
import os
import re
from .deps import tmsave
from . import common


#: WRITING THE PLAYER SAVE. The client has NO save-write API -- only
#: `sqMgCpLoadPlayerSaveData(+Check)`; SE's SERVER owned `U/g/TM0DataFile` and
#: built it from game events. So persistence is our job,
#: and it is done by patching the member's stored blob, which `_resource_blob`
#: then serves verbatim on the next 3:0 read -- no rebuild, no restart.
#:
#: Offsets are read off the gates that consume them:
#:     +0x3B  u8  GUILD   0 unset · 1 Warriors · 2 Voyagers · 3 Summoners
#:                        gate TM.dll rva 0x14C866: guild==0 -> draw the guild
#:                        screen, else skip to state 0x15.
#:     +0x126 u8  the "skip handle linking" checkbox (@Opt=/HNSS=)
_SAVE_LEN = 12328                       # what sqMgCpReadFile asks for
_SAVE_FILE_LEN = _SAVE_LEN + 4          # + the 4-byte trailer the signer fills
SAVE_OFF_GUILD = 0x3B


def _save_file(member_id):
    """The stored save blob for one member -- the name `_resource_file` builds.

    WARNING: Resolve the directory the way `responders.RESOURCE_DIR` does, POL_RESOURCE_DIR
    FIRST. Deriving it from POL_DATA_DIR alone agrees today only because nothing
    sets the override; the day something does, we would write a save the lobby
    never reads and the guild screen would come back with nothing in the log to
    say why.
    """
    root = os.environ.get("POL_RESOURCE_DIR")
    if not root:
        root = os.path.join(os.environ.get("POL_DATA_DIR", "/data"), "resources")
    return os.path.join(root, "%s.U_g_TM0DataFile.bin" % member_id)


def _save_patch(member_id, off, value):
    """Set one byte of a member's save, creating the blob if it does not exist.

    Returns (old, new) or None if nothing was written. Atomic: the replace is
    what the lobby's next read sees, so a torn file can never be served.
    """
    if member_id is None:
        return None
    path = _save_file(member_id)
    try:
        with open(path, "rb") as f:
            buf = bytearray(f.read())
    except OSError:
        buf = bytearray(_SAVE_FILE_LEN)      # a fresh player: all zeros
    if len(buf) < _SAVE_FILE_LEN:
        buf.extend(b"\x00" * (_SAVE_FILE_LEN - len(buf)))
    old = buf[off]
    if old == value:
        return None                          # already right; do not churn the file
    buf[off] = value & 0xFF
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(buf)
        os.replace(tmp, path)                # atomic
    except OSError:
        return None
    return old, value & 0xFF


def _guild_persist_enabled():
    return os.environ.get("POL_TM_GUILD_PERSIST", "1") == "1"


def _save_patch_many(member_id, changes):
    """`_save_patch_fields` for a map of single BYTES -- `{offset: value}`.

    The `@Opt=` shape, kept because that is what every option field is. Anything
    with a dword in it (the table-settings preset) goes through
    `_save_patch_fields` directly.
    """
    return _save_patch_fields(member_id,
                              {off: (1, val) for off, val in changes.items()})


def _save_patch_fields(member_id, changes):
    """Set SEVERAL fields of a member's save in one rewrite.

    `changes` is {offset: (width, value)}, width 1 or 4. Returns
    {offset: (old, new)} for the fields that actually moved, or `{}`.

    WARNING: WIDTH IS NOT COSMETIC. The options block is all bytes, but the table
    preset's `bm`/`lu`/`ll`/`au`/`al` are DWORDS -- `0x14DB61` reads +0xE0 with
    `mov eax, dword ptr [...]`. Writing `au=300` as a byte stores 44, and a rank
    band of 0..44 refuses everybody exactly the way 0..0 did.

    Same file, same atomic replace as `_save_patch` --
    that is the one-byte case of this, and the two should fold together when
    whoever owns this file next touches it; kept separate today only because
    `_save_patch` was in flight uncommitted when this landed.

    `@Opt=` carries TWELVE option bytes, so doing it a byte at a time would
    rewrite 12,332 bytes twelve times and leave eleven windows in which the
    lobby could serve a half-applied save.
    """
    # THREE WAYS THIS COULD FAIL WITHOUT SAYING SO -- all now audible. None of
    # them is what prod was doing, and that took a wrong diagnosis to establish:
    # twelve `@Opt=` arrived and produced no save patch, which reads like a lost
    # write and is not one. The client ECHOES its whole option set every time, so
    # with a zero header it sends zeros back and NOTHING MOVES -- `moved` is
    # empty and there is correctly nothing to log. The proof is member 6's own
    # header: the only non-zero option bytes it had were +0x103 and +0x126, which
    # are exactly the two the client sent as non-zero (`CT=60`, `HNSS=1`). This
    # writer put them there. It works.
    #
    # The three below are real latent traps, not the observed behaviour, and the
    # READ one is the dangerous member of the set: falling back to zeros on a
    # read error would write a ZERO HEADER over a good save and manufacture the
    # very bug `tmsave.DEFAULT_HEADER` exists to prevent.
    if not changes:
        return {}
    if tmsave is None:
        # The one hard dependency: `write_field` owns the width encoding, and
        # open-coding it here is how the two would drift. Say so rather than
        # writing a partial header.
        common._say("tm: %d setting(s) cannot be saved -- tmsave did not import"
              % len(changes))
        return {}
    if member_id is None:
        common._say("tm: @Opt= carried %d setting(s) but this session has NO MEMBER -- "
              "DISCARDED. The client was told /Ans=1, so it believes they were "
              "saved and will not resend them." % len(changes))
        return {}
    path = _save_file(member_id)
    try:
        with open(path, "rb") as f:
            buf = bytearray(f.read())
    except FileNotFoundError:
        # A player with no save yet. Mint one the way `tmsave.build` does, so a
        # first-ever settings write does not create the zero header that
        # `tmsave.DEFAULT_HEADER` exists to prevent.
        buf = bytearray(_SAVE_FILE_LEN)
        if tmsave is not None:
            for off, (width, val) in tmsave.default_header_writes().items():
                tmsave.write_field(buf, off, width, val)
        common._say("tm: member %s has no save; minting one with the default header "
              "before applying %d setting(s)" % (member_id, len(changes)))
    except OSError as exc:
        # WARNING: NEVER FALL BACK TO ZEROS ON A READ ERROR. The old code did, so a
        # permissions or I/O fault would have rebuilt the save from nothing and
        # written a ZERO HEADER over a good one -- turning a transient error into
        # the exact bug `tmsave.DEFAULT_HEADER` documents.
        common._say("tm: member %s save is UNREADABLE (%r) -- refusing to write, "
              "because rebuilding it from zeros is how settings get destroyed"
              % (member_id, exc))
        return {}
    if len(buf) < _SAVE_FILE_LEN:
        buf.extend(b"\x00" * (_SAVE_FILE_LEN - len(buf)))
    moved = {}
    for off, (width, value) in sorted(changes.items()):
        if not (0 <= off and off + width <= _SAVE_FILE_LEN):
            continue
        got = tmsave.write_field(buf, off, width, value)
        if got:
            moved[off] = got
    if not moved:
        return {}                            # already right; do not churn the file
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(buf)
        os.replace(tmp, path)                # atomic
    except OSError as exc:
        # The resources directory is root-owned on prod; a container that cannot
        # write it silently threw away every setting the player changed.
        common._say("tm: member %s save WRITE FAILED (%r) -- %d setting(s) lost. The "
              "client was told /Ans=1 and will not resend them."
              % (member_id, exc, len(moved)))
        return {}
    return moved


#: `@Opt=` IS THE PROFILE WRITE-BACK CHANNEL -- the general case of the guild
#: write above, and the thing that closes the "already linked to handle X, start
#: game?" prompt. Read off `TM.dll.unpacked`:
#:
#:   * the sender is `0x13F800` / `0x140ACF`, both `push 0x5245E8C ; call
#:     0x5018370`, and `0x5245E8C` is the base of the option-globals block that
#:     `apply_options_from_save` (`0x14D9F0`) fills STRAIGHT OUT OF THE SAVE.
#:     So the twelve dwords of that struct are the twelve save bytes below, and
#:     `@Opt=` is the client telling us which of them it just changed.
#:   * `0x140AAD` sends it **only when the value actually moved**
#:     (`if ([0x5245EB8] == [scene+0x150]) skip`), and `[0x5245EB8]` is the
#:     "skip handle linking from now on" checkbox on that very prompt. Tick the
#:     box -> `@Opt=/…/HNSS=1` -> this write -> the prompt stops drawing.
#:
#: The client CLAMPS three of these on load, so a value we cannot make sense of
#: is not dangerous: `+0x100`/`+0x101` are masked `& 1`, `+0x102` folds anything
#: above 0x14 to 8, and `+0x103` outside 0x1E..0x46 becomes 0x3C. Store what the
#: client sent; it will normalise its own value back.
#:
#: WARNING: MEASURED: the offsets, the order, and which global each field reads.
#:
#: VERIFIED: AND SIX OF THE MEANINGS ARE NOW MEASURED TOO (2026-08-20), from the client's
#: own Options screen under its `Default` button plus a live confirmation -- three
#: bytes served, three values read back off the screen:
#:
#:     /Se=  +0x0B3   SE Volume              1=Low 2=Medium 3=High
#:     /Bgm= +0x0B4   BGM Volume             (same list; default High)
#:     /CMD= +0x100   Auto Member Display    boolean -- the `& 1` mask
#:     /CAD= +0x101   Auto Chat Display      boolean
#:     /CL=  +0x102   Chat Window Size       LINES; >0x14 folds to 8
#:     /CT=  +0x103   Chat Window Transp.    PERCENT; outside 0x1E..0x46 -> 0x3C
#:
#: The clamps above are what made this readable: a marker pass wrote 147 into
#: +0x102 and the screen said "8", which is the fold, and that is how `CL` was
#: identified. See `tmsave.DEFAULT_HEADER`, which now carries the values a MINTED
#: save gets -- a zero header is the bug this whole class of "settings ignore
#: their defaults" reports comes from.
#:
#: STILL UNMEASURED: `/Ar=` (+0x0B0, Card Placement -> "Normal"), `/Cu=` (+0x0B1),
#: `/Vi=` (+0x0B2), `/Per=` (+0x0B5), `/Ran=` (+0x0B6, Rankings Display ->
#: "Display name"). The LABELS are known from the Default screen; the index each
#: list uses for them is not. `/HNSS=` has its meaning from its call site.
SAVE_OFF_HNSS = 0x126
OPT_FIELD_OFFSETS = (
    (b"Ar",   0x0B0),      # struct +0x00
    (b"Cu",   0x0B1),      # struct +0x04
    (b"Se",   0x0B3),      # struct +0x08
    (b"Bgm",  0x0B4),      # struct +0x0C
    (b"Per",  0x0B5),      # struct +0x10
    (b"Ran",  0x0B6),      # struct +0x14
    (b"Vi",   0x0B2),      # struct +0x18  (out of offset order on purpose)
    (b"CMD",  0x100),      # struct +0x1C
    (b"CAD",  0x101),      # struct +0x20
    (b"CL",   0x102),      # struct +0x24
    (b"CT",   0x103),      # struct +0x28
    (b"HNSS", SAVE_OFF_HNSS),   # struct +0x2C
)


def _opt_enabled():
    return os.environ.get("POL_TM_OPT", "1") == "1"


def _opt_persist_enabled():
    return os.environ.get("POL_TM_OPT_PERSIST", "1") == "1"


def _opt_ans():
    """`/Ans=` for `@OptAns=`. Must be > 0.

    The poll at `0x889B0` is byte-for-byte `@InitAns`'s: look up code 133, find
    the key `@OptAns`, return `/Ans=`. Its caller `0x13F83A` does `cmp edi,0 ;
    jg <success>` -- so 0 reads as "still pending" and the scene keeps polling,
    and negative raises the error dialog. 1 is the least-committal positive.
    """
    try:
        return int(os.environ.get("POL_TM_OPT_ANS", "1"), 0)
    except ValueError:
        return 1


def _opt_fields(cmd):
    """{save offset: value} for every `@Opt=` field present in `cmd`.

    Values may be NEGATIVE on the wire -- the builder emits a `-` and negates
    (`0x18933`) rather than printing an unsigned -- so the sign is parsed and
    then truncated to the byte the client will read back.
    """
    out = {}
    for name, off in OPT_FIELD_OFFSETS:
        m = re.search(rb"/%s=(-?\d+)" % name, cmd)
        if m:
            out[off] = int(m.group(1)) & 0xFF
    return out
