"""Starting a game: card select, player names and game ids, @VsGameInit, @StartData and @TurnData.
"""
import os
import tmbattle
import re
from . import common, protocol, purse, pushqueue


#: (chan, table index) -> the set of members who have sent `@CardSelect=`.
#: See the `@CardSelect=` handler: the `/Ans=` vector has to be true, and this
#: is what makes it true.
_CARD_READY = {}


def _cardselect_body(seats, ready, msgid, me=0, n_solo=None):
    """`@CardSelect=/Ans=<per seat>/Ok=0`, addressed to (0x43, msgid).

    `/Ans=` occurrence i is ABSOLUTE seat i; arm 0x1030C9 rotates it to the
    display slot itself. 1 means that seat has chosen -- 0x10317E gives a seat
    whose byte goes 0 -> 1 the extra `[esi+0x19F+48k] = 5`, which is why 1 is
    the "has confirmed" value and not merely non-zero.

    `n_solo` is the VS. COM player count (`_com_n`); it only matters when
    `seats` is empty, where 2 used to be hardcoded and a 3-player COM game got
    a two-entry vector.
    """
    n = len(seats) or int(n_solo or 2)
    yes = common._env_int("POL_TM_CARDSELECT_ANS", 1)
    # WARNING: `/Ok=` IS "EVERYONE IS READY, GO" -- NOT A SUCCESS FLAG, AND NOT A
    # CONSTANT. Measured 2026-08-20T13:24Z: both clients read the answer
    # (`---->Recv=CARDSELECT`), both showed cards selected, and both then sat
    # for ever polling 7/35/36/40 and never asking for 8.
    #
    # The match scene is a state machine on `[esi+0x16F]` dispatched at 0xC2827
    # (`add eax, -0x17`, bound 8, table 0x5052F18), and the card-select state is
    # **0x17**:
    #
    #     0xC2835  0x101A10(7)
    #     0xC283A  cmp eax, 2        <- TWO, not 1
    #     0xC283D  jne 0xC2884       <- otherwise stay here for ever
    #     0xC287D  [esi+0x16F] = 0x18
    #
    # and 0x1031BC computes that return as **`/Ok=` + 1**. So `/Ok=0` returns 1,
    # which satisfies every `cmp eax, 1` caller but leaves THIS state parked.
    # `/Ok=1` returns 2 and releases it: 0x18 falls straight to 0x19, which
    # drains 7 (0xC28B5's `jg` loop) and then asks for **8 = @StartData**.
    #
    # So the value is a function of the vector, not a knob: 0 while anyone is
    # still choosing, 1 once they all have. `POL_TM_CARDSELECT_OK` forces it.
    forced = os.environ.get("POL_TM_CARDSELECT_OK")
    # WARNING: NO SEATS = A VS. COM GAME, AND IT IS READY THE MOMENT THE HUMAN PICKS.
    # `bool(seats)` made `everyone` False FOREVER for a COM game -- there are no
    # lobby seats to be ready -- so `/Ok=` stayed 0, 0x1031BC returned 1, and the
    # card-select state `[esi+0x16F]=0x17` parked exactly as this banner warns.
    # Live 2026-08-22: the board came up, the player chose five cards, and the
    # COM opponent "selected cards" for ever.
    #
    # This is the THIRD time a PvP seat gate has blocked the COM path -- the same
    # shape as `_push_muchmake` (seat count) and `_queue_vsgameinit` (`/N=` > 1).
    # When a COM feature stalls, look for a seat count before looking for a
    # missing message. The human's own submission is the whole quorum here: the
    # opponent is the server. `POL_TM_CARDSELECT_OK` still forces the field, and
    # `POL_TM_COM_CARDSELECT_OK=0` restores the old behaviour for an A/B.
    solo = not seats and common._env_int("POL_TM_COM_CARDSELECT_OK", 1)
    everyone = solo or (bool(seats)
                        and all(pushqueue._push_key(m) in ready for m, _i in seats))
    okv = common._env_int("POL_TM_CARDSELECT_OK", 0) if forced else (1 if everyone else 0)
    # WARNING: RECIPIENT FIRST, for the same reason the board is -- and this is what
    # "player 2 was pushed into selecting cards, the game just auto selected"
    # was. Measured 2026-08-20T11:47:33Z: m6 had just picked and we answered it
    #
    #     @CardSelect=/Ans=0/Ans=1
    #
    # because the vector was in TABLE order and m6 sits at table seat 1. The
    # client reads occurrence 0 and nothing else (see `_vsgame_body`), so it read
    # its OWN state as `0` -- not chosen -- immediately after choosing.
    if seats:
        vec = [yes if pushqueue._push_key(m) in ready else 0 for m, _i in seats]
        if common._env_int("POL_TM_VSGAME_SEAT0", 1) and me:
            vec = vec[me:] + vec[:me]
    else:
        vec = [yes] * n
    return (protocol.encode_code(protocol.IN_MATCH_CODE | ((msgid & 0xFF) << 16))
            + b"@CardSelect=/Ans=" + b"|".join(b"%d" % v for v in vec)
            + b"/Ok=%d" % okv)


#: *** EVERY `:str` FIELD IN THIS PROTOCOL IS HEX-ENCODED ASCII. ***
#:
#: Measured 2026-08-20 and confirmed byte for byte against a live client's own
#: trace. `0xAAD60` is the parser behind every field `tmcmds.py map` marks
#: `:str` -- `@VsGameInit`'s `/CN=`, the chat roster's `/CN=`, SHOPINIT's
#: `/SN=`, `@ResultData`'s `/RN=` -- and it is not a text extractor at all. Its
#: tail, 0xAAFDA..0xAB005, walks the value TWO CHARACTERS AT A TIME:
#:
#:     hi = c > '9' ? c - 0x37 : c - 0x30      ; 0xAAFE3
#:     lo = same for the next character        ; 0xAAFF4
#:     append (hi << 4) + lo                   ; 0xAB002, 0xAB03B
#:     esi += 2 ; edi += 2 ; while edi+1 <= end
#:
#: i.e. `4C6170746F70` -> `Laptop`. 0xAAF33's `start + 2 > end -> return empty`
#: is the "not even one byte" guard, and 0xAAF3E's `esi + 1` is the second
#: character of the first pair.
#:
#: WARNING: THE PROOF IS THE TESTER'S OWN SCREEN. Reported live: "the player names
#: seem to still be garbage text". We sent `/CN=<name1>|<name2>` in plain text -- correct
#: on the wire -- and the client's `%s` print of the parsed slots (rva 0x10271A)
#: emitted `7A CD B9 FE FD` and `9C FE FD`. Feed the two plain-text names to
#: the decoder above and you get exactly those bytes: 'L'(0x4C) - 0x37 = 0x15,
#: << 4 = 0x50, + 'a'(0x61) - 0x37 = 0x2A -> **0x7A**, which is the `z` the log
#: opens with. Not a hypothesis -- an identity.
#:
#: WARNING: UPPERCASE ONLY. The decoder's `c - 0x37` branch is taken for anything above
#: '9', so 'A'..'F' land on 10..15 and lowercase 'a'..'f' land on 0x2A..0x2F --
#: silently wrong, no error anywhere.
#:
#: WARNING: AND THE PROJECT HAD ALREADY CAPTURED THIS, in this file. The `@GameEN=`
#: banner records the client's OWN send as
#: `/CN=/HN=4C6170746F705465737432/L=1` -- `HN` is the same tester's name in exactly this
#: encoding -- and the `OWNER_PUSH_CODE` note says `@Init=` uses `/CN=` "for a
#: content id (hex ASCII)". The convention was on the wire in both directions
#: and only the ints had been re-derived.
#:
#: `POL_TM_HEXSTR=0` sends the raw text again, for an A/B without a rebuild.
def _str_field(value):
    """One `:str` value, in the encoding `0xAAD60` actually decodes."""
    if isinstance(value, str):
        try:
            raw = value.encode("cp932")
        except UnicodeEncodeError:
            raw = value.encode("cp932", "replace")
    else:
        raw = bytes(value or b"")
    if common._env_int("POL_TM_HEXSTR", 1) != 1:
        return raw
    return raw.hex().upper().encode("ascii")


def _com_player_name(char_index, seat):
    """The `/CN=` for a COM seat, HEX-ENCODED -- see `_str_field`.

    WARNING: UNMEASURED, AND DELIBERATELY GENERIC. The COM opponents' real names live
    in the client's own character-select data; nothing on the wire has ever
    carried one (`@ComGame=/Com=` is a list of INDEXES, and `@ComGameInit=`
    carries no names at all), so there is nothing to reproduce here -- only a
    watcher needs them, and a blank label is a blank label on the board. The
    pattern is a knob: `POL_TM_COM_NAME` takes one `%d`, the character index.
    If a capture ever shows the real names, that table replaces this.
    """
    pattern = os.environ.get("POL_TM_COM_NAME", "COM %d")
    try:
        name = (pattern % int(char_index)) if "%d" in pattern else pattern
    except (TypeError, ValueError):
        name = pattern.replace("%d", str(seat))
    return _str_field(name[:15])


def _vsgame_player_name(member_id, seat):
    """The `/CN=` for one seat, HEX-ENCODED -- see `_str_field`.

    Never empty: the client copies it into a 16-byte slot at rva
    0x2B6218 + i*0x10 and prints it with `%s`, so an empty one is a blank label
    on the board. The 15-character budget is on the DECODED text; the wire form
    is twice that.

    WARNING: A tester's screenshot of the first real match is the measurement that
    forced the encoding: the second player's label read a literal `z` followed
    by squares, and 0x7A `z` is precisely `('L'-0x37)<<4 + ('a'-0x37)` -- the
    client hex-decoding the plain-text name we had been sending."""
    name = ""
    try:
        import tmroom
        name = tmroom.name_of(member_id) or ""
    except Exception:
        name = ""
    name = name.strip()
    if not name:
        pattern = os.environ.get("POL_TM_VSGAME_NAME", "Player%d")
        name = (pattern % (seat + 1)) if "%d" in pattern else pattern
    return _str_field(name[:15])


#: member_id -> the GAME-ID that client compares `@VsGameInit`'s `/ID=` against.
#:
#: WARNING: THIS REPLACES A WORKAROUND BUILT ON A WRONG MEASUREMENT, and the client is
#: what corrected it. `_vsgame_ids` used to put a chosen value at the
#: recipient's own seat because `CalcPlayerData` printed `GAME-ID = 0` in one
#: 2026-08-19 log, so no real id was thought to exist. Measured 2026-08-20T11:22Z
#: with the trace on both machines, the guest answered:
#:
#:     [tm] +1024A9  ---->Recv=PLGAMEINIT     it read the board
#:     [tm] +10257E  ID=NOT_FOUND             and could not find itself
#:
#: and its own debug line one screen earlier says why:
#:
#:     [tm] +80E87  gameID = 1000001602
#:
#: 1000001602 is 0x3B9AD042 -- **exactly the `/GID=` that same client sends in
#: `@Init=`** (`/GID=000000003B9AD042`). So GAME-ID is the player's content id,
#: every client reports its own at login, and the two players' ids differ. The
#: zero was a stale reading of a field that had not been populated yet.
#:
#: So there is nothing to work around: send the REAL ids, the same list to
#: everyone, and each client finds itself at its own seat.
_GAME_IDS = {}


def _note_game_id(member_id, cmd):
    """Remember this member's `/GID=` off `@Init=`. Hex ASCII on the wire."""
    if member_id is None:
        return
    m = re.search(rb"/GID=([0-9A-Fa-f]{1,16})", cmd or b"")
    if not m:
        return
    try:
        gid = int(m.group(1), 16)
    except ValueError:
        return
    if _GAME_IDS.get(pushqueue._push_key(member_id)) == gid:
        return
    _GAME_IDS[pushqueue._push_key(member_id)] = gid
    common._say("tm: member %s GAME-ID = %d (0x%X) -- from @Init= /GID=. This is what "
         "@VsGameInit's /ID= is matched against (the client prints the same "
         "number as `gameID = %d`)." % (member_id, gid, gid, gid))


def _game_id_of(member_id):
    """This member's GAME-ID, or None if they have not sent `@Init=` here."""
    return _GAME_IDS.get(pushqueue._push_key(member_id))


def _note_pol_id(member_id, cmd):
    """Remember this member's `@Init=/NN=` -- Tetra Master's POL-ID -- verbatim.

    Hex ASCII on the wire and hex ASCII in the store: the client writes it with
    the 16-digit hex formatter at 0xACDA0 and reads it back with the accumulator
    at 0xAB72E, so echoing its own characters is the one representation that
    cannot get the nibble order wrong. `tmroom.note_pol_id` is where it lands
    and why; `_chat_roster_body` is what spends it.
    """
    if member_id is None:
        return
    m = re.search(rb"/NN=([0-9A-Fa-f]{1,16})", cmd or b"")
    if not m:
        return
    try:
        import tmroom
    except ImportError:
        return
    try:
        if tmroom.note_pol_id(member_id, m.group(1)):
            common._say("tm: member %s POL-ID = %s -- from @Init= /NN=. This is what "
                 "the chat roster's /NN= is matched against; a client skips the "
                 "row carrying its own (TM.dll 0x0AFE3)."
                 % (member_id, m.group(1).decode("ascii").upper()))
    except Exception as exc:
        common._say("tm: member %s POL-ID %r not stored (%r)"
             % (member_id, m.group(1), exc))


def _vsgame_ids(seats, me):
    """The `/ID=` list, one per seat, FROM THIS RECIPIENT'S POINT OF VIEW.

    WARNING: THE CLIENT LOOKS FOR ITS OWN ID AND NOTHING ELSE. 0x1024F9 walks the
    `/ID=` occurrences comparing each against the global at TM.dll rva
    **0x294BD4**; the first that matches is that player's seat index, and if none
    matches it prints `ID=NOT_FOUND` and returns -1, which is a refusal.
    Everything after is derived from that index -- the seat rotation at
    [obj+0x19B+48i], which `@No<i>=` section is read for money, which name goes
    on which side of the board.

    0x294BD4 is what `CalcPlayerData` prints as **`GAME-ID`** (0x818B1 loads the
    trio [0x294BD8]/[0x294BDC]/[0x294BD4]; 0x81930..0x8194E prints them as
    POL-ID / GAME-ID / HAND-ID), and in the only trace that carries the line --
    `logs/shim-FRAMEWORK.log`, twice -- it reads **0**. Nothing in the image
    writes it with an absolute store, so we have never assigned one and both
    clients are holding the same value.

    That is survivable because a push is PER MEMBER: each recipient gets its own
    copy of this message, so the recipient's own id goes at THEIR seat and a
    value they cannot match goes everywhere else. `POL_TM_VSGAME_SELFID` is what
    we believe their GAME-ID to be (0, measured) and `POL_TM_VSGAME_PEERID_BASE`
    is the first of the deliberately-not-that values.
    """
    # WARNING: NOT `/GID=`, AND THAT WAS A RETRACTION. `@VsGameInit` compares against
    # the global at rva **0x294BD4**, which `CalcPlayerData` prints as
    # `GAME-ID` and which measures **0**. The client ALSO prints
    # `gameID = 1000001602` at 0x80E82 -- but that reads [0x2B52A0/A4], the
    # pair that feeds `/GID=` in `@Init=`, a DIFFERENT variable. Sending the
    # content id put a number in the list that nothing compares against, and the
    # guest answered ID=NOT_FOUND with its own id sitting right there.
    self_id = common._env_int("POL_TM_VSGAME_SELFID", 0)
    if common._env_int("POL_TM_VSGAME_SEAT0", 1):
        # The recipient is seat 0 by construction, so its own value goes first
        # and every peer gets something it cannot match.
        return [self_id] + [self_id + 1 + i for i in range(len(seats) - 1)]
    real = [_game_id_of(m) for m, _i in seats]
    if all(v is not None for v in real):
        return real
    # FALLBACK, and it should never be needed once every player has sent
    # `@Init=` on this connection. Kept because a board built from a guessed id
    # at least lets the RECIPIENT find itself, which is strictly better than a
    # message every client refuses -- and it logs, so a run that uses it says so.
    self_id = common._env_int("POL_TM_VSGAME_SELFID", 0)
    base = common._env_int("POL_TM_VSGAME_PEERID_BASE", 1)
    out = []
    for i, (mid, _ident) in enumerate(seats):
        known = real[i]
        if known is not None:
            out.append(known)
            continue
        if i == me:
            out.append(self_id)
            continue
        v = base + i
        if v in out or v == self_id:
            v = self_id + len(seats) + i + 1
        out.append(v)
    common._say("tm:   WARNING: no GAME-ID for seat(s) %s -- falling back to synthesised ids "
         "%r. That client never sent @Init= here, and it will answer "
         "ID=NOT_FOUND unless its own id is at its own seat."
         % (", ".join(str(i) for i, v in enumerate(real) if v is None), out))
    return out


def _vsgame_body(seats, me, rules):
    """`@VsGameInit=` for the player sitting at seat `me`.

    Field order and widths off arm 0x102476, in the order it parses them:

        /N=          -> [obj+0xE5] and [obj+0xC7]; [obj+0xE6] = N-2.
                        **Must be > 1** -- 0x1024D5 sends `cl <= 1` to 0x102943,
                        which prints `StartAloneERROR` and fails the scene.
        /ID= x N     -> see `_vsgame_ids`: the seat search, then the rotation.
        /CN= x N     -> the 16-byte name slots, in ABSOLUTE seat order (the
                        per-player record's name pointer is assigned from the
                        rotated index at 0x102701, not the other way round).
        /R=  x 7     -> the ruleset; see `VSGAME_RULE_KEYS`.
        @No<i>=      -> one section per ABSOLUTE seat i, matched by name
                        (sprintf "@No%d", 0x10282F). Inside it:
                          /RA= -> dword [obj+0x180+48k]   average rank
                          /M=  -> dword [obj+0xC8], **only when k == 0**
                                  (0x1028BE), i.e. only out of the recipient's
                                  OWN section -- money is private
                          /CP= -> word  [obj+0x17C+48k]   card points
                        where k = (i - me) mod N is the display slot.
    """
    # WARNING: THE RECIPIENT GOES FIRST, AND THE CLIENT CANNOT BE TOLD OTHERWISE.
    # Measured 2026-08-20 across three runs, and the rule is exceptionless:
    #
    #   host  11:22  /ID=0/ID=2                    its id at occurrence 0  -> ID=0
    #   guest 11:22  /ID=1/ID=0                    its id at occurrence 1  -> NOT_FOUND
    #   guest 11:37  /ID=1000000302/ID=1000001602  its id nowhere          -> NOT_FOUND
    #
    # Every success matched at occurrence 0; every failure needed a later one.
    # The reason is in the parser: `0xAB080`'s third argument is NOT an
    # occurrence index -- `0xAB098` reads it as a BYTE and builds a string from
    # it, and `0x51B3944` (the token it searches for) is `"="`. This is the
    # map-keyed-on-`name=` parser this file's header already describes, so a
    # REPEATED `/ID=` collapses: the loop at 0x1024F9 re-reads the same value
    # every iteration and can only ever match on the first.
    #
    # So the seat numbering is PER RECIPIENT: each client is player 0 in its own
    # copy, which is what a card game wants anyway -- you are always the near
    # side of the board. Everything downstream already agrees: the rotation
    # `[obj+0x19B+48i] = (i + me) % N` is the identity at me=0, and `@No<i>=`'s
    # display slot `k = (i - me) mod N` becomes i.
    #
    # `POL_TM_VSGAME_SEAT0=0` restores absolute seat order for an A/B.
    if common._env_int("POL_TM_VSGAME_SEAT0", 1) and me:
        seats = seats[me:] + seats[:me]
        me = 0
    n = len(seats)
    # VERIFIED: REPEATED VALUES ARE PIPE-SEPARATED UNDER ONE KEY, NOT A REPEATED KEY.
    # `0xAB080(name, buf, index, &out)`: after finding `name=` it runs the loop
    # at 0xAB15A -- `index` iterations of "find the next separator and step past
    # it" -- where the separator (0x51B2B78) is **`|`** and the value ends at the
    # next `/` or `@` (0x51B3950, a find-first-of set). So the third argument IS
    # an occurrence ordinal; it just indexes into a pipe list.
    #
    # We were emitting `/ID=0/ID=1`, so occurrence >= 1 had no `|` to step to and
    # every one of those reads failed. That single mistake produced all three
    # of the symptoms on screen, because none of the call sites checks the
    # return value before storing:
    #   * `/ID=`  -- only the first id readable  -> ID=NOT_FOUND for the guest
    #   * `/CN=`  -- 0x102635 SKIPS the copy on failure, leaving the 16-byte
    #                name slot holding whatever was there -> a row of squares
    #   * `/R=`   -- 0x10276C reads `[esp+0x20]` regardless of the result, so
    #                six of the seven rules came from stack garbage. One of them
    #                is the time limit, which is why a match nobody touched
    #                "auto-selected" the cards immediately.
    #
    # The client had been showing us the format the whole time: its own
    # `@CardSelect=/C=5@N0=/D=161|100|2|90|99|31|21|255` is eight `|`-separated
    # values under one key.
    body = b"@VsGameInit=/N=%d" % n
    body += b"/ID=" + b"|".join(b"%d" % v for v in _vsgame_ids(seats, me))
    body += b"/CN=" + b"|".join(_vsgame_player_name(m, i)
                                for i, (m, _ident) in enumerate(seats))
    body += b"/R=" + b"|".join(b"%d" % v for v in rules)
    for i, (mid, _ident) in enumerate(seats):
        # `/M=` only carries a real balance in the RECIPIENT's own section.
        # 0x1028BE reads it for `k == 0` and discards every other section's, so
        # putting another player's money on this wire would be data the client
        # cannot use and we have no evidence the game ever shares.
        body += b"@No%d=/RA=%d/M=%d/CP=%d" % (
            i, common._env_int("POL_TM_VSGAME_RA", 0),
            purse.money_of(mid) if i == me else 0,
            common._env_int("POL_TM_VSGAME_CP", 0))
    return body


#: How many `/F=` values `@StartData` carries, indexed by `[obj+0xE6]` (= N-2).
#: Read straight out of the image at rva 0x23359E: {16, 25, 233, ...}. For two
#: players that is **16** -- a 4x4 board, which is Tetra Master's. The third
#: entry is nonsense, so only the 2- and 3-player counts are real.
TILES_BY_PLAYERS = (16, 25)


def _startdata_body(seats, me, starter=0, n_solo=None, codes=None):
    """`@StartData=` -- the deal. Arm 0x1034E2, `Recv=STARTDATA`.

    WARNING: THIS IS WHAT THE SCENE IS WAITING FOR. Measured 2026-08-20T13:38Z: once
    `/Ok=1` released card select, both clients moved to state 0x19 and began
    polling **7 then 8** (`0xC28AE`'s drain, then `0xC28C8`'s `0x101A10(8)`).
    Command 8 is this message.

    Field for field, and note it is gated like every other in-match message on
    the session pair `@VsGameInit` established ([obj+0x28]/[obj+0x2C], 0x1034FD):

        /S=      -> 0x10354E: the STARTING PLAYER, run through the same seat
                    rotation as everything else (`(N - me + S) % N`) and stored
                    at [0x2B63C4]+0x1B0 and +0x178. Because our copies are
                    recipient-first (me = 0), this is already in the recipient's
                    own frame -- so it must be encoded PER RECIPIENT, or both
                    players believe they lead.
        /B=      -> byte [0x2B63C4]+0x174
        /E=      -> byte [0x2B63C4]+0x175
        /F= xM   -> M = TILES_BY_PLAYERS[N-2]; [0x2B63C4]+0x1F5+i. The BOARD:
                    one value per tile, 16 of them for two players.
        /L= xN   -> per player, [obj+0x1A8+48k] at the rotated slot; a failed
                    parse writes 0 (0x103689), so the count must be exact.

    VERIFIED: `/F=`, `/B=` AND `/E=` ARE THE BOARD, MEASURED 2026-09-06 -- the three
    fields this docstring used to call unknown. `/F=` is one OBJECT CODE per
    tile (blocks, chance blocks, rotating blocks, special tiles), `/B=` is how
    many of those codes are block-family and `/E=` how many are special tiles.
    `codes` comes from `_match_objects`; the whole mechanism is in
    `tmbattle.roll_board`.

    WARNING: THE TWO COUNTS ARE DERIVED FROM `codes`, NEVER ROLLED SEPARATELY. The
    client's reveal loop divides by the number of tiles still carrying a code,
    so a count that outruns the array is a DIVIDE BY ZERO in the client.
    `POL_TM_START_B` / `POL_TM_START_E` still force them for an A/B and will
    happily crash a client if you force them too high -- that is what they are
    for. `POL_TM_START_TILE` fills every tile with one code (the old flat
    board; =0 is the pre-2026-09-06 behaviour).

    WARNING: `/L=` IS STILL UNKNOWN, and is still zero.

    The client says whether it read the message at all: `---->Recv=STARTDATA`
    (0x103520) is in the traced sink.
    """
    n = len(seats) or int(n_solo or 2)
    tiles = TILES_BY_PLAYERS[min(max(n - 2, 0), len(TILES_BY_PLAYERS) - 1)]
    tiles = common._env_int("POL_TM_START_TILES", tiles)
    # The starter, moved into THIS recipient's frame. `_vsgame_body` rotated the
    # seats so the recipient is 0; the same rotation has to be applied here or
    # the two clients disagree about who leads.
    s_val = (int(starter) - int(me)) % n
    # THE BOARD. Absolute -- unlike `/S=` it is NOT rotated into the
    # recipient's frame, because a tile index is a tile index for everybody.
    # WARNING: THIS ONLY SENDS. `codes` is whatever `_match_objects` decided for this
    # match -- rolled, or forced by POL_TM_START_F/POL_TM_START_TILE -- and it
    # is the SAME array that seeded `_MATCH_BOARD`. Reading an override here
    # instead is what made the server and the client play different boards on
    # 2026-09-07T00:16Z; see the banner in `_match_objects`.
    board_codes = (list(codes or []) + [0] * tiles)[:tiles]
    b_val, e_val = tmbattle.board_counts(board_codes)
    b_val = common._env_int("POL_TM_START_B", b_val)
    e_val = common._env_int("POL_TM_START_E", e_val)
    body = (b"@StartData=/S=%d/B=%d/E=%d" % (s_val, b_val, e_val))
    body += b"/F=" + b"|".join(b"%d" % c for c in board_codes)
    body += b"/L=" + b"|".join(b"%d" % common._env_int("POL_TM_START_L", 0)
                               for _ in range(n))
    return body


#: The per-match random table. WARNING: BOTH CLIENTS MUST GET THE SAME EIGHT VALUES --
#: `@TurnData`'s `/R=` is pre-rolled randomness the client stores and uses
#: locally (it prints them itself as `RandTbl=` then `%d,` per value), so two
#: different tables would have the two players computing different outcomes for
#: the same battle. Generated once per (chan, table) and reused for every
#: recipient of that turn.
_TURN_RAND = {}


def _turn_rand(key, fresh=False):
    """Eight values for `/R=`, identical for everyone in this match."""
    if fresh or key not in _TURN_RAND:
        import random as _r
        lo = common._env_int("POL_TM_RAND_LO", 0)
        hi = common._env_int("POL_TM_RAND_HI", 255)
        _TURN_RAND[key] = [_r.randint(lo, hi) for _ in range(8)]
    return _TURN_RAND[key]


def _turndata_body(seats, me, active, rand, scores=None, n_solo=None):
    """`@TurnData=` -- whose turn it is. Arm 0x1036A3, `Recv=TURNINFO`.

    Measured 2026-08-20T14:19Z: once `@StartData` was read (both clients printed
    `---->Recv=STARTDATA`, and the tester got the turn-order roulette on
    screen) both began polling **command 9**, which is this.

    Off the parser, and gated on the same session pair as everything else
    (0x1036BE):

        /A=     the ACTIVE player -- whose turn it is. Run through the same
                rotation as `@StartData`'s `/S=` (0x103713) and stored in the
                SAME fields, [0x2B63C4]+0x1B0 and +0x178, so it is the "current
                player" the board draws from. Per recipient, like /S=.
        /S= xN  per player -- THE SCORE. The store at 0x103799 is
                `[0x5246580 + 48k]` (esi in the arm is the parser's `this`
                0x52463E0, +0x1A0 = 0x5246580): the SAME per-seat field the
                client's own turn-end tally rewrites by walking the board and
                counting TILES per owner (0xC73EF zeroes it, 0xC7440
                increments per tile; no hand component). So /S= must carry
                the per-seat BOARD TILE COUNT, in the recipient's frame
                (occ 0 = the recipient; the client's own-seat global
                0x524657B is always 0 in its own frame).

                WARNING: Serving anything else FIGHTS the client's own tally over
                one display: the hand-remaining counts served until
                2026-08-22 made the score panel drop to the hand count on
                every announcement and snap back to tiles at each turn end
                -- the live report "score fluctuated, 1 point with 6 cards on
                the field", live that evening. The earlier "COM hand
                refilled" report against constant /S=5 was the same field.
        /R= x8  the RANDOM TABLE, [0x2B63C4]+0x1A8+i, exactly eight
                (0x1037F2 `cmp edi, 8`). The client logs them itself as
                `RandTbl=` / `%d,`.

    The random range is unmeasured; POL_TM_RAND_LO/HI sweep it.
    POL_TM_TURN_S is the constant fallback when no scores are passed.
    """
    n = len(seats) or int(n_solo or 2)
    a_val = (int(active) - int(me)) % n
    sc = scores or [common._env_int("POL_TM_TURN_S", 5)] * n
    return (b"@TurnData=/A=%d" % a_val
            + b"/S=" + b"|".join(b"%d" % v for v in sc[:n])
            + b"/R=" + b"|".join(b"%d" % v for v in rand[:8]))
