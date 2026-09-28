"""Table settings (@Tet=/@Tab=): parsing them, the owner's preset in the save, and each table's rule
set.
"""
import os
import re
from .deps import tmsave
from . import common, matchmaking, peers, protocol, reservation, savefile, seating, tablerow, vscom


#: The `@Tet=` and `@Tab=` field names, in the order the client writes them.
#: Measured off the wire, not off the binary -- these arrive as plaintext with
#: their own names, which is why they could be read at all.
#:
#: WARNING: ONLY `lu`/`ll` HAVE A HOME IN THE 104-BYTE RECORD, as `block[44..48]` and
#: `block[39..43]`. `au`/`al` do NOT -- checked, and `block[38]` is the only
#: rank-shaped field there and `0x6708C` bounds it to 0..8, so it cannot hold 100
#: or 300. That is the open question to settle BEFORE authoring: either
#: the band lives somewhere else in the record, or it only ever reaches the
#: client over `@Tab=` -- and `0x87220` being a PARSER says the message route
#: exists. Storing the whole set here costs nothing and is what either answer
#: needs.
TABLE_SETTING_KEYS = (b"bm", b"du", b"st", b"cb", b"ca", b"gs", b"tl",
                      b"in", b"lu", b"ll", b"au", b"al", b"co", b"pa")


#: WARNING: `pw` IS THE PASSWORD TEXT, NOT A NUMBER -- measured 2026-08-22. The
#: tester turned Password on and typed HIHIHI; the client sent
#: `/pa=1/pw=4849484948492A2A`, which is `H I H I H I * *` in hex, i.e. eight
#: characters padded with `*`. So `pa` is the on/off flag and `pw` is the
#: secret itself.
#:
#: WARNING: AND WE HAD BEEN DESTROYING IT TWO WAYS AT ONCE. `pw` was not in
#: `TABLE_SETTING_KEYS`, so it was never parsed and `table_info_body` emitted
#: the absent-key default -- every reply said `/pw=0`, i.e. "the password is 0".
#: Adding it to the numeric list would have been WORSE, not better: the
#: `(-?\d+)` capture stops at the first hex letter, so `4849484948492A2A` would
#: have been stored as 4849484948492 and echoed as that -- a plausible-looking
#: number that is a different password. It is parsed as TEXT and echoed
#: VERBATIM, which is also the only form that cannot be wrong: the client's own
#: characters go back to it unaltered.
TABLE_SETTING_TEXT_KEYS = (b"pw",)


def parse_table_settings(cmd):
    """`{name: int|str}` for every `@Tet=`/`@Tab=` field present, plus
    `Tbl`/`Game` when the carrier is `@Save=`. Absent keys are simply absent --
    a missing field and a zero one are different facts and the caller may care.

    `TABLE_SETTING_TEXT_KEYS` come back as STRINGS, verbatim."""
    out = {}
    for key in TABLE_SETTING_KEYS + (b"Tbl", b"Game"):
        m = re.search(b"/" + key + rb"=(-?\d+)", cmd)
        if m:
            out[key.decode("ascii")] = int(m.group(1))
    for key in TABLE_SETTING_TEXT_KEYS:
        m = re.search(b"/" + key + rb"=([0-9A-Fa-f]*)", cmd)
        if m:
            out[key.decode("ascii")] = m.group(1).decode("ascii")
    return out


def _preset_persist_enabled():
    return os.environ.get("POL_TM_SAVE_PRESET", "1") == "1"


def table_preset_fields(cmd):
    """`({offset: (width, value)}, which_half)` for one `@Save=` (code 0x24).

    VERIFIED: THE PERSISTENCE ROUTE, AND IT IS THE SAME ONE `@Opt=` ALREADY USES. See
    the `TABLE_PRESET` banner in `tmsave`: `apply_options_from_save` (`0x14D9F0`)
    seeds the settings dialog's two pages out of save +0xB8..+0xF0, and `@Save=`
    is the client handing back what the player just changed. Writing it there is
    the whole of "the table settings do not persist".

    WARNING: ONE HALF AT A TIME, AND THAT IS NOT AN OPTIMISATION. `@Save=` always
    carries all fourteen fields, but the dialog only fills in the half whose PAGE
    sent it; the other half is whatever the globals held, which on a zero save is
    zeros. Persisting the whole message would therefore blank the half the player
    was not looking at -- turning a bug that loses settings between sessions into
    one that loses them between PAGES.

    The discriminator is the builder's own two arguments (`0x87410` args 5 and 6,
    emitted as `/Tbl=` and `/Game=`, and it sends `@Save=` when EITHER is
    non-zero):

        `0x512ED`  passes Game=1  -> the RULES page      -> the `@Tet=` half
        `0x53674`  passes Tbl=1   -> the RESTRICTIONS pg -> the `@Tab=` half

    WARNING: A message naming NEITHER page is not a preset write at all -- `0x87410`
    would have built the bare code 0x14 pair for it. Return nothing rather than
    guess a half.
    """
    fields = parse_table_settings(cmd)
    if int(fields.get("Game", 0)):
        half, names = "@Tet= (the rules page)", tmsave.TABLE_PRESET_TET
    elif int(fields.get("Tbl", 0)):
        half, names = "@Tab= (the restrictions page)", tmsave.TABLE_PRESET_TAB
    else:
        return {}, None
    out = {}
    for name in names:
        if name.decode("ascii") not in fields:
            continue                # absent and zero are different facts
        off, width = tmsave.TABLE_PRESET_BY_NAME[name]
        out[off] = (width, fields[name.decode("ascii")])
    return out, half


def _persist_table_preset(cmd, member_id):
    """Write an `@Save=`'s half of the preset into the member's save file."""
    if not _preset_persist_enabled() or tmsave is None:
        return
    try:
        changes, half = table_preset_fields(cmd)
    except Exception as exc:
        common._say("tm:   ...preset parse failed (%r)" % (exc,))
        return
    if not changes:
        common._say("tm:   ...this @Save= names neither page (/Tbl=0 and /Game=0), so "
              "there is no half to store -- captured, not written")
        return
    moved = savefile._save_patch_fields(member_id, changes)
    if not moved:
        # NOT a lost write. The client echoes its whole half every time, so a
        # re-send of values already stored moves nothing -- exactly the
        # false-alarm `_save_patch_fields` documents for `@Opt=`.
        common._say("tm:   %s preset already matches the save -- nothing to write"
              % half)
        return
    common._say("tm:   %s preset STORED for member %s: %s" % (half, member_id,
         " ".join("+0x%03X %d->%d" % (o, a, b)
                  for o, (a, b) in sorted(moved.items()))))


def _note_table_settings(cmd, member_id, code=None, peer_nick=None):
    """Record one `@Tet=`/`@Tab=` set and publish the table it belongs to.

    Returns `(fields, index)`, so the selftest can assert on both.
    """
    fields = parse_table_settings(cmd)
    where = "code %#06x" % code if code is not None else "?"
    common._say("tm: table settings from member %s (%s, peer %s): %s"
         % (member_id, where, (peer_nick or b"-"),
            " ".join("%s=%s" % kv for kv in sorted(fields.items()))
            or "NO RECOGNISED FIELDS"))

    chan = room = None
    try:
        import tmroom
        chan = tmroom.room_of(member_id) if member_id is not None else None
        room = tmroom.room_peer(chan) if chan else None
    except Exception as exc:
        common._say("tm:   ...roster unavailable (%r)" % (exc,))
    # WARNING: `/Tbl=` IS NOT A TABLE NUMBER, SO IT IS NOT A HINT. Measured off
    # `0x87410`: the builder takes `/Tbl=` and `/Game=`
    # as its last two ARGUMENTS and emits `@Save=` when either is non-zero --
    # `0x512ED` passes Game=1 for the rules page and `0x53674` passes Tbl=1 for
    # the restrictions page. They name WHICH PAGE saved, nothing else.
    #
    # Feeding it in as `tbl_hint` made it beat the peer, which IS the table:
    #
    #     table -> UNRESOLVED  [peer 0xd0e4bcbb92 indexes to 2 but /Tbl= says 0
    #                           -- TAKING /Tbl=, the formula is what is inferred]
    #     ...table 0 is not in the shipped fixture -- nothing to update
    #
    # measured on prod 2026-08-20T19:41:47Z, from a real save at table 2. Table 0
    # does not exist, so every `@Save=` at a table above 0 resolved to nothing.
    # The peer nick is the table (and `_note_table_settings`'s own peer
    # arithmetic), and on the one carrier that has a `/Tbl=` at all it is the
    # page id -- so there is never a second opinion worth taking here.
    index, why = peers.table_index_for_peer(peer_nick, room, None, chan=chan)
    common._say("tm:   table -> %s  [%s]" % (index if index else "UNRESOLVED", why))
    if index is None or not chan:
        return fields, None

    # PUBLISH IT. This is the whole point: the row the other players in the room
    # read out of `b/g/PTL` is the one that has to move, and `note_table` both
    # writes it and queues the `TD` delta that tells a client already holding
    # the snapshot.
    #
    # WARNING: WHAT IS PUBLISHED IS THE SETTINGS BLOCK AND NOTHING ELSE. Seats and the
    # state byte are NOT touched here, because "the player sent the settings for
    # this table" and "the player is now sitting at it" are different facts and
    # only the first one is measured. `@GameEN=` is the reservation; when its
    # `/EN=` verdict is understood, that is where seating belongs.
    try:
        import tmroom
        row, _authored = tablerow._table_rows(tmroom, chan, index)
        if row is None:
            common._say("tm:   ...table %d is not in the shipped fixture -- nothing to "
                 "update" % index)
            return fields, index
        # WARNING: THE SETTINGS BLOCK ONLY. `row[3]` (the state) and the occupancy id
        # inside `row[6]` belong to whoever is seated, and `_settings_block`
        # writes only the card-level letters -- so publishing settings can no
        # longer un-seat anybody. See `_table_rows`.
        _table_rules_put(chan, index, fields)
        # WARNING: AND WHETHER THE OWNER HAS FINISHED. Only code 0x14 -- the bare
        # `@Tet=`/`@Tab=` pair the client sends on **Confirm** -- means "done".
        # 0x24 (`@Save=`) is the preset write and arrives repeatedly WHILE the
        # dialog is open. See `_announce_match` for what this gates.
        if code == protocol.TABLE_SETTINGS_CONFIRM:
            if (chan, index) not in seating._TABLE_CONFIRMED:
                common._say("tm:   %s table %d CONFIRMED by its owner -- a match may "
                     "start here now" % (chan, index))
            seating._TABLE_CONFIRMED.add((chan, index))
            # A FRESH GAME is being set up: clear the @GameReady= "started" set so
            # the coming start sequence's first @GameReady= (the host's) is treated
            # as the initiator. Confirm happens once, before any @GameReady=, and
            # unlike the seat path it does not fire on the PART churn that the
            # start sequence itself causes -- so the guard survives that window.
            matchmaking._MATCH_STARTED.pop((chan, index), None)
            matchmaking._MATCH_BEGAN.pop((chan, index), None)
            try:
                tmroom.note_table_confirmed(chan, index)   # survives the deploy
            except Exception as exc:
                common._say("tm:   confirm not persisted (%r) -- a restart before the "
                     "match starts will re-brick this table" % (exc,))
        row[6] = tablerow._settings_block(fields, row[6])
        # THE CONFIRM FLIPS recruiting(1) -> ready(2) so the owner's tile
        # becomes the clickable two-pawns (reads the count, not +0x108) and
        # Start Game shows -- the flow that worked live before this was wrongly
        # reverted. Joins should be done by now; the owner confirms once
        # everyone is seated. See _occupancy_state.
        _seated_now = seating._seats_of(chan).get(index) or []
        _authored = tmroom.fixture_table(index)
        row[3] = tablerow._occupancy_state(_seated_now, (_authored or row)[3],
                                  confirmed=tablerow._confirmed_flag(tmroom, chan, index),
                                  inplay=True if matchmaking._match_running(chan, index)
                                  else None)
        matchmaking._push_table_info(tmroom, chan, index, why="the owner just set them",
                         skip=member_id)
        # ...and if the table filled up WHILE the dialog was open, the confirm
        # is the moment the match becomes startable. Without this the players
        # sit there: the seat that would have announced it has already been
        # taken and `@GameEN=` will not arrive again.
        if code == protocol.TABLE_SETTINGS_CONFIRM:
            seated = seating._seats_of(chan).get(index) or []
            if seated:
                # Refresh the reservation slot on confirm too -- the gate reads
                # the confirm-set greyed flag, so a re-push after confirm lets
                # the tile flip to the "start enabled" code.
                reservation._push_reservation_list(tmroom, chan, index, seated)
                matchmaking._announce_match(tmroom, chan, index, seated)
        if tmroom.note_table(chan, row):
            common._say("tm:   %s table %d published, sequence %d -- every client in "
                 "the room will see it" % (chan, index, tmroom.sequence(chan)))
    except Exception as exc:
        common._say("tm:   ...could not publish table %d (%r)" % (index, exc))
    return fields, index

#: (room, table index) -> the last settings set its owner sent us, so the 0x13
#: push is an ECHO of the owner's own values rather than anything we made up.
#:
#: WARNING: A CACHE NOW, NOT THE STORE. `tmroom.note_table_rules` is the store, because
#: this dict is per-PROCESS and the rules have to outlive a restart -- see
#: `_table_rules_put`. Kept because the selftest and any caller without a live
#: roster still need somewhere to put them.
_TABLE_SETTINGS = {}


def _table_rules_put(chan, index, fields):
    """Record a table's rule set, in shared state as well as in memory."""
    _TABLE_SETTINGS[(chan, index)] = dict(fields)
    try:
        import tmroom
        tmroom.note_table_rules(chan, index, fields)
    except Exception as exc:
        # NOT fatal: the in-memory copy still serves this process, which is
        # exactly the behaviour we had before. Say so, because a table whose
        # rules quietly stop surviving restarts is invisible until somebody is
        # stuck in front of it.
        common._say("tm:   ...could not persist table %s rules (%r) -- they will be "
              "lost on the next restart" % (index, exc))


def _com_rules_of(chan, index):
    """A VS. COM game's rules, as the player chose them on the COM screen.

    WARNING: A COM GAME HAS NO LOBBY TABLE, SO `_table_rules_get` ALWAYS MISSED IT
    (2026-09-26). The @ComGame= handler echoed the player's `/Rule=` back in
    `/R=` -- the client's rules screen was right -- but the board deal looked
    the rules up by (chan, index) = (None, push key), found nothing, and fell
    through to `TET_DEFAULTS`: st=1 cb=1 ca=1. A player asked for all three
    off (`/Rule=0|0|0|0|3|0|0|0`) and got a rotating block, two chance blocks
    and two special tiles. The handler now keeps the rules on the `_COM_GAME`
    entry (keyed by the same push key the deal uses) and this reads them.
    `POL_TM_COM_BOARD_RULES=0` restores the defaults-only deal.
    """
    if chan or index is None or not common._env_int("POL_TM_COM_BOARD_RULES", 1):
        return None
    return (vscom._COM_GAME.get(index) or {}).get("rules") or None


def _table_rules_get(chan, index):
    """A table's rule set: SHARED STATE FIRST, then this process's cache.

    WARNING: THE ORDER MATTERS. The shared copy is the one that survived the restart
    and the one the other container can also see; the in-memory dict is only
    ever a same-process shortcut. Reading memory first would make a freshly
    started container answer "no rules" for a table the file knows about --
    which is the bug this pair exists to fix.
    """
    if index is None or not chan:
        return None
    try:
        import tmroom
        got = tmroom.table_rules(chan, index)
        if got:
            return got
    except Exception:
        pass
    return _TABLE_SETTINGS.get((chan, index))
