"""A connection that dies without a PART must retire its Tetra Master record.

THE BUG IT PINS, measured live 2026-08-20. Observed in live testing: **one** client in
`#TM0R001` and `tm-roster.json` held **two** `stat 2` records: PCTest was gone
from the IRC registry and still standing in the game roster. The dead-socket path
in `responders` did the IRC half of the cleanup -- `ROOMS.drop`, a broadcast
`QUIT :Connection closed`, `PRESENCE.unregister` -- and never told `tmroom`, so
records only ever retired on the two CLEAN exits: an IRC `PART` and class-L
`<PC>`.

WHY A TIMEOUT WOULD HAVE BEEN WRONG, since that was the first instinct. `<PD>` is
event-driven, not periodic: measured across one live session, the gaps between a
member's own record re-sends run 2 s, 5 s, three minutes, ninety minutes. There
is no heartbeat on that channel, so an expiry has nothing to work against and SE
cannot have used one either. The connection IS the mechanism.

AND THE ONE IT MUST NOT BECOME. Judging presence from current IRC membership on
every read was already tried and reverted, because auth-session churn blinked
people out of each other's lists (`tmroom.note_member`'s banner). This retires
ONCE, on an actual close, which is the same shape as the `PART` the module
already trusts -- so the seat-ordering and the `PC` delta have to come out
identical to the PART path, and that equivalence is asserted below.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tm_testenv                                                  # noqa: E402
tm_testenv.setup()          # this tree's services + the OpenLobby core

TMP = tempfile.mkdtemp(prefix="roster-retire-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_TM_ROSTER_KEY"] = "tm:test:%s:roster" % os.path.basename(TMP)

import responders as C                                             # noqa: E402
import tmtitle as R                                                # noqa: E402
import tmroom                                                      # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}"
                                                       if detail else ""))
    if not ok:
        FAILS.append(label)


def seed(chan="#TM0R001"):
    """Two members standing in one room, as `<DE>` would have left them."""
    for d in (tmroom._RECORDS, tmroom._ROOMS_SEQ, tmroom._DELTAS,
              tmroom._NAMES, tmroom._GUIDS, tmroom._POLIDS):
        d.clear()
    tmroom._OWNER[0] = True
    for mid, name in ((6, "LaptopTest2"), (9, "PCTest")):
        tmroom.note_member(mid, tmroom.synth_member(chan, mid, name), chan)
    return chan


chan = seed()
check(len(tmroom.members(chan)) == 2, "two members seeded",
      f"{len(tmroom.members(chan))} in {chan}")

# --------------------------------------------------------------------------- #
# The close.
before_seq = tmroom.sequence(chan)
R._roster_retire_on_close(9)

left = [m for m, _ in tmroom.members(chan)]
check(left == [6], "the dropped member is retired, the other one stands",
      f"remaining={left}")
check(tmroom.room_of(9) is None, "the retired member is in no room")

# The survivors have to be TOLD, or a client holding the row keeps drawing it --
# that is the ghost this whole fix is about, one layer further out.
after = tmroom.deltas_after(chan, before_seq)
tags = [d[1] for d in after]
check("PC" in tags, "a PC delta is queued for everyone still in the room",
      f"deltas={tags}")

# --------------------------------------------------------------------------- #
# It must be a no-op for anyone who is not in a room, because this runs on EVERY
# socket close in the service -- lobby-only sessions, redirect hops, POP3, the
# lot. A close-retire that raised, or that churned the roster file, would be paid
# for by every disconnect on the server.
seed()
before = dict(tmroom._RECORDS)
R._roster_retire_on_close(4242)                 # never heard of them
check(tmroom._RECORDS == before, "a member in no room changes nothing")
R._roster_retire_on_close(0)                    # a falsy id
check(tmroom._RECORDS == before, "member id 0 changes nothing")

# --------------------------------------------------------------------------- #
# The knob, because a live rollback must not need a deploy of new code.
seed()
os.environ["POL_TM_RETIRE_ON_CLOSE"] = "0"
try:
    R._roster_retire_on_close(9)
    check(len(tmroom.members(chan)) == 2,
          "POL_TM_RETIRE_ON_CLOSE=0 leaves the record alone")
finally:
    os.environ.pop("POL_TM_RETIRE_ON_CLOSE", None)

# --------------------------------------------------------------------------- #
# EQUIVALENCE WITH THE PART PATH. A dirty close and a clean PART must leave the
# roster in the same state -- if they diverge, one of the two is wrong and the
# next person will not know which.
seed()
R._roster_retire_on_close(9)
dirty = (sorted(m for m, _ in tmroom.members(chan)), tmroom.room_of(9))

seed()
tmroom.forget_member(9)                         # what the PART path calls
clean = (sorted(m for m, _ in tmroom.members(chan)), tmroom.room_of(9))

check(dirty == clean, "a dirty close leaves the same roster as a clean PART",
      f"close={dirty} part={clean}")

# --------------------------------------------------------------------------- #
# THE LAST-SESSION GUARD. Shipping the retire without this cost a live match:
# member 9 was DROPPED three times while playing, because a launch holds several
# connections and ANY of them closing retired him. The roster then served to the
# other player was missing himself.
class _Sess:
    def __init__(self, mid):
        self.member_id = mid


check(C._member_still_present([_Sess(9), _Sess(6)], 9) is True,
      "a member with another session in the room is still present")
check(C._member_still_present([_Sess(6)], 9) is False,
      "a member whose final session went is not present")
check(C._member_still_present([], 9) is False, "an empty room is not present")
check(C._member_still_present([_Sess("9")], 9) is True,
      "the id compares by value, not by type")
check(C._member_still_present([_Sess(None), _Sess(9)], 9) is True,
      "a session with no member id does not mask a real one")
check(C._member_still_present([_Sess(9)], None) is False,
      "a caller with no member id retires nobody")

# --------------------------------------------------------------------------- #
# POL-IDS MUST SURVIVE A RESTART. `_publish` dumps the in-memory maps and takes
# ownership, and a fresh process starts empty -- so without a seed the first
# publish after a bounce writes an empty `polids` over the good data. Records
# survive because clients re-send <DE> constantly; a POL-ID does not, because
# @Init=/NN= arrives once per LAUNCH. Measured 2026-08-20: member 9's id was
# captured at 15:47, lost to a bounce, and at 17:32 both players were served a
# roster naming only member 6 -- so the one listed player was listed to himself
# and his client hid the row as "me". An empty list, from an id we already knew.
seed()
tmroom.note_pol_id(6, "AB12CD56EB0F5932")
tmroom.note_pol_id(9, "AB12CD1EEEAE9C3B")
check(tmroom.pol_id_of(9) == "AB12CD1EEEAE9C3B", "both POL-IDs stored")

# the bounce: memory gone, the published snapshot still in the store
tmroom._POLIDS.clear()
tmroom._POLIDS_SEEDED[0] = False
tmroom._OWNER[0] = False
tmroom._SHARED.forget()

check(tmroom.pol_id_of(9) == "AB12CD1EEEAE9C3B",
      "a POL-ID survives a restart", "recovered from the published snapshot")

# ...and a live capture must beat the snapshot, never the other way round
tmroom._POLIDS.clear()
tmroom._POLIDS_SEEDED[0] = False
tmroom.note_pol_id(9, "AAAABBBBCCCCDDDD")     # this process just learned better
check(tmroom.pol_id_of(9) == "AAAABBBBCCCCDDDD",
      "a live capture wins over the published snapshot")

# --------------------------------------------------------------------------- #
# THE 09-25 SHAPE: AN OLD CONNECTION CLOSING MUST NOT FREE THE NEW ONE'S SEAT.
# A leftover client signed in as the same account exited while the member's
# current connection was seated and playing at table 2 ("member 28 left
# #TM0R001 -- released table(s) 2"). The core only checks the ROOM channel; a
# player in a match is in the TABLE channel on the other connection.
from tmgame import seating                                         # noqa: E402
from tmplugin import corenames as CN                               # noqa: E402


class _Live:
    def __init__(self, mid, chans, alive=True):
        self.member_id, self.chans, self.alive = mid, chans, alive


class _Presence:
    def __init__(self, sessions):
        self.sessions = sessions

    def sessions_for(self, mid):
        return [s for s in self.sessions if s.member_id == mid]


class _Rooms:
    def channels_of(self, sess):
        return list(sess.chans)


def seat_28(chan):
    seed(chan)
    tmroom.note_member(28, tmroom.synth_member(chan, 28, "PhoneUser"), chan)
    seating._SEATED_ADOPTED.add(chan)
    seating._SEATED[chan] = {2: [(28, b"PEER2")]}


real_presence, real_rooms = CN.PRESENCE, CN.ROOMS
try:
    seat_28(chan)
    CN.PRESENCE = _Presence([_Live(28, [b"#TM0T002"])])
    CN.ROOMS = _Rooms()
    R._roster_retire_on_close(28)
    check(seating._SEATED[chan].get(2) == [(28, b"PEER2")],
          "an old connection closing leaves the seat the live one plays from",
          repr(seating._SEATED[chan]))
    check(tmroom.room_of(28) == chan, "...and the room record")

    # the live connection then closes too: now it IS the last one
    CN.PRESENCE = _Presence([])
    R._roster_retire_on_close(28)
    check(not seating._SEATED[chan].get(2), "the LAST connection closing frees the seat",
          repr(seating._SEATED[chan]))

    # a Viewer lobby connection is not a Tetra Master connection: a crashed
    # game must still free its seat
    seat_28(chan)
    CN.PRESENCE = _Presence([_Live(28, [b"#POLLOBBY"])])
    R._roster_retire_on_close(28)
    check(not seating._SEATED[chan].get(2),
          "a connection in no #TM0 channel does not hold the seat")

    # the knob restores the unconditional release (so the check can fail)
    seat_28(chan)
    CN.PRESENCE = _Presence([_Live(28, [b"#TM0T002"])])
    os.environ["POL_TM_CLOSE_KEEPS_LIVE_SEAT"] = "0"
    try:
        R._roster_retire_on_close(28)
    finally:
        os.environ.pop("POL_TM_CLOSE_KEEPS_LIVE_SEAT", None)
    check(not seating._SEATED[chan].get(2),
          "POL_TM_CLOSE_KEEPS_LIVE_SEAT=0: the 09-25 release comes back")
finally:
    CN.PRESENCE, CN.ROOMS = real_presence, real_rooms
    seating._SEATED.pop(chan, None)

# --------------------------------------------------------------------------- #
# THE RANKINGS HEADER LAYOUT IS CHOSEN BY THE CLIENT'S BUILD, NOT BY OUR OWN
# ECHOED STATUS BYTE. `_peer_is_ps2()` says PS2 for every client; the login
# NICK's signature (PS2 TTTTT7I..., PC TTTTTAI...) is what tells them apart.
import tmrank                                                      # noqa: E402
from tmplugin import rankings, fetches                             # noqa: E402

real_sig, real_ps2 = CN._session_get, CN._peer_is_ps2
pc_head = bytes(range(28))
try:
    CN._peer_is_ps2 = lambda: True                # what the live hello always says
    for sig, want_len, what in (("TTTTTAIABCDEFGH", 28, "a PC"),
                                ("TTTTT7IABCDEFGH", 24, "a PS2"),
                                (None, 24, "an unknown client (the old answer)")):
        CN._session_get = (lambda s: (lambda k: s if k == "client_sig" else None))(sig)
        got = rankings._rkdata_for_build(tmrank.RKDATA, pc_head)
        check(len(got) == want_len, f"{what} is served the {want_len}-byte TM0RkData",
              f"{len(got)}B")
        n = fetches.resource_length(tmrank.RKDATA)
        check((n is None) if want_len == 28 else (n == tmrank.RKDATA_LEN_PS2 + 4),
              f"...and declared at the matching length for {what}", repr(n))
    check(rankings._rkdata_for_build(tmrank.RKDATA, pc_head)[:4] == pc_head[4:8],
          "the PS2 view is the PC header from +0x04 (unchanged)")
    CN._session_get = lambda k: "TTTTTAIABCDEFGH" if k == "client_sig" else None
    os.environ["POL_TM_RKDATA_DETECT"] = "magic"
    try:
        check(len(rankings._rkdata_for_build(tmrank.RKDATA, pc_head)) == 24,
              "POL_TM_RKDATA_DETECT=magic: the old answer, PS2 for a PC (can fail)")
    finally:
        os.environ.pop("POL_TM_RKDATA_DETECT", None)
finally:
    CN._session_get, CN._peer_is_ps2 = real_sig, real_ps2

print()
print("roster-retire: " + ("OK" if not FAILS else f"FAILED ({len(FAILS)})"))
for f in FAILS:
    print("   - " + f)
raise SystemExit(1 if FAILS else 0)
