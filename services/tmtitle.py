"""Tetra Master as a title plugin for the OpenLobby core.

This module is what `POL_TITLES=tmtitle` loads into the core's `login` and
`authsess` processes. It holds every piece of Tetra Master logic that used to
live inside the core's responders.py -- the room roster behind `b/g/PTL`, the
auction house, the ranking lists, the trade relay on the game envelope, the
lobby-list live counts and the player save's factory defaults -- registered
with the core through `titles.Title` (see services/titles.py in OpenLobby for
the contract). The game itself (board, battle, cards, shop, VS. COM) is
`tetramaster.py` and its siblings; this module is the seam that hands the
core's traffic to it.

Nothing here imports `responders`. The core plumbing these functions use is
bound by name through `titles.core` (`_CORE_NAMES` below): the moved code
keeps the names it always had, and running this module outside the core (the
selftests) binds the standalone defaults instead.
"""
import json
import os
import re
import struct
import sys
import time

import titles
from titles import core as _core

import tetramaster
import tmauction
import tmfixtures
import tmrank
import tmroom
import tmsave

HERE = os.path.dirname(os.path.abspath(__file__))

#: Every core name the moved code reaches for. Bound at import and again when
#: the core (re)binds its handle; see titles.Core.__doc__ for what each is.
_CORE_NAMES = (
    "log", "NoPad", "PRESENCE", "ROOMS", "accounts", "polpro", "RESOURCE_DIR",
    "_game_notice_line", "_irc_host", "_session_get", "_session_sid",
    "_sess_member_id", "_member_content_id", "_live_rooms", "_room_of_member",
    "_resource_file", "_resource_read_file", "_resource_stored", "_fetch_subject",
    "_peer_is_ps2", "_mail_mint", "_member_primary_handle",
    "_session_handle_id", "_self_ip", "_member_display_name",
)


def _rebind():
    for name in _CORE_NAMES:
        try:
            globals()[name] = getattr(_core, name)
        except AttributeError:
            globals()[name] = None


_rebind()

#: THE PROVEN CONFIGURATION AS DEFAULTS. These are the values the live service
#: ran Tetra Master with (its compose enumerated every knob; the ones below
#: are the ones it set). A deployment overrides any of them in its
#: environment; nothing else needs setting. The knobs are read where they
#: are used (search the name), each with its reason.
RELEASE_DEFAULTS = {
    "POL_TM_EVENT_START": "-1",      # -1/-1 = no event: the event board and
    "POL_TM_EVENT_END": "-1",        # countdown stay off until a window is set
    "POL_TM_CV_INIT": "excinit",
    "POL_TM_EINIT_RT": "1",
    "POL_TM_TRADE_CARD": "1",
    "POL_TM_TRADE_CARD_MAX": "10",
    "POL_TM_GAMEML_DELAY_MS": "0",
    "POL_TM_BOARD_OBJECTS": "1",     # the board's blocks and special tiles
    "POL_TM_SCRAMBLE": "1",
    "POL_TM_WAGER_DOUBLEUP": "1",
    "POL_TM_WAGER_REMATCH": "1",
    "POL_TM_COMBO": "defeated",
    "POL_TM_ABANDON_CLEANUP": "1",
    "POL_TM_DECK_NAMES": "1",
    "POL_TM_DECK_NAME_BASE": "1",
    "POL_TM_CHAMPION_SAVE_SLOT": "0",
    "POL_TM_DECK_SLOTS": "1",
    "POL_TM_TAKE_PVP": "1",
    "POL_TM_GETAWAY_TO": "all",
    "POL_TM_ROTATING_STEP": "1",
    "POL_TM_READY_HOLD_LOSER": "0",
    "POL_TM_CONTINUE_PARTIAL": "0",
}
for _k, _v in RELEASE_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

#: The reply templates ship with the title: config/polpro.json in a checkout,
#: /app/polpro.json in the image (the Dockerfile copies it beside the code).
POLPRO_SPEC_CANDIDATES = (
    os.path.normpath(os.path.join(HERE, os.pardir, "config", "polpro.json")),
    os.path.join(HERE, "polpro.json"),
)

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
# the roster, the auction, the rankings, the profile pool, chat
# ---------------------------------------------------------------------------
def _roster_sequence():
    """The UPDATE SEQUENCE of the room this connection's member is standing in, or None.

    WARNING: THIS IS WHAT `$SERIAL` MUST RESOLVE TO, and it is the second half of the
    live-roster change. `tmroom.build_ptl` stamps `b/g/PTL` `+0x40` with the room's
    sequence, `cp__002fb560` reads that field as the sequence the snapshot
    represents, and the client reports it straight back on every `<DR>`. So the
    number we answer `<DO>` with has to be THE SAME NUMBER -- and until this it
    was not: `polpro._file_serial` globbed the resource directory for the newest
    stored `*.b_g_PTL.bin` and returned ITS `+0x40`.

    Measured 2026-08-18, with the roster already live and one room in play:

        tm-roster.json   #TM0R001 seq 2     <- stamped into the blob we serve
        stored file      +0x40 = 23         <- what `<DO>` was answering

    A client that read 2 and is told 23 is 21 deltas behind, which is inside
    `cp__002fae98`'s window, so it queues nothing, applies nothing and
    re-requests -- and that is the `b/g/PTL` re-fetch storm in `lobby.log`, 623
    fetches at a flat 3 s cadence. The snapshot half landing without this made
    the mismatch WORSE, not better, because before it both numbers came from the
    same stale file and at least agreed with each other.

    Returns None -- meaning "fall back to the old glob" -- for a session we
    cannot place in a room, which is every non-Tetra-Master caller on this
    channel and every client that has not sent its `<DE>` yet.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return None
    try:
        _m = _session_get("member_id")
        # Record-first -- same fix, same measurement as `_roster_delta_reply`.
        chan = ((tmroom.room_of(_m) if _m else None) or _room_of_member(_m))
        if not chan:
            return None
        # WARNING: MIRROR `_ptl_with_live_roster`'S CONDITION, DO NOT APPROXIMATE IT.
        # This used to ask `tmroom.members(chan)` -- "do we hold a record?" --
        # which was right until the roster began taking WHO IS PRESENT from the
        # room registry (a333f757). A player who has not sent `<DE>` yet is now
        # built into the blob from a synthesised record, so the blob IS stamped
        # with the room's sequence while `members(chan)` is still empty -- and
        # the old test would have fallen back to the directory glob for exactly
        # that player, re-opening the mismatch this function exists to close.
        # The rule is simply: if a blob gets BUILT, its sequence is the answer.
        who = ((_live_rooms() or {}).get(chan) or {}).get("who") or []
        if not any(row.get("member_id") for row in who)                 and not tmroom.members(chan):
            return None                    # build_ptl returns `base` unchanged
        return tmroom.sequence(chan)
    except Exception:                      # a roster miss must not cost a reply
        return None


def _roster_sync_presence(state):
    """Give a room-registry arrival a member record, so the DELTAS carry it too.

    `tmroom.build_ptl` already synthesises a row for somebody the IRC registry
    knows and we hold no `<DE>` for -- but only at FETCH time, into the snapshot.
    A client that already has the snapshot never fetches again (that is the point
    of the delta stream), so without this it would never learn about them at all:
    the roster's sequence only moves when a RECORD changes, and a synthesised row
    is not a record.

    Storing the synthesised row through `note_member` fixes both halves at once --
    it bumps the room's sequence, emits the `PD`, and is idempotent, because
    `note_member` returns False when the values are unchanged. So this can run on
    every registry mutation, which is where it is called from.

    WARNING: ONE DIRECTION ONLY -- IT ADDS, IT NEVER REMOVES. Absence from the registry
    is NOT a departure: an auth session that blips drops its rows for a moment,
    and keying presence to the socket is precisely the bug that made people blink
    out of each other's lists (see `tmroom.note_member` and `build_ptl`'s "A
    UNION, NOT A FILTER"). A player leaves when their own record says so
    (`stat = 0`) or when the member list is rebuilt from a fetch.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    try:
        for chan, entry in (state or {}).items():
            if not str(chan).startswith("#TM0R"):
                continue                    # Tetra Master rooms only
            for row in (entry or {}).get("who") or []:
                mid = row.get("member_id")
                if not mid or tmroom.room_of(mid):
                    continue                # unbound session, or already placed
                name = row.get("name") or row.get("nick") or ""
                if tmroom.note_member(mid, tmroom.synth_member(chan, mid, name)):
                    log("authserv",
                        f"  roster {chan}: member {mid} ({name!r}) is in the "
                        f"room with no <DE> yet -- synthesised, sequence "
                        f"{tmroom.sequence(chan)}")
    except Exception as exc:
        log("authserv", f"  roster: presence sync failed ({exc!r}) -- the "
                        f"snapshot still synthesises the row on fetch")


def _roster_delta_reply(payload):
    """The `<DD>` answer to a class-L `<DR>`, or None to use the template.

    WARNING: THIS IS THE LAST STEP OF THE LIVE-ROSTER CHANGE, AND IT IS WHY THE `b/g/PTL` RE-FETCH STORM EXISTS.
    `<DR>(N)` is the client reporting its own update sequence every 2 s
    (`cp__002fb218`, gated on `LobbyState == 15`). We have always answered it with
    `<DO>`, and `<DO>` is not a version reply -- `cp__002fb7d0` ignores its value
    entirely and, if `LobbyState == 15`, just calls the snapshot reload
    (`cp__002fc608`, a 0xC050 = 49232-byte fetch, which is `tmroom.TOTAL` exactly).
    So every poll was ordering a full re-read of `b/g/PTL`, which is the flat 3 s
    623-fetch cadence in `lobby.log` -- not a client chasing a serial, a client
    doing what we told it to.

    What SE sends instead is the deltas: `<DD>` carrying (sequence, payload) GROUP
    pairs that `cp__002fae98` walks and `cp__002fb290` applies. Three answers,
    and the difference between them is the whole point:

        deltas_after -> [..]   send them, and the client's list changes with no
                               fetch at all
        deltas_after -> []     the client is CURRENT: say NOTHING. Silence is the
                               correct answer to a poll with no news, and it is
                               what stops the storm.
        deltas_after -> None   we cannot serve it from the log (too far behind,
                               or claiming a sequence we never issued -- what a
                               client holding a blob from a previous process
                               looks like). Fall through to the template, i.e.
                               `<DO>`, i.e. reload. That arm is now RARE instead
                               of universal.

    Returns `(payload_bytes_or_None, handled)`. `handled` False means "not a
    `<DR>` I can speak to" and the caller does exactly what it did before.
    """
    if tmroom is None or os.environ.get("POL_TM_DELTAS", "1") != "1":
        return None, False
    try:
        groups = polpro.parse(payload)
        if not groups or groups[0][0] != "DR":
            return None, False
        have = (groups[0][1] or ["0"])[0]
        member = _session_get("member_id")
        # TRADE RE-SERVE CLOCK (2026-08-24): the room-band @Pong is a ~60s
        # keepalive (measured Cnt=21 -> Cnt=22 one minute apart), useless
        # against the client's one-shot @TrAns purge; THIS <DR> roster
        # heartbeat is the ~2s clock. The tick queues via _queue_push and the
        # copies ride the normal unprompted flusher within seconds.
        try:
            tetramaster._trade_reserve_tick(member)
        except Exception:
            pass
        # RECORD-FIRST, exactly as `_ptl_with_live_roster` does and for a
        # measured reason: Lex's re-joined session carried `member_id 0` in the
        # registry row (rooms-live.json `who=[.., (0, None)]`), so the
        # registry-only lookup placed him nowhere and every `<DR>` he sent --
        # including `<DR>(93)` at sequence 93, CURRENT -- was answered `<DO>`,
        # a reload, forever (measured 22:10:19). His `<DE>` had been recorded
        # six minutes earlier; the RECORD knew exactly where he was.
        chan = ((tmroom.room_of(member) if member else None)
                or _room_of_member(member))
        if not chan:
            return None, False           # cannot place them: template as before
        # WARNING: THE "IS THIS BASE LIVE?" GATE THAT STOOD HERE IS RETRACTED -- see
        # the banner above `_PTL_TEMPLATE_SERIAL`. `have` IS the test: a client
        # reports the `+0x40` of the blob it holds, and `deltas_after` already
        # refuses a sequence we never issued or cannot chain to. Nothing this
        # process holds in memory can second-guess that, because the blob was
        # served by a DIFFERENT CONTAINER.
        deltas = tmroom.deltas_after(chan, have)
        if deltas is None:
            log("authserv", f"  roster {chan}: <DR>({have}) from member "
                            f"{member} is outside the delta log (we are at "
                            f"{tmroom.sequence(chan)}) -- answering with the "
                            f"template, i.e. a reload")
            return None, False
        if not deltas:
            log("authserv", f"  roster {chan}: <DR>({have}) from member "
                            f"{member} is CURRENT -- silent, which is what stops "
                            f"the b/g/PTL re-fetch storm")
            return None, True
        # ONE `<DD>` IS ONE IRC NOTICE, SO DO NOT STUFF IT. A client rejoining at
        # 0 can be owed the whole `DELTA_WINDOW` (64), and 64 `PD` groups is
        # ~13 KB on a band whose largest measured line is nothing like that.
        #
        # WARNING: BUT A PREFIX DOES NOT ADVANCE THE CLIENT. MEASURED 2026-08-20, and
        # it is the correction to this block's own previous comment, which said
        # "the client applies it, advances, and the next poll 2 s later carries
        # the rest -- convergent". It is not convergent. It is a hard hang:
        #
        #     19:08:19  <DR>(0) is owed 39 deltas -- sending the first 16
        #     19:08:22  <DR>(0) is owed 39 deltas -- sending the first 16
        #     ...every ~1.5 s, both clients, for as long as anyone watched
        #
        # In live testing it showed up as Tetra Master hanging on "Retrieving
        # table info..." when reserving a table. The prefix path had NEVER RUN before
        # that afternoon -- a backlog over 16 was needed to reach it -- and in
        # all 52 executions since, not one client advanced off the sequence it
        # came in on. There is no instance in any log of a chunked batch
        # converging. Whatever the client does with a batch that does not bring
        # it current, applying it and re-reporting the new sequence is not it.
        #
        # SO WHEN WE CANNOT SEND THE WHOLE CHAIN, SEND NONE OF IT and let the
        # client reload the snapshot instead. That is the same answer
        # `deltas_after` already gives a client past `RELOAD_BEHIND`, it is a
        # path with years of evidence behind it, and `build_ptl` stamps the blob
        # with the current sequence -- so the client comes back CURRENT and the
        # next poll is the silent one. Convergent, without having to be right
        # about a size limit we have not measured.
        #
        # `POL_TM_DELTA_CHUNK=0` sends the lot in one notice (the old escape
        # hatch, still there). `POL_TM_DELTA_PREFIX=1` restores the truncating
        # behaviour for whoever wants to measure WHY the client refuses it --
        # that is a real open question, just not one to hang the room on.
        #
        # WARNING: 16 IS MEASURED NOT TO APPLY -- the default came down to 4 on
        # 2026-08-20T22:33. A client at <DR>(0) owed a COMPLETE 16-delta chain
        # (len == chunk, so the reload guard above never fired) was handed the
        # identical <DD> every 3 s, ten times and counting, never moving off 0
        # -- observed live as the client TIMING OUT ON ROOM EXIT. Same
        # client, same evening: 1 delta applies, 4 deltas apply ("<DR>(0) -> 4
        # delta(s) ... then CURRENT", 00:20), 16 does not, twice (as a 16-of-39
        # prefix at 19:07 and as this complete 16-of-16). The ceiling is
        # somewhere in 5..15 and UNMEASURED; 4 is the largest count ever seen
        # to work. Anything longer is a reload, which is measured to converge.
        chunk = int(os.environ.get("POL_TM_DELTA_CHUNK", "4") or 0)
        if chunk and len(deltas) > chunk:
            if os.environ.get("POL_TM_DELTA_PREFIX", "0") == "1":
                log("authserv", f"  roster {chan}: <DR>({have}) from member "
                                f"{member} is owed "
                                f"{len(deltas)} deltas -- POL_TM_DELTA_PREFIX=1, "
                                f"sending the first {chunk}. THIS HAS NEVER "
                                f"BEEN SEEN TO CONVERGE; expect a re-poll at "
                                f"{have}")
                deltas = deltas[:chunk]
            else:
                log("authserv", f"  roster {chan}: <DR>({have}) from member "
                                f"{member} is owed "
                                f"{len(deltas)} deltas, more than the {chunk} one "
                                f"notice carries -- answering with the template, "
                                f"i.e. a RELOAD. A prefix does not advance the "
                                f"client and re-polls for ever.")
                return None, False
        # THE RECIPIENT'S OWN ROW USES THE MEMBER-NUMBER SELF-ID -- same rule as
        # `build_ptl`, applied here so a periodic self `<PD>` cannot re-stamp the
        # row with a guid and re-break the "can't select yourself" guard. Only
        # the fetcher's own rows change; every other member's delta is untouched.
        # Value-0 first (the roster key id, POL_TM_VALUE0 -- must agree with
        # build_ptl's snapshot), then the per-recipient self-row override.
        deltas = tmroom.rewrite_value0_deltas(deltas)
        deltas = tmroom.rewrite_self_deltas(deltas, member)
        out = polpro.build(tmroom.dd_groups(deltas))
        log("authserv", f"  roster {chan}: <DR>({have}) from member {member} -> "
                        f"{len(deltas)} delta(s) {[d[1] for d in deltas]} up to "
                        f"sequence {deltas[-1][0]}")
        return out, True
    except Exception as exc:
        log("authserv", f"  roster: delta reply failed ({exc!r}) -- the template "
                        f"still stands")
        return None, False


def _auction_store_file():
    """This member's exhibit list -- the file the lobby band already serves.

    ONE FILE IS THE WHOLE STORE. `_resource_blob` hands these bytes to `3:0` and
    `_fetch_len` measures the same file, so the `<SN>` computed here cannot
    drift from what the client is given. Two different CONTAINERS answer those
    two bands, so a shared file is the only thing that can keep them in step --
    see the POL-5135 note on `_AUCTION_LIST_PATHS`.
    """
    return _resource_file(_EXHIBIT_LIST_PATH)


def _write_resource(path, data):
    """Write a resource blob atomically, creating the directory if need be.

    Atomic because the LOBBY BAND reads this file from another container while
    the auth band writes it: a torn write is a client handed half a record, and
    `_fetch_len` would have measured the other half. `os.replace` is the same
    move the stamp/spool writers here already use.
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


#: The BROWSE result. It cannot share `U/g/TM0_EXHIBITLIST`: that path is
#: member-scoped by `_resource_file`, so answering a browse with it would hand
#: the player their OWN listings back and call it "all cards up for auction".
#: We name the file, so browse gets its own -- written per member because the
#: result set is per request, and read back on the lobby band by the same
#: member-scoped rule.
_AUCTION_BROWSE_PATH = "U/g/TM0_AUCLIST"


#: The BID HISTORY of one auction. Its own path for a hard reason: `<SI>+<IB>`
#: already names `U/g/TM0_BIDLIST` and is answered `<SS>`, which drives the
#: EXHIBIT reader at 0xC0, while `<BH>` answered `<HS>` drives the BID reader at
#: 0x48. One path cannot be read at two strides, and we name the file, so the
#: bid history gets its own.
_AUCTION_BIDHIST_PATH = "U/g/TM0_BIDHIST"


def _auction_bids_file(auction_id):
    """Where auction `n`'s bids live -- GLOBAL, not per member.

    A bid history is the same for everyone looking at that auction, unlike the
    exhibit list, which is per seller. So this is keyed on the auction id and
    lives outside the member-scoped naming `_resource_file` applies.
    """
    return os.path.join(RESOURCE_DIR, "auction-%d.bids.bin" % int(auction_id))


def _auction_bids(auction_id):
    """Auction `n`'s bid rows, sorted, or b"" if nobody has bid."""
    try:
        with open(_auction_bids_file(auction_id), "rb") as f:
            blob = f.read()
    except OSError:
        return b""
    if len(blob) % tmauction.BID_REC:
        log("lobby", f"  auction: bids for {auction_id} are {len(blob)}B, not a "
                     f"whole number of 0x{tmauction.BID_REC:X} records -- "
                     f"taking the first {len(blob) // tmauction.BID_REC}")
        blob = blob[:len(blob) // tmauction.BID_REC * tmauction.BID_REC]
    return tmauction.sort_bids(blob)


def _auction_find(auction_id):
    """`(store_file, blob, index)` for an auction id, or None.

    Searches every member's store because a bid arrives from the BROWSE list,
    where the bidder has no idea whose listing it is -- which is exactly why
    `<AI>` had to become globally unique before this function could exist.
    """
    import glob
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", _EXHIBIT_LIST_PATH) + ".bin"
    for fn in sorted(glob.glob(os.path.join(RESOURCE_DIR, "*." + suffix))):
        try:
            with open(fn, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        for i in range(tmauction.count(blob)):
            try:
                if tmauction.read_record(blob, i)["ai"] == auction_id:
                    return fn, blob, i
            except ValueError:
                break
    return None


def _auction_with_bid_counts(blob):
    """Every listing's `<BC>` set from its own bid file, at SERVE time.

    Derived rather than trusted because `<BC>` is believed to gate the bidder
    column, and a stored count can drift from the bids it counts -- listings
    written before `apply_bid` set it carry 0 and would show "-" for ever. The
    bid files are the only source of truth for how many bids exist, so ask them
    every time; the lists are small and read far more often than they are wrong.
    """
    if tmauction is None:
        return blob
    out = bytearray(blob)
    for i in range(tmauction.count(blob)):
        try:
            rec = tmauction.read_record(blob, i)
        except ValueError:
            break
        n = tmauction.bid_count(_auction_bids(rec["ai"]))
        struct.pack_into("<I", out, i * tmauction.REC + 0x98, n & 0xFFFFFFFF)
    return bytes(out)


def _auction_all_rows():
    """Every member's listings, concatenated -- the browse result set.

    The per-member store files ARE the database, so the aggregate is a glob and
    a join. Sorted by filename purely so the order is stable between requests;
    the client is told a count and hands back an `<AI>`, neither of which
    depends on order, but an unstable list would reshuffle under a player
    mid-scroll.
    """
    import glob
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", _EXHIBIT_LIST_PATH) + ".bin"
    out = bytearray()
    for fn in sorted(glob.glob(os.path.join(RESOURCE_DIR, "*." + suffix))):
        try:
            with open(fn, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        # A short tail is a torn or hand-edited file. Take the whole records and
        # say so rather than serving a fragment: `_fetch_len` measures what we
        # write, so a partial record would be a length the reader cannot use.
        if len(blob) % tmauction.REC:
            log("lobby", f"  auction: {os.path.basename(fn)} is {len(blob)}B, "
                         f"not a whole number of 0x{tmauction.REC:X} records -- "
                         f"taking the first {len(blob) // tmauction.REC}")
            blob = blob[:len(blob) // tmauction.REC * tmauction.REC]
        out += blob
    return _auction_with_bid_counts(bytes(out))


def _auction_next_id():
    """The `<AI>` for a fresh listing: one past every id still in use.

    WARNING: "IN USE" INCLUDES THE BID FILES, NOT JUST THE ROWS. Settlement removes a
    SOLD listing's row but leaves its `auction-N.bids.bin` behind, so minting
    from the rows alone hands a settled id straight back out -- and the bid
    files are keyed on nothing but that id. Measured 2026-09-13: every earlier
    listing had settled, the store was empty, Lex's new listing minted as
    auction 1 and inherited two stale test bids from an earlier settled listing,
    rendering "High bid 0 by PCTest (2 bids)" on a listing nobody had bid on --
    and at `<NC>` the sweep would have SOLD the card to PCTest for 100.
    """
    import glob
    used = tmauction.max_auction_id(_auction_all_rows())
    for fn in glob.glob(os.path.join(RESOURCE_DIR, "auction-*.bids.bin")):
        m = re.match(r"auction-(\d+)\.bids\.bin$", os.path.basename(fn))
        if m:
            used = max(used, int(m.group(1)))
    return used + 1


def _auction_count_rows(now=None):
    """The rows the Price List should COUNT: the browse-visible set the lazy
    sweep leaves, computed READ-ONLY.

    WARNING: THE COUNT AND THE BROWSE MUST AGREE. `_auc_counts_live` runs on the LOBBY
    band (the `login` container) and answers `b/g/TM0AucData` -- the five band
    counts that decide which Price List rows the client will even let you open.
    The BROWSE runs on the auth band (`authsess`) and calls `_auction_sweep()`
    FIRST, which settles any expired listing that has bids as SOLD and REMOVES
    it. So counting `_auction_all_rows()` raw over-counts by exactly those
    to-be-sold rows: measured 2026-09-02, the Price List said 1 in the Cheap
    band, the player opened it, the browse swept the one expired-with-bid
    listing to SOLD, and the list came back empty.

    The fix is to count the store as the sweep would LEAVE it, without running
    the sweep here -- a sweep from the login container would race authsess's and
    could double-credit a sale (the bands live in separate containers). Only the
    expired-with-bids row is dropped by the sweep; every other row it keeps
    (a relist only resets `<CM>`/`<BC>`, and a no-bid row already has `<CM>`=0,
    so banding by `asking_price` is unchanged), so those are counted as-is.
    """
    import glob
    now = int(now if now is not None else time.time())
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", _EXHIBIT_LIST_PATH) + ".bin"
    out = bytearray()
    for fn in sorted(glob.glob(os.path.join(RESOURCE_DIR, "*." + suffix))):
        try:
            with open(fn, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        if len(blob) % tmauction.REC:
            blob = blob[:len(blob) // tmauction.REC * tmauction.REC]
        for i in range(tmauction.count(blob)):
            try:
                rec = tmauction.read_record(blob, i)
            except ValueError:
                break
            if tmauction.expired(rec, now) and \
                    tmauction.bid_count(_auction_bids(rec["ai"])):
                continue                     # SOLD -- the browse drops it
            out += blob[i * tmauction.REC:(i + 1) * tmauction.REC]
    return _auction_with_bid_counts(bytes(out))


def _auction_rows():
    """The stored blob for this member, or b"" -- never the shipped fixture.

    Deliberately NOT falling back to `_tm_template_blob`: the fixture exists to
    give every member a legal non-empty list while the store did not exist, and
    a real store makes it a phantom listing in everybody's Cards for Sale.
    """
    try:
        with open(_auction_store_file(), "rb") as f:
            return _auction_with_bid_counts(f.read())
    except OSError:
        return b""


#: THE AUCTION-NOTICE SENDER. SE authored these server-side, so the "From" is
#: ours to choose -- there is NO Tetra Master sender baked into the client
#: (verified: TM.dll and its data carry the auction UI strings but no sender
#: name; the O/m/ record's +0x10 name field is what SE filled). A dedicated
#: synthetic guid so the record renders the NAME and resolves to no real
#: player's profile. `POL_TM_AUCTION_FROM` overrides the display name.
#: WARNING: NOT YET CONFIRMED LIVE: whether the recipient's Viewer renders "Tetra
#: Master" from +0x10 for a sender guid it does not know, or falls back to
#: "Unknown User", is the one bit the static read cannot settle -- read the
#: first delivered notice's From line. If it is wrong, the fix is the sender
#: guid, not the name.
_TM_AUCTION_SENDER_GUID = 0x0000544D41754331   # "TMAuC1" -- no real member

#: The three notices, env-overridable so wording tweaks need no redeploy.
#: `{card}` and `{amount}` are filled per settlement (card name from CardPrm,
#: the closing bid); a template that omits a token simply does not show it, and
#: the phrasing here reads cleanly with the card named or not (`_notice_fill`
#: collapses the leftover space of an unresolved `{card}`). Subject is capped at
#: 15 cp932 bytes by the record; the body may run longer.
_AUCTION_NOTICES = {
    "sold":     ("Card Sold",
                 "Your card {card}sold at auction for {amount} gil. The payment "
                 "is ready to collect -- visit the Auction House and choose "
                 "Check Out to receive it."),
    "won":      ("Auction Won",
                 "You won {card}with a bid of {amount} gil. Visit the Auction "
                 "House and choose Check Out to pay for and receive your card."),
    "returned": ("Card Returned",
                 "Your card {card}did not sell before the auction period ended. "
                 "Visit the Auction House and choose Check Out to retrieve it."),
    "outbid":   ("Auction Ended",
                 "You were outbid and did not win {card}-- your {amount} gil "
                 "bid was refunded. Collect it at Check Out."),
}


def _notice_fill(template, card="", amount=None):
    """Substitute {card}/{amount} without str.format (custom env text may carry
    stray braces). A named card becomes 'Cinna ' so 'Your card {card}sold' reads
    'Your card Cinna sold'; unknown collapses to 'Your card sold'."""
    card_frag = (str(card) + " ") if card else ""
    out = template.replace("{card}", card_frag)
    out = out.replace("{amount}", "0" if amount is None else str(int(amount)))
    return " ".join(out.split())           # tidy any doubled space


def _auction_notice(recipient_member, which, card="", amount=None):
    """Send a Tetra Master POL Message about an auction result, fire-and-forget.

    A notice that cannot be delivered (unknown member, DB down, mint refused)
    must NEVER hold up the settlement that earned it: the pending store is the
    source of truth and the message is the courtesy nudge. `POL_TM_AUCTION_NOTICE=0`
    switches the whole feature off; `POL_TM_AUCTION_{SUBJ,BODY}_<WHICH>` tweak
    the text per notice (`{card}`/`{amount}` tokens honoured there too).
    """
    if os.environ.get("POL_TM_AUCTION_NOTICE", "1") != "1":
        return
    if accounts is None or which not in _AUCTION_NOTICES:
        return
    subj_default, body_default = _AUCTION_NOTICES[which]
    subject = os.environ.get("POL_TM_AUCTION_SUBJ_" + which.upper(), subj_default)
    body = _notice_fill(
        os.environ.get("POL_TM_AUCTION_BODY_" + which.upper(), body_default),
        card=card, amount=amount)
    from_name = os.environ.get("POL_TM_AUCTION_FROM", "Tetra Master")
    try:
        member = int(recipient_member)
    except (TypeError, ValueError):
        log("authserv", f"  auction notice: {which!r} recipient "
                        f"{recipient_member!r} is not a member id -- skipped")
        return
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            hid = _member_primary_handle(db, member)
        finally:
            db.close()
        if not hid:
            log("authserv", f"  auction notice: member {member} has no handle "
                            f"-- {which!r} notice not sent")
            return
        path = _mail_mint(from_name, _TM_AUCTION_SENDER_GUID,
                          accounts.handle_guid(hid), subject, body)
        if path:
            log("authserv", f"  auction notice: {which!r} POL Message minted to "
                            f"member {member} (handle {hid}) from {from_name!r}")
    except Exception as e:                                    # pragma: no cover
        log("authserv", f"  auction notice: {which!r} to member {member} FAILED "
                        f"({e}) -- settlement stands, message lost")


def _auction_sweep(now=None):
    """Expire listings past `<NC>`: relist what may relist, leave the rest.

    Run from the request path rather than a timer -- an auction only matters
    when somebody looks at it, and a lazy sweep needs no scheduler, survives a
    restart, and cannot drift from the store the way a background job can.

    WARNING: NOTHING IS EVER DELETED HERE, and that is the whole safety property. A
    SOLD or an out-of-relists UNSOLD listing still holds the player's card, and
    the only way to give a card back is the `DI`/`BI` delivery lists, whose
    968-byte format is NOT decoded (see tmauction's docstring). Dropping the row
    would destroy the card. So those two states are left in place and counted;
    they will keep showing as expired, with "Time left" pinned at 0h, until
    settlement exists. That is visibly wrong on screen and RECOVERABLE, which is
    the right trade against silently eating somebody's collection.
    """
    if tmauction is None:
        return
    import glob
    now = int(now if now is not None else time.time())
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", _EXHIBIT_LIST_PATH) + ".bin"
    relisted = stuck_sold = stuck_unsold = sold = unsold = 0
    for fn in sorted(glob.glob(os.path.join(RESOURCE_DIR, "*." + suffix))):
        try:
            with open(fn, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        out, changed, drop = bytearray(blob), False, []
        for i in range(tmauction.count(blob)):
            try:
                rec = tmauction.read_record(blob, i)
            except ValueError:
                break
            if not tmauction.expired(rec, now):
                continue
            bids = _auction_bids(rec["ai"])
            if tmauction.bid_count(bids):
                # SOLD. The seller is owed the winning bid, the winner is owed
                # the card. `_auction_bids` sorts ascending, so the last row is
                # the highest -- the winner.
                win = tmauction.read_bid(bids, tmauction.bid_count(bids) - 1)
                seller = os.path.basename(fn).split(".", 1)[0]
                # WARNING: RESOLVE THE WINNER TO A MEMBER ID. Bid rows recorded
                # before the member id reached the recorder carry member=0
                # (measured: auction 1's two rows, 2026-08-20), and a pending
                # credit keyed by NAME lands in a file the Check Out door --
                # which looks up by member id -- can never read. The handle
                # map is the resolver; an unresolvable/ambiguous name still
                # credits under the name so nothing is lost, and says so.
                winner = win["member"] or 0
                if not winner and tmroom is not None:
                    winner = tmroom.member_by_name(win["name"]) or 0
                if not winner:
                    log("authserv", f"  auction: {rec['ai']} winner "
                                    f"{win['name']!r} has NO resolvable member "
                                    f"id -- crediting under the NAME; the door "
                                    f"cannot serve it until moved by hand")
                try:
                    tmauction.add_pending(seller, money=win["amount"])
                    # WON, not returned -- rides Check Out's cards-B section
                    # ("Successful Bid"), so it does not render as "Returned
                    # Card" the way a shared `cards` list did.
                    tmauction.add_pending(winner or win["name"],
                                          won=rec["ii"])
                except OSError as e:
                    # PENDING FIRST, REMOVAL SECOND. If the credit does not
                    # land, the listing stays -- a card that still exists is
                    # recoverable, a deleted one is not.
                    log("authserv", f"  auction: {rec['ai']} sold but the "
                                    f"credit failed ({e}) -- holding the "
                                    f"listing rather than losing the card")
                    stuck_sold += 1
                    continue
                drop.append(i)
                changed = True
                sold += 1
                log("authserv", f"  auction: {rec['ai']} SOLD to "
                                f"{win['name']!r} for {win['amount']} -- "
                                f"seller {seller} credited, card queued")
                # THE NOTICE THE MENU PROMISES. "Messenger will send you a
                # notice regarding payment" -- as a per-user POL Message from
                # "Tetra Master", the same channel SE delivers friend requests
                # on (_mail_mint). Names the card (CardPrm) and the closing
                # bid. Fire-and-forget: the pending store is the truth, the
                # message is the nudge to go collect.
                _card = tetramaster.card_name(rec["ii"][0]) if tetramaster else ""
                _auction_notice(seller, "sold", card=_card, amount=win["amount"])
                if winner:
                    _auction_notice(winner, "won", card=_card,
                                    amount=win["amount"])
                # AND THE LOSERS -- one notice per losing bidder, AT SETTLEMENT
                # (not per-outbid, which is spammy in a contested auction). Each
                # was refunded when outbid; this is the reminder that gil is
                # waiting at Check Out. Amount = the sum of their bids on THIS
                # auction (each of which was refunded). Only when the winner is
                # resolved, so we can reliably tell losers from the winner.
                if winner:
                    _bids = _auction_bids(rec["ai"])
                    _losers = {}          # member -> [name, total refunded here]
                    for _k in range(tmauction.bid_count(_bids)):
                        _b = tmauction.read_bid(_bids, _k)
                        _bm = _b["member"] or 0
                        if not _bm and tmroom is not None:
                            _bm = tmroom.member_by_name(_b["name"]) or 0
                        if not _bm or int(_bm) == int(winner):
                            continue      # winner or unresolvable -- not a loser
                        row = _losers.setdefault(int(_bm), [_b["name"], 0])
                        row[1] += _b["amount"]
                    for _lm, (_ln, _tot) in _losers.items():
                        _auction_notice(_lm, "outbid", card=_card, amount=_tot)
                    if _losers:
                        log("authserv", f"  auction: {rec['ai']} -- outbid "
                                        f"notice to {len(_losers)} loser(s): "
                                        f"{[n for n,_ in _losers.values()]}")
                continue
            if rec["am"] > 0:
                row = blob[i * tmauction.REC:(i + 1) * tmauction.REC]
                out[i * tmauction.REC:(i + 1) * tmauction.REC] =                     tmauction.relist(row, now)
                changed = True
                relisted += 1
                log("authserv", f"  auction: {rec['ai']} did not sell -- "
                                f"relisting, {rec['am'] - 1} relist(s) left")
            else:
                # UNSOLD and out of relists -- the card goes back to its owner.
                seller = os.path.basename(fn).split(".", 1)[0]
                try:
                    tmauction.add_pending(seller, card=rec["ii"])
                except OSError as e:
                    log("authserv", f"  auction: {rec['ai']} unsold but the "
                                    f"return failed ({e}) -- holding")
                    stuck_unsold += 1
                    continue
                drop.append(i)
                changed = True
                unsold += 1
                log("authserv", f"  auction: {rec['ai']} UNSOLD -- card queued "
                                f"back to {seller}")
                _auction_notice(seller, "returned",
                                card=tetramaster.card_name(rec["ii"][0])
                                if tetramaster else "")
        if drop:
            out = bytearray(b"".join(
                bytes(out[k * tmauction.REC:(k + 1) * tmauction.REC])
                for k in range(tmauction.count(blob)) if k not in drop))
        if changed:
            try:
                _write_resource(fn, bytes(out))
            except OSError as e:
                log("authserv", f"  auction: cannot write the relist for "
                                f"{os.path.basename(fn)} ({e}) -- left as is")
    if stuck_sold or stuck_unsold:
        # One summary line, not one per listing per request: this fires on every
        # auction request and would otherwise bury the log.
        log("authserv", f"  auction: {stuck_sold} sold and {stuck_unsold} "
                        f"out-of-relist listing(s) are EXPIRED AND UNDELIVERED "
                        f"-- settlement needs the DI/BI format, so they are "
                        f"held, not dropped")


def _tm_auction_reply(payload):
    """Class-A `<ER>` (list a card) and `<SI>`+`<IO>` (my sale list).

    WHY THIS IS CODE AND NOT A polpro.json ENTRY: `<SN>` is a COUNT OF THIS
    MEMBER'S LISTINGS, and a spec key is a request SHAPE -- every member sends
    a byte-identical `<SI>`+`<IO>`. The static entry can only answer one number
    for everyone, which is why it had to be pinned to a shipped fixture. Same
    reason `_tm_rank_reply` exists one class over.

    Returns `(payload_bytes_or_None, handled)`; `handled` False falls through to
    the static spec, which answers the honest failure arm.
    """
    if tmauction is None or polpro is None or not tmauction.enabled():
        return None, False
    groups = polpro.parse(payload)
    if not groups:
        return None, False
    lead = groups[0][0]
    member = _session_get("member_id")
    # Expire before answering, so every list, browse and bid sees a store that
    # has already been brought up to date.
    _auction_sweep()

    # WARNING: A BARE `<SI>` IS THE BROWSE QUERY -- "Display all cards up for auction".
    # Measured 2026-08-20T19:15:08Z, and it took shipping `b/g/TM0AucData` to
    # see one at all: with every band count zero the menu greyed every row, so
    # the client could not send it. The SECOND GROUP is the whole discriminator
    # -- `<IO>` my sales, `<IB>` my bids, nothing at all = everybody's.
    #
    # WARNING: THE TWELVE `<SI>` CRITERIA ARE ALL ZERO IN ALL THREE FORMS, so nothing
    # here says WHICH price band was chosen. Either the band is not in the
    # request and the client filters the fetched list itself, or every band we
    # have seen was "All Cards". Do not read a band out of these values until
    # two different bands are known to have produced different bytes.
    if lead == "SI" and (len(groups) == 1 or groups[1][0] == "MP"):
        rows = _auction_all_rows()
        band = None
        if len(groups) > 1:
            # `<MP>(lo,hi)` IS THE PRICE BAND, and the CLIENT chooses the
            # bounds -- "Cheap Cards" sent `<MP>(0,1000 )` (measured 19:26:09Z,
            # note the trailing space, so strip before int()). So the four
            # filtered rows are ours to FILTER, never to invent: whatever range
            # arrives is the definition of that band.
            # WARNING: THE VALUES CARRY WIRE PUNCTUATION. Measured 19:31:54Z, the pair
            # arrives as `['1001', '2000\x07 ']` -- the group terminator AND a
            # trailing space live inside the last value. `.strip()` removes the
            # space and leaves the `\x07`, which is exactly how the first
            # attempt failed. Take the leading digits and ignore the rest
            # rather than enumerating the punctuation we happen to have seen.
            nums = []
            for v in groups[1][1]:
                m = re.match(r"\s*(-?\d+)", v)
                if m:
                    nums.append(int(m.group(1)))
            if len(nums) == 2:
                band = (nums[0], nums[1])
                rows = tmauction.filter_band(rows, band[0], band[1])
            else:
                log("authserv", f"  auction: <MP> with {groups[1][1]!r} is not a "
                                f"(lo,hi) pair -- serving the UNFILTERED list "
                                f"rather than guessing at the band")
        n = tmauction.count(rows)
        try:
            _write_resource(_resource_file(_AUCTION_BROWSE_PATH), rows)
        except OSError as e:
            log("authserv", f"  auction: cannot stage the browse list ({e}) -- "
                            f"declining so the client gets <SF>, not a count "
                            f"it cannot fetch")
            return None, False
        log("authserv", f"  auction: browse{'' if band is None else ' %d..%d' % band}"
                        f" -> {n} listing(s) ({len(rows)}B) for member {member}")
        return polpro.build([("SS", [_AUCTION_BROWSE_PATH]),
                             ("SN", [str(n)])]), True

    # `<SI>+<IB>` -- "Cards Bid On". This was answered by the STATIC polpro.json
    # entry until now, which could only ever say `<SN>`(0), so the screen was
    # permanently empty however many bids a player had placed. It is answered
    # `<SS>`, not `<HS>`: both list screens drive the EXHIBIT reader, so this
    # serves 0xC0 LISTING records -- the auctions bid on -- not 0x48 bid rows.
    if lead == "SI" and len(groups) > 1 and groups[1][0] == "IB":
        me = (tmroom.name_of(member) if tmroom is not None else "") or ""
        allrows = _auction_all_rows()
        keep = []
        for i in range(tmauction.count(allrows)):
            rec = tmauction.read_record(allrows, i)
            bids = _auction_bids(rec["ai"])
            if any(tmauction.bid_is_member(tmauction.read_bid(bids, k), member, me)
                   for k in range(tmauction.bid_count(bids))):
                keep.append(allrows[i * tmauction.REC:(i + 1) * tmauction.REC])
        rows = b"".join(keep)
        n = tmauction.count(rows)
        try:
            _write_resource(_resource_file("U/g/TM0_BIDLIST"), rows)
        except OSError as e:
            log("authserv", f"  auction: cannot stage the bid-on list ({e}) -- "
                            f"declining, the static entry answers <SN>(0)")
            return None, False
        log("authserv", f"  auction: member {member} ({me!r}) has bid on {n} "
                        f"auction(s) ({len(rows)}B)")
        return polpro.build([("SS", ["U/g/TM0_BIDLIST"]), ("SN", [str(n)])]), True

    if lead == "SI" and len(groups) > 1 and groups[1][0] == "IO":
        rows = _auction_rows()
        n = tmauction.count(rows)
        log("authserv", f"  auction: member {member} has {n} listing(s) "
                        f"({len(rows)}B stored)")
        return polpro.build([("SS", [_EXHIBIT_LIST_PATH]), ("SN", [str(n)])]), True

    # `<BH> <AI>(n)` -- the bid history of one auction. THIS IS ON THE PATH TO
    # BIDDING, not a side screen: measured in live testing that opening the bid
    # form forces a history retrieve first, so while this answered `<HF>` no bid
    # could ever be placed. That is why it comes before `<BR>`.
    if lead == "BH":
        vals = {t: v for t, v in groups}
        try:
            ai = int((vals.get("AI") or ["0"])[0].strip() or 0)
        except ValueError:
            ai = 0
        if ai <= 0:
            log("authserv", f"  auction: <BH> with no usable <AI> "
                            f"({vals.get('AI')!r}) -- declining, <HF> answers")
            return None, False
        bids = _auction_bids(ai)
        n = tmauction.bid_count(bids)
        try:
            _write_resource(_resource_file(_AUCTION_BIDHIST_PATH), bids)
        except OSError as e:
            log("authserv", f"  auction: cannot stage the bid history ({e}) -- "
                            f"declining so the client gets <HF>, not a count "
                            f"it cannot fetch")
            return None, False
        log("authserv", f"  auction: bid history for auction {ai} -> {n} bid(s) "
                        f"({len(bids)}B) for member {member}")
        # WARNING: `<SN>`(0) IS THE HONEST ANSWER AND MAY NOT BE A USABLE ONE. The
        # ranking list's `<LN>`(0) made its scene tear itself down before it
        # ever read the file, and nothing yet says this one behaves better --
        # an auction with no bids is exactly the case a player hits first. If
        # the bid form still will not open on zero, that is the same shape and
        # the fix is the same: serve one zero row and say so.
        return polpro.build([("HS", [_AUCTION_BIDHIST_PATH]),
                             ("SN", [str(n)])]), True

    # `<BR> <AI>(n) <BM>(amount) <BC>(?)` -- place a bid. Measured 19:46:15Z.
    # WARNING: `<BC>` came through as 0 and is UNREAD: we neither interpret nor echo
    # it. It has room to be a card offered alongside the bid, or a flag; until
    # one arrives non-zero there is nothing to decode and guessing would put a
    # meaning in the store that the client never sent.
    if lead == "BR":
        vals = {t: v for t, v in groups}

        def _num(tag):
            try:
                return int((vals.get(tag) or ["0"])[0].strip() or 0)
            except ValueError:
                return None

        ai, bm = _num("AI"), _num("BM")
        if not ai or bm is None:
            log("authserv", f"  auction: <BR> with unusable <AI>/<BM> "
                            f"({vals.get('AI')!r}/{vals.get('BM')!r}) -- "
                            f"declining, <BF> answers")
            return None, False
        found = _auction_find(ai)
        if found is None:
            log("authserv", f"  auction: <BR> for auction {ai}, which no store "
                            f"holds -- declining, <BF> answers")
            return None, False
        fn, blob, idx = found
        rec = tmauction.read_record(blob, idx)
        floor = tmauction.min_bid(rec)
        if bm < floor:
            # The CLIENT computes this same floor and pre-fills it, so a bid
            # under it means we and it disagree about the listing -- refuse
            # rather than record a number the seller's screen will contradict.
            log("authserv", f"  auction: <BR> {bm} on auction {ai} is under the "
                            f"floor {floor} (<CM> {rec['cm']} + <BI> "
                            f"{rec['bi']}) -- declining, <BF> answers")
            return None, False
        bidder = (tmroom.name_of(member) if tmroom is not None else "") or ""
        # WARNING: SELF-BIDDING IS ALLOWED, and that is not an endorsement. The client
        # offers it (measured live: a seller bid on their own listing), and we do
        # not know whether SE's server refused. Inventing the rule here would be
        # inventing game behaviour; if it should be refused, that belongs in a
        # measurement, not a guess.
        now = int(time.time())
        # THE PREVIOUS HIGH BIDDER, captured BEFORE this bid is appended -- it
        # is who gets outbid and refunded. The bid store's last row is the
        # current high bid.
        _prev_bids = _auction_bids(ai)
        _outbid = (tmauction.read_bid(_prev_bids,
                                      tmauction.bid_count(_prev_bids) - 1)
                   if tmauction.bid_count(_prev_bids) else None)
        # <BC> gets the bid count INCLUDING this one -- the row we are about to
        # append. See apply_bid: this is the field believed to gate the bidder
        # column, and an off-by-one there would be a silent "-" again.
        updated = tmauction.apply_bid(
            blob[idx * tmauction.REC:(idx + 1) * tmauction.REC], bidder, bm,
            bid_count=tmauction.bid_count(_prev_bids) + 1)
        new_blob = (blob[:idx * tmauction.REC] + updated
                    + blob[(idx + 1) * tmauction.REC:])
        try:
            _write_resource(fn, new_blob)
            _write_resource(_auction_bids_file(ai),
                            _prev_bids
                            + tmauction.build_bid(bidder, bm, now, member))
        except OSError as e:
            log("authserv", f"  auction: cannot record the bid ({e}) -- "
                            f"declining so the client gets <BF>, not a bid we "
                            f"did not keep")
            return None, False
        # *** ESCROW. The client debits ITSELF the bid amount on confirm
        # ("Pay this bid amount and update the high bid?", Aucti 98) -- verified
        # in game -- so we MIRROR it server-side or the next wallet sync
        # (/M=money_of()) restores the gil, exactly the trap the settlement
        # credit hit. And the previous high bidder is REFUNDED (Aucti 229 "Your
        # bid has been refunded"), collected at Check Out's Bid Refund section
        # (money-B). WARNING: NOT CONFIRMED LIVE: whether money-B credits the wallet
        # like money-A -- confirm on the first refund.
        try:
            _was = tetramaster.money_of(member)
            tetramaster._set_money(member, max(0, _was - bm),
                                   f"auction {ai} bid held ({bm})")
            if _outbid and _outbid["amount"]:
                _pm = _outbid["member"] or 0
                if not _pm and tmroom is not None:
                    _pm = tmroom.member_by_name(_outbid["name"]) or 0
                if _pm:
                    tmauction.add_pending(_pm, refund=_outbid["amount"])
                    log("authserv", f"  auction {ai}: {_outbid['name']!r} "
                                    f"(member {_pm}) OUTBID -- {_outbid['amount']}"
                                    f" gil queued as a Bid Refund")
                else:
                    log("authserv", f"  auction {ai}: previous high bidder "
                                    f"{_outbid['name']!r} has no resolvable "
                                    f"member id -- {_outbid['amount']} gil refund "
                                    f"NOT queued; recover by hand")
        except Exception as _e:                              # pragma: no cover
            log("authserv", f"  auction {ai}: bid recorded but the escrow move "
                            f"FAILED ({_e}) -- the bid stands, gil accounting "
                            f"needs a hand")
        d = tmauction.read_record(updated)
        log("authserv", f"  auction: member {member} ({bidder!r}) bid {bm} on "
                        f"auction {ai}; <CM> now {d['cm']}")
        return polpro.build(tmauction.bs_groups(d)), True

    if lead == "ER":
        vals = {t: v for t, v in groups}
        ii = vals.get("II")
        if not ii or len(ii) != 16:
            # NOT OURS TO ANSWER. A malformed <II> means we would be storing a
            # card we cannot hand back intact, and <II> must survive verbatim.
            log("authserv", f"  auction: <ER> with a {len(ii or [])}-value <II> "
                            f"-- declining, the static <EF> answers")
            return None, False
        rows = _auction_rows()
        n = tmauction.count(rows)
        try:
            rec = tmauction.new_listing(
                [int(x) for x in ii],
                seller=(tmroom.name_of(member) if tmroom is not None else "") or "",
                sp=int(vals.get("SP", ["0"])[0]),
                bi=int(vals.get("BI", ["0"])[0]),
                et=int(vals.get("ET", ["0"])[0]),
                am=int(vals.get("AM", ["0"])[0]),
                # GLOBAL, not per member -- the max `<AI>` across EVERY store,
                # plus one. `<AI>` is the only thing a `<BH>` or a `<BR>` sends
                # to say WHICH auction, and browse puts every member's rows in
                # one list, so a per-member id cannot identify one there. This
                # was per-member for about an hour; it is fixed before any bid
                # can arrive, and before enough listings exist to renumber.
                #
                # Deriving it from what is on disk rather than from a counter
                # file is deliberate: a counter is a second source of truth
                # that can get out of step. But the ROWS alone are not enough
                # -- a settled auction's bid file outlives its row, see
                # `_auction_next_id`.
                auction_id=_auction_next_id())
        except (ValueError, TypeError) as e:
            log("authserv", f"  auction: <ER> fields unparsable ({e}) -- "
                            f"declining, the static <EF> answers")
            return None, False
        blob = rows + rec
        try:
            _write_resource(_auction_store_file(), blob)
        except OSError as e:
            # STORING IS THE POINT. Answering <ES> for a listing we did not keep
            # is the AUCMONEY trap one door over -- the client would accept a
            # sale with no backing record and never reconcile it. Fall through
            # to <EF> instead, which is honest and un-hangs the screen.
            log("authserv", f"  auction: cannot store the listing ({e}) -- "
                            f"declining so the client gets <EF>, not a lie")
            return None, False
        d = tmauction.read_record(rec)
        log("authserv", f"  auction: member {member} listed card {d['ii'][0]} "
                        f"as auction {d['ai']} for {d['sp']} "
                        f"(ends {d['nc']}); {tmauction.count(blob)} listing(s)")
        return polpro.build(tmauction.es_groups(d)), True

    return None, False


def _tm_rank_reply(payload):
    """The `<RF>`+`<LN>` answer to a class-R `<RR>`, or fall through.

    `<RR>(id)` names ONE OF FIVE LISTS (`tmrank.LISTS`; `0x8AE30` rejects an id
    >= 5), and polpro.json cannot tell them apart -- a spec key is a TAG, not a
    value, so the static `TM0:RR` entry answers every list with one path and one
    row count. That was correct while the only content was a single zero row and
    is wrong the moment there is a tally: VS. Ratings and Grand Total are the
    same players in a different ORDER, and the order is the ranking.

    So this names `U/g/TM0_RANKLIST<id>` and answers `<LN>` = the number of rows
    THAT FILE ACTUALLY HOLDS. The count and the bytes are read from the same
    file by the same function, which is what makes them impossible to disagree
    -- the failure this replaces is a client told 1 row and handed 232 bytes of
    something else, i.e. POL-5135.

    Returns `(payload_bytes_or_None, handled)`. `handled` False means the static
    spec entry answers, exactly as before.
    """
    if tmrank is None or polpro is None:
        return None, False
    if os.environ.get("POL_TM_RANK", "1") != "1":
        return None, False
    try:
        groups = polpro.parse(payload)
        if not groups or groups[0][0] != "RR":
            return None, False
        try:
            rank_id = int((groups[0][1] or ["2"])[0])
        except ValueError:
            return None, False
        if rank_id not in tmrank.LISTS:
            # The client's own gate refuses these before they reach the wire, so
            # one arriving means a reading of ours is wrong. Say so and let the
            # static entry answer rather than naming a file that cannot exist.
            log("authserv", f"  rankings: <RR>({rank_id}) is not one of "
                            f"{sorted(tmrank.LISTS)} -- falling through")
            return None, False
        path = tmrank.path_for(rank_id)
        blob = _rank_list_blob(path)
        rows = len(blob) // tmrank.REC if blob else 0
        if rows <= 0:
            # `<LN>` 0 is not "an empty list", it is "close the screen with no
            # error" (rva 0x1750AE). Fall through to the static entry, which
            # names the one-row fixture.
            log("authserv", f"  rankings: nothing to serve for RankID {rank_id} "
                            f"-- falling through to the polpro.json entry")
            return None, False
        rows = min(rows, tmrank.MAX_ROWS)     # 0x8AF00: 0 < num <= 100
        menu, label, key, _ = tmrank.LISTS[rank_id]
        log("authserv", f"  rankings: <RR>({rank_id}) = {label} (menu {menu}, "
                        f"ranked on {key}) -> {path} with {rows} row(s)")
        return polpro.build([("RF", [path]), ("LN", [str(rows)])]), True
    except Exception as exc:
        log("authserv", f"  rankings: reply failed ({exc!r}) -- the polpro.json "
                        f"entry still stands")
        return None, False


def _tm_pool_note(payload):
    """`<CR>` is the client handing us its CHARACTER POOL. Keep it.

    Measured 2026-08-20 off this very log -- three accounts, all the same shape:

        <CR>(0x000000003B9ACA66,1000000102) <AN>(1000000102) <CI>(1,Card Level 0)

    and `0x3B9ACA66 == 1000000102 == accounts._default_content_id(1, 2)`. So the
    identity the rankings row is matched on (TM.dll rva 0x17545E, row+0x10/+0x14
    against `0x52452A0`/`0x52452A4`) is the member's Tetra Master **Content ID**,
    and the client tells it to us every session.

    We RECORD it rather than only computing it, for the reason `tmroom.
    note_pol_id` records `/NN=` verbatim: a value echoed back cannot be wrong,
    and a derived one is only right until the thing it derives from moves. The
    computed value is still checked against it here, because that check is free
    and it is the one thing that would otherwise fail SILENTLY -- a mismatched
    cid does not error, it just tells the player "Did not rank" while their own
    name is on the screen.

    `<CI>`'s second value is the client's own status string (`Card Level 116`) --
    a live per-player stat nothing else here captures.
    """
    if tmrank is None or polpro is None:
        return
    try:
        groups = dict(polpro.parse(payload))
        if "CR" not in groups:
            return
        member = _session_get("member_id")
        if not member:
            return
        cr = groups.get("CR") or []
        ci = groups.get("CI") or []
        cid = int(cr[0], 16) if cr and cr[0].lower().startswith("0x") else None
        csid = int(ci[0]) if ci and ci[0].lstrip("-").isdigit() else None
        # ASK THE DB, don't recompute (2026-08-23). `cid_for`'s fallback is the
        # RETRACTED computed mint, which is still right for every account that
        # predates the allocator and wrong for every account after it -- so
        # handing it the stored value is the difference between a real alarm and
        # a WARNING: line on every new player's first Tetra Master session.
        want = tmrank.cid_for(member, _member_content_id(member,
                                                         tmrank.POOL_CONTENT_CODE))
        if cid is not None and cid != want:
            log("authserv", f"  WARNING: TM pool: member {member} calls itself {cid} "
                            f"but our Content ID mint says {want} -- the ranking "
                            f"row would be matched against the wrong identity. "
                            f"Recording what the CLIENT said; see tmrank.cid_for")
        if tmrank.note_pool(member, cid=cid, csid=csid,
                            cname=(cr[1] if len(cr) > 1 else None),
                            cinfo=(ci[1] if len(ci) > 1 else None)):
            log("authserv", f"  TM pool: member {member} cid={cid} csid={csid} "
                            f"{(ci[1] if len(ci) > 1 else '')!r} recorded for the "
                            f"ranking tally")
    except Exception as exc:
        log("authserv", f"  TM pool: could not record this <CR> ({exc!r}) -- the "
                        f"tally falls back to the Content ID mint")


def _roster_note_guid(member_id):
    """Record the LOBBY-BAND self id for the member record's `+0x00`.

    `_capture_self_guid` already learns this from `u/account`'s fetch subject and
    stores it as `handle.client_guid`; this just carries it into the roster. It
    is the value the working placeholder carried (0x860FB3E2A2) and it has the
    right shape -- 44-bit guid plus a 6-bit slot -- where the 64-bit
    `@Init=/NN=` id does not.
    """
    if tmroom is None or accounts is None or not member_id:
        return
    # THE NAME THE ROW DRAWS, AND IT IS NOT BEHIND THE HANDLE-ID GATE. The
    # client sends value 4 empty and cannot do better -- it does not know other
    # players' handles -- and `+0x28` is what the room screen draws,
    # so without this the row appears with NO NAME ON IT. It needs only the
    # member, so a session whose handle we cannot resolve still gets a name;
    # putting it below the `hid` check would have silently skipped exactly the
    # sessions most likely to need it.
    try:
        nm = _member_display_name(member_id)
        if nm and tmroom.note_name(member_id, nm):
            log("authserv", f"  roster: member {member_id} draws as {nm!r}")
    except Exception as exc:
        log("authserv", f"  roster: cannot name member {member_id} ({exc!r})")
    try:
        db = accounts.connect(os.environ.get("POL_ACCOUNTS_DB", accounts.DEFAULT_DB))
        try:
            hid = _session_handle_id(db)
            if not hid:
                return
            row = db.execute("SELECT client_guid FROM handle WHERE id = ?",
                             (int(hid),)).fetchone()
            g = int((row["client_guid"] or 0)) if row is not None else 0
            if g and tmroom.note_guid(member_id, g):
                log("authserv", f"  roster: member {member_id} (handle {hid}) "
                                f"is {g:#x} on the lobby band -- recorded")
            # WARNING: SAY IT OUT LOUD WHEN WE ARE GUESSING. With no `client_guid` the
            # record's `+0x00` falls back to our own row id: correctly SHAPED,
            # resolvable by nobody, and the row goes quiet with nothing to show
            # for it. `client_guid` is learned from the `u/account` fetch
            # subject, so it arrives only after that client has fetched it once.
            elif tmroom.guid_is_placeholder(member_id)                     and tmroom.warn_once(member_id):
                log("authserv",
                    f"  roster: member {member_id} (handle {hid}) has NO "
                    f"client_guid yet, so its member record carries our row id "
                    f"-- the row may not resolve on any client until that "
                    f"handle fetches u/account")
        finally:
            db.close()
    except ValueError as exc:
        log("authserv", f"  roster: REFUSED a lobby-band id -- {exc}")
    except Exception as exc:
        log("authserv", f"  roster: cannot read the lobby-band id ({exc!r})")


def _roster_note_departure(chan):
    """Retire this connection's member record when they PART a Tetra Master room."""
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    try:
        if not bytes(chan).startswith(b"#TM0R"):
            return
        member = _session_get("member_id")
        # WARNING: A PART NO LONGER RELEASES SEATS (2026-08-20, POL_TM_SEAT_PERSISTS).
        # The client keeps its reservation across a room exit -- it sends no
        # exit message on leaving and offers an explicit "Cancel reservation"
        # in the tile menu -- so releasing here manufactured a live divergence:
        # the owner's client showed the yellow reserved tile while the server
        # had freed the table, and the second player was offered it as free and
        # took it out from under him (measured 23:32:26). The seat now dies
        # with the SESSION (retire-on-close, retire-stale, the reconcile's
        # no-live-session sweep) or by the client's own cancel (@GameExit=).
        # The ROOM RECORD still retires below: presence in the room and a claim
        # on a table are different facts with different lifetimes now.
        if member and tetramaster is not None \
                and os.environ.get("POL_TM_SEAT_PERSISTS", "1") != "1":
            tetramaster.release_seats(member)
        if member and tmroom.forget_member(member):
            log("authserv", f"  roster {chan.decode('latin1', 'replace')}: "
                            f"member {member} PARTED -- record retired, PC "
                            f"delta queued for everyone still in the room")
    except Exception as exc:
        log("authserv", f"  roster: departure not recorded ({exc!r})")


def _roster_retire_stale(chan):
    """Retire records whose member has NO session anywhere and an OLD record.

    WARNING: THE MEMBER-RECORD EDITION OF THE TABLE-STATE INVARIANT, reported twice in
    one evening (2026-08-20: "it says all three users are in that room" and then
    "LT2 shows PCTest being in the room... it hasn't been online in like two
    hours"). `_roster_retire_on_close` covers a close THIS process sees; a close
    that lands while the process is down -- five deploys ran that evening --
    lands on nobody, and the record is then adopted from the file forever.

    TWO conditions, BOTH required, because each alone is the bug its own banner
    warns about:

      * `PRESENCE.sessions_for(mid)` is empty -- no live connection anywhere on
        the service. A DEPLOY BOUNCE survivor is protected by the second test,
        not this one: their client pongs within ~2 s of the relay re-attaching,
        so they hold a session again long before anyone's `<DE>` sweeps a room.
      * the record's own timestamp (value 6, epoch seconds -- the field the
        client stamps on every re-send) is older than POL_TM_RECORD_STALE_S
        (default 600). This is what keeps a moment of session churn from
        blinking somebody out -- the fault `tmroom.note_member`'s banner records
        the last socket-keyed presence attempt causing.

    Retiring emits the `PC` delta through `forget_member`, so every client in
    the room watches them leave rather than holding the ghost -- and a mistaken
    retire heals on the member's next event, exactly like a missed one does.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    if os.environ.get("POL_TM_RETIRE_STALE", "1") != "1":
        return
    try:
        grace = float(os.environ.get("POL_TM_RECORD_STALE_S", "600") or 600)
    except ValueError:
        grace = 600.0
    now = time.time()
    for mid, values in list(tmroom.members(chan)):
        if PRESENCE.sessions_for(mid):
            continue                    # connected somewhere: not ours to judge
        try:
            stamped = float(values[6])
        except (TypeError, ValueError, IndexError):
            stamped = 0.0               # an unstamped record cannot prove youth
        if stamped and now - stamped < grace:
            continue                    # recent: could be a bounce in progress
        if tetramaster is not None:
            tetramaster.release_seats(mid, chan)   # seats first, as ever
        if tmroom.forget_member(mid):
            log("authserv",
                f"  roster {chan}: member {mid} has NO live session and a "
                f"record {int(now - stamped) if stamped else '?'}s old -- "
                f"retired (the close that should have done this landed while "
                f"no process was listening)")


def _roster_retire_on_close(member_id):
    """The member's connection died without a PART. Retire their room record.

    VERIFIED: THIS IS HOW THE ORIGINAL DID IT, and that is a measurement, not a
    preference. `<PD>` is **event-driven, not periodic** -- measured 2026-08-20
    across a live session, the gaps between one member's own record re-sends run
    2 s, 5 s, three minutes, ninety minutes -- so there is no heartbeat on this
    channel for an expiry to work against, and SE cannot have aged records out
    either. `PART` and class-L `<PC>` are the CLEAN exits; the connection itself
    is the only thing that can cover the dirty ones.

    WARNING: **NOT the reverted socket-presence filter.** That one judged presence from
    current IRC membership on EVERY READ, so a moment's churn blinked people out
    of each other's lists -- `tmroom.note_member`'s banner is right that presence
    belongs to the record. This retires ONCE, on an actual close, which is the
    same shape as the `PART` this module already trusts.

    WARNING: **THE SERVER-RESTART CASE IS EXCLUDED BY CONSTRUCTION -- do not "fix" it
    with a shutdown flag.** This runs from a channel handler's `finally`, and the
    service installs no SIGTERM handler, so a container restart kills the process
    without ever running it. That is the same fact the deliberate-close bit above
    relies on ("it stays up only on the path that never runs a `finally`: being
    killed"). A deploy bounce therefore leaves every record standing, which is
    what we want: the client is still there, we are the ones who went away.
    WARNING: IF A SIGNAL HANDLER IS EVER ADDED TO THIS SERVICE THIS COMMENT BECOMES
    WRONG, and a player sitting still in a room will blank out of everyone's
    list after every deploy.
    """
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    if os.environ.get("POL_TM_RETIRE_ON_CLOSE", "1") != "1":
        return
    try:
        # SEATS FIRST, WHILE `room_of` CAN STILL PLACE THEM. Retiring the record
        # first orphans the seat -- that is `_roster_note_departure`'s own
        # hard-won ordering, and the reason it carries a comment about it.
        chan = tmroom.room_of(member_id)
        if not chan:
            return                      # not in a TM room -- nothing to retire
        if tetramaster is not None:
            tetramaster.release_seats(member_id)
        if tmroom.forget_member(member_id):
            log("authserv", f"  roster {chan}: member {member_id} DROPPED "
                            f"(connection closed without a PART) -- record "
                            f"retired, PC delta queued for everyone still in "
                            f"the room")
    except Exception as exc:
        log("authserv", f"  roster: close-retire failed ({exc!r}) -- ignored")


def _roster_note_room_peer(target):
    """Record the room peer guid behind a class-L target nick. Never raises."""
    if tmroom is None or tetramaster is None:
        return
    try:
        guid = tetramaster.peer_guid(target)
        if guid is None or tetramaster.peer_class(guid) != tetramaster.ROOM_PEER_CLASS:
            return
        chan = _room_of_member(_session_get("member_id"))
        if chan and tmroom.note_room_peer(chan, guid):
            log("authserv", f"  roster {chan}: room peer is {guid:#x} (nick "
                            f"{target!r}) -- table peers index against it")
    except Exception as exc:
        log("authserv", f"  roster: room peer not recorded ({exc!r})")


def _roster_note(payload, tag):
    """Keep a class-L `<DE>`/`<PD>` member record. Never raises -- a roster miss
    must not cost the client its reply."""
    if tmroom is None or os.environ.get("POL_TM_ROSTER", "1") != "1":
        return
    try:
        groups = polpro.parse(payload)
        if not groups:
            return
        cmd, values = groups[0]
        member_id = _session_get("member_id")
        # WARNING: `<PC>` AND `@Init=/NN=` ARE THE WRONG ID -- retracted 2026-08-18.
        # They carry a 64-BIT value (0xAB12CD56EB0F5932) and the member record
        # wants the LOBBY-BAND self id (0x860FB3E2A2), which is what the working
        # placeholder carried and what `u/account`'s fetch subject is. The shape
        # says why: an id here is a 44-bit guid plus a 6-bit SLOT,
        # so `>> 44` must be a real slot -- 0 for the
        # working value, garbage for the 64-bit one, and the row vanishes.
        if cmd == "PC":
            # ...BUT IT IS STILL THE DEPARTURE, AND DROPPING IT LEFT A GHOST.
            # Measured 2026-08-20, live, two members in #TM0R001: one walked out,
            # the other kept seeing them in the member list for as long as the
            # room stayed open. The frame arrives exactly as it should --
            #     00:25:54  polpro L <- <PC>(0xAB12CD56EB0F5932)
            # -- and `tmroom.forget_member` has been sitting here able to answer
            # it since it was written; its ONLY caller in the tree was its own
            # selftest.
            #
            # The retraction above is about the PAYLOAD's id, and it stands: that
            # 64-bit value cannot build a record. It does not follow that the
            # message is useless, because we do not need it to say WHO -- `<PC>`
            # arrives on the leaver's OWN session, so the session's member_id is
            # the answer, and it is the same id `<DE>` was recorded under.
            #
            # Entering a room sends `<PC>` for the previous one (measured: PC at
            # 00:25:54, `<DE>` for the new room at 00:26:22), so a removal that
            # is immediately followed by a re-add is normal and self-correcting.
            # READ THE ROOM FIRST -- forget_member drops the record, and
            # `room_of` then has nothing to answer from. `members("?")` is worse
            # than useless here: room_id_for falls back to room 1, so the count
            # would silently be some other room's.
            # VERIFIED: AND ITS PAYLOAD IS THE **POL-ID**, WHICH IS WORTH KEEPING.
            # The retraction above says this 64-bit value cannot build a member
            # record, and that stands. It is still a real id with a real reader:
            # Tetra Master's chat roster matches `/NN=` against exactly this
            # number (TM.dll 0x0AFE3, fed from `@Init=/NN=`), so `<PC>` is a
            # second source for it on a session where we missed the `@Init=`.
            # It goes to `note_pol_id`, NOT to `note_guid` -- those are two
            # different ids for the same person and the latter refuses this one.
            if member_id and values:
                try:
                    # polpro hands the value back with its parentheses on --
                    # `<PC>(0xAB12CD56EB0F5932)` parses to `'(0x...)'` -- and
                    # `note_pol_id` takes the wire form as it comes.
                    tmroom.note_pol_id(member_id, values[0])
                except Exception:
                    pass
            chan = tmroom.room_of(member_id) if member_id else None
            if member_id and tmroom.forget_member(member_id):
                log("authserv",
                    f"  roster: <PC> from member {member_id} -- dropped from "
                    f"{chan or '?'}, "
                    f"{len(tmroom.members(chan)) if chan else 0} left")
            return
        if cmd not in ("DE", "PD") or len(values) < 11:
            return
        # NO ROOM LOOKUP. `<DE>` arrives BEFORE the IRC JOIN (measured: 07:36:25
        # against a JOIN 1.5 s later), so requiring one threw away every room
        # entry. The record carries its own room at value 9.
        _roster_note_guid(member_id)
        if tmroom.note_member(member_id, values):
            chan = tmroom.room_of(member_id) or "?"
            log("authserv",
                f"  roster {chan}: {tag.decode('latin1')} <{cmd}> from member "
                f"{member_id} -- {len(tmroom.members(chan))} in the room, "
                f"sequence {tmroom.sequence(chan)}")
            # EVERY ROOM ENTRY RECONCILES THE ROOM. The <DE> precedes the PTL
            # fetch (it arrives even before the IRC JOIN -- see the banner
            # above), so the state is healed before anybody can look at it.
            # Idempotent; publishes only on change; never costs the reply.
            # RECORDS FIRST: the seat sweep keys on members(chan), so a record
            # retired here frees its seats in the same pass.
            if cmd == "DE" and chan != "?":
                try:
                    _roster_retire_stale(chan)
                except Exception as exc:
                    log("authserv",
                        f"  roster {chan}: stale-record sweep failed ({exc!r})")
                if tetramaster is not None:
                    # ENTERING A ROOM RETIRES A SEAT LEFT IN ANOTHER ONE. A seat
                    # cannot outlive the presence that made it, and a member is
                    # present in exactly one room -- so their <DE> here is proof
                    # any seat they hold elsewhere is stale. reconcile can't see
                    # it (its holder still has a session), so it is swept from the
                    # room CHANGE. See tetramaster.release_seats_elsewhere.
                    try:
                        tetramaster.release_seats_elsewhere(member_id, chan)
                    except Exception as exc:
                        log("authserv",
                            f"  roster {chan}: seat-elsewhere sweep failed ({exc!r})")
                    try:
                        tetramaster.reconcile_room_tables(chan)
                    except Exception as exc:
                        log("authserv",
                            f"  roster {chan}: reconcile failed ({exc!r})")
    except Exception as exc:
        log("authserv", f"  roster: {exc!r} -- ignored, the reply still stands")


#: *** A TETRA MASTER CHAT LINE HAS TO BE RE-BROADCAST, NOT ECHOED. ***
#:
#: The client SENDS `42 00 08 00 @Chat=...` -- code 0x42, msgid **8**. Relaying
#: that verbatim is what we did for weeks, and it produces exactly the symptom
#: seen in live testing: the "new message" icon lights up and the chat panel
#: stays empty. TM.dll dispatches msgid at rva 0x07E3FF and the two arms do
#: different jobs:
#:
#:     msgid 8 (0x07E8A9)  parses @Chat, sets flags        -> THE INDICATOR
#:     msgid 4 (0x07E42F)  parses @Chat + /Num= + /C=,
#:                         then formats a display line     -> THE CHAT LOG
#:
#: Only msgid 4 ever puts text in the panel, and nobody had ever been sent one --
#: which is also why the SENDER never saw their own line. This client does not
#: self-render; the log is fed only by what comes back from the server.
#:
#: VERIFIED: AND ITS GATE IS ENTIRELY OURS TO SATISFY -- read off the binary rather than
#: guessed, after two rounds of guessing were offered and declined. Both operands
#: come from the same `0xA9780(char_offset)` two-hex-digit reader that decoded the
#: save header:
#:
#:     a9780(0) -> frame+0x41C = CODE
#:     a9780(4) -> frame+0x420 = MSGID    (the 4/6/8 dispatch reads this)
#:     a9780(6) -> frame+0x424 = SHOP
#:
#:     0x07E4A2  mov ecx, [esp+0x444]   -> frame+0x424 = the header's SHOP byte
#:     0x07E4A9  mov eax, [esp+0x34]    -> frame+0x14  = the parsed `/Num=`
#:     0x07E4B0  cmp ecx, eax / jne bail
#:
#: So **`/Num=` must equal the header's shop byte**. Not a sequence, not a
#: counter, nothing derived from client state -- we write both halves. `/C=` is
#: parsed just before the gate and takes no part in it.
#:
#: WARNING: SENT TO THE SENDER TOO, deliberately: `ROOMS.broadcast`'s usual `exclude`
#: is right for a protocol where the client draws its own line, and this one does
#: not. If a duplicate ever appears for the sender, that is the knob to turn
#: first. `POL_TM_CHAT_RELAY=0` restores the verbatim relay.


def _tm_chat_rebroadcast(body):
    """A TM0 `@Chat=` line rewritten into the msgid-4 form, or None.

    None means "not a Tetra Master chat line" and the caller relays verbatim --
    group chat on `#XXL` is a different subsystem with its own measured envelope
    and must not be touched by this.
    """
    # WARNING: OFF BY DEFAULT -- THE msgid-4 HYPOTHESIS IS FALSIFIED. Measured on CASPC
    # with the receiver instrumented, 2026-08-20:
    #
    #   msgid 8 (verbatim)  -> the "new message" indicator LIGHTS, panel empty
    #   msgid 4 (rewritten) -> no indicator, panel empty, and the receiver's
    #                          trace shows NO `find("@Chat")` AT ALL -- the only
    #                          two find() calls in the whole session are
    #                          @TeachDVAns and @InitAns
    #
    # So msgid 4 never reaches the chat handler; it is dropped before it. msgid 8
    # WAS being ingested -- the indicator is the proof -- and this transform took
    # a message that got in and made one that does not.
    #
    # What that re-establishes: the empty panel under msgid 8 is a problem AFTER
    # ingestion, not a dispatch problem. The 4/6/8 switch at 0x07E3FF is real and
    # reads the msgid, but arm 4 is evidently not reachable for a line arriving
    # this way -- reading the switch correctly did not mean the arm was ours to
    # aim at.
    #
    # KEPT, not deleted: the rewrite is correct as written and one env flip away,
    # and the next question needs it. Nobody has yet watched a msgid-8 chat line
    # arrive at an INSTRUMENTED receiver -- every observation of msgid 8 was made
    # before CASPC had its trace back. Do that before changing anything else.
    if os.environ.get("POL_TM_CHAT_RELAY", "0") != "1":
        return None
    if not body.startswith(b"GTM0G") or b"@Chat=" not in body:
        return None
    head, cmd = body[5:13], body[13:]
    if len(head) != 8:
        return None
    try:
        code = struct.unpack("<I", bytes.fromhex(head.decode("ascii")))[0]
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        msgid = int(os.environ.get("POL_TM_CHAT_MSGID", "4"), 0) & 0xFF
        # SHOP 1, NOT 0 -- and this value satisfies BOTH readings of the gate.
        #
        # MEASURED 2026-08-20: with msgid 8 the client lit its "new message"
        # indicator and drew nothing; with msgid 4 the indicator STOPPED. So the
        # rewritten header is reaching the client and being dispatched on, and
        # the msgid-4 arm is running and BAILING -- delivery was never the issue.
        #
        # The only gate in that arm compares the header's shop byte against a
        # slot the `/Num=` parse fills (0xAB080's 4th argument). What was never
        # verified is WHICH of that parser's two output pointers gets what:
        #
        #   if it holds the parsed VALUE     -> /Num=1 vs shop 1 matches
        #   if it holds a FOUND-FLAG/LENGTH  -> present => 1, vs shop 1 matches
        #
        # shop 0 / Num=0 passes only the first reading, and it did not render --
        # which is itself evidence for the second. 1 covers both, so this is one
        # test rather than a permutation sweep.
        shop = int(os.environ.get("POL_TM_CHAT_SHOP", "1"), 0) & 0xFF
    except ValueError:
        msgid, shop = 4, 0
    out = (body[:5]
           + struct.pack("<I", (code & 0xFF) | (msgid << 16) | (shop << 24))
                 .hex().upper().encode("ascii")
           + cmd)
    if b"/Num=" not in out:
        out += b"/Num=%d" % shop          # THE GATE: must equal the shop byte
    return out


#: *** THE CHAT-LINE FIX: THE SENDER'S NAME GOES BETWEEN `#CHAT#` AND THE TAB. ***
#:
#: Read off TM.dll 2026-08-21. The wire
#: text of a chat line is `/Dt=#CHAT#<name>\t<text>` -- Jan's drain spells it out
#: (`sprintf("%s%s%c%s", "#CHAT#", name, '\t', text)`, janwire.py) -- and the
#: client sends the name EMPTY, the same server-fills-it shape as `<DE>`'s name
#: (tmroom.note_name) and `@Chat=/NN=`. On receive, the parser at 0xAB810
#: REQUIRES the marker and strips it, then the panel widget (event 0x300005D arm,
#: 0xCF97) splits on the TAB and DISCARDS the line unless the name part is
#: 1..15 bytes (0xCFDE jle / 0xCFE7 jge -> bail). A verbatim relay therefore
#: delivers `<empty>\t<text>` and every line dies there -- on every screen, for
#: every receiver, and for the sender too (the client's local self-echo carries
#: its own empty name, so it bails identically; the panel is fed only by what
#: comes back from the server).
#:
#: Hence also: SENT TO THE SENDER TOO, like the msgid-4 experiment above and for
#: the same measured reason ("this client does not self-render").
#:
#: The name is trimmed, never padded: a 15-space-padded record name would land
#: the tab at byte 16 and hit the other side of the same gate. Fallback is the
#: IRC nick -- a real identifier of the sender, not a fabrication -- because an
#: unknown name would otherwise mean the line stays invisible, which is the bug.
#:
#: `POL_TM_CHAT_SENDER_NAME=0` restores the verbatim relay. A line that already
#: carries a name between the marker and the tab is left alone.
def _tm_chat_fill_name(body, sess, nick, member_id=None):
    """A TM0 `@Chat=` chat line (code 0x42 msgid 8) with the sender's name
    filled into `/Dt=#CHAT#<name>\\t`, or None to relay verbatim.

    The sender is named either by their ChatSession (`sess`, the room-relay
    caller) or by an explicit `member_id` (the whisper caller, where the game
    envelope handler has only the thread-local session)."""
    if os.environ.get("POL_TM_CHAT_SENDER_NAME", "1") == "0":
        return None
    if not body.startswith(b"GTM0G") or b"@Chat=" not in body:
        return None
    head = body[5:13]                     # 8 hex chars: code . msgid shop
    if len(head) != 8 or head[0:2] != b"42" or head[4:6] != b"08":
        return None                       # roster arms and other codes: not ours
    at = body.find(b"/Dt=#CHAT#")
    if at < 0:
        return None
    ins = at + len(b"/Dt=#CHAT#")
    tab = body.find(b"\t", ins)
    if tab < 0 or tab != ins:
        return None                       # no tab, or a name is already there
    if member_id is None and sess is not None:
        member_id = _sess_member_id(sess)
    name = ""
    if tmroom is not None and member_id is not None:
        try:
            name = tmroom.name_of(member_id) or ""
        except Exception:
            name = ""
    source = "tmroom"
    if not name:
        name = (nick or b"").decode("latin1", "replace")
        source = "nick"
    # The gate is a BYTE position: 1..15 bytes before the tab. cp932 because
    # that is the client's text encoding; names are ASCII in practice.
    nb = name.strip().encode("cp932", "replace")[:15]
    if not nb:
        return None
    log("authserv", f"  TM chat: sender name '{nb.decode('cp932', 'replace')}' "
                    f"({source}) filled into /Dt= for {nick.decode('latin1')}")
    return body[:ins] + nb + body[ins:]


# ---------------------------------------------------------------------------
# the game envelope (class G on the auth band)
# ---------------------------------------------------------------------------
def notice(cls, payload, text, target, nick, srv, sess, tag=b"TM0"):
    """Tetra Master's own text format on the game envelope. Same
    envelope/class as Janhourou's binary channel ('G'), but the body is
    `<8 hex code><command>` -- see tetramaster.handle_line. The shared
    POLpro classes (P/R/A/L) are the core's: PASS hands them back.
    """
    if cls in (b"P", b"R", b"A", b"L"):
        return titles.PASS
    # *** A WHISPER IS DELIVERED, NOT ANSWERED. *** Measured live
    # 2026-08-21T01:45:38Z: a whisper is this same game envelope addressed
    # to the TARGET member's NICK --
    #     NOTICE USJ49D490 :GTM0G42000801@Chat=/La=1/Dt=#CHAT#\tUWAH
    # -- code 0x42 msgid 8, header shop byte 01 (the byte the panel widget
    # maps to display mode 2, the whisper presentation; preserved because
    # the fill never touches the header). It used to fall through to
    # tetramaster.handle_line and die as "captured, silent" -- never
    # delivered, no error to the sender. Same empty-name gate as room chat,
    # so the same fill; and the
    # sender gets an echo for the same measured reason (this client does
    # not self-render its own line). One session per side, first that
    # accepts: a member can hold room-band AND table-band sockets, and the
    # client's message store is process-global, so sending to every
    # session would ingest the line twice. POL_TM_CHAT_WHISPER=0 restores
    # the old swallowing (bisection only).
    whisper = None
    if os.environ.get("POL_TM_CHAT_WHISPER", "1") == "1":
        whisper = _tm_chat_fill_name(text, None, nick,
                                     member_id=_session_get("member_id"))
    if whisper is not None:
        wprefix = b":" + nick + b"!~x@" + _irc_host(srv) + b" NOTICE "
        delivered = echoed = 0
        for ts in PRESENCE.sessions_by_nick(target):
            if ts.send([wprefix + target + b" :" + whisper]):
                delivered = 1
                break
        for ts in PRESENCE.sessions_by_nick(nick):
            if ts.send([wprefix + nick + b" :" + whisper]):
                echoed = 1
                break
        log("authserv",
            f"  TM whisper {nick.decode('latin1')} -> "
            f"{target.decode('latin1')}: delivered={delivered} "
            f"echoed={echoed}"
            + ("" if delivered else " -- TARGET NOT ONLINE, dropped"))
        return None
    # *** THE TRADE NEGOTIATION IS DELIVERED, NOT ANSWERED -- like the
    # whisper above. *** Measured live 2026-08-22T04:44:59Z, the first
    # player trade ever initiated against this server:
    #     NOTICE UA4XX8PKP :GTM0G21000000@Tr=/NN=AB12CD31DC3F63B6/HID=0/...
    #     NOTICE UA4XX8PKP :GTM0G23000000@TrCan=          (63 s later)
    # -- code 0x21 `@Tr=` addressed to the TARGET PLAYER'S NICK (not the
    # room service peer), body carrying only the SENDER's identity. The
    # target client polls codes 0x21/0x25/0x23/0x2A/0x2C every frame in
    # the lobby scene (its standing trade ear),
    # so a verbatim forward lands in its message store and the
    # 0x21 handler at TM.dll 0x089B02 consumes it. Both fell through to
    # handle_line and died "captured, silent" -- the invite never reached
    # the other player.
    #
    # Verbatim, and NO ECHO -- deliberately unlike chat: the sender's own
    # client polls the same codes, so an echoed `@Tr=` would pop a trade
    # invite FROM THEMSELVES on the initiator's screen. `@TrAns=` (0x22)
    # and `@TrCanAck=` (0x2A) ride the same relay on the return leg,
    # addressed to the initiator's nick. POL_TM_TRADE_RELAY=0 restores the
    # old swallowing (bisection only).
    # VERIFIED: THE TRADE IS PEER-TO-PEER, and the SERVE-A-LIST model is RETRACTED
    # (static RE 2026-08-22, tm-trade-negotiation.md). The trade board is a
    # SCENE (TM.dll tick 0x15590) both clients enter after the accept, and
    # it runs its OWN handshake on codes the server must RELAY, not author:
    #   * @TrID= / @TrEnt=  (code 0xA0) -- state 1 sends these to the
    #     partner; state 3 consumes the relayed @TrID. NOT in the old key
    #     set, so they fell through to handle_line and DIED -- a definite
    #     blocker no matter which scene runs.
    #   * @Card=/@Data=/@Quit= on code 0xA2, msgid 25/26/27 -- the live
    #     card-list / selection / decide exchange. The client addresses
    #     these to the SESSION PEER ([0x5242958], set by the @Init(24) the
    #     accept serve authors below), which folds to the PARTNER's login
    #     nick, so they arrive here addressed to the partner exactly like
    #     @Tr=. They are 0xA2-family and share the @Card=/@Data= strings
    #     with the auction, so they are matched by CODE+MSGID, not string.
    # All relayed verbatim, framed from the sender, NO echo -- same law as
    # @Tr= (the sender polls the same codes; an echo self-invites).
    # VERIFIED:VERIFIED: `@TrEnt=` / `@TrID=` (code 0xA0) ARE A REQUEST TO US, NOT PEER
    # TRAFFIC -- measured 2026-08-24T16:38:33Z, the FIRST TIME this project
    # has ever seen either on the wire:
    #
    #   NOTICE UKJ7LOE8G :GTM0GA0000000@TrEnt=
    #        /NN=AB12CD1EEEAE9C3B|0000000000000000/HID=0|0/Dm=0|0/Vol=0|0
    #        -> TARGET NOT ONLINE, dropped
    #
    # `UKJ7LOE8G` is a SERVICE nick -- the same one `@Init=` is addressed to
    # -- so relaying it peer-to-peer drops it every time, which is what that
    # log line is. The client is asking the server to admit it to the trade
    # session, and the trade board (TM.dll tick 0x15590) then PARKS IN STATE
    # 2 polling code 0xA1 for the answer. That park is the "Now Loading"
    # screen seen in live testing: no error, `err=0x00`, the board simply
    # waiting on a reply we have never sent.
    #
    # `@TrEntAns`'s only appearance in the image is the PARSE site at
    # 0x8A9BE -- the client never builds it, so it is server-authored by
    # construction.
    #
    # WARNING: `/Ans=` IS A GRANT CODE, NOT AN IDENTITY. The reading this block
    # used to carry ("`0x8A9DF` reads `/Ans=` and stores the result as a
    # 64-bit at `[0x5242958]`") is RETRACTED -- it conflated two different
    # slots of the same stack frame, and it is why the board sat in state 2
    # all evening with `code=0xA1 -> FOUND` in its own store. Read the frame
    # (`0x8A950`, F = esp after the prologue):
    #
    #   0x8A972  record  = F+0x28            <- 0x825A0(out, 0xA1, -1)
    #   0x8A976  F+0x14  = 0                 <- the return slot, preset 0
    #   0x8A9BE  0xAA920(record, "@TrEntAns", &F+0x18)
    #   0x8A9DF  0xAB080("/Ans=", &F+0x18, 0, &F+0x14)   <- out is F+0x14
    #   0x8A9E9  [0x5242958] = F+0x30 / F+0x34 = **record+0x8/+0xC**
    #   0x8AA23  edi = F+0x14 ; ... ; return edi
    #
    # So `[0x5242958]` comes off the RECORD's own sender stamp -- i.e. from
    # how the NOTICE is FRAMED, not from anything we write in the body --
    # and `/Ans=` is the FUNCTION'S RETURN VALUE. `0xAB080` ends at 0xAB3F4
    # with `call 0x1E2C30` (atoi) and `mov [out], eax`, negated on a leading
    # '-'. It is a DECIMAL int parser.
    #
    # `atoi("AB12CD31DC3F63B6") == 0`. That is the whole bug: we served a
    # 16-hex-digit POL-ID into a field the client atoi's, got 0, and 0 is
    # exactly the value that keeps the board where it was.
    #
    # The board's use of the return (tick 0x15590 state 2, 0x15915):
    #
    #     edi = 0x8A950()
    #     edi >  0          -> [0x52281FC]=0xF, [+0x164]=0, jmp 0x16308
    #                          and 0x16308 is `inc [esi+0x1BC]` -- STATE 3
    #     edi == -256       -> error dialog 0x19D
    #     edi <  0          -> 0x194030(edi), the server-error message table
    #     edi == 0          -> 0x195260(scene, esi, 0, 0xBA); that returns 0,
    #                          `je 0x1630E` skips the increment -> PARK
    #
    # The same shape, with the same `jle` guard, is how the client reads
    # every other `/Ans=` in the image -- `@CardEnt` at 0x8BA50 and 0x8BEB9
    # both `cmp eax,0 / jle <skip>` before adopting the session destination.
    # A small POSITIVE integer means GRANTED; <= 0 means it is not.
    #
    # KEY: AND THE TEN "RESOURCE BITS" WERE NEVER THE GATE. `[load+0x160]` is
    # set at 0x12EFD5 -- `ebp = 1 << i ; or [esi+0x160], ebp` -- inside the
    # loading widget's own tick 0x12EBE0, and its guard at 0x12EFBC is
    # `percent >= 100 && frame == 3`. The bits are a CONSEQUENCE of the
    # progress float reaching 100, not a precondition for it. Progress is
    # clamped to 80.0f (0x158A4) for as long as sub-state `[board+0x1A0]`
    # is 0, and nothing in state 2 ever moves that sub-state -- only states
    # 3 and 9 do (0x15A9F, 0x15DC6). `bits=0x000/0x3FF` was the symptom of
    # a board stuck one state earlier, not a missing resource load.
    _TRADE_KEYS = (b"@Tr=", b"@TrAns=", b"@TrCan=", b"@TrCanAck=",
                   b"@TrID=", b"@TrEnt=")
    if (b"@TrEnt=" in payload or b"@TrID=" in payload)                 and os.environ.get("POL_TM_TRENT_ANS", "1") == "1":
        try:
            _me = _session_get("member_id")
            _pm = None
            try:
                # BOTH SIDES of the binding -- a one-sided lookup returned
                # None live at 01:52:03Z and cost the partner its grant.
                _pm = tetramaster.trade_partner_of(_me)
            except Exception:
                pass
            # VERIFIED: A GRANT CODE. Both id spaces were tried and both parked
            # the board, for the same reason -- neither is a decimal number:
            #
            #   2026-08-24T16:52:25Z  /Ans=000000E13883D826   guid_of
            #   2026-08-24T21:39:21Z  /Ans=AB12CD31DC3F63B6   pol_id_of
            #                                                 atoi -> 0 both
            #
            # The second one is the run that logged `store lookup #226037:
            # code=0xA1 -> FOUND` and STILL did not move: the record landed,
            # the name matched, and the field parsed to the one value that
            # means "not granted". `POL_TM_TRENT_ANS_VALUE` overrides it
            # without a rebuild; anything <= 0 is a refusal, and a negative
            # is looked up in the client's server-error table (0x194030).
            try:
                _ans = (os.environ.get("POL_TM_TRENT_ANS_VALUE", "1")
                        .strip().encode("latin1")) or b"1"
                int(_ans)          # decimal or the client reads it as 0
            except Exception:
                _ans = b"1"
            _peer_id = 1 if _pm is not None else 0
            # WARNING: RETURN THE LINE, DO NOT SEND IT. This is the whole bug,
            # and probe_lookup settled it: the client polled code 0xA1
            # **2,168 times, every one "not found"** while `store_count=1`.
            # Three send variants (idle-drain framing, relay framing, two id
            # spaces) all reported `sent=True` and none ever reached the
            # store -- because a TM0 SERVICE answer does not travel that way.
            #
            # The proven path is a few hundred lines below, and it is how
            # `@TeachDVAns`, `@InitAns` and `<CS>` have always worked:
            #
            #     out.append(NoPad(_game_notice_line(
            #         b"G" + tag + b"G" + r, target, nick, srv)))
            #     return out
            #
            # i.e. the reply is RETURNED, and `_auth_channel_loop` sends it
            # on the socket the request arrived on -- the "answered with N
            # line(s)" line in the log. An out-of-band
            # `PRESENCE.sessions_by_nick(...).send()` is the PUSH path, which
            # is for unprompted traffic to a peer, not for answering a
            # service request. The framing was right the first time; the
            # delivery mechanism never was.
            _body = b"A1000000@TrEntAns=/Ans=" + _ans
            # VERIFIED: AND THE STATE-3 RECORD, `(0xA2, 24) @Init` -- MEASURED LIVE
            # 2026-08-24T23:19:58Z: with /Ans=1 the board left state 2 on
            # tick #372 and parked in state 3, which is exactly what state
            # 3 polls for. It rides the SAME two paths as the grant (reply
            # + partner push) because it is wanted one tick later and a
            # store record keeps until something consumes it.
            #
            # The arm, read at 0x1064CA (`0x101A10(n)` tries (0x43,n), then
            # (0xB2,n) at 0x1051BB, then (0xA2,n) at 0x10648C; n=24 is arm 0
            # of the 0xA2 table and prints `---->Recv=TRADEINIT`):
            #
            #   0xAA920(rec, "@Init")            required, else bail
            #   0xAB080("/ID=")   -> low byte -> [sess+0x31]   THE CHANNEL
            #   sprintf("@No%d", [sess+0x19B])   <- the recipient's OWN index
            #   0xAA920(rec, "@No<idx>")         required, else bail
            #     0xAB080("/RA=") -> [sess+4]  dword
            #     0xAB080("/M=")  -> [sess+0]  word
            #     0xAB080("/CP=")
            #     0xAB080("/S=")  -> [sess+0xEC] word
            #   0x10B6B0(rec) = rec+0x8/+0xC -> [sess+0x28/0x2C]  (self-stamp)
            #
            # We serve BOTH `@No0=` and `@No1=` because `[sess+0x19B]` is the
            # recipient's own seat and we do not get to see it; extra
            # elements are inert (0xAA920 is a container lookup by name).
            # Zeros are the invents-nothing default -- a trade has no rank,
            # money or board yet.
            #
            # PARTIAL: `/ID=0` IS THE ONE GUESS. `/ID=`'s low byte becomes
            # `[sess+0x31]`, and EVERY later 0xA2 arm gates on
            # `0x10B6C0(rec) == [sess+0x31]` (0x10668D, 0x1066B0, ...) where
            # `0x10B6C0` is `rec+0x3D0`. Both sides of that comparison
            # initialise to 0 -- the session at 0x101196, the record at
            # 0xA8359 -- so 0 is the self-consistent choice. **If it is
            # wrong the failure is specific and visible: `---->Recv=
            # TRADEINIT` appears and the board advances, and then 25/26/27
            # are silently dropped at 0x106CBC.** That symptom means read
            # what the store stamps into rec+0x3D0 (writers 0x826AD /
            # 0x827A7 / 0x828AB) and put THAT in `/ID=`.
            _init_on = os.environ.get("POL_TM_TRADE_INIT24", "1") == "1"
            _no = os.environ.get("POL_TM_TRADE_INIT24_NO",
                                 "/RA=0/M=0/CP=0/S=0").encode("latin1")
            _init_body = (b"A2001800@Init=/ID="
                          + os.environ.get("POL_TM_TRADE_INIT24_ID",
                                           "0").strip().encode("latin1")
                          + b"@No0=" + _no + b"@No1=" + _no)
            log("authserv",
                "  TM trade: answering %s with (0xA1) @TrEntAns=/Ans=%s "
                "(from member %r, partner %r) AS A REPLY on this socket "
                "-- %s"
                % (b"@TrID=" if b"@TrID=" in payload else b"@TrEnt=",
                   _ans.decode(), _me, _pm,
                   "watch for the board to leave state 2" if _peer_id else
                   "WARNING: PARTNER UNKNOWN -- the grant still stands, but the "
                   "partner gets no copy and will park in state 2"))
            if _init_on:
                log("authserv",
                    "  TM trade: + (0xA2,24) %s -- the state-3 record. "
                    "Watch the client's own trace for "
                    "'---->Recv=TRADEINIT', then the board probe for "
                    "state=0x4" % _init_body.decode("latin1"))
            # KEY: AND THE PARTNER NEEDS ONE TOO -- IT NEVER ASKS.
            # State 1 branches on the role: index 0 SENDS @TrID=/@TrEnt=,
            # index 1 SKIPS. Both then park in state 2 polling code 0xA1.
            # So answering only the requester leaves the other client
            # waiting for ever on a message it is not designed to request --
            # which is exactly what the second test machine's screen has been
            # showing. The static fix order said "to BOTH" and this is what
            # that meant.
            #
            # Measured: only 127.0.0.1 ever sends 0xA0, and the other
            # machine's store polled code 0xA1 4,924 times, all misses,
            # because we had sent it nothing. That machine's store DOES
            # take pushes -- `code=0x2C -> FOUND` is our own go flag -- so
            # the partner's copy goes out on the same synchronous
            # freshest-session path the go flag uses.
            #
            # WARNING: The "id space MIRRORED, each side is told the OTHER side's
            # POL-ID" note that used to live here went with the retracted
            # reading above: `/Ans=` is not an identity at all, so there is
            # nothing to mirror. Both copies carry the SAME grant code.
            # `[0x5242958]` is set from the record's own sender stamp
            # (`record+0x8`), i.e. from how this NOTICE is FRAMED -- so if
            # the destination ever needs to change, change `target` in the
            # `_game_notice_line` call, not the body.
            try:
                if _pm is not None:
                    _pbody = b"A1000000@TrEntAns=/Ans=" + _ans
                    # The partner's SESSIONS, freshest first -- and its
                    # nick comes off the session rather than a lookup
                    # table, so the line is addressed to whatever that
                    # connection actually calls itself.
                    _pses = sorted(
                        PRESENCE.sessions_for(int(_pm)),
                        key=lambda x: (x.in_room_recently(),
                                       getattr(x, "last_heard", 0)),
                        reverse=True)
                    _pnick = getattr(_pses[0], "nick", None) if _pses else None
                    if _pnick:
                        _ppad = (b" " if os.environ.get(
                            "POL_GAME_NOTICE_PAD") == "space" else b"")
                        _psent = False
                        # The grant FIRST, then the state-3 record, on the
                        # one session that takes them -- order matters only
                        # in that the board consumes them one state apart.
                        _pbodies = ([_pbody, _init_body] if _init_on
                                    else [_pbody])
                        for _ps in _pses:
                            _plines = [
                                NoPad(_game_notice_line(
                                    b"GTM0G" + _b, target,
                                    getattr(_ps, "nick", _pnick), srv))
                                for _b in _pbodies]
                            if _ps.send(_plines, pad_override=_ppad):
                                _psent = True
                                break
                        log("authserv",
                            "  TM trade: PARTNER copy of (0xA1) "
                            "@TrEntAns=/Ans=%s%s pushed to %s (member %r) "
                            "sent=%s -- index 1 never sends @TrEnt but "
                            "parks in state 2 all the same"
                            % (_ans.decode(),
                               " + (0xA2,24) @Init" if _init_on else "",
                               _pnick.decode("latin1", "replace"),
                               _pm, _psent))
                    else:
                        log("authserv",
                            "  TM trade: PARTNER copy NOT sent -- no nick "
                            "for member %r" % (_pm,))
            except Exception as _pe:
                log("authserv",
                    f"  TM trade: partner @TrEntAns copy failed: {_pe!r}")
            return [NoPad(_game_notice_line(b"GTM0G" + _b, target,
                                            nick, srv))
                    for _b in (([_body, _init_body] if _init_on
                                else [_body]))]
        except Exception as e:
            log("authserv", f"  TM trade @TrEnt answer failed: {e!r}")
        return None
    _trade_hit = any(k in payload for k in _TRADE_KEYS)
    # WARNING: INITIALISE OUTSIDE THE BRANCH. The `_TRADE_KEYS` hit above skips the
    # `if not _trade_hit` block entirely, so a flag set only in there is
    # UNDEFINED on the `@Tr=` path -- i.e. a NameError on the invite, the
    # one message that has always worked. Caught before shipping; the classic
    # shape of a switch that does not reach every caller.
    _trade_a2 = False
    _rpm = None
    if not _trade_hit and os.environ.get("POL_TM_TRADE_RELAY", "1") == "1":
        # Code+msgid match for the 0xA2 negotiation (25 TRADEQUIT / 26
        # TRADELIST / 27 TRADESELECT). payload is cls+ebody; the ebody
        # carries the 8-hex header decode_ebody reads.
        try:
            _code, _ = tetramaster.decode_ebody(payload[1:])
        except Exception:
            _code = None
        # 25 (TRADEQUIT) and 27 (TRADESELECT) always; 26 (TRADELIST)
        # behind POL_TM_TRADE_RELAY26. The old blanket exclusion of 26
        # rested on the "a SECOND (0xA2,26) record crashes" theory, which
        # was RETRACTED (2026-08-23: the first authored 20-card record
        # crashes; 08-22's 11-18-card records did not -- the crash tracks
        # LOGGED LINE LENGTH, see note_trade_accept). Relaying the
        # clients' OWN lists is the protocol end-state: the native builder
        # chunks at <=15 cards/message, under the suspected logger
        # threshold by construction. Default OFF only so the length A/B
        # (short authored list) runs single-variable; flip to 1 for the
        # relay-model run and set POL_TM_TRADE_CARD=0 with it.
        #
        # VERIFIED: 26 IS ON BY DEFAULT NOW (2026-08-24T23:32Z). Both clients
        # reached card selection and sent their OWN lists, and we dropped
        # every one of them:
        #
        #   m6  A2001A00@Card=/P=0/E=1/S=0/C=1@N0=/D=8|9|0|9|5|26
        #   m3  A2001A00@Card=/P=1/E=1/S=0/C=1@N0=/D=57|65|1|35|45|6
        #   m3  A2001A00@Card=/P=1/E=0                      <- terminator
        #
        # That is the native builder's own shape, measured, and it settles
        # the /E=//S=//C= argument: one @N<i>= section per card, /C= the
        # count in THIS message, /E= a more-follow flag (0 terminates).
        # `/P=` is the sender's own SEAT (m6 sent 0, m3 sent 1) and is
        # global, not per-recipient -- so relay it VERBATIM, never rewrite.
        #
        # WARNING: THE KNOB IS GONE ON PURPOSE, AND IT HAD TO BE. Changing this
        # module's default from "0" to "1" did NOTHING on prod:
        # `docker-compose.prod.yml` line 129 pins
        # `POL_TM_TRADE_RELAY26: "${POL_TM_TRADE_RELAY26:-0}"`, so the
        # container env carries a literal 0 and `os.environ.get(k, "1")`
        # never sees its own default. Verified in the RUNNING container,
        # not in the file: `docker inspect ... .Config.Env` -> RELAY26=0.
        # And a deploy pipeline that ignores docker-compose*.yml would
        # ship nothing for a compose edit without a hand-recreate.
        # Two defaults for one setting, the compose one winning silently --
        # the same compiled-vs-deployed-defaults shape seen elsewhere.
        #
        # The knob only ever existed to keep the authored-list length A/B
        # single-variable. That A/B is over and the authored serve is gone;
        # relaying each client's OWN list is the protocol, so there is no
        # longer a question for a switch to answer.
        #
        # KEY: AND THE SET IS THE CLIENT'S OWN DISPATCH TABLE, not a guess.
        # 0x1064BB bounds `n-24` at 0x10 and indexes the byte table at
        # 0x5096E14, which selects an arm from 0x5096DF0:
        #
        #   24  0x1064CA  @Init        TRADEINIT   (we author this one)
        #   25  0x106645  @Quit        TRADEQUIT
        #   26  0x1067B5  @Card        TRADELIST
        #   27  0x106A7A  @Data        TRADESELECT
        #   28  0x106B42  @Data        TRADEDECIDE   <- the CONFIRM
        #   30  0x106BFF  @Quit        TRADECLOSE
        #   39  0x1066E2  @Dead
        #   40  0x106759  @DataError
        #   29, 31..38    -> 0x106CBC, the bail; not real messages
        #
        # The old allowlist stopped at 27, so both clients could pick cards
        # and SEE each other's picks -- 26 and 27 relayed -- and then had no
        # way to agree, because 28 was dropped on the floor. Live report,
        # 2026-08-24T23:52Z: "I have cards selected on both sides, and I
        # don't see the option to actually do the trade." 27 `@Data=` is the
        # 30-slot selection state (`/P=1/M=1/D=1|0|0|...`) and it was
        # flowing; 28 is the decision that follows it.
        _relay_msgids = (24, 25, 26, 27, 28, 30, 39, 40)

        if _code is not None and (_code & 0xFF) == 0xA2 \
                and ((_code >> 16) & 0xFF) in _relay_msgids:
            _trade_hit = True
            _trade_a2 = True
    if os.environ.get("POL_TM_TRADE_RELAY", "1") == "1" and _trade_hit:
        # WARNING: FRESHEST SESSION FIRST, measured 2026-08-22T05:02:59Z: the first
        # relayed @Tr= reported delivered=1 and the target client showed
        # NOTHING -- the write went to one of the member's several RESUMED
        # zombie sessions (three resumes + a ghost re-attach in the half
        # hour before), whose dead socket buffers bytes without erroring.
        # `alive` only turns False when a write fails, so "first that
        # accepts" is exactly the wrong order. The room-band heartbeat
        # (<DR> every 1-3 s) is the proof of life; sort by it.
        # KEY: THE TRADE BOARD DOES NOT ADDRESS THE PARTNER -- IT ADDRESSES
        # THE SERVICE, and the whisper relay below can never deliver that.
        # Measured 2026-08-24T23:32-23:33Z, board live in card selection:
        #
        #   NOTICE UKJ7LOE8G :GTM0GA2001A00@Card=/P=0/E=1/S=0/C=1@N0=...
        #   NOTICE UKJ7LOE8G :GTM0GA2001900@Quit=/P=1
        #                     ^ the SERVICE nick -> candidates=0, dropped
        #
        # Why: `[0x5242958]` is the board's send destination, and 0x8A950
        # sets it from the `@TrEntAns` RECORD's sender stamp (record+0x8) --
        # which is how WE framed that NOTICE, i.e. `target`. So the board
        # talks back to whoever we answered as, by construction.
        #
        # And the return leg has to keep that framing. Every 0xA2 arm gates
        # TWICE before it will look at a record (0x106660):
        #
        #   0x10B6B0(rec) = rec+0x8/+0xC  ==  [sess+0x28/0x2C]   sender
        #   0x10B6C0(rec) = rec+0x3D0     ==  [sess+0x31]        channel
        #
        # and `[sess+0x28/0x2C]` was self-stamped from OUR `@Init` record at
        # 0x1064F3. So a relayed card list must arrive framed from the SAME
        # nick `@Init` came from -- `target` -- or the partner drops it
        # silently at 0x106CBC. Framing it from the sending PLAYER (what the
        # whisper relay does, and correct for `@Tr=`) fails that gate.
        _a2_to = None
        if _trade_a2:
            try:
                _rm = _session_get("member_id")
                _rpm = tetramaster.trade_partner_of(_rm)
                if _rpm is not None:
                    _a2_to = sorted(
                        PRESENCE.sessions_for(int(_rpm)),
                        key=lambda s: (s.in_room_recently(),
                                       getattr(s, "last_heard", 0)),
                        reverse=True)
            except Exception as _re:
                log("authserv",
                    f"  TM trade: 0xA2 partner lookup failed: {_re!r}")
        if _a2_to:
            # from `target` (the service we answered as), to the partner.
            delivered = 0
            chosen = b"-"
            for ts in _a2_to:
                _tn = getattr(ts, "nick", None)
                if not _tn:
                    continue
                if ts.send([NoPad(_game_notice_line(text, target, _tn,
                                                    srv))]):
                    delivered = 1
                    chosen = ts.peer_ip
                    break
            log("authserv",
                f"  TM trade 0xA2 -> PARTNER member {_rpm}: "
                f"{tetramaster.describe_line(payload[1:])} "
                f"delivered={delivered} via={chosen.decode('latin1')} "
                f"sessions={len(_a2_to)} -- framed from {target!r} so the "
                f"partner's [sess+0x28] sender gate passes"
                + ("" if delivered else " -- NOT DELIVERED"))
            # CAPTURE BOTH HALVES AS THEY GO PAST. The offer lists (26) and
            # the selection bitmaps (27) are everything needed to apply the
            # swap server-side, and we are already relaying them, so nothing
            # extra has to be asked for. See `apply_trade_swap`.
            try:
                _mid = (_code >> 16) & 0xFF
                if _mid == 26:
                    tetramaster.note_trade_offer(_rm, text)
                elif _mid == 27:
                    tetramaster.note_trade_select(_rm, text)
            except Exception as _pe2:
                log("authserv",
                    f"  TM trade: capture failed: {_pe2!r}")
            # KEY: THE COMMIT IS OURS TO SEND. `(0xA2,28) @Data=/M=3` is the
            # only message in the trade the CLIENT CANNOT BUILD: a scan for
            # a `push 0x1C` + `push 0xA2` builder finds nothing, and the arm
            # itself (0x106B42) proves the shape -- on `/M= == 3` it checks
            # **both** `[sess+0x19D] == 2` (my mode) and `[sess+0x1CD] == 2`
            # (the partner's, set from their msgid-27 `/M=`) before calling
            # 0x106E30, the commit. A self-sent message would not need to
            # verify its own sender's mode. So it is server-authored, like
            # `@TrEntAns` and `@Init` -- we are the trade service.
            #
            # Measured 2026-08-24T23:57Z+, both clients live in the board:
            #
            #     6x @Data=/P=0/M=1     selection changes
            #     3x @Data=/P=0/M=2     READY
            #     6x @Data=/P=1/M=1
            #     3x @Data=/P=1/M=2     READY
            #
            # Both sides at mode 2, nothing else on the wire but @Pong, and
            # the trade sat there -- the tester had ticked the checkbox by
            # their name and hit confirm on both. Arm 28 needs only the
            # `@Data` element, the two gates and `/M=`, so the whole record
            # is `A2001C00@Data=/M=3`. Sent to BOTH, framed from `target`
            # exactly like the relay above, and ONCE per readiness edge.
            if ((_code >> 16) & 0xFF) == 27 and _rpm is not None:
                try:
                    _m = re.search(rb"/M=(\d+)", text)
                    _mode = int(_m.group(1)) if _m else -1
                    _tk = tetramaster._push_key(_rm)
                    _pk = tetramaster._push_key(_rpm)
                    # setdefault, NOT `.get(...) or {}` -- the latter
                    # mutates a throwaway dict when an entry is missing, so
                    # readiness would never persist and the commit would
                    # silently never fire. A silent no-op is the worst
                    # failure mode this file has.
                    _me_st = tetramaster._TRADE_PENDING.setdefault(_tk, {})
                    _pa_st = tetramaster._TRADE_PENDING.setdefault(_pk, {})
                    # Readiness is an EDGE, both ways: mode 2 arms, anything
                    # else disarms, so backing out of a confirm and
                    # re-confirming works and we never double-commit.
                    _me_st["ready"] = (_mode == 2)
                    if _mode != 2:
                        _me_st.pop("committed", None)
                        _pa_st.pop("committed", None)
                    if (_mode == 2 and _pa_st.get("ready")
                            and not _me_st.get("committed")):
                        _me_st["committed"] = True
                        _pa_st["committed"] = True
                        # WARNING: `GTM0G` IS NOT OPTIONAL. The first cut of this
                        # sent the bare record and the client never saw it:
                        # arm 28 prints `---->Recv=TRADEDECIDE` BEFORE it
                        # parses `/M=`, and no TRADEDECIDE appeared in the
                        # trace, so it was thrown out at the class/framing
                        # gate rather than at the mode check. Every other
                        # send in this file carries the prefix -- the relay
                        # passes `text`, which already has it, and the
                        # @TrEntAns/@Init path writes `b"GTM0G" + body`.
                        _cbody = b"GTM0GA2001C00@Data=/M=3"
                        _cpad = (b" " if os.environ.get(
                            "POL_GAME_NOTICE_PAD") == "space" else b"")
                        _cgot = []
                        for _cm in (_rm, _rpm):
                            _sent = False
                            for _cs in sorted(
                                    PRESENCE.sessions_for(int(_cm)),
                                    key=lambda s: (s.in_room_recently(),
                                                   getattr(s, "last_heard",
                                                           0)),
                                    reverse=True):
                                _cn = getattr(_cs, "nick", None)
                                if not _cn:
                                    continue
                                if _cs.send([NoPad(_game_notice_line(
                                        _cbody, target, _cn, srv))],
                                        pad_override=_cpad):
                                    _sent = True
                                    break
                            _cgot.append("m%s=%s" % (_cm, _sent))
                        log("authserv",
                            "  TM trade: BOTH SIDES READY (/M=2) -- sent "
                            "(0xA2,28) @Data=/M=3, the commit the client "
                            "cannot build, to %s. Watch for the cards to "
                            "actually change hands (arm 0x106B42 -> "
                            "0x106E30)." % ", ".join(_cgot))
                except Exception as _ce:
                    log("authserv",
                        f"  TM trade: commit trigger failed: {_ce!r}")
            # PARTIAL: AND THE CLIENTS ANSWER THE DECIDE. Measured 00:52Z: after
            # `(0xA2,28) @Data=/M=3` landed (`---->Recv=TRADEDECIDE` in the
            # client's own trace, so it passed BOTH gates), each client sent
            #
            #     GTM0GA2001C00@Ans=/P=0/M=2
            #     GTM0GA2001C00@Ans=/P=1/M=2
            #
            # addressed to the SERVICE. Arm 28 only matches `@Data`, so
            # relaying these to the partner is a no-op -- they are for US.
            # `@Ans=` is built at 0x10904D by the same trade-send dispatcher
            # as `@Data=`, carrying `/P=` from `[sess+0x19B]` and `/M=`.
            #
            # VERIFIED:VERIFIED: CONFIRMED LIVE 2026-08-25T01:02:12Z -- THE TRADE
            # COMPLETED. Arm 28 has exactly two branches: `/M=3` (gated on
            # both modes == 2) and `/M=4` at 0x106BE9, which has no gates and
            # makes 0x101A10(28) return 1 for the pump's drain at 0x19E41.
            # The full handshake, measured:
            #
            #   BOTH SIDES READY (/M=2)    -> (0xA2,28) @Data=/M=3
            #   both clients  @Ans=/P=n/M=2
            #   BOTH SIDES ANSWERED        -> (0xA2,28) @Data=/M=4
            #   -> `---->Recv=TRADEDECIDE` twice, board leaves state 8
            #
            # So the decide is a TWO-PHASE exchange and the service drives
            # both halves. POL_TM_TRADE_ANS4=0 disables.
            if (((_code >> 16) & 0xFF) == 28 and b"@Ans=" in text
                    and _rpm is not None
                    and os.environ.get("POL_TM_TRADE_ANS4", "1") == "1"):
                try:
                    _am = re.search(rb"/M=(\d+)", text)
                    _amode = int(_am.group(1)) if _am else -1
                    _ak = tetramaster._push_key(_rm)
                    _apk = tetramaster._push_key(_rpm)
                    _a_me = tetramaster._TRADE_PENDING.setdefault(_ak, {})
                    _a_pa = tetramaster._TRADE_PENDING.setdefault(_apk, {})
                    _a_me["ans"] = (_amode == 2)
                    if (_amode == 2 and _a_pa.get("ans")
                            and not _a_me.get("done")):
                        _a_me["done"] = True
                        _a_pa["done"] = True
                        _fbody = b"GTM0GA2001C00@Data=/M=4"
                        _fpad = (b" " if os.environ.get(
                            "POL_GAME_NOTICE_PAD") == "space" else b"")
                        _fgot = []
                        for _fm in (_rm, _rpm):
                            _fs_ok = False
                            for _fs in sorted(
                                    PRESENCE.sessions_for(int(_fm)),
                                    key=lambda s: (s.in_room_recently(),
                                                   getattr(s, "last_heard",
                                                           0)),
                                    reverse=True):
                                _fn = getattr(_fs, "nick", None)
                                if not _fn:
                                    continue
                                if _fs.send([NoPad(_game_notice_line(
                                        _fbody, target, _fn, srv))],
                                        pad_override=_fpad):
                                    _fs_ok = True
                                    break
                            _fgot.append("m%s=%s" % (_fm, _fs_ok))
                        log("authserv",
                            "  TM trade: BOTH SIDES ANSWERED (@Ans=/M=2) "
                            "-- sent (0xA2,28) @Data=/M=4, phase 2 of the "
                            "decide exchange, to %s. CONFIRMED LIVE "
                            "2026-08-25T01:02Z -- the trade completes."
                            % ", ".join(_fgot))
                        # KEY: AND NOW ACTUALLY MOVE THE CARDS. Collections
                        # are server-authoritative, so without this the
                        # swap lives only on the two clients and is gone at
                        # the next launch -- live report, 01:1xZ, having
                        # completed a trade minutes earlier: "can confirm
                        # the trade did not persist". All-or-nothing; see
                        # apply_trade_swap for the refusal cases.
                        _rep = tetramaster.apply_trade_swap(_rm, _rpm)
                        log("authserv",
                            "  TM trade: COLLECTIONS -- %s" % _rep)
                        # KEY: AND CLOSE THE SESSION. Board state 10 polls
                        # `0x101A10(0x1E)` = msgid 30 TRADECLOSE, and on a
                        # hit it sets sub-state 1 (progress 80 -> 101, so
                        # the loading screen finishes) and advances 10 -> 11
                        # (0x15F34/0x15F3A). Without it the board parks at
                        # `state=0xA pct=80` -- live report, 02:16Z: "got to
                        # finishing the trade but now still at the now
                        # loading when completing". Same clamp-at-80 park as
                        # the original state-2 bug, one state from the end.
                        #
                        # Arm 30 (0x106BFF) wants `@Quit` + the usual two
                        # gates, then parses `/P=` and `/B=`, calls
                        # 0x101530 (the player-data recalc) and clears
                        # `[sess+0xEA]` -- the teardown. `/P=` is the
                        # recipient's OWN seat, which is how the client uses
                        # /P= in every record it sends (@Card, @Data, @Ans);
                        # we captured it from those. `/B=` lands in a byte
                        # at [0x524648E]; 0 is the invents-nothing default.
                        # POL_TM_TRADE_CLOSE=0 disables, POL_TM_TRADE_CLOSE_B
                        # overrides /B= without a rebuild.
                        if os.environ.get("POL_TM_TRADE_CLOSE", "1") == "1":
                            _bv = os.environ.get(
                                "POL_TM_TRADE_CLOSE_B", "0").strip()
                            _cgot2 = []
                            for _qm2 in (_rm, _rpm):
                                _seat = (tetramaster._trade_state(_qm2)
                                         .get("seat"))
                                _qbody = (b"GTM0GA2001E00@Quit=/P=%d/B=%s"
                                          % (int(_seat or 0),
                                             _bv.encode("latin1")))
                                _ok2 = False
                                for _qs in sorted(
                                        PRESENCE.sessions_for(int(_qm2)),
                                        key=lambda s: (
                                            s.in_room_recently(),
                                            getattr(s, "last_heard", 0)),
                                        reverse=True):
                                    _qn = getattr(_qs, "nick", None)
                                    if not _qn:
                                        continue
                                    if _qs.send([NoPad(_game_notice_line(
                                            _qbody, target, _qn, srv))],
                                            pad_override=_fpad):
                                        _ok2 = True
                                        break
                                _cgot2.append("m%s(/P=%s)=%s"
                                              % (_qm2, _seat, _ok2))
                            log("authserv",
                                "  TM trade: CLOSE -- sent (0xA2,30) "
                                "@Quit= to %s; board state 10 polls msgid "
                                "30 and only then finishes its loading"
                                % ", ".join(_cgot2))
                except Exception as _ae:
                    log("authserv",
                        f"  TM trade: @Ans follow-up failed: {_ae!r}")
        else:
            tprefix = b":" + nick + b"!~x@" + _irc_host(srv) + b" NOTICE "
            cand = sorted(PRESENCE.sessions_by_nick(target),
                          key=lambda s: (s.in_room_recently(),
                                         getattr(s, "last_heard", 0)),
                          reverse=True)
            delivered = 0
            chosen = b"-"
            for ts in cand:
                if ts.send([tprefix + target + b" :" + text]):
                    delivered = 1
                    chosen = ts.peer_ip
                    break
            log("authserv",
                f"  TM trade relay {nick.decode('latin1')} -> "
                f"{target.decode('latin1')}: "
                f"{tetramaster.describe_line(payload[1:])} "
                f"delivered={delivered} via={chosen.decode('latin1')} "
                f"candidates={len(cand)}"
                + ("" if delivered else " -- TARGET NOT ONLINE, dropped"))
        # KEY: A `(0xA2,25) @Quit=` IS THE END OF THE TRADE, AND UNTIL NOW
        # NOBODY TOLD THE SERVER. Live report, 2026-08-24T23:33Z: "even though
        # we had a graceful trade quit, our lobby status hasn't changed from
        # 'busy' -- I'd still have to restart TM to do another trade." The
        # quit was addressed to the service nick and dropped (see above), so
        # `_TRADE_PENDING` kept the pair armed and the busy mark stood. Now
        # that we actually see it, disarm both sides.
        # BOTH `@Quit` arms end it: 25 TRADEQUIT and 30 TRADECLOSE.
        if _trade_a2 and ((_code >> 16) & 0xFF) in (25, 30):
            try:
                _qm = _session_get("member_id")
                _qpm = tetramaster.trade_partner_of(_qm)
                tetramaster.note_trade_cancel(
                    *[x for x in (_qm, _qpm) if x is not None])
                log("authserv",
                    "  TM trade: (0xA2,25) @Quit= -- trade ENDED, "
                    "disarmed members %r and %r" % (_qm, _qpm))
            except Exception as _qe:
                log("authserv",
                    f"  TM trade: @Quit disarm failed: {_qe!r}")
        # ARM THE TRADE-SESSION HANDSHAKE (static RE 2026-08-22, the
        # serve-a-list model RETRACTED). On an accepted @TrAns=/Ans=1 both
        # clients enter the trade board scene (TM.dll 0x15590) and run the
        # @TrID/@TrEnt (0xA0) -> @TrEntAns (0xA1) -> @Init (0xA2,24)
        # handshake before any card exchange. note_trade_accept now queues
        # @TrEntAns + @Init framed from the PARTNER (states 2/3), NOT the
        # old (0xA2,1)+(0xA2,26) list. The card lists are RELAYED, never
        # authored (each client sends its own via the relay above).
        #
        # WARNING: THE PRIME REMAINING SUSPECT for the "immediate error after
        # accept": scene 0x15590 STATE 0 walks the client's table array
        # (0x521bb70) for a row whose status byte +0x28 == 'D' and jumps to
        # the error state (0x12c) if NONE is found -- BEFORE state 1 sends
        # @TrID. So the decisive measurement for the next run is simply:
        # does EITHER client emit an inbound @TrID= (code 0xA0)? If YES,
        # state 0 passed and @TrEntAns/@Init are the next links to verify.
        # If NO @TrID ever arrives, state 0 (the 'D' table) is the blocker
        # and the handshake serve is moot -- solve the table status next.
        # POL_TM_TRADE_SERVE=0 disables the authored side entirely.
        # KEY: THE VS-COM-SHAPED PATH, RESTORED (2026-08-24, live report: "we
        # were able to get it a bit farther when I first started on this and
        # it was using the same sort of approach as VS COM").
        #
        # It was removed by `20bd5087` on the strength of a STATIC trace --
        # "the 0x2C flag sets a byte read nowhere, the (0x41,7) wake is never
        # polled" -- and that overruled two LIVE measurements which are still
        # the best evidence we have:
        #
        #  * `08409060`, ~26,000 client store lookups off the shim log: the
        #    client polls exactly two groups, the trade family
        #    **`0x2C` 0x2A 0x25 0x23 0x21** (15,927x) and the game family
        #    `0x41` `0x1A` (10,521x). 0x2C is polled constantly, and it is
        #    the ONE code in that ear with no client-side send site -- i.e.
        #    server-push only, which is what a go-signal looks like.
        #  * `1c54cd08`, 05:30Z: after the 0x2C flag landed **the accepter's
        #    scene ADVANCED** to polling (0x41,7) + 0x1A -- the same bare
        #    wake-up + @GameOwner pair the VS reservation flow waits on.
        #    (0x41,7)'s handler (0x8477F) parses NOTHING and drains (0x41,9)
        #    and (0x41,0x10) behind it: arrival alone is the event, the shape
        #    of "stop waiting, it is happening".
        #
        # That "scene advanced" is the furthest this has ever got, and every
        # model since has gone backwards from it. Three months of static
        # reading has since falsified its own successors -- the game band
        # (watchdog flat at 0 across three runs), the seat ('D' row; seating
        # both players changed nothing) -- so the static objection that
        # retired this one no longer outranks the measurement.
        #
        # WARNING: THE 0x25 PROBE IS NOT RESTORED. It was a vocabulary-harvest
        # device, never a fix, and 20bd5087's one durable observation is that
        # an unmatchable 0x25 body gets injected into the card list as a
        # garbage entry. No reason to carry that risk into a fix attempt.
        #
        # WARNING: MIXED EVIDENCE ON THE WAKE, kept honest: `f03a913c` reverted the
        # (0x41,7) push because at 15:20Z it was delivered and sat UNREAD --
        # that run's accepter polled only the ear codes. The 14:14Z run did
        # poll it. That inconsistency was blamed on a leftover SEAT, and the
        # seat model is now dead, so the real cause is unexplained. Hence its
        # own switch: POL_TM_TRADE_WAKE=0 leaves the go flag alone.
        #
        # POL_TM_TRADE_GO=0 disables both.
        if delivered and b"@TrAns=" in payload and b"/Ans=1" in payload \
                and os.environ.get("POL_TM_TRADE_GO", "1") == "1":
            try:
                _both = {_session_get("member_id"), _sess_member_id(cand[0])}
                _wake = os.environ.get("POL_TM_TRADE_WAKE", "1") == "1"
                # WARNING: THE GO FLAG MUST GO OUT **SYNCHRONOUSLY**, AND THE
                # FIRST ATTEMPT AT THIS PROVED IT (2026-08-24T14:17Z).
                # Queued via `_queue_push` it rode the idle drain and was
                # filed by the client **4.0 s after the accept**
                # (14:17:10.81 accept -> 14:17:14.82 push), while the
                # client's own probe_file order shows the trade already
                # over by then:
                #
                #     FILED 21000000@Tr=        the invite
                #     pgate = 0xFEAFDC3F        the partner gate SET
                #     pgate = 0x00000000        ZEROED -- the trade is done
                #     FILED 2C000100@TrGo=      ...the flag lands HERE
                #
                # `POL_TM_IDLE_MIN_S` is 4 s and the scene dies in about one
                # frame, so the queue can never win that race -- it is the
                # same "+14 s vs a scene that errors in seconds" trap the
                # card-list serve already fell into. The one mechanism
                # MEASURED to reach the client's store promptly is the
                # idle-drain framing sent on the freshest session at accept
                # time, which is what the board serve below uses. Use it.
                #
                # Both directions: `nick` sent the @TrAns (the accepter),
                # `target` is the initiator it answers, and each copy is
                # framed FROM the other one -- the id key that decides which
                # object the client thinks the message is about.
                _pad = (b" " if os.environ.get("POL_GAME_NOTICE_PAD")
                        == "space" else b"")

                def _sync_send(_to, _from, _body, _what):
                    try:
                        _c = sorted(PRESENCE.sessions_by_nick(_to),
                                    key=lambda s: (s.in_room_recently(),
                                                   getattr(s, "last_heard", 0)),
                                    reverse=True)
                        _l = NoPad(_game_notice_line(
                            b"GTM0G" + _body, _from, _to, srv))
                        for _s in _c:
                            if _s.send([_l], pad_override=_pad):
                                return True
                    except Exception as _e:
                        log("authserv", f"  TM trade {_what} send failed: {_e}")
                    return False

                # WARNING: RETRACTED, AND THIS IS THE STUCK "BUSY". The note here
                # said "the handler (0x08CC7F) sets `go` when the MSGID is
                # non-zero". 0x08CC7F is not a go handler -- it is the only
                # consumer of code 0x2C in the whole image (swept: one call
                # site), and it lives inside 0x8CC40, which is the client's
                # **am-I-busy predicate**:
                #
                #   0x8CC73  push 0x2c ; 0x825A0(out, 0x2C, -1)
                #   0xA8A63  jne  0xa8a74          found -> busy
                #   0xA8A65  push 0x11             block[11] = 17 = 'R'
                #   0xA8A79  mov  al, [0x524648E]  otherwise, this byte
                #
                # block[11] is the 12th letter of the member record's
                # 16-letter status block (base 0x5245F8B, so block[11] =
                # 0x5245F96, setter 0xA7B70). Measured on BOTH players all
                # evening: 'A' -> 'R' the moment a trade starts, and never
                # back, which is the live report's "both users still show as
                # being busy" and why only a TM restart -- which clears the
                # store -- ever fixed it.
                #
                # The block base is nailed by two independent constants: the
                # reset at 0xA7DF0 writes 'I' to 0x5245F98 and 'B' to
                # 0x5245F9A, which are block[13] and block[15] in every
                # observed string, and block[0] is the NAME LENGTH ('D'=3
                # for a 3-letter name, 'L'=11 for an 11-letter one).
                #
                # So the flag's ONLY effect on the client is to make the
                # player report itself busy. It does not open the board and
                # it advances nothing -- the board comes up through the
                # handshake we now serve in full (@TrEntAns -> @Init ->
                # ... -> the (0xA2,30) close). It is scaffolding from before
                # that was understood, and it is actively harmful, so it is
                # OFF by default. POL_TM_TRADE_GOFLAG=1 restores it for an
                # A/B; POL_TM_TRADE_GO=0 still disables the wake as well.
                _go_a = _go_i = False
                if os.environ.get("POL_TM_TRADE_GOFLAG", "0") == "1":
                    _go_a = _sync_send(nick, target,
                                       b"2C000100@TrGo=/P=0", "go flag")
                    _go_i = _sync_send(target, nick,
                                       b"2C000100@TrGo=/P=0", "go flag")
                # THE WAKE STAYS QUEUED, and that is deliberate rather than
                # an oversight. The 05:30Z measurement is that the scene
                # advanced to POLLING (0x41,7) only AFTER the flag landed --
                # so the wake only means anything if the flag worked, and if
                # it worked there is a live scene a few seconds later to
                # receive it. Sending both in one breath would also violate
                # the stagger law `_queue_push` documents.
                if _wake:
                    for _m in _both:
                        if _m is not None:
                            tetramaster._queue_push(
                                _m, b"41000700@GameStart=",
                                why="trade accepted: the (0x41,7) wake the "
                                    "advanced scene was measured polling")
                log("authserv",
                    "  TM trade accept -- 0x2C go flag: accepter=%s "
                    "initiator=%s (OFF by default -- its only consumer is "
                    "the client's am-I-busy predicate 0x8CC40)%s"
                    % (_go_a, _go_i,
                       "; (0x41,7) wake queued behind it" if _wake
                       else "; wake OFF"))
            except Exception as e:
                log("authserv", f"  TM trade go-push failed: {e}")
        if delivered and b"@TrAns=" in payload and b"/Ans=1" in payload:
            try:
                # (member, partner member, partner nick) both ways round:
                # `nick` sent this @TrAns (the accepter), `target` is who
                # it answers (the initiator). The partner nick is the
                # SOURCE the session pushes ride -- see note_trade_accept.
                _acc = _session_get("member_id")
                _ini = _sess_member_id(cand[0])
                # Role-aware (2026-08-24): the accepter's scene polls
                # (0xA2,1)+list; the initiator's 25/26 waiter wants the
                # list ONLY (an init would clog its ring unconsumed).
                tetramaster.note_trade_accept((_acc, _ini, target, "acc"),
                                              (_ini, _acc, nick, "ini"))
                # SERVE THE ACCEPTER NOW, VIA ITS FRESHEST SESSION, WITH
                # THE IDLE-DRAIN'S EXACT MECHANICS (2026-08-24). History
                # of this block, one failure per model: the idle-push
                # queue delivered at +14s (scene errors in seconds); the
                # in-this-reply serve was transmitted on the live @TrAns
                # socket and the client NEVER FILED it (zero store
                # FOUNDs, the purge popped nothing -- 01:49Z run). The
                # ONE path that has measurably FILED a served (0xA2,26)
                # is the idle drain's chat_sess.send of a
                # NoPad(_game_notice_line(...)) with the game-notice pad
                # override (00:40:46Z, consumed by the 26-waiter). Same
                # bytes, same framing -- just synchronously at accept
                # time, on the freshest session (the relay's zombie
                # lesson, 28c8d7fc). Re-serves stay as backup.
                # WARNING: THIS SERVE HAS ITS OWN COPY OF THE SWITCH, AND IT WAS
                # MISSING. `POL_TM_TRADE_SERVE` was defaulted OFF in
                # tetramaster.py, but the three gates there cover
                # note_trade_accept / the re-serve tick / the @Init serve --
                # NOT this synchronous accept-time send, which calls
                # `_trade_entry_lines` directly. So the 14:17Z run still put
                # three (0xA2,*) lines on the wire ("3 line(s) sent to the
                # accepter NOW" in the log) while the commit message said the
                # authored serve was off, and the run tested two changes
                # instead of one. A switch that does not reach every caller
                # is not a switch.
                _served = 0
                _serve_on = os.environ.get("POL_TM_TRADE_SERVE", "0") == "1"
                try:
                    _acand = [] if not _serve_on else sorted(
                        PRESENCE.sessions_by_nick(nick),
                        key=lambda s: (s.in_room_recently(),
                                       getattr(s, "last_heard", 0)),
                        reverse=True)
                    _pad = (b" " if os.environ.get("POL_GAME_NOTICE_PAD")
                            == "space" else b"")
                    for _why, _line in ([] if not _serve_on
                                        else tetramaster._trade_entry_lines(
                                            _acc, _ini, "acc")):
                        _nline = NoPad(_game_notice_line(
                            b"GTM0G" + _line, target, nick, srv))
                        for _ts in _acand:
                            if _ts.send([_nline], pad_override=_pad):
                                _served += 1
                                break
                except Exception as e:
                    log("authserv",
                        f"  TM trade accept-time serve failed: {e}")
                log("authserv",
                    ("  TM trade accept -- board serves armed for both "
                     "sides; %d line(s) sent to the accepter NOW "
                     "(idle-drain framing, freshest session), initiator "
                     "via the <DR>-clocked re-serves." % _served)
                    if _serve_on else
                    "  TM trade accept -- authored (0xA2,*) board serve is "
                    "OFF (POL_TM_TRADE_SERVE=1 restores it); the go flag is "
                    "the accept path now")
            except Exception as e:
                log("authserv", f"  TM trade serve arm failed: {e}")
        # A cancel (either direction) disarms it -- the board is gone.
        elif delivered and (b"@TrCan=" in payload
                            or b"@TrCanAck=" in payload):
            try:
                tetramaster.note_trade_cancel(
                    _session_get("member_id"), _sess_member_id(cand[0]))
            except Exception:
                pass
        return None
    # Tetra Master's own text format. Same envelope/class as Janhourou's
    # binary channel ('G'), but the body is `<8 hex code><command>` -- see
    # tetramaster.handle_line. Answering @TeachDV clears the "retrieving
    # default data" timeout; the rest is captured, not invented.
    if os.environ.get("POL_TM0", "1") != "1":
        log("authserv", f"  TM0 disabled -- {tetramaster.describe_line(payload[1:])}")
        return None
    try:
        # VERIFIED: MEMBER AND PEER ON EVERY TM0 LINE. Two clients' logs have to
        # be readable as ONE conversation -- without the member id, an
        # interleaved capture of two players is unattributable, and every
        # question in this subsystem so far has been "which of them did
        # that". The peer nick is here too because it IS the table id
        # (`tetramaster.table_index_for_peer`).
        _tm_member = _session_get("member_id")
        log("authserv", f"  TM0[m{_tm_member} {target!r}] <- "
                        f"{tetramaster.describe_line(payload[1:])}")
        # `target` is the peer NICK the client addressed -- the TM0 service
        # peer. tetramaster derives `/Shm=` (the @Init destination id) from
        # it; see the banner above `_teach_shm`. Nothing else uses it.
        # THE CLIENT'S OWN ID. `@Init=/NN=` carries the guid it calls
        # itself by, and it arrives on EVERY session -- unlike class-L
        # `<PC>`, which the roster also learns from but which is not
        # guaranteed. See `tmroom.note_guid`.
        # (An `@Init=/NN=` guid capture lived here and was REMOVED: it is
        # the 64-bit id, not the lobby-band one the member record needs.
        # See the `<PC>` note in `_roster_note`.)
        # `member_id` lets tetramaster PERSIST what the client tells us --
        # the guild from `@Init=/GLD=` lands in that member's save at
        # +0x3B, which is the byte the guild screen's gate reads.
        reply = tetramaster.handle_line(payload[1:], peer="auth-band",
                                        peer_nick=target,
                                        member_id=_session_get("member_id"))
    except Exception as e:
        log("authserv", f"  tetramaster raised on {payload[:80]!r}: {e} -- silent")
        return None
    # VERIFIED: DERIVE THE CLIENT'S ID KEY FROM THIS CONNECTION (2026-08-22). Both
    # halves are already here and nowhere else: `nick` is the client's login
    # nick (whose base-36 fold is the wire form of its own id) and
    # `tmroom.pol_id_of` is the SAME id in app form, as the client sent it in
    # `@Init=/NN=`. `K` is what makes a published table id round-trip -- see
    # `tmroom.CLIENT_KEY_DEFAULT`. Cheap, idempotent, and it LOGS a
    # disagreement with the compiled default instead of hiding one.
    try:
        import tmroom as _tmroom
        _self = _tmroom.pol_id_of(_tm_member)
        if _self:
            _tmroom.learn_client_key(_self, tetramaster.peer_guid(nick))
    except Exception:
        pass
    if not reply:
        # WARNING: SAY WHAT WAS DROPPED. This line used to name neither the code
        # nor the body, so a client stuck polling an unanswered slot looked
        # identical to one idling -- which is exactly what it cost on
        # 2026-09-07T02:05Z: the post-game "Change Settings" screen polled
        # every 15 s and the log could only say that SOMETHING went
        # unanswered. A bodyless poll decodes to no `@Cmd=` at all, so the
        # repr of the raw payload is the only thing that identifies it.
        log("authserv", "  TM0: no answer for this command -- captured, "
                        "silent: %r" % (bytes(payload[:64]),))
        return None
    # handle_line may answer with SEVERAL E-bodies: the card shop needs its
    # @ShEnter reply followed by an UNSOLICITED SHOPINIT push, because the
    # client sends nothing more once it has the endpoint. A bare bytes reply
    # stays a single line, so every other caller is unaffected.
    replies = reply if isinstance(reply, (list, tuple)) else [reply]
    out = []
    for r in replies:
        # Per-item, not one try around the loop: handle_line has already
        # POPPED any pending pushes into `replies`, so an encode raise here
        # used to kill the whole connection with those bodies in hand --
        # and this band never resends. One malformed body now costs itself,
        # with a traceback, not the reply and every push behind it.
        try:
            log("authserv", f"  TM0[m{_tm_member} {target!r}] -> "
                            f"{tetramaster.describe_line(r)}")
            out.append(NoPad(_game_notice_line(b"G" + tag + b"G" + r,
                                               target, nick, srv)))
        except Exception:
            import traceback as _tb
            log("authserv", f"  TM0 encode of {r[:80]!r} raised -- skipped:\n"
                            f"{_tb.format_exc()}")
    return out


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

#: Tetra Master's RANKING list file, and the same variable-length deal for the
#: same reason. `sqMgRkcpReadRankList` (TM.dll rva 0x1A6F70) asks for
#: `rows * 232` -- the 0x1A6FC4 chain `7n -> n + 4*7n = 29n -> <<3` -- where
#: `rows` follows from the `<LN>` we send, so no constant in _FETCH_PATHLEN can
#: be right for more than one list length.
#:
#: WE NAME THE FILE: the ranking reply's lead group `<RF>` carries the path, in
#: the same shape as the auction's `<SS>`/`<HS>`. It is `config/polpro.json`'s
#: `TM0:RR` entry that decides it, so this constant must agree with that entry.
_RANK_LIST_PATH = "U/g/TM0_RANKLIST"
_RANK_LIST_REC = 232


def _pool_character_name(cid):
    """The TM pool's character name for `cid`, or None.

    WARNING: `tmrank`'s `cname` IS NOT A CHARACTER NAME, whatever it is called.
    `_tm_pool_note` fills it from `<CR>` value[1], and that value is the
    **decimal Content ID** -- the live prod pool is full of rows like
    `{"cid": 30000046, "cname": "30000046"}`. Trusting the field's NAME instead
    of reading what is in it is what put Content ID digits back in the Tetra
    Master char-list slot on 2026-08-23, undoing the very bug the +0x18 work had
    just fixed. (`tm-shipped-data-beats-the-docs`, one directory over.)

    So: a purely numeric value is the id echoed back, not a name, and this
    returns None for it. If SE's client ever does send a real name there, it
    will not be all digits and it will be used.
    """
    if tmrank is None or cid is None:
        return None
    try:
        for rec in (tmrank.observed_pools() or {}).values():
            if accounts.content_id_int(rec.get("cid")) != cid:
                continue
            name = (rec.get("cname") or "").strip()
            return name if name and not name.isdigit() else None
    except Exception:
        pass
    return None


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
    if path == _AUCTION_BIDHIST_PATH:
        # WARNING: A DIFFERENT STRIDE FROM THE LISTS BELOW -- 0x48, not 0xC0.
        # `sqMgAccpReadBidList` (0x1A4A70) asks for `n*9<<3`, where the
        # exhibit reader asks for `n*3<<6`. Serving this path through the
        # 0xC0 branch would declare 192 bytes per row to a reader expecting
        # 72 and hang it on the difference, which is why the bid history
        # cannot share `U/g/TM0_BIDLIST` with `<SI>+<IB>`.
        try:
            have = os.path.getsize(_resource_read_file(path))
        except OSError:
            have = 0
        if have:
            rec = tmauction.BID_REC if tmauction is not None else 0x48
            if have % rec:
                log("lobby", f"  3:0 {path!r}: {have}B is NOT a whole "
                             f"number of 0x{rec:X} bid records -- the "
                             f"reader checksums at a record boundary and "
                             f"will reject it.")
            log("lobby", f"  3:0 {path!r}: serving its OWN {have}B "
                         f"({have // rec} bid(s) of 0x{rec:X}) + 4 trailer, "
                         f"unpadded. WARNING: Must match the `<SN>` sent with "
                         f"`<HS>` on the auth band.")
            return have + 4
        log("lobby", f"  3:0 {path!r}: no bids stored -- serving the EMPTY "
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
            have = os.path.getsize(_resource_read_file(path))
        except OSError:
            have = 0
        if have:
            if have % 0xC0:
                log("lobby", f"  3:0 {path!r}: {have}B is NOT a whole number "
                             f"of 0xC0 records -- the reader will checksum at "
                             f"a record boundary and reject it.")
            log("lobby", f"  3:0 {path!r}: serving its OWN {have}B "
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
        tmpl = _tm_template_blob(path)
        if tmpl:
            log("lobby", f"  3:0 {path!r}: nothing stored -- serving the "
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
        log("lobby", f"  3:0 {path!r}: nothing stored -- serving the EMPTY "
                     f"list (0 records + 4 trailer, unpadded). This is the "
                     f"honest answer while no auction state exists; it pairs "
                     f"with `SN`=0 in polpro.json.")
        return 4
    if tmrank.is_list_path(path):
        # THE RANKING LIST, on the same unpadded terms as the auction lists
        # above and for the same measured reason -- the reader's length is
        # `rows * 232`, not this opcode's default. See `_RANK_LIST_PATH`.
        try:
            have = os.path.getsize(_resource_read_file(path))
        except OSError:
            have = 0
        if have:
            if have % _RANK_LIST_REC:
                log("lobby", f"  3:0 {path!r}: {have}B is NOT a whole number "
                             f"of {_RANK_LIST_REC}B records -- the reader "
                             f"checksums at a record boundary and will "
                             f"reject it.")
            log("lobby", f"  3:0 {path!r}: serving its OWN {have}B "
                         f"({have // _RANK_LIST_REC} row(s) of "
                         f"{_RANK_LIST_REC}) + 4 trailer, unpadded. WARNING: The "
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
        tmpl = _rank_list_blob(path)
        if tmpl:
            log("lobby", f"  3:0 {path!r}: nothing stored -- serving the "
                         f"published/shipped {len(tmpl)}B "
                         f"({len(tmpl) // _RANK_LIST_REC} row(s)) + 4 "
                         f"trailer, unpadded.")
            return len(tmpl) + 4
        log("lobby", f"  3:0 {path!r}: nothing stored AND no shipped "
                     f"template -- serving 0 rows + 4 trailer. WARNING: The client "
                     f"will only survive this if polpro.json's TM0:RR says "
                     f"`LN`=0, and a zero count closes the screen.")
        return 4
    if (tmrank is not None and path == tmrank.RKDATA
            and _peer_is_ps2()
            and os.environ.get("POL_TM_RKDATA_PS2", "1") == "1"):
        # THE CONSOLE ASKS FOR 24, NOT THE PC's 28. Serving 28 + 4 is
        # what `sqMgReadFileCheck` rejected with -8250, rendered on
        # screen as `8250-37069` ("Error occurred while retrieving
        # rankings data"); measured 2026-09-09T01:22:56Z against the
        # console's own log ring. `tmrank.to_ps2` carries the
        # disassembly. +4 is this opcode's trailer, as everywhere else
        # in this table.
        n = tmrank.RKDATA_LEN_PS2 + 4
        log("lobby", f"  3:0 {path!r}: PS2 build -- serving {n} "
                     f"({tmrank.RKDATA_LEN_PS2} + 4 trailer), not the "
                     f"PC's {FETCH_PATHLEN.get(path)}")
        return n
    return None


#: Tetra Master `b/g/TM0SML` (and its alternate TM0SML2) default-data blob.
#: Parser reversed in TMaster.pex (validator at 0x00420560, accessors 0x00420a10/
#: 0x00420a30). Two gates, and all-zeros fails the first:
#:   * u32 at +0x00 = record COUNT; must be > 0 (else state 300).
#:   * 48-byte records start at +0x08; the client scans record[i] for one whose
#:     flag u32 (at buffer +0x14 + i*48) == 1 (else state 400). On a match it
#:     succeeds (state 3) and reads that record's u64 at +0x08 (0x003ab840).
#: So the minimal valid blob is: count=1, and record[0].flag=1 at +0x14. The rest
#: is zero-padded to the declared length. This is a FIRST cut -- the record body
#: (u64 at +0x08, and whatever the 48 bytes really encode: a card/rule/deck entry)
#: is still zero, which may surface a later gate; iterate against a savestate.
#: The peer every Tetra Master service entry is addressed to. It is the value we
#: already hand the client as `Shm=000000384EA5822C` in `@TeachDVAns`, the
#: subject it fetches `b/g/TM0IML` and `b/g/ZL` under, and the same id the PC
#: side's card-service entry uses (`tools/tmptl.py`'s type-`'F'` row).
_TM_SERVICE_PEER = 0x384EA5822C

#: `b/g/TM0SML` / `TM0SML2` -- the SERVICE list, read exactly like the TM0IML
#: manifest and laid out from TMaster.pex 2026-09-08 (load base 0x280000):
#:
#:     +0x00  u32   count            getter 0x00420a30 (lw [0x005c6190])
#:     +0x08  entry[16], STRIDE 48   -> 0x08 + 16*48 = 776, the exact payload
#:              +0x00  u64  id       getter 0x003ab840 (buf +0x08 + i*48)
#:              +0x0c  u32  flag     getter 0x00420a10 (buf +0x14 + i*48)
#:
#: The scan at 0x00420590 is the manifest's twin: count 0 -> state 300, no
#: flag-1 entry -> state 400, and on success it reads that entry's **u64 id**
#: (0x0042061c) and passes it to 0x003a9660 -- which builds an `sqMgJoinChannel`
#: request. So the id is a DESTINATION, and a zero there is "join nothing".
#:
#: KEY: That is why the card shop answered `0-37085` ("Timed out while connecting
#: to the server") with NOTHING on the wire: the flag was right, so the client
#: selected the entry and then tried to join channel 0. The shape was correct
#: from the start; only the address was missing.
_TM0SML_INIT = (struct.pack("<I", 1)                    # +0x00 count = 1
                + struct.pack("<I", 0)                  # +0x04 (reader ignores)
                + struct.pack("<Q", _TM_SERVICE_PEER)   # +0x08 entry[0].id
                + struct.pack("<I", 0)                  # +0x10 entry[0] pad
                + struct.pack("<I", 1))                 # +0x14 entry[0].flag = 1

#: KEY: `b/g/TM0CVML` IS THE PRIZE CENTER'S MANIFEST, AND IT IS TM0SML'S TWIN.
#: Laid out from TMaster.pex 2026-09-09, off the savestate taken when the
#: console could open every ranking screen EXCEPT this one.
#:
#: The tell was that Prize Center logged NO error and made NO request: the
#: client's own log ring in EE RAM ends on a SUCCESS (`sqMg:ReadSuccess[0]`,
#: `CVML(+$0)->$005C6CF0($188)`), and our lobby log shows why -- we answered
#: `b/g/TM0CVML` with 396B of zeros, so the count is 0 and the scene stops
#: before it ever asks us for anything. A screen that opens onto nothing is
#: this family's failure mode, not an error code.
#:
#:     0x00427fa0  lw v0,0x6cf0(at)      getter: count, buffer +0x00
#:     0x00427b2c  jal 0x00427fa0        s3 = that count
#:     0x00427b38  bne s3, zero, ...     non-zero -> run the scan
#:     0x00427b40  addiu v1, zero, 300   ZERO -> state 300, and it stops
#:     0x00427b98  addiu v1, zero, 400   no flag-1 entry -> state 400
#:
#: instruction for instruction the scan at 0x00420590 that `_TM0SML_INIT`
#: documents, which is why this shares that constant's shape exactly:
#:
#:     +0x00  u32   count
#:     +0x08  entry[8], STRIDE 48
#:              +0x00  u64  id     getter 0x003abc10 (buffer +0x08 + i*48)
#:              +0x0c  u32  flag   getter 0x00427f80 (buffer +0x14 + i*48)
#:
#: and the arithmetic closes exactly on the 392-byte payload we already declare
#: in `_FETCH_PATHLEN` (measured at TMaster.pex 0x003ab118, `a3 = 392`):
#: `0x08 + 8*48 = 392`. That is the same independent check that confirmed
#: TM0IML (0x10 + 8*48 = 400) and TM0SML (0x08 + 16*48 = 776).
#:
#: KEY: THE ID IS A DESTINATION HERE TOO. On a flag-1 hit the scan reads that
#: entry's u64 (0x003abc10) and hands it to 0x003ab9d0, which builds **`@CvReq=`**
#: -- `/NN=` `/HID=` `/Dm=` `/Vol=` `/CN=` -- and waits for **`@CvEnter=/EN=`**.
#: We have answered that pair since 2026-08-16 (`tetramaster.MSG_CVREQ`), so
#: nothing else needs building: this is the card-shop lesson one file over,
#: where `_TM0SML_INIT` had the right shape and a ZERO id and the shop died at
#: `0-37085`. Addressing it to `_TM_SERVICE_PEER` is what fixed that one.
#:
#: PS2-ONLY, so unlike `b/g/TM0RkData` there is no PC layout to keep: the PC's
#: `b/g/` set is ZL / RL / PTL / TM0RkData / TM0AucData / TM0Event{...} and
#: TM.dll carries no TM0CVML string at all -- the PC reaches `@CvReq=` without
#: any manifest, which is why the Prize Center worked there and not here.
#:
#: POL_RESOURCE_INIT_B_G_TM0CVML=<hex> overrides the blob (the generic
#: `POL_RESOURCE_INIT_<PATH>` lookup below); an empty count restores today's
#: "the screen will not open" behaviour.
_TM0CVML_INIT = (struct.pack("<I", 1)                   # +0x00 count = 1
                 + struct.pack("<I", 0)                 # +0x04 (reader ignores)
                 + struct.pack("<Q", _TM_SERVICE_PEER)  # +0x08 entry[0].id
                 + struct.pack("<I", 0)                 # +0x10 entry[0] pad
                 + struct.pack("<I", 1))                # +0x14 entry[0].flag = 1

#: The `b/g/TM0IML` MANIFEST -- laid out from TMaster.pex, 2026-09-08.
#:
#: AN ALL-ZERO MANIFEST IS FATAL, and this is the instruction that proves it
#: (module load base 0x280000; a plaintext TMaster.pex lives on
#: E:\ps2hdd\build\pol-plaintext.img, the repo has no decrypted copy):
#:
#:     0x003b7210  lui at,0x005c / lw v0,0x64a0(at)   <- getter: manifest +0x00
#:     0x00423944  jal  0x003b7210                    <- s2 = that count
#:     0x00423950  bne  s2, zero, 0x0042398c          <- non-zero: run the loop
#:     0x0042396c  addiu a3, zero, 92                 <- zero: RAISE ERROR 92
#:
#: and the error table's base is 37000, so selector 92 is the on-screen
#: **0-37092**, "Unrecoverable error occurred" -- measured live on a console
#: 2026-09-08 and matched 5/5 against the launches that fetched this file.
#: That retires the old note here ("empty manifest = nothing to chain-load"):
#: empty is not neutral, it is the wall.
#:
#: LAYOUT, and the arithmetic closes exactly on the 400-byte payload:
#:
#:     +0x00  u32   count
#:     +0x04  bytes      indexed by [0x00450d18]  (the TM0RkData chain, 0x003aac48)
#:     +0x08  halfwords  indexed by [0x00450d1c]  (       "            , 0x003aac50)
#:     +0x10  entry[8], STRIDE 48                 -> 0x10 + 8*48 = 400
#:              +0x00  u64  id      getter 0x003b7a90 (manifest +0x10 + i*48)
#:              +0x0c  u32  flag    getter 0x00423dc0 (manifest +0x1c + i*48)
#:
#: The loop at 0x00423994 walks i in 0..count-1 looking for the FIRST entry
#: whose flag == 1, stores its index and stashes its u64 id; if it runs off the
#: end without finding one it raises selector 193 instead. So a valid manifest
#: needs BOTH a non-zero count AND a flag-1 entry -- one without the other just
#: trades 0-37092 for 0-37193.
#:
#: The id is the one we already hand the client as `Shm=000000384EA5822C` in
#: `@TeachDVAns`, which is also the subject it fetches `b/g/TM0IML` and `b/g/ZL`
#: under -- so it is an id this client is known to accept, rather than a guess.
#:
#: WARNING: NOT YET PROVEN PAST THIS POINT. What consumes the stashed id (stored to
#: 0x005d15e0) is unread, so the next wall may simply be a different selector.
#: The `b/g/TM0RkData` chain (0x003aabd0, 24-byte sub-reads) hangs off a
#: DIFFERENT state machine (called only from 0x003fc0f4) and is deliberately not
#: served here; add it if a launch shows the client asking for it.
_TM0IML_INIT = (
    struct.pack("<I", 1)                    # +0x00 count = 1
    + b"\x00" * 12                          # +0x04 byte / +0x08 halfword arrays
    + struct.pack("<Q", _TM_SERVICE_PEER)    # +0x10 entry[0].id
    + struct.pack("<I", 0)                  # +0x18 entry[0] pad
    + struct.pack("<I", 1)                  # +0x1c entry[0].flag = 1  <- selected
).ljust(400, b"\x00")

#: `b/g/TM0CQL` -- the COM-battle SERVER LIST. Same family again, and the
#: arithmetic closes again: `0x08 + 32*48 = 1544`, the payload we serve.
#:
#:     +0x00  u32   count            getter 0x004231f0 (lw [0x005c6630])
#:     +0x08  entry[32], STRIDE 48
#:              +0x00  u64   id      getter 0x003ab260
#:              +0x10  char  host[32]
#:
#: KEY: **entry+0x10 IS A HOSTNAME OR IP, AS TEXT** -- not an id, which is what
#: makes this list different from TM0IML/TM0SML. Read out of the code rather
#: than guessed: the COM screen calls `sqMgOpen('TM0', entry+0x10, <u64>,
#: '46.49')` (TMaster.pex 0x0029901c, the ONLY sqMgOpen call site in the whole
#: module), which hands entry+0x10 as the first argument to polcore
#: `[0x00101210]` = `0x00135850` -> `0x00135558`, and there:
#:
#:     0x00135600  lb   v0, 0(s0)          <- the field's FIRST BYTE
#:     0x0013560c  addu v0, v0, <ctype>    <- index the ctype table
#:     0x00135614  andi a0, a0, 0x0004     <- test the DIGIT flag
#:     0x00135618  beq  a0, zero, 0x135638 <- not a digit: resolve as a HOSTNAME
#:     0x00135620  jal  0x00132b48         <- a digit: parse as a dotted IP
#:
#: So a NUL-terminated string, 32 bytes, and the client itself decides IP vs
#: name by whether it starts with a digit. On failure the session state at
#: 0x0043CB60 never reaches 1, and that is the gate the COM screen tests
#: (0x00422838 -> selector 138 -> the measured **0-37138**).
#:
#: WARNING: THE PORT IS NOT ESTABLISHED. It is not a constant on the polcore path that
#: was read, so it comes from somewhere unread. Serving our own advertised
#: address makes the console TELL US the port -- its connect attempt lands in
#: our logs -- which is a measurement rather than a guess, and is the intended
#: next step. Until something answers on that port the COM screen will still
#: fail; this only gets the attempt out of the client.
#:
#: POL_TM_COM_HOST overrides; empty (and no POL_ADVERTISE) serves zeros, i.e.
#: exactly today's behaviour.
_TM_COM_HOST = (os.environ.get("POL_TM_COM_HOST")
                or os.environ.get("POL_ADVERTISE") or "").strip()

if _TM_COM_HOST:
    _TM0CQL_INIT = (
        struct.pack("<I", 1)                            # +0x00 count = 1
        + struct.pack("<I", 0)                          # +0x04
        + struct.pack("<Q", _TM_SERVICE_PEER)           # entry[0] +0x00 id
        + b"\x00" * 8                                   # entry[0] +0x08
        + _TM_COM_HOST.encode("ascii", "replace")[:31].ljust(32, b"\x00")
    ).ljust(1544, b"\x00")                              # entry[0] +0x10 host
else:
    _TM0CQL_INIT = b""


#: *** ZONE OCCUPANCY -- WHY JOINING A ZONE NEVER MOVED THE COUNT. ***
#: `b/g/ZL`'s player count has always been "sum the headcounts of the rooms this
#: zone's `b/g/RL%03d` names", which is what `janlobby.zone_players` computes too.
#: That is not wrong, it is INCOMPLETE, and the live report is the proof:
#: entering a zone changed nothing, because a player standing on the ROOM LIST
#: has not joined an IRC channel and so is in no room, in no headcount, and
#: invisible to every count we serve. Zone occupancy is not room occupancy.
#:
#: THE STATE MACHINE IS IN THE FETCH LOG, not inferred. One session, 2026-08-18:
#:
#:     07:48:28  b/g/ZL     the zone-select screen  -> in NO zone
#:     07:48:54  b/g/ZL     a refresh of it
#:     07:48:57  b/g/RL000  ENTERED zone 0
#:     07:49:47  b/g/ZL     backed out to the zone list again
#:     07:49:55  b/g/RL000  back into zone 0
#:     08:03:13  b/g/ZL     out
#:
#: So `b/g/RL%03d` IS the zone-entry event and `b/g/ZL` is the zone-exit event.
#: Both are 3:0 fetches on the LOBBY band, and so is the `b/g/ZL` we have to
#: answer -- observation and consumption are in ONE process (`login`), which is
#: why this needs no published snapshot file the way the room registry does.
#:
#: WARNING: A ZONE'S PLAYERS ARE A UNION, NOT A SUM. Somebody who walked through the
#: room list into a room is in BOTH sets and must be counted once. Rooms are the
#: authority on identity here (they carry `member_id`); a session we cannot name
#: is counted as a room occupant only, which is the pre-existing behaviour.
#:
#: WARNING: AND IT IS LEASED, NOT LATCHED. A client that crashes on the room list sends
#: no exit event, and a latched entry would inflate the zone for the life of the
#: process. `POL_TM_ZONE_LEASE` seconds (0 disables the whole mechanism and
#: restores the room-sum).
_ZONE_OF = {}                       # member_id -> [zone_id, last_seen monotonic]


def _zone_lease():
    return float(os.environ.get("POL_TM_ZONE_LEASE", "1800") or 0)


def _note_zone_presence(zone):
    """`zone` is the id from a `b/g/RL%03d` fetch, or None for `b/g/ZL` (exit)."""
    if _zone_lease() <= 0:
        return
    mid = _session_get("member_id")
    try:
        mid = int(mid or 0)
    except (TypeError, ValueError):
        mid = 0
    if not mid:
        return                      # unnamed session: the room sum still has it
    if zone is None:
        if _ZONE_OF.pop(mid, None) is not None:
            log("lobby", f"  zone: member {mid} is back at the zone list")
        return
    was = _ZONE_OF.get(mid)
    _ZONE_OF[mid] = [int(zone), time.monotonic()]
    if not was or was[0] != int(zone):
        log("lobby", f"  zone: member {mid} entered zone {zone}")


def _zone_occupants(zone):
    """Members whose last zone event put them in `zone` and whose lease is live."""
    lease = _zone_lease()
    if lease <= 0:
        return set()
    now = time.monotonic()
    for mid in [m for m, (_z, t) in _ZONE_OF.items() if now - t > lease]:
        _ZONE_OF.pop(mid, None)
    return {m for m, (z, _t) in _ZONE_OF.items() if z == int(zone)}


def _lobby_counts_live(path, data, subject=0):
    """Live player/room counts in `b/g/ZL` and `b/g/RL%03d` for TETRA MASTER.

    The zone and room screens have always shown placeholders because both blobs
    are authored files that nobody updated. `janlobby` builds Jan's from the room
    registry already, but it is gated to Jan's content id and its topology is not
    TM's -- see the banner on `_jan_lobby_blob`. This does the counts only, by
    PATCHING the authored blob, so every other field stays as the TM track
    authored it. Field map and the cross-check that confirms it on TM: see
    `tmroom`'s "THE ZONE AND ROOM LISTS" banner.
    """
    if tmroom is None or os.environ.get("POL_TM_LOBBY_COUNTS", "1") != "1":
        return data
    if path != "b/g/ZL" and not (path.startswith("b/g/RL")
                                 and path[6:].isdigit()):
        return data
    try:
        live = _live_rooms() or {}
        room_members = {
            c: {int(r.get("member_id") or 0)
                for r in ((e or {}).get("who") or []) if r.get("member_id")}
            for c, e in live.items()}
        # COUNT THE SAME PEOPLE `build_ptl` DRAWS, OR THE TWO SCREENS DISAGREE.
        # This used to take the registry's own `members` field, which counts ROWS
        # -- and the registry holds a duplicate row after a reconnect, measured
        # 2026-08-20 (`members=3` for two people, and the member pane drew
        # one tester twice until `build_ptl` was made immune to it). The
        # snapshot de-duplicates on member id and this did not, so the room list
        # could say 3 while Table Members showed 2, from one registry.
        #
        # The duplicate is its own bug; a COUNTER should be immune to a repeated
        # input regardless -- the same argument `build_ptl`'s "`have` MUST GROW
        # AS WE GO" banner makes, one screen over.
        #
        # WARNING: FALL BACK, DO NOT ZERO. A channel whose rows carry no member id yet
        # is not empty -- ids bind slightly after the JOIN -- so it keeps the
        # registry's count rather than dropping to 0 and flickering.
        head = {c: (len(room_members.get(c) or ())
                    or int((e or {}).get("members", 0)))
                for c, e in live.items()}
        if path.startswith("b/g/RL"):
            # THE ROOM-LIST FETCH IS THE ZONE-ENTRY EVENT. See `_ZONE_OF`.
            _note_zone_presence(int(path[6:]))
            # THE TABLE COLUMNS, from the same rows `build_ptl` serves that room
            # -- one source, so the room list and the table screen cannot
            # disagree. See `tmroom.RL_F_TABLES`: those offsets are SE's own
            # names via janlobby and are NOT confirmed on TM, so this is also the
            # measurement that settles them. The screen shows 15/16 today and the
            # authored bytes at both offsets are 1/0 and 0x00, so 15/16 comes
            # from neither -- if the column does not move, the read site has to
            # come out of TM.dll before anything else is written there.
            # WARNING: DEFAULT OFF AGAIN, 2026-08-20, SAME DAY IT WENT IN. Writing
            # these two offsets made the room list "horrifically off" on screen
            # (live report). That is a RESULT, not a failure: it proves the client
            # does read at least one of them, so `0x18`/`0x86` are live fields --
            # and it proves they do not mean what janlobby's names say they mean
            # for THIS title, because the authored bytes there were 1/0 and 0x00
            # while the screen read 15/16.
            #
            # Standing rule: match the original, do not ship the stopgap. Until the read
            # site is found in TM.dll, the authored placeholder is a KNOWN wrong
            # number and this was an UNKNOWN wrong number, which is worse -- it
            # cannot be told from a protocol fault by anyone looking at the
            # screen. `POL_TM_ROOM_TABLE_COUNTS=1` re-enables it for whoever goes
            # looking, and the log line prints exactly what it wrote.
            tbl = {}
            if os.environ.get("POL_TM_ROOM_TABLE_COUNTS", "0") == "1":
                base = _tm_template_blob("b/g/PTL")
                for _i, chan in tmroom._room_channels(data):
                    counts = tmroom.room_table_counts(chan, base) if base else None
                    if counts:
                        tbl[chan] = counts
            out = tmroom.patch_room_list(data, head, tables_by_room=tbl)
            if out != data:
                log("lobby", f"  {path}: live headcounts patched in "
                             f"({sum(head.values())} player(s) across "
                             f"{len([c for c in head if head[c]])} room(s)); "
                             f"table columns -> "
                             f"{sorted(set(tbl.values())) or 'not written'} "
                             f"(total, open) for {len(tbl)} room(s)")
            return out
        # ...AND THE ZONE LIST IS THE EXIT EVENT: this screen is where you stand
        # when you are in no zone at all.
        _note_zone_presence(None)
        # For the zone list we need each zone's OWN room list to know which
        # channels belong to it.
        #
        # WARNING: READ THEM THE WAY THE CLIENT IS ACTUALLY SERVED, WHICH IS NOT THE
        # SAME AS READING THE STORED FILE. This loop used to `open()` the stored
        # resource only, and on prod `data/resources/` holds NO `b_g_RL*.bin` at
        # all -- the room lists are server-authored fixtures that ship in
        # `services/tmdata/`. So every zone missed, `rls` came out EMPTY,
        # `patch_zone_list` found no room list for any row and patched nothing,
        # and the zone screen showed its authored placeholders for ever.
        #
        # WARNING: AND IT FAILED SILENTLY, which is why it survived: the log line is
        # gated on `out != data`, so "patched nothing" and "nothing needed
        # patching" printed the same thing -- nothing. Measured 2026-08-20: five
        # `b/g/ZL` serves in one session, not one count line, while `b/g/RL000`
        # on the very same fetches logged "2 player(s) across 1 room(s)".
        #
        # This is the FOURTH time a server-authored fixture has been read from a
        # dev-only stored path (`b/g/ZL`, `b/g/PTL`, `U/g/TM0_RANKLIST`, now the
        # zone list's view of `b/g/RL`) -- see `_tm_template_blob`'s banner:
        # server-authored fixtures must ship. Stored copy first so a
        # per-member override still wins, shipped fixture behind it.
        rls = {}
        for zid in range(0, 32):
            rl_path = "b/g/RL%03d" % zid
            blob = None
            try:
                with open(_resource_read_file(rl_path, subject), "rb") as f:
                    blob = f.read()
            except OSError:
                blob = _tm_template_blob(rl_path)
            if blob:
                rls[zid] = blob
        zone_members = {}
        for zid in rls:
            who = _zone_occupants(zid)
            if who:
                zone_members[zid] = who
        out = tmroom.patch_zone_list(data, head, rls,
                                     zone_members=zone_members,
                                     room_members=room_members)
        standing = sum(len(v) for v in zone_members.values())
        if not rls:
            # NEVER SILENT AGAIN. "Found no room list" and "the counts were
            # already right" used to print identically, and that is the whole
            # reason the bug above went unnoticed through five serves.
            log("lobby", f"  b/g/ZL: WARNING: NO room list resolved for any zone -- "
                         f"neither a stored b/g/RL### nor a shipped fixture. "
                         f"Every zone keeps its AUTHORED placeholder count.")
        elif out == data:
            log("lobby", f"  b/g/ZL: live counts already match the authored "
                         f"blob ({len(rls)} room list(s) resolved, "
                         f"{sum(head.values())} player(s) in rooms) -- nothing "
                         f"to patch")
        else:
            log("lobby", f"  b/g/ZL: live counts patched in from "
                         f"{len(rls)} room list(s)"
                         + (f", including {standing} player(s) standing in a "
                            f"zone but in no room" if standing else ""))
        return out
    except Exception as exc:
        log("lobby", f"  {path}: live count patch failed ({exc!r}) -- serving "
                     f"the authored blob")
        return data


def _zone_host_live(path, data):
    """`b/g/ZL`'s dial target is THIS server, whatever the fixture says.

    Each zone row carries the host the client dials for the zone's game
    connection (`tmroom.ZL_F_HOST`, +0x2C), and the authored fixture carries
    the box it was CAPTURED on -- dev's LAN address. Served from prod, that
    field put both testers in SYN_SENT to 127.0.0.1:51241 and errored
    TRM-8196-37130 (measured live 2026-08-19). Deliberately NOT behind
    POL_TM_LOBBY_COUNTS: a wrong count is cosmetic, a wrong address is a dead
    rooms list. POL_TM_ZONE_HOST overrides the value (a hostname is legal when
    the client can resolve it); the default is the address we advertise
    everywhere else, which a hosts-file-less client can always dial.
    """
    if tmroom is None or path != "b/g/ZL":
        return data
    # \U0001f534 POL_ADVERTISE BEFORE _self_ip(). The docstring above has always
    # said "the address we advertise everywhere else", and this knob is
    # documented as "override the zone host (else POL_ADVERTISE)" -- but the code
    # fell straight through to `_self_ip()`, which is `_SELF_IP[0] or 127.0.0.1`
    # and is UNSET inside the container. So every zone row went out dialling
    # **127.0.0.1**: the PC clicked a zone, tried to connect to itself, failed
    # and dropped back to the title screen (observed live, 2026-09-09). Exactly the
    # failure this function was written to prevent, one address further along.
    host = (os.environ.get("POL_TM_ZONE_HOST")
            or os.environ.get("POL_ADVERTISE") or _self_ip())
    try:
        out, replaced = tmroom.patch_zone_hosts(data, host)
    except ValueError as exc:
        log("lobby", f"  b/g/ZL: zone host NOT patched ({exc}) -- serving as "
                     f"authored")
        return data
    if replaced:
        log("lobby", f"  b/g/ZL: zone dial target -> {host} "
                     f"(fixture said {', '.join(replaced)})")
    return out


#: *** A CLIENT'S BASE IS NAMED BY THE SEQUENCE IT ECHOES, NOT BY A DICT. ***
#:
#: WARNING: RETRACTION, 2026-08-20. What stood here was `_PTL_LIVE_BASE`, an in-process
#: `member_id -> chan` map of everyone we had served a LIVE-ROSTER `b/g/PTL` to,
#: and `_roster_delta_reply` refused to send deltas to anyone missing from it.
#: **It could never be true.** `b/g/PTL` is served by the `login` container
#: (`command: directory,lobby,world,mail`) and `<DR>` is answered by `authsess`
#: (`command: authserv`) -- two processes, two address spaces. The map was
#: written in one and read in the other, so the read side saw an empty dict for
#: every member, for ever. Measured on prod the day it shipped:
#:
#:     authserv.log   "-> N delta(s)"          0
#:                    "is CURRENT -- silent"   0
#:                    "NO live roster"        78   (throttled once per member)
#:                    `<DR>` seen         17,547
#:     lobby.log      live b/g/PTL builds    20-60 PER MINUTE, all session
#:
#: So every one of those 17,547 polls was answered `<DO>` = reload, which is the
#: b/g/PTL re-fetch storm the delta path existed to kill, running at full rate again. And
#: the diagnosis that came with it -- "the clients never re-fetch, so a member
#: whose base was built empty stays broken" -- is falsified by the same two logs:
#: they re-fetch every 3 s and the blob they get is live and current.
#:
#: THE STREAM ALREADY WORKED, and the previous night's log is the proof:
#:
#:     00:20:25  <DR>(0) -> 4 delta(s) ['PD','PD','PD','PD'] up to sequence 4
#:     00:20:29  <DR>(4) is CURRENT -- silent
#:
#: A client sitting at 0 was handed the whole chain and came back current. That
#: is a TEMPLATE HOLDER HEALING ITSELF: `tmroom.deltas_after` replays every `PD`
#: from `have + 1`, and replaying from 0 rebuilds the entire member list. There
#: is nothing to gate.
#:
#: WHAT THE REAL RACE WAS (and it is one -- the measurement was right, only the
#: cure was wrong). The shipped fixture carries `+0x40 = 1`:
#:
#:   Lex          00:44:59  <DE>            -> recorded, sequence 1
#:                00:45:04  fetch b/g/PTL   -> live, 1 member, sequence 1
#:   tester B     00:46:01  fetch b/g/PTL   -> room_of(6) EMPTY: template served,
#:                                             and the template SAYS SEQUENCE 1
#:                00:46:02  <DE>            -> recorded, sequence 2
#:
#: tester B then polled `<DR>(1)` -- a truthful echo of a blob that contains
#: nobody -- and 1 was, by coincidence, the room's real sequence at the moment
#: Lex arrived. So we served it only delta 2 (its own arrival) and called it
#: current. The collision is the bug: **an authored constant occupying a live
#: sequence number**. `_ptl_unknown_base` removes it by stamping 0 -- "I hold
#: nothing" -- on any blob we serve without placing its fetcher, which is a
#: sequence no room is ever at once anybody is in it (`_roster_sync_presence`
#: bumps on arrival), and which the replay path already handles. Fixed in the
#: DATA WE SERVE, not in a fixture, so a stale stored per-member copy is covered
#: too.
#:
#: WARNING: DO NOT REINTRODUCE A CROSS-BAND MEMORY. Anything the `<DR>` answer needs to
#: know about a fetch must go through `tmroom`'s published file, which is what
#: that module's `_OWNER`/`_publish`/`_read_file` dance is FOR. A plain dict at
#: module scope in this file is invisible to the other container -- that is the
#: mistake above, and `_room_of_member`'s docstring warned about it already.
_PTL_TEMPLATE_SERIAL = 0


def _ptl_unknown_base(data):
    """`data` with `+0x40` forced to 0 -- "this blob describes no known room".

    Serving the fixture's own `+0x40` is what let an authored constant collide
    with a live sequence; see the banner above. 0 is safe and self-healing: the
    client echoes it on `<DR>`, `deltas_after` replays the room's whole log, and
    the list rebuilds without a fetch.
    """
    if tmroom is None or len(data) < tmroom.SERIAL_OFF + 4:
        return data
    out = bytearray(data)
    struct.pack_into("<I", out, tmroom.SERIAL_OFF, _PTL_TEMPLATE_SERIAL)
    # WARNING: AND THE TABLE IDS MUST STILL ROUND-TRIP THE ID KEY. The fixture's
    # pre-fold ids (`0x0021...`) are exactly the ids a nick cannot carry, and
    # this path used to serve them raw -- which made the FIRST VS. COM attempt
    # of every launch fail ("could not play against the computer") while the
    # retry, on a room-folded refetch, worked. See `tmroom.fold_unknown_tables`.
    try:
        tmroom.fold_unknown_tables(out)
    except Exception:
        pass
    return bytes(out)


def _ptl_with_live_roster(path, data):
    """`b/g/PTL`'s member half, rebuilt from who is ACTUALLY in the room.

    The stored file stays the source of the TABLE half -- tables are genuinely
    server-defined content -- but its member block is a snapshot of a room, and a
    snapshot of a room has to be GENERATED. `+0x40` is stamped with the room's
    sequence because `cp__002fb560` reads it as exactly that, so the number
    the client echoes back on `<DR>` finally means something.

    Falls through to the stored bytes whenever we cannot say which room the
    fetcher is in -- the pre-roster behaviour, and the right answer for a client
    we hold no records for -- but with `+0x40` blanked by `_ptl_unknown_base`,
    because a blob that describes no room must not claim a live sequence. That
    one byte-field is the difference between a second player who heals on their
    next poll and one who is told they are up to date about nobody.
    """
    if tmroom is None or path != "b/g/PTL" or len(data) < tmroom.TOTAL:
        return data
    if os.environ.get("POL_TM_ROSTER", "1") != "1":
        return data
    try:
        member = _session_get("member_id")
        if not member:
            # No session, no room, and NOT a failure. Without this the `int()`
            # inside `room_of` throws and the handler logs "roster build failed",
            # which reads like a bug in the roster on a path where there is
            # simply nobody to look up. A log line that cannot tell those two
            # apart is the thing this project keeps losing hours to.
            return _ptl_unknown_base(data)
        # The RECORD says which room, not the IRC registry -- a client blipping
        # its auth session used to blink everyone out of everyone else's list.
        chan = tmroom.room_of(member) or _room_of_member(member)
        if not chan:
            log("lobby", f"  b/g/PTL: member {member} is in NO room we know yet "
                         f"(no <DE>, not in the registry) -- serving the blob "
                         f"with +0x40 = 0, so its first <DR> replays the whole "
                         f"log instead of claiming to be current")
            return _ptl_unknown_base(data)
        # WHO IS PRESENT comes from the room registry, not from who happens to
        # have sent a record -- see `tmroom.synth_member`. Measured 2026-08-18:
        # rooms-live.json held two people and tm-roster.json one, so the second
        # player was invisible in Table Members.
        # Anyone the IRC registry knows but who has not sent a record yet still
        # gets a row, via `synth_member`.
        who = ((_live_rooms() or {}).get(chan) or {}).get("who") or []
        present = [(row.get("member_id"), row.get("name") or row.get("nick"))
                   for row in who]
        built = tmroom.build_ptl(chan, data, present=present or None,
                                 self_member=member)
        if built is data:
            # `build_ptl` bailed: this room has no records and no live tables, so
            # what we hold is still the authored fixture and its `+0x40` is an
            # authored constant. Same treatment as "no room at all".
            return _ptl_unknown_base(data)
        n = struct.unpack_from("<i", built, tmroom.MEMBER_COUNT_OFF)[0]
        t = struct.unpack_from("<i", built, tmroom.TABLE_COUNT_OFF)[0]
        log("lobby", f"  b/g/PTL: built from the LIVE roster of {chan} FOR "
                     f"MEMBER {member} -- {n} member(s) and {t} table(s) IN THE "
                     f"BLOB, sequence {tmroom.sequence(chan)}")
        # Conclusive line for the self-guard fix: what value 0 we served for the
        # fetcher's OWN row, next to the id the client's self-guard holds. If the
        # model is right these match (readself.py's TM self-id == this value).
        if os.environ.get("POL_TM_SELF_ROW_ID", "0") == "1":
            log("lobby", f"  b/g/PTL: self row for member {member} served value 0 "
                         f"= {tmroom.self_row_value0(member)} (client self-slot; "
                         f"readself.py's TM self-id should equal this)")
        return built
    except Exception as exc:
        # Blank the serial here too: we do not know what this blob describes, and
        # "I hold nothing" is the only claim that cannot mislead the delta path.
        log("lobby", f"  b/g/PTL: roster build failed ({exc!r}) -- serving the "
                     f"stored blob with +0x40 = 0")
        return _ptl_unknown_base(data)


TM_SAVE_PATH = "U/g/TM0DataFile"


def _tm_save_defaults(path, data):
    """A FRESH Tetra Master save carries the FACTORY header, never zeros.

    WARNING: THIS IS `_tm_template_blob`'S BUG A FIFTH TIME, and the one that reaches
    the player fastest: `U/g/TM0DataFile` has no shipped template and no
    `RESOURCE_INIT` entry, so a member who has never stored a save is answered
    with 12332 zeros -- and the client reads its settings STRAIGHT out of that
    header (`apply_options_from_save`, TM.dll rva 0x14D9F0). Every setting whose
    default is not zero therefore reads as the first entry of its list. What the
    player sees is **SE Volume and BGM Volume Off on a brand new player**
    (+0xB3/+0xB4 = 0), with the chat window at 0 lines beside it.

    `tmsave.DEFAULT_HEADER` has held the right answer since 2026-08-20 -- the
    twelve factory values the client itself sent when the Options screen's
    `Default` button was pressed -- but it was only ever applied on the WRITE
    side (mint-on-first-@Opt, `tmsave.build`). That is too late, and worse, the
    zeros LAUNDER themselves: the client echoes its whole option set back in
    `@Opt=`, so the zeros it just read from us get stored as if the player had
    chosen them, and from then on the save legitimately says Off. Prod members
    4, 8, 9 and 11 are exactly that -- cards in the collection, options block
    still all zero. Serving the defaults is what breaks the loop.

    WARNING: **THE FRESH BLOB ONLY -- never a stored one.** Zero is a LEGAL value here
    (Off, muted, "Don't skip"), so healing a stored save would overwrite a
    deliberate choice: the same trap `iniheal` documents for the shim's ini and
    `tmsave.apply_defaults` documents for this file. A save that predates this
    is repaired explicitly, per member, with `tools/tmsave.py --heal`.
    """
    if tmsave is None or path != TM_SAVE_PATH:
        return data
    if os.environ.get("POL_TM_SAVE_DEFAULTS", "1") != "1":
        return data
    out = bytearray(data)
    moved = {}
    for off, (width, val) in sorted(tmsave.default_header_writes().items()):
        got = tmsave.write_field(out, off, width, val)
        if got:
            moved[off] = got
    # AND THE OPENING BALANCE, which is the same bug one field over: money lives
    # at `tmsave.MONEY_OFF` and the shop reads it from the SAVE (measured
    # 2026-08-18 -- `/M=` in SHOPINIT does not feed the Money display), so a
    # brand new player is shown whatever this field says until something else
    # writes their save. `money_of` is the one accessor for this and opens the
    # account at `POL_TM_START_MONEY` if there is none -- the same write the
    # first shop action would do, at the same value, just early enough to be
    # SEEN. That makes THIS the fastest of the three channels that put a
    # balance on a new player's screen (the others: `@ComGameInit=/M=`, which
    # assigns the live wallet at save-struct +0xC8, and the save `money_of`
    # authors on the next collection write).
    #
    # WARNING: AND THE SENTENCE THAT USED TO BE HERE -- "was shown 0 gold ... and
    # could not buy the pack that is meant to start them off" -- WAS THE
    # ARGUMENT FOR A 10000 STIPEND, AND IT IS FALSE. The pack meant to start
    # them off is the **Pauper's Pack**, and `PackPrm.BIN` prices it at **0**.
    # A player with nothing can already buy it; that is what it is for. The
    # 10000 that premise protected was minted here, seen by two players on
    # 2026-08-25 ("given 10000 gil" after a COM quit, and again on a relog),
    # and `POL_TM_START_MONEY` now defaults to 0. Do not re-argue it from
    # "they cannot afford a pack" without checking that table first.
    member = _session_get("member_id")
    if member and tetramaster is not None:
        try:
            bal = int(tetramaster.money_of(member))
        except Exception as exc:                 # never fail a fetch over money
            log("lobby", f"  {path!r}: cannot read member {member}'s balance "
                         f"({exc!r}) -- serving the header without it")
        else:
            got = tmsave.write_field(out, tmsave.MONEY_OFF, 4, bal)
            if got:
                moved[tmsave.MONEY_OFF] = got
    if moved:
        log("lobby", f"  {path!r}: no stored save -- serving the FACTORY header "
                     f"({len(moved)} field(s): "
                     + ", ".join("+0x%03X=%d" % (o, nw)
                                 for o, (_, nw) in sorted(moved.items()))
                     + "). A zero header reads as SE/BGM Volume Off.")
    return bytes(out)


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
    # phantom event -- a stranger ("Lex") sitting atop everyone's event board.
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
    if path in ("b/g/TM0EventList", "b/g/TM0EventDataList", "b/g/TM0EventMemberList"):
        if not _tm_event_active():
            return None
        return tmfixtures.template(path)
    if path == _EXHIBIT_LIST_PATH:
        return None                         # per-member store only; no fixture
    data = tmfixtures.template(path)
    if data is None:
        return None
    if path == "b/g/ZL":
        data = _zl_with_live_host(data)
    elif path.startswith("b/g/RL"):
        data = _rl_name_ps2(data)
    return data


#: `b/g/ZL` record +0x2C is a HOST, not a channel name -- rewrite it at serve
#: time so it can never go stale.
#:
#: The shipped file had **127.0.0.1** baked into it, an address on a LAN the
#: console is not on (it reaches us over the tailnet), so the string was
#: unreachable for every client that has ever read it.
#:
#: It is a host because of what consumes it: accessor 0x003a7320 hands the field
#: to TMaster.pex **0x00299220**, which is structurally identical to the COM
#: screen's opener at 0x00298f70 -- same shape, same 'IRC Session start already.'
#: string, same "log and return 1000" arm -- differing only in which state global
#: it guards (0x0043CB58 here, 0x0043CB60 there). Both take a host string and
#: open a session; see `_TM0CQL_INIT` for the other one, where the same argument
#: is proved to be text by the client's own first-byte digit test.
#:
#: Layout from tools/tmzonelist.py, measured off five leaf accessors: 0x48
#: header + 32 records * 64, count at +0x40, host at record +0x2C, 16 bytes.
#: POL_TM_ZONE_HOST overrides; unset falls back to POL_ADVERTISE; neither set
#: leaves the file's own bytes alone.
def _zl_with_live_host(data):
    host = (os.environ.get("POL_TM_ZONE_HOST")
            or os.environ.get("POL_ADVERTISE") or "").strip()
    if not host or len(data) < 0x48:
        return data
    HDR, REC, COUNT_OFF, F_CHANNEL, F_BYTE = 0x48, 64, 0x40, 0x2C, 0x3C
    width = F_BYTE - F_CHANNEL
    enc = host.encode("ascii", "replace")[:width - 1].ljust(width, b"\x00")
    buf = bytearray(data)
    try:
        n = struct.unpack_from("<I", buf, COUNT_OFF)[0]
    except struct.error:
        return data
    for i in range(min(n, (len(buf) - HDR) // REC)):
        o = HDR + i * REC + F_CHANNEL
        if buf[o:o + width] != enc:
            buf[o:o + width] = enc
    return bytes(buf)


#: \U0001f534\U0001f534 THE BUILD DISCRIMINATOR IS NOT TRUSTWORTHY FOR PAYLOAD SHAPE.
#:
#: `_peer_is_ps2()` reads `magic[+0x09]` from the lobby hello, and it was
#: introduced for SEND PACING -- `_lobby_ps2_send`, explicitly "payload-neutral,
#: identical bytes, only the send cadence changes". A false positive there costs
#: nothing, so it was never validated as a build test. Reusing it to choose a
#: RECORD LAYOUT is a different bar, and it does not clear it.
#:
#: MEASURED 2026-09-09 on prod: **every** hello in the log reports
#: `magic[+0x09]=0x00 (PS2)` -- 23 of 23 -- including the ones from the PC
#: Tetra Master that was on screen at the time, and `0xfa` (the value the comment
#: at the parse site calls "PC Viewer") has NEVER been observed. So the PC is
#: classified PS2 and gets the console's layout: live testing showed
#: "maids' Dreamworld" on the PC even AFTER the zone file was restored, because
#: the transform fired on the PC's own fetch (lobby.log 03:44:22).
#:
#: \u26a0 `_rkdata_for_build` (b/g/TM0RkData) rides the SAME gate and is still
#: default-on. It was proven on the console; it has NOT been checked on the PC,
#: and if the gate misfires there too the PC is being served the console's
#: 24-byte ranking header. That is the next thing to verify.
#:
#: Until there is a signal that has actually been shown to separate the builds
#: (the login NICK's client field is the documented one --
#: `accounts.get_client_token` / `nick_client_sig`, but it lives on the auth band
#: and the resource fetch is served by the `login` container), the two NAME
#: transforms below default OFF. That serves the PC layout -- which is what the
#: stored files now hold -- to everyone. The console gets a cosmetic prefix back
#: ("AAAMermaids' Dreamworld", "AFreewheeler Room 1"); the PC gets working zone
#: and room lists. Set POL_TM_ZL_NAME_PS2=1 / POL_TM_RL_NAME_PS2=1 to force the
#: console layout once the gate is sound.
#:
#: `b/g/RL%03d` room NAME: the PS2 reads it one byte EARLIER than the PC does.
#:
#: Reported from a screenshot: "still got the A in front of room names". Our
#: file is not corrupt -- it holds a clean "Freewheeler Room 1" at +0x2A -- and
#: the console renders "AFreewheeler Room 1". The only way to get exactly that
#: is to read the string starting at **+0x29**, so on this build the name field
#: begins there and the byte we were writing at +0x29 is its FIRST CHARACTER.
#:
#: +0x29 was set to 'A' (0x41) deliberately, as `tools/tmroomlist.py --set-vscom`,
#: on the strength of `0x45D78 cmp dword [0x52454FC], 0x41` -- a VS. COM gate
#: measured in **TM.dll, the PC build**. That reading may well be right there. It
#: is not right here, and this is the same class of mistake as answering the PS2
#: shop with `@EQuit`: a PC-derived offset applied to a build that lays the
#: record out differently. `tmroomlist.py`'s own docstring already warned that
#: Janhourou reads this record with a different map in which +0x29 is a name
#: byte -- the PS2's TM map turns out to agree with Janhourou here, not the PC.
#:
#: Shifting the name down one byte fixes the display AND retires the 0x41, which
#: on this build was never a gate -- which may matter, because VS. COM is exactly
#: what is failing.
#:
#: Guarded three ways: only `b/g/RL*`, only records whose +0xB8 channel starts
#: `#TM0` (RL001..004 in the same directory are JANHOUROU's and have their own
#: map -- writing here once turned "Test Room 1-1" into "TAst Room 1-1"), and
#: only when POL_TM_RL_NAME_PS2 is on (default 1; set 0 for the PC layout).
def _rl_name_ps2(data):
    # \U0001f534 PER-BUILD, not unconditional. This shifts the room name one byte
    # EARLIER because the console reads it at +0x29 -- but the PC reads it at
    # +0x2A, and +0x29 is where the PC's 0x41 sits. Applied to everyone it does
    # to room names exactly what `7b278b01` did to zone names: fixes the console
    # and eats the PC's first character. That regression was reported on the zone
    # list 2026-09-09 ("maids' Dreamworld"); this is the same bug one field over,
    # fixed before it was reported rather than after.
    if not _peer_is_ps2():
        return data
    if os.environ.get("POL_TM_RL_NAME_PS2", "0") != "1":
        return data
    HDR, REC, COUNT_OFF = 0x48, 200, 0x40
    F_VSCOM, F_NAME, F_CHAN, NAME_LEN = 0x29, 0x2A, 0xB8, 32
    if len(data) < HDR + REC:
        return data
    buf = bytearray(data)
    try:
        n = struct.unpack_from("<I", buf, COUNT_OFF)[0]
    except struct.error:
        return data
    n = min(n, (len(buf) - HDR) // REC)
    moved = 0
    for i in range(n):
        e = HDR + i * REC
        if bytes(buf[e + F_CHAN:e + F_CHAN + 4]) != b"#TM0":
            return data                      # not Tetra Master's -- hands off
        name = bytes(buf[e + F_NAME:e + F_NAME + NAME_LEN])
        if not name.split(b"\x00")[0]:
            continue
        buf[e + F_VSCOM:e + F_VSCOM + NAME_LEN] = name
        buf[e + F_VSCOM + NAME_LEN] = 0      # re-terminate the shifted field
        moved += 1
    return bytes(buf) if moved else data


def _auc_counts_live(path, data):
    """`b/g/TM0AucData` with the REAL Price List counts patched in.

    Twenty bytes, five `u32`, in menu order (All / Cheap / Affordable /
    Expensive / Exorbitant). The client copies them straight into its count
    array at `0x11EA5D` and GREYS any row whose count is zero, so this is what
    decides which bands a player can even open.

    Applied to the stored blob and the shipped template alike, exactly as
    `_ptl_with_live_roster` is: a fixture here would be a lie the moment anybody
    lists a card, and the shipped one is deliberately all-zero so that a failure
    to patch degrades to "no cards" -- the honest answer -- instead of to stale
    numbers that promise listings the browse will not return.

    WARNING: The counts use `tmauction.BANDS`; the FILTER uses the range the request
    carried. Those can only disagree if SE's client changes its bounds, and if
    they ever do, the client is right.
    """
    if tmauction is None or path != "b/g/TM0AucData" or not tmauction.enabled():
        return data
    try:
        counts = tmauction.band_counts(_auction_count_rows())
    except Exception as e:                       # never fail a fetch over this
        log("lobby", f"  auction: cannot compute band counts ({e}) -- serving "
                     f"the blob unpatched")
        return data
    out = bytearray(data)
    need = 4 * len(counts)
    if len(out) < need:
        out.extend(b"\x00" * (need - len(out)))
    for i, c in enumerate(counts):
        struct.pack_into("<I", out, 4 * i, min(int(c), 0xFFFFFFFF))
    if any(counts):
        log("lobby", f"  auction: Price List counts {counts} "
                     f"(All/Cheap/Affordable/Expensive/Exorbitant)")
    return bytes(out)


#: `b/g/ZL` zone NAME: the PC eats FIVE leading bytes, the PS2 eats TWO.
#:
#: WARNING: THIS IS A REGRESSION I CAUSED AND THEN HAD TO UNDO. `7b278b01`
#: "dropped the stray name prefix" by EDITING THE STORED FILE -- turning
#: `EN` `AAA` `Mermaids' Dreamworld` into `EN` `Mermaids' Dreamworld`. That fixed
#: the console, which had been rendering `AAAMermaids' Dreamworld`, and it broke
#: the PC, which then rendered `maids' Dreamworld`: reported 2026-09-09 as
#: "zone names are weird now, cut off with a odd icon".
#:
#: The `AAA` was never stray. The record is
#:
#:     +0x0C  "EN"    language
#:     +0x0E  "AAA"   THREE bytes the PC consumes and the PS2 does not
#:     +0x11  name    (PC reads here; the PS2 reads from +0x0E)
#:     +0x2C  host    rewritten live by `_zone_host_live`
#:
#: so the two builds start the string three bytes apart. A file edit cannot serve
#: both -- it picks for everyone, which is the same trap `POL_TM_SHOPQUIT_PS2`
#: documents. The stored file is back to the shape it shipped in and the CONSOLE
#: gets a transform instead, keyed on the same `_peer_is_ps2()` the rankings
#: layout already uses.
#:
#: Only `b/g/ZL`, only records whose +0x0E is literally `AAA` (so it is
#: idempotent and a no-op if the file is ever reshaped), and it stops at +0x2C so
#: it can never walk into the host field. `POL_TM_ZL_NAME_PS2=0` disables.
def _zl_name_for_build(path, data):
    if path != "b/g/ZL" or not _peer_is_ps2():
        return data
    if os.environ.get("POL_TM_ZL_NAME_PS2", "0") != "1":
        return data
    HDR, REC, COUNT_OFF = 0x48, 64, 0x40
    # The pad is now three SPACES, not "AAA" (see the zone file); both are
    # accepted so this still works if either shape is ever restored.
    F_PAD, F_NAME, F_HOST = 0x0E, 0x11, 0x2C
    PADS = (b"AAA", b"   ")
    if len(data) < HDR + REC:
        return data
    buf = bytearray(data)
    try:
        n = struct.unpack_from("<I", buf, COUNT_OFF)[0]
    except struct.error:
        return data
    n = min(n, (len(buf) - HDR) // REC)
    moved = 0
    for i in range(n):
        e = HDR + i * REC
        if bytes(buf[e + F_PAD:e + F_NAME]) not in PADS:
            continue
        buf[e + F_PAD:e + F_HOST] = (bytes(buf[e + F_NAME:e + F_HOST])
                                     + b"\x00" * 3)
        moved += 1
    if moved:
        log("lobby", f"  3:0 {path!r}: PS2 layout -- name shifted 3B earlier in "
                     f"{moved} zone record(s) (the console reads it at +0x0E, "
                     f"the PC at +0x11; POL_TM_ZL_NAME_PS2=0 disables)")
    return bytes(buf)


def _rkdata_for_build(path, data):
    """`b/g/TM0RkData` as THIS client's build reads it.

    One file, two readers. `tmrank.build_rkdata` writes the PC's 28-byte layout
    and `tmrank.to_ps2` slices the console's 24-byte view out of it -- a slice,
    not a second encoder, because both builds hold the same fields in the same
    order and differ only by the unread word the PS2 dropped from the front.
    The measurements are in `tmrank.to_ps2`'s comment.

    Applied in BOTH of `_resource_blob`'s chains, last, and only to this path:
    the published tally and the shipped fixture are both PC-shaped on disk, so a
    transform that ran on only one of them would serve the console a correct
    header when the weekly job had run and a misaligned one when it had not.

    POL_TM_RKDATA_PS2=0 serves the PC layout to everyone -- the pre-2026-09-09
    behaviour, kept so this can be A/B'd on the same screen without a redeploy.
    It gates the LENGTH in `_lobby_paylen` too, because the two have to move
    together: a right-shaped header at a length the console did not ask for is
    exactly the bug being fixed here.
    """
    if tmrank is None or path != tmrank.RKDATA:
        return data
    if not _peer_is_ps2() or os.environ.get("POL_TM_RKDATA_PS2", "1") != "1":
        return data
    out = tmrank.to_ps2(data)
    log("lobby", f"  3:0 {path!r}: PS2 layout -- {len(data)}B PC header sliced "
                 f"to {len(out)}B (dropping the PC's unread +0x00)")
    return out


#: Fresh (never stored) blobs that must not be all zeros. See the banners
#: above each: an empty manifest is the 0-37092 wall, not 'nothing to load'.
RESOURCE_INIT = {
    "b/g/TM0SML": _TM0SML_INIT,
    "b/g/TM0SML2": _TM0SML_INIT,
    "b/g/TM0IML": _TM0IML_INIT,
    "b/g/TM0CVML": _TM0CVML_INIT,
}
if _TM0CQL_INIT:
    RESOURCE_INIT["b/g/TM0CQL"] = _TM0CQL_INIT


# ---------------------------------------------------------------------------
# the member profile (the Viewer's content profile for content id 2)
# ---------------------------------------------------------------------------
_TM_CARD_LEVEL, _TM_TITLE, _TM_AVG_RANK = 16, 24, 9


def profile_fields(cid, member_id):
    """`{schema slot: value}` for the Tetra Master content profile: Card
    Level (16), Average Rank (9) and Title (24), only when held. See the
    core's _content_game_fields for the slot map and why unset stays unset.
    """
    out = {}
    try:
        import tmrank
        with open(tmrank.pool_file(), encoding="utf-8") as fh:
            pool = json.load(fh)
        want = accounts.content_id_int(cid)
        level = None
        # `member_id` reaches us from the handle link, which a Content ID
        # the client sent but nothing has linked yet does NOT have. The
        # pool is KEYED BY MEMBER ID, so the row that gives us the card
        # level also names whose it is -- use it rather than dropping the
        # stats for exactly the players who have been playing.
        for pool_key, row in (pool or {}).items():
            if int(row.get("cid") or 0) == want:
                if member_id is None:
                    member_id = pool_key
                lvl = row.get("card_level")
                if lvl is None:
                    # Fall back to the client's own `<CI>` string. It reads
                    # "Card Level 116", and it is what the CLIENT told us --
                    # rows recorded before `card_level` was split out carry
                    # only this.
                    m = re.search(r"(\d+)", str(row.get("cinfo") or ""))
                    lvl = m.group(1) if m else None
                if lvl is not None:
                    level = int(lvl)
                    out[_TM_CARD_LEVEL] = level
                break
        # AVERAGE RANK and TITLE. Both come from the career stats the match
        # path keeps in the collection's `rank` block, and both are served
        # only when that block has a game in it -- a player who has never
        # played has no average, and the title is a FUNCTION of the average,
        # so guessing one fabricates the other.
        collection = tmrank.collection_of(member_id) or {}
        stats = collection.get("rank") or {}
        games = int(stats.get("games") or 0)
        avg = int(stats.get("avg_rank") or 0)
        if games and avg:
            # Slot 9 is a 64-byte STRING (type 1 in `prof_002.pib`, and the
            # same widget kind as Player Name), so the SERVER decides the
            # formatting. Two decimals is the game's own rendering of this
            # fixed point everywhere it appears -- `Average Rank 0.48` on
            # Player Data, `2.96` on the ranking rows.
            out[_TM_AVG_RANK] = "%d.%02d" % (avg // 100, avg % 100)
            try:
                import tm_title
                # The SAME three inputs, in the SAME units, the client's own
                # picker uses -- money from the collection (which is what
                # the save's +0x34 is written from), card level from the
                # pool the client sent, average rank from the career block.
                # Feeding it anything else would show a title the game
                # itself would not.
                money = int(collection.get("money") or 0)
                idx = tm_title.title_index(money, level or 0, avg)
                if idx:
                    out[_TM_TITLE] = idx
            except Exception as exc:
                log("lobby", f"content profile: no title for member "
                             f"{member_id} ({exc!r}) -- leaving it unset, "
                             f"which the Viewer draws as Unknown")
    except Exception as exc:
        log("lobby", f"content profile: Tetra Master fields for {cid} failed "
                     f"({exc!r}) -- leaving them unset")
    return out


# ---------------------------------------------------------------------------
# glue that used to be inline in the core, now the title's own
# ---------------------------------------------------------------------------
def _rank_list_blob(path):
    """The bytes behind a ranking path: the published tally, else the shipped
    fixture, else None. ONE function for both halves on purpose -- `_tm_rank_reply`
    counts rows with it and `resource_template` serves bytes with it, so the
    `<LN>` we promise and the file we hand over are the same file by
    construction."""
    if not (tmrank.is_list_path(path) or path == tmrank.RKDATA):
        return None
    blob = tmrank.stored(path)               # <resources>/tmrank/, the job's output
    if blob:
        return blob
    return _tm_template_blob(path)           # services/tmdata/, shipped fallback


def _part_echo(nick, srv, sess):
    """Acknowledge a channel-less `PART :` -- the PS2 VS. COM leave.

    A VS. COM session on the console sends PART with an EMPTY channel after the
    game, from a session in no channel; an unacknowledged one leaves the client
    in state 3 until 0-37160 "Timed out while disconnecting from the server."
    It must be a SUCCESS echo (state 3 does `blez` on the result, so an error
    numeric fails the same way). `PART :<nick>` was tried live and rejected;
    the exact mirror of the request (an empty channel) is what the console
    accepts -- pinned live 2026-09-09. `POL_TM_PART_ECHO=nick|both` restores the
    other shapes; `POL_TM_PART_EMPTY=0` restores the old silence.

    Returns the lines to send, or None when this connection IS in a channel (the
    core then parts that channel properly)."""
    if sess is None or os.environ.get("POL_TM_PART_EMPTY", "1") != "1":
        return None
    if ROOMS.channels_of(sess):
        return None
    _form = os.environ.get("POL_TM_PART_ECHO", "mirror")
    _pre = b":" + nick + b"!~x@" + _irc_host(srv) + b" PART"
    _mirror = _pre + b" :"            # exact mirror of `PART :`
    _withnick = _pre + b" :" + nick   # tried live, rejected
    if _form == "mirror":
        _acks = [_mirror]
    elif _form == "nick":
        _acks = [_withnick]
    else:
        _acks = [_mirror, _withnick]
    log("authserv",
        f"channel-less PART (the PS2 VS. COM leave) -- "
        f"acknowledging so sqMgCommandReqCheck completes; this "
        f"session is in no channel, nothing mutated "
        f"(POL_TM_PART_EMPTY=0 restores the old silence); "
        f"echo form {_form!r}: {_acks!r}")
    return _acks


_CHECKOUT_PATHS = ("u/g/TM0_PM", "u/g/TM0_BI", "u/g/TM0_DI", "u/g/TM0_RM")


def _checkout_empty(path, subject):
    """True when a Check Out settlement file should be answered File Not Found.

    THE CHECK OUT EMPTY SIGNAL IS "NO FILE", NOT AN EMPTY STREAM. The scene's
    loader (TM.dll 0x1331E0) treats -650 as advance-with-the-bit-clear, and the
    inner scene -- the whole four-section stream -- is only BUILT when
    [scene+0x188] has a bit, i.e. when at least one of the four settlement saves
    EXISTED. Serving all-zero success for all four forces every empty visit
    through the stream and into "The server is busy." (dialog 0x149); -650 x4
    skips the stream and draws the real empty screen ("You have no bids or
    payments to make"). With anything pending the files serve zeros exactly as
    before, so the settlement flow is untouched. POL_TM_CHECKOUT_EMPTY_NODATA=0
    reverts."""
    if path not in _CHECKOUT_PATHS:
        return False
    if os.environ.get("POL_TM_CHECKOUT_EMPTY_NODATA", "1") != "1":
        return False
    if _resource_stored(path, subject):
        return False
    _co_member = _session_get("member_id")
    try:
        _co_pend = tmauction.pending(_co_member)
    except Exception:
        _co_pend = None
    if (_co_pend is not None and not _co_pend.get("money")
            and not _co_pend.get("cards")
            and not _co_pend.get("won")
            and not _co_pend.get("refund")):
        log("lobby", f"  3:0 {path!r}: member {_co_member} has NOTHING "
                     f"pending -- replying -650 File Not Found (type "
                     f"0x7e) so Check Out skips the stream entirely "
                     f"(the loader's bit stays clear)")
        return True
    return False


def _pool_character(cid):
    """(name, info, member_id) for a Content ID from the character pool the
    client filled on `<CR>`, or None. The name goes through
    `_pool_character_name` (a purely numeric `cname` is the id echoed back,
    not a name)."""
    want = accounts.content_id_int(cid) if accounts else cid
    for mid, rec in (tmrank.observed_pools() or {}).items():
        if accounts and accounts.content_id_int(rec.get("cid")) == want:
            return (_pool_character_name(want), rec.get("cinfo") or None, mid)
    return None


def _band_role(cmd_txt):
    """(priority, label) for what this in-session line says the band is.

    MATCH THE CODE HEADER, NOT THE KEY NAME. `@Init=` alone is not a game-band
    marker -- the card shop's SHOPINIT arm uses the same key on code 0xA2 -- so
    this reads the class letter AND the two-hex code out of the `GTM0G<code>`
    envelope: `GTM0GE1` = 0xE1 @TeachDV, `GTM0G80` = 0x80 @Init. Those two codes
    are the handshake and nothing else speaks them. The class-L `<D..>` roster
    poll marks the ROOM band. The game handshake outranks the room poll if a
    connection somehow carries both, because it is the handshake that makes TM
    record this socket as its GameIrcID."""
    if b"GTM0GE1" in cmd_txt or b"GTM0G80" in cmd_txt:
        return (2, "GAME band (@TeachDV/@Init handshake)")
    if b"GTM0L<D" in cmd_txt:
        return (1, "ROOM band (class-L <D..> roster poll)")
    return None


class TetraMaster(titles.Title):
    tag = b"TM0"
    content_code = 2
    fetch_pathlen = FETCH_PATHLEN
    resource_init = RESOURCE_INIT
    polpro_spec_files = tuple(p for p in POLPRO_SPEC_CANDIDATES if os.path.isfile(p))

    def core_bound(self):
        _rebind()

    def describe(self):
        return tetramaster.describe()

    # --- the auth band ---
    def notice(self, cls, payload, text, target, nick, srv, sess):
        return notice(cls, payload, text, target, nick, srv, sess)

    def polpro_reply(self, cls, payload):
        # THE DELTA STREAM ANSWERS `<DR>` BEFORE THE TEMPLATE DOES (class L);
        # the RANKING LISTS (R) and the AUCTION (A) answer themselves for the
        # same reason -- which list, and whose count, is a VALUE the static
        # template cannot express. Declining falls through to the template.
        if cls == b"L":
            return _roster_delta_reply(payload)
        if cls == b"R":
            return _tm_rank_reply(payload)
        if cls == b"A":
            return _tm_auction_reply(payload)
        return None, False

    def polpro_noted(self, cls, payload, target):
        # THE ROSTER: `<DE>`/`<PD>` carry this member's own record; recorded
        # whether or not there was a reply. THE CHARACTER POOL: the client is
        # telling us who it is. THE ROOM PEER: every class-L line is addressed
        # to the room's roster peer, whose nick folds to the guid table peers
        # index against.
        _roster_note(payload, self.tag)
        if cls == b"P":
            _tm_pool_note(payload)
        _roster_note_room_peer(target)

    def roster_sequence(self):
        return _roster_sequence()

    def part_echo(self, nick, srv, sess):
        return _part_echo(nick, srv, sess)

    def room_parted(self, chan):
        _roster_note_departure(chan)

    def room_notice(self, body, sess, nick):
        rebroadcast = _tm_chat_rebroadcast(body)
        if rebroadcast is not None:
            return ("rebroadcast", rebroadcast)
        filled = _tm_chat_fill_name(body, sess, nick)
        if filled is not None:
            return ("filled", filled)
        return None

    def rooms_changed(self, state):
        _roster_sync_presence(state)

    def session_closed(self, member_id):
        _roster_retire_on_close(member_id)

    def band_role(self, cmd_txt):
        return _band_role(cmd_txt)

    def idle_pushes(self, member_id, peers):
        return tetramaster.idle_pushes(member_id, peers)

    def requeue_pushes(self, member_id, items, why=""):
        tetramaster.requeue_pushes(member_id, items, why=why)

    # --- the lobby band ---
    def resource_length(self, path):
        return resource_length(path)

    def resource_nodata(self, path, subject):
        return _checkout_empty(path, subject)

    def resource_template(self, path):
        # THE PUBLISHED RANKING TALLY OUTRANKS THE SHIPPED FIXTURE, looked up
        # through the SAME function that answered `<LN>` on the other band.
        if tmrank.is_list_path(path) or path == tmrank.RKDATA:
            return _rank_list_blob(path)
        return _tm_template_blob(path)

    def resource_patch(self, path, data, subject):
        # THE SAME PATCHES FOR A STORED BLOB AND A TEMPLATE, IN THE SAME ORDER:
        # the template branch once skipped the live roster and served the
        # shipped tables with a permanently EMPTY member list.
        data = _ptl_with_live_roster(path, data)
        data = _lobby_counts_live(path, data, subject)
        data = _zone_host_live(path, data)
        data = _zl_name_for_build(path, data)
        data = _auc_counts_live(path, data)
        data = _rkdata_for_build(path, data)
        return data

    def store_patch(self, path, data):
        # A fresh save must carry the FACTORY option header rather than the
        # zeros that read as "every volume Off".
        return _tm_save_defaults(path, data)

    # --- the member profile ---
    def profile_fields(self, cid, member_id):
        return profile_fields(cid, member_id)

    def character_name(self, cid):
        return _pool_character_name(cid)

    def character(self, cid):
        return _pool_character(cid)

    def describe_line(self, body):
        return tetramaster.describe_line(body)


def register():
    return titles.register(TetraMaster())
