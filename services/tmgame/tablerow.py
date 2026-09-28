"""A table's published row: owner and owner changes, occupancy, the settings block and republishing.
"""
import os
from . import common, matchmaking, matchstart, protocol, pushqueue, seating, vscom


def _confirmed_flag(tmroom, chan, index):
    """Has this table's owner pressed rules-Confirm? Process set first, shared
    flag second -- the same two sources `_announce_match`'s gate reads, so the
    served STATE and the match hold can never disagree about the phase."""
    if (chan, index) in seating._TABLE_CONFIRMED:
        return True
    try:
        return bool(tmroom.table_confirmed(chan, index))
    except Exception:
        return False


def _table_owner(seats):
    """The member whose id the tile is drawing, i.e. the table's owner, or None.

    The SAME PICK AS `_occupancy_block`, on purpose. That function writes the
    first non-zero id in seat order into `block[14..29]` and the client compares
    it against its own guid; if "the owner" here were computed any other way the
    two could disagree, and a disagreement about who owns a table is invisible
    until somebody is stuck in front of it.
    """
    for mid, ident in seats or ():
        if ident:
            return mid
    return None


def _owner_changed(before, after):
    """The member who owned this table and no longer does, or None.

    THE OWNER IS SEAT 0, BY THE SAME RULE THE TILE DRAWS BY. `_occupancy_block`
    publishes the FIRST seat with a non-zero id and nothing else, so ownership is
    not a separate field we could disagree with, and the transfer itself is
    already implicit in any republish. What is never implicit is TELLING the
    people still sitting there -- they are in a scene polling for a message, not
    re-reading a tile.

    None covers all three no-op cases in one test, and each of them matters:
    the owner did not move; nobody is left to inherit (the table simply empties
    and goes back to its authored state); or the heir carried no `/NN=`, which
    `_occupancy_block` already refuses to fabricate an id for -- so there would
    be nothing to put in the message either.
    """
    was, now = _table_owner(before), _table_owner(after)
    return was if (now is not None and was is not None and was != now) else None


def _push_owner_change(tmroom, chan, index, left, seats):
    """Tell whoever is still at table `index` that they have inherited it.

    `@GameOwner` IS AN OWNERSHIP TRANSFER, AND THIS IS THE CASE IT WAS MEASURED
    FOR. See `OWNER_PUSH_CODE`: pushed on a match by mistake, the client rendered
    "Since <blank> has cancelled their game you are now the owner" -- which is
    wrong for a match and exactly right here. It is code 0x1A, and the
    reservation scene the survivors are sitting in polls for it itself
    (`0x84D69: 0x825A0(buf, 0x1A, -1)`), so unlike most of this family there is
    no question about whether anything is listening.

    `/CN=` is the DISPLAY NAME OF THE PLAYER WHO LEFT -- the blank in that
    measured line was our own empty `/CN=`, which is how the field was
    identified. So it is filled from the roster here rather than left empty, and
    a member we hold no name for degrades to the blank the message already
    survives.

    `/NN=` IS THE NEW OWNER'S TETRA MASTER ID, NOT THE LOBBY-BAND GUID -- the
    same 64-bit id `@GameEN=/NN=` carried and `_occupancy_block` writes into the
    tile, which is the one the receiving client compares against itself (see the
    three-ids banner in `tmroom`). Taking it from the seat list is what keeps
    those two the same number by construction.

    Default ON, unlike `POL_TM_OWNER_PUSH` (which gates the match-announce misuse
    and stays off): a switch that has always shipped off is untested code, and
    this is the one place the message is known to be correct.
    `POL_TM_OWNER_HANDOFF=0` restores the silence.
    """
    # WARNING: THE CONFIRM BELONGS TO THE PERSON WHO PRESSED IT (live testing,
    # 2026-08-21 ~13:5xZ: owner left, the heir inherited a state-2 "Playing"
    # table with Observe-only menus on every client -- the departed owner's
    # confirm kept the recruiting state alive with nobody entitled to it).
    # Clearing it here, in the ONE helper every hands-changed path calls,
    # drops the row back to state 1: the heir sees my-table, opens the rules,
    # and confirms for THEMSELVES -- the same rule _republish_table already
    # applies when a table empties. The republish below re-derives the state
    # AFTER the clear, so the flip rides the same TD as the handoff.
    seating._TABLE_CONFIRMED.discard((chan, index))
    try:
        tmroom.clear_table_confirmed(chan, index)
    except Exception:
        pass
    _republish_table(tmroom, chan, index, seats)
    if not common._env_int("POL_TM_OWNER_HANDOFF", 1):
        common._say("tm:   POL_TM_OWNER_HANDOFF=0 -- table %d changed hands and nobody "
             "is being told" % index)
        return
    heir = _table_owner(seats)
    ident = next((i for m, i in seats if m == heir), 0)
    name = (tmroom.name_of(left) or "").strip()
    # WARNING: `/CN=` AND `/QN=` ARE `:str` FIELDS -- HEX-ENCODED ASCII. The arm
    # (0x84DB8 / 0x84DEF, read 2026-09-07) decodes both with 0xAAD60, the
    # same hex-pair reader as @VsGameInit's `/CN=`; a raw name decodes to
    # garbage and drew the "<blank> has cancelled" notice.
    body = (protocol.encode_code(matchmaking.OWNER_PUSH_CODE)
            + b"@GameOwner=/NN=%016X/CN=%s/QN=%s"
            % (ident & 0xFFFFFFFFFFFFFFFF, matchstart._str_field(name),
               matchstart._str_field(os.environ.get("POL_TM_OWNER_QN", ""))))
    common._say("tm: %s table %d CHANGED HANDS -- member %s (%r) left, member %s "
         "inherits it (/NN=%016X). Pushing @GameOwner to %s."
         % (chan, index, left, name or "?", heir, ident & 0xFFFFFFFFFFFFFFFF,
            ", ".join(str(m) for m, _i in seats)))
    for mid, _ident in seats:
        pushqueue._queue_push(mid, body, "table %d is theirs now" % index,
                    after=common._env_int("POL_TM_OWNER_HANDOFF_GAP", 0))


def _republish_table(tmroom, chan, index, who, why="released"):
    """Push one table's row after its seat list changed underneath us.

    Used when seating somewhere ELSE releases a seat here -- without it the
    vacated table keeps whatever it was last published with, which is the stuck
    "still reserved" row from the live report. Also the row refresh the
    match-start/match-end paths ride (`why` keeps the log honest about which).
    """
    try:
        row, authored = _table_rows(tmroom, chan, index)
        if row is None:
            return
        # A VS. COM game occupies its table without a lobby SEAT, so an empty
        # seat list is not an empty table -- see `_COM_AT`. Substituted HERE
        # rather than in the callers so that every republish path keeps the
        # tile, including one triggered by somebody else's seat change
        # elsewhere in the room.
        who = who or vscom._com_who(chan, index)
        cap = int(row[4] or 0)
        row[5] = str(min(len(who), cap) if cap else len(who))
        row[6] = _occupancy_block(row[6], who)
        row[3] = _occupancy_state(who, (authored or row)[3],
                                  confirmed=_confirmed_flag(tmroom, chan, index),
                                  inplay=True if matchmaking._match_running(chan, index)
                                  else None)
        if not who:
            # The next owner must confirm for THEMSELVES -- a confirm inherited
            # from the previous owner announces their guests into an open
            # settings dialog, which is the @GameNG= strand the gate prevents.
            seating._TABLE_CONFIRMED.discard((chan, index))
            # ...and an emptied table's match is OVER, whatever the exit path
            # was (clean @GameExit=, crash, TTL reap). Without this the
            # process-local flag would republish the NEXT group's reservation
            # as "Playing". _occupancy_state already served the authored state
            # above (who is empty), so this cannot change THIS row.
            matchmaking._MATCH_STARTED.pop((chan, index), None)
            matchmaking._MATCH_BEGAN.pop((chan, index), None)
            try:
                tmroom.clear_table_confirmed(chan, index)
            except Exception:
                pass
        if tmroom.note_table(chan, row):
            common._say("tm:   %s table %d %s -> %s seated"
                 % (chan, index, why, row[5]))
    except Exception as exc:
        common._say("tm:   table %d not %s (%r)" % (index, why, exc))


def _table_rows(tmroom, chan, index):
    """`(base, authored)` for one table -- the row to EDIT, and the row to fall
    back to.

    WARNING: EDITING THE FIXTURE IS WHY A RESERVATION VANISHED. Measured live
    2026-08-20: `@GameEN=` seated the player at sequence 3 (state 1 + the id, and
    the other player DID see "Setting Up"), then the `@Tet=`/`@Tab=` pair arrived
    and this module rebuilt the row from `fixture_table()` -- the AUTHORED row --
    publishing state 7 and id 0 at sequence 5. Our own settings handler wiped our
    own occupancy, which is exactly "once it's done, it goes back to showing as
    empty for the other person".

    So `base` is the LIVE row when one exists. `authored` is still returned
    because the state has to be restorable: an empty table must go back to what
    the fixture says, not stay on whatever the last occupant set.
    """
    authored = tmroom.fixture_table(index)
    live = dict(tmroom.tables(chan)).get("#TM0T%03d" % index)
    return (list(live) if live else (list(authored) if authored else None),
            authored)


def _occupancy_state(who, authored, confirmed=False, inplay=None):
    """`+0x14` for a table with `who` at it.

    WARNING: STATE 1 IS A REASONED PICK, NOT A MEASUREMENT, and it is the only part of
    this that is. It is the one arm in the 0x674C4 switch that READS the block's
    id (0x67265), so it is the only state under which an occupancy id can render
    at all; states 3/4/5 return the empty code 2 no matter what we write. The
    authored state is restored the moment the table empties, so a wrong guess
    costs one screen and not a stuck table. `POL_TM_TABLE_STATE` overrides.

    WARNING: THE PHASE MODEL, RESTORED 2026-08-21 (live testing caught the regression):
    a two-part disassembly reconciles the tile arm and the menu arm --

      * TILE arm 0x674C4: state 1's OWNER branch (0x672E4) reads the reservation
        count [0x51def2c] (= screenobj+0x108) -- 0 -> **code 2 (LOCKED)**; state
        2's arm (0x67344) reads the seat COUNT directly -> (n<<4)|3 = 0x23, a
        CLICKABLE two-pawns tile with the my-table frame. So an owner sitting on
        a state-1 table with an empty reservation slot is LOCKED OUT; on state 2
        they are not.
      * MENU arm 0x45ABC: states 1/3/4/5 -> VAR2 (JOIN); state 2/6 -> VAR4 (no
        join). The OWNER gets VAR1 (Start Game) at any state.

    Reconciled phase model, which is the flow that worked live (confirm->state 2,
    7b740142) before it was wrongly reverted (27a9a470):
      * occupied, NOT confirmed -> **state 1** (recruiting): foreign players get
        the JOIN menu, the owner is in the reserve/rules dialogs (not looking at
        the tile), so the state-1 owner-tile lock never bites.
      * occupied, CONFIRMED     -> **state 2** (ready): the owner's tile is the
        clickable two-pawns (reads the count, NOT +0x108) and Start Game shows
        via VAR1; further joins close (the owner confirms once everyone is in).
      * The owner confirms LAST, so 3-player works by joining BEFORE confirm.

    `POL_TM_TABLE_STATE` overrides the recruiting value; `POL_TM_TABLE_STATE_
    CONFIRMED` (default 2) the confirmed one. Start Game still ungreys only when
    the reservation slot fills (gate 0x5C0D0) -- state 2 makes the tile
    reachable; +0x108 makes the button pressable.
    """
    if not who:
        # THE AUTHORED VALUE, VERBATIM. The clamp that stood here is RETRACTED
        # with 6151fa3c (see tmroom.EMPTY_TABLE_STATE): authored 7 IS the
        # client's deterministic empty tile -- display code 0 is pre-stored and
        # `state > 6` skips the only `mov ecx, 2` -- while clamping to 0 handed
        # free tables the state-0 arm's `cmp eax, ebp`, a render that varies
        # with client state. Measured 22:18: the heal TD flipped 7 -> 0 and
        # "Setting up" appeared on a free table moments later.
        try:
            return str(int(os.environ.get("POL_TM_TABLE_EMPTY_STATE",
                                          str(authored)), 0) & 0xFF)
        except (TypeError, ValueError):
            return str(authored)
    # VERIFIED: CONFIRMED -> STAY AT STATE 1 (default flipped 2->1, 2026-08-22). The
    # state-2 flip was a WORKAROUND for +0x108 never filling: at state 1 the
    # owner tile arm 0x672E4 reads +0x108 and LOCKS when it is 0, so state 2 (arm
    # reads the seat count instead) was used to make the tile clickable after
    # confirm. But state 2 gives foreign players VAR4 = NO JOIN -- the guest
    # lockout a tester hit. Now that +0x108 FILLS (proven live 2026-08-22 via
    # tm_store_probe: the msgid-4 re-feed drains into +0x108 once +0x41c arms),
    # the workaround is obsolete: state 1 unlocks the owner tile from +0x108 AND
    # keeps joins open (VAR2), so a guest can still seat and drive +0x108 to >=2,
    # which is what ungreys Start Game (gate 0x5C0D0 needs >=2). Set
    # POL_TM_TABLE_STATE_CONFIRMED=2 to restore the old flip. inplay (>=3 seated,
    # OR a running match -- callers pass _match_running(), set on @GameReady= and
    # cleared on the last @GameExit=) goes to state 2 -- a table mid-game shows
    # "Playing"/Observe and joins close.
    if inplay is None:
        inplay = len(who) >= common._env_int("POL_TM_INPLAY_AT", 3)
    if inplay:
        return "2"
    if confirmed:
        try:
            return str(int(os.environ.get("POL_TM_TABLE_STATE_CONFIRMED",
                                          "1"), 0) & 0xFF)
        except ValueError:
            return "1"
    try:
        return str(int(os.environ.get("POL_TM_TABLE_STATE", "1"), 0) & 0xFF)
    except ValueError:
        return "1"


def _occupancy_block(current, who):
    """block[14..29] := the occupant's 64-bit id, or zeros when nobody is there.

    THE ORDER IS MEASURED, not assumed: `letters(value, 16)`, most significant
    nibble first, proved against a verbatim model of the client's own four-group
    unpack at 0x66DBB over 2,004 values. See `tools/tmptl.py`'s banner.

    WARNING: ONE id, one field. Sixteen nibbles is exactly 64 bits; there is no second
    slot here for a second player, whatever a table seats.
    """
    import tmroom                       # services/, so it is always mounted
    block = seating._block_fit(current, 30).encode("latin1", "replace")
    ident = 0
    for _mid, i in who:
        if i:
            ident = i
            break
    if not who:
        out = bytearray(block)
        out[4:6] = tmroom.letters(0, 2)
        out[14:30] = tmroom.letters(0, 16)
        return bytes(out).decode("latin1")
    if not ident:
        # The client compares this against ITS OWN guid. A member we hold no
        # guid for would get a fabricated one, which is the "an id fed to a
        # consumer is worse than a blank tile" case -- so leave it empty and say
        # so, rather than publish a number nobody will ever match.
        common._say("tm:   ...this message carried no /NN= -- occupancy id left EMPTY "
             "(a wrong id is worse than none)")
        return current
    out = bytearray(block)
    out[14:30] = tmroom.letters(ident & 0xFFFFFFFFFFFFFFFF, 16)
    # VERIFIED: block[4..5] IS THE COUNT, AND IT GATES EVERY ARM OF THE SWITCH.
    # Measured 2026-08-20 at 0x671DD after live testing reported the tile still
    # said "Setting Up" with occupancy published:
    #
    #     mov dl, [esi-0x22]      ; block[4]
    #     mov cl, [esi-0x21]      ; block[5]
    #     shl eax, 4 ; add eax, ecx
    #     -> eax = ((block[4]-'A') << 4) + (block[5]-'A')
    #
    # and every arm opens by testing it: `cmp eax, ebp; je 0x67361` -> display
    # code 2, which is the code states 3/4/5 give unconditionally and is what
    # renders "Setting Up". We author 'AA' = 0, so the occupancy branch fell
    # through to the default however good the id was. With the count present,
    # state 1 + a foreign id reaches 0x6730D: 1 -> code 0x11, n -> (n << 4) | 5.
    #
    # WARNING: SAFE TO WRITE ONLY [4..5]. `tools/tmptl.py` warns that a table's label
    # writes [4..13] and that **[6..13] is the PASSWORD field** -- readable text
    # there sets a password nobody can type. The count is the two bytes before
    # it and nothing else moves.
    out[4:6] = tmroom.letters(min(len(who), 0xFF), 2)
    return bytes(out).decode("latin1")


def _settings_block(fields, current):
    """The 64-byte `+0x28` block with the fields we can PLACE written in.

    WARNING: ONLY `lu`/`ll` MOVE, AND THAT IS DELIBERATE. An earlier wire reading
    established that `au`/`al` -- the band that actually decides whether a
    reservation is accepted -- have NO home in `block[30..48]`: its only
    rank-shaped field is `block[38]`, bounded 0..8 by `0x6708C`, which cannot
    hold 100 or 300. The instruction is to settle where they live BEFORE
    authoring, so everything else is carried in the stored set and left out of
    the blob rather than written to a plausible-looking wrong offset.
    """
    import tmroom                       # NOT tools/tmptl -- see its banner
    block = seating._block_fit(current, 49).encode("latin1", "replace")
    out_pre = bytearray(block)
    # VERIFIED: THE WAGER, 2026-08-22 -- and it is the whole of "the other player sees
    # Wager 0". THE VIEW PANEL DOES NOT READ OUR 0x13 AT ALL. Measured live with
    # `tm_store_probe` while that panel was OPEN: every `0x521ef78[i]+0x30` was
    # 0 and our pushed 0x13 was still sitting unread in the store. The panel is
    # the loop at `0x66D10`, which walks the TABLE RECORD (`0x521BB70 + 0x4E` =
    # `block[0x26]`, `add esi, 0x68` per row) -- so everything it draws comes
    # from THIS block and nothing else. `0x13`/`@Rule=` feeds the OWNER's config
    # panel; they are two scenes over two different channels, and three fixes to
    # the push channel could never have moved this screen.
    # `tools/tmptl.py` decoded the block on 2026-08-17: the wager is
    # `block[30..33]`, masked to 16 bits by `0x66FCC`. We have never written it,
    # so it read as the authored padding -- 0. (Historically it read 48047, the
    # NUL-padding artefact that banner explains; 0 is the same hole, one
    # generation later.)
    bet = fields.get("bm")
    if bet is not None:
        try:
            bet = int(bet)
        except (TypeError, ValueError):
            bet = None
    if bet is not None and 0 <= bet <= 0xFFFF:
        out_pre[30:34] = tmroom.letters(bet, 4)
    elif bet is not None:
        common._say("tm:   ...wager %s does not fit the 16 bits 0x66FCC masks it to; "
             "left as authored" % bet)
    # THE TIME LIMIT, `block[35]`, an index into `tmptl.TIME_LIMITS`
    # ("0:15" "0:30" "1:00" "3:00" "5:00" "10:00"); `0x6701F` reads anything
    # above 4 as 5. Shipped on the same evidence that carried `bm`, which is
    # now confirmed live: the NAME, the observed RANGE (every `@Tet=` ever
    # captured carries tl=0, 3 or 5 -- inside 0..5 and nothing else), and the
    # fact that it is the only index-shaped field in the message against the
    # only index-shaped slot in the block. Out of range is left alone rather
    # than clamped, so a wrong reading shows up as "unchanged" rather than as a
    # plausible wrong time.
    lim = fields.get("tl")
    if lim is not None:
        try:
            lim = int(lim)
        except (TypeError, ValueError):
            lim = None
    if lim is not None and 0 <= lim <= 5:
        out_pre[35:36] = tmroom.letters(lim, 1)
    elif lim is not None:
        common._say("tm:   ...time limit %s is not an index into TIME_LIMITS (0..5); "
             "left as authored" % lim)
    # THE FOUR RULE BITS, `block[34]` -- MEASURED ONE TOGGLE AT A TIME against
    # the wire with a tester narrating each control (2026-08-22), which is
    # the only reason this is right: the message order (`bm du st cb ca gs tl`)
    # is NOT the bit order, so anything positional would have shuffled three of
    # the four. `tmptl.RULE_BITS` is the bit order and the NAMES line up with
    # the field names once you stop reading the message left to right:
    #     bit 0  double_up       <- du   ("Double Up" on, du 0 -> 1)
    #     bit 1  chance_block    <- cb   ("Chance Blocks" off, cb 1 -> 0)
    #     bit 2  special_tile    <- st   ("Special Tiles" off, st 1 -> 0)
    #     bit 3  rotating_block  <- ca   ("Rotating Blocks" off, ca 1 -> 0)
    # WARNING: AND `gs` IS NOT A RULE BIT. Four toggles accounted for all four bits
    # without it, so the fifth boolean belongs to something else and writing it
    # into this byte would corrupt a rule. It stays unmapped until measured.
    _RULE_FIELD_BIT = (("du", 0), ("cb", 1), ("st", 2), ("ca", 3))
    have = [(k, b) for k, b in _RULE_FIELD_BIT if fields.get(k) is not None]
    if have:
        rules = 0
        for key, bit in _RULE_FIELD_BIT:
            try:
                if int(fields.get(key, 0)):
                    rules |= 1 << bit
            except (TypeError, ValueError):
                pass
        out_pre[34:35] = tmroom.letters(rules & 0x0F, 1)

    # QUITTING, `block[36]` -- and `gs` is the field, which reads like a rule
    # and is not one. Measured at BOTH ENDS of the scale (2026-08-22): the
    # tester set "Substitute" and `gs` went 1 -> 0; set "Loss" and it went
    # 0 -> 2. `tmptl.QUITTING` is ["Substitute", "Invalid", "Loss"], so index 0
    # and index 2 are pinned by measurement and 1 is the only slot left.
    # WARNING: STORED ONE-BASED. `0x67053` does `(v - 1) & 3`, so a stored 0 reads as
    # "Loss" -- writing the raw index would put every table one step wrong AND
    # make "Substitute" read as "Loss", the worst possible off-by-one here.
    quit_v = fields.get("gs")
    if quit_v is not None:
        try:
            quit_v = int(quit_v)
        except (TypeError, ValueError):
            quit_v = None
    if quit_v is not None and 0 <= quit_v <= 2:
        out_pre[36:37] = tmroom.letters(quit_v + 1, 1)
    elif quit_v is not None:
        common._say("tm:   ...quitting %s is not an index into QUITTING (0..2); left "
             "as authored" % quit_v)

    # PASSWORD, `block[37]` bit 2. Measured 2026-08-22: the tester turned
    # Password on and `pa` went 0 -> 1 (with the secret itself arriving
    # separately as `pw`). `block[37]` is `(password << 2) | (observe & 3)`, so
    # ONLY BIT 2 IS TOUCHED -- the observe half stays as authored because `in`
    # has never been seen at anything but 0 and is still unmeasured. Writing the
    # whole byte would blank an Observe setting we cannot yet read.
    pw_on, obs = fields.get("pa"), fields.get("in")
    if pw_on is not None or obs is not None:
        try:
            cur37 = (ord(block[37:38]) - 0x41) & 0x0F if len(block) > 37 else 0
        except (TypeError, ValueError):
            cur37 = 0
        # OBSERVE, bits 0-1. Measured: "No Comments Allowed" moved `in` 0 -> 2,
        # and `0x66F7B` reads 3 as 0, so the field is 0..2 and maps straight
        # across. Each half is written ONLY if its field is present, so a
        # message carrying one and not the other cannot blank the other.
        if obs is not None:
            try:
                v = int(obs)
            except (TypeError, ValueError):
                v = None
            if v is not None and 0 <= v <= 2:
                cur37 = (cur37 & ~0x03) | v
            elif v is not None:
                common._say("tm:   ...observe %s is out of 0..2 (0x66F7B reads 3 as 0)"
                     "; left as authored" % obs)
        if pw_on is not None:
            cur37 &= ~0x04
            try:
                if int(pw_on or 0):
                    cur37 |= 0x04
            except (TypeError, ValueError):
                pass
        out_pre[37:38] = tmroom.letters(cur37 & 0x0F, 1)

    # VERIFIED: THE COMMENT, `block[38]` -- the "[38] is rank / co has no home" reading
    # is RETRACTED, this time by opening the consumer instead of fitting ranges
    # (2026-08-22, static, TM.dll.unpacked). The earlier field map called [38] "a 0..8
    # field, Average Rank's shape" because 0x6708C clamps it -- but the clamp's
    # consumer is the COMMENT line of the table-info dialog (ctor 0x601B0,
    # labels 'Master' 441 / 'Comment' 451), read end to end:
    #
    #     0x66F5D  cx = (block[38]<<4) | block[37]        the pair build
    #     0x6708C  cmp dl, 8; ja -> 0                     row+0x40 = block[38]
    #     0x60561  id = u16[0x51B2F7C + row[0x40]*2]      the dialog's value
    #              -> Lobby.BIN 385..393 = 'No Comment', 'Tetra buddies
    #                 wanted!', ... 'Flexible rules!'     NINE presets = 0..8
    #
    # `co` is that index: the tester picked "Tetra buddies wanted!" (preset 1)
    # and `co` went 0 -> 1. The 0..8 bound IS the preset count, not a rank. We
    # never wrote [38], so every table drew index 0 = "No Comment" -- the
    # live report "comments don't persist". Out of range is left alone (the
    # client would clamp it to 0 anyway, but "unchanged" beats "silently No
    # Comment").
    com = fields.get("co")
    if com is not None:
        try:
            com = int(com)
        except (TypeError, ValueError):
            com = None
    if com is not None and 0 <= com <= 8:
        out_pre[38:39] = tmroom.letters(com, 1)
    elif com is not None:
        common._say("tm:   ...comment %s is not a preset index (0..8); left as "
             "authored" % com)
    # WARNING: `au`/`al` (the rank band, 300/100) STILL have no home in the block --
    # with [38] now taken by the comment there is no rank-shaped slot at all;
    # they are carried in the stored set only (the wire reading stands for them).
    block = bytes(out_pre)
    current = block.decode("latin1")
    lo, hi = fields.get("ll"), fields.get("lu")
    if lo is None or hi is None:
        return current
    if not (0 <= lo <= 0xFFFFF and 0 <= hi <= 0xFFFFF and lo <= hi):
        common._say("tm:   ...card level %s-%s is not a range this block can hold; "
             "left as authored" % (lo, hi))
        return current
    out = bytearray(block)
    # The ENCODER's order, which is `tmptl.table_settings`'s: card_max occupies
    # block[39..43] and card_min block[44..48]. The panel draws them the other
    # way round ("Card Level min - max"), which is the trap the earlier field map got
    # backwards once already -- so this follows the writer, not the screen.
    out[39:44] = tmroom.letters(hi, 5)
    out[44:49] = tmroom.letters(lo, 5)
    return bytes(out).decode("latin1")
