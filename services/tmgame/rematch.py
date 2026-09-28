"""Play again? (@Continue): the answer ladder, the tally and the reset for a rematch."""
import time
from . import boardrules, common, matchmaking, matchstart, placement, pots, scoring, webwatch


#: The `/Ans=` ladder, read off the panel itself.
#:
#: `@Continue` is the post-game **"Play again?"** panel (`Playmes.BIN` 93; ctor
#: 0x10B780, built from 0xC2406, 13 states over table 0x10DCB8). Its constructor
#: at 0x10B7AB presets a player's slot to 2 when that player CANNOT play again,
#: which is what names 1 and 2; 3 has no writer anywhere in the image except the
#: receive arm itself, so it is a value only a server can produce.
CONTINUE_UNANSWERED = 0         #: has not answered
CONTINUE_AGAIN = 1              #: playing again      (sent from 0x10D27B / 0x10D375)
CONTINUE_DECLINE = 2            #: not playing again  (sent from 0x10D92B)
CONTINUE_FINAL = 3              #: SERVER ONLY -- "stop asking, tear the panel down"

#: (chan, index) -> {push_key(member): the highest answer that member has given}
#:
#: WARNING: MAX, NOT ASSIGN -- because that is what the client does. 0x104F8F/0x104F93
#: is `if (stored < received) stored = received`, so a slot only ever climbs. A
#: tally that could go back down would disagree with the panel it is driving.
_MATCH_CONTINUE = {}

#: (chan, index) -> push keys whose "Play again?" PANEL IS OPEN (their @Ready=
#: arrived). WARNING: A live-status update (sh>=2) delivered before the panel exists
#: sits in the client's store and is READ AFTER the seat answers -- which
#: bounces that seat back to the interactive panel (the rule the vote handler
#: states below). Measured 2026-09-07T04:37Z on the Deck: the status was pushed
#: at :19.56 while it was still on the result screen, its @Ready came :19.77,
#: its Yes :21.75, then TWO Recv=CONTINUE and no GameContinue -- a greyed panel;
#: the PC, which got only the tally, continued. So the status now waits for
#: @Ready (`POL_TM_CONTINUE_READY_GATE=0` restores the immediate push) and a
#: panel opening with a vote already in gets it one reply behind the @Ready
#: answer (`POL_TM_CONTINUE_READY_GAP`).
_CONTINUE_READY = {}


def _continue_body(answers, bval=0):
    """`@Continue=` -- the "Play again?" tally. Arm 0x104EE1, `Recv=CONTINUE`.

    `answers` is already in the RECIPIENT'S frame (occurrence 0 = the recipient);
    `_continue_frame` does that rotation.

    Two shapes here are load-bearing and both are hazards of the arm:

    * **One key, `|`-separated, exactly N values.** The same encoding `/ID=` and
      `/CN=` use -- see `_vsgame_body`'s banner. The loop at 0x104F40 runs
      `[obj+0xE5]` times (N, from GameInit's `/N=`) and **0x104F71 reads the out
      slot without checking the parse return**, so a short list maxes STACK
      GARBAGE into the client's per-player records. That is `/R=`'s bug one
      field over, and here a garbage value >= 3 tears the panel down instantly.
    * **`/B=` is repeated too.** It is read at occurrence `[obj+0x19B]` -- the
      recipient's own seat -- not at 0. Under the recipient-first convention
      that is 0 and one value would do; emitting N copies is correct under both
      conventions and costs nothing. (`/B=` is not really a `@Continue` field at
      all: it is the shared scene-exit byte 0x2B648E that `@Quit`(0x43,5),
      `@Quit`(0xA2,30) and SHOPQUIT(0xB2,23) also write. Meaning UNMEASURED --
      `POL_TM_CONTINUE_B` sweeps it, exactly as
      `POL_TM_MATCHQUIT_B` does for the same byte on the match quit.)
    """
    vals = [int(v) for v in answers]
    return (b"@Continue=/Ans=" + b"|".join(b"%d" % v for v in vals)
            + b"/B=" + b"|".join(b"%d" % int(bval) for _ in vals))


def _continue_frame(vals, me):
    """`vals` in ABSOLUTE seat order -> the frame the recipient at seat `me` reads.

    The client maps occurrence `i` to its own slot `(N - me + i) % N`
    (0x104F59..0x104F71), so with `POL_TM_VSGAME_SEAT0` making every recipient
    seat 0 -- the rule `_vsgame_body` established and everything downstream
    already follows -- the list is simply rotated recipient-first. Flip that knob
    and this rotation has to come off with it, which is why it reads the same
    env var rather than assuming.
    """
    vals = [int(v) for v in vals]
    if common._env_int("POL_TM_VSGAME_SEAT0", 1) and me:
        return vals[me:] + vals[:me]
    return vals


def _reset_for_rematch(chan, index):
    """Everything a fresh game needs cleared, when there is no fresh announcement.

    WARNING: A REMATCH DOES NOT GO THROUGH `@VsGameInit`, SO `_remember_match` NEVER
    RUNS. 0xC243E takes the panel's `0x4000054` straight back to the deal scene
    (`[obj+0x16F] = 0x18`) and the client re-picks its five cards. Without this
    the second game inherits the first one's hands, board and turn counter --
    `/H=` would index the wrong card, and `_CARD_READY` still holding both
    players would fire the deal on the first `@CardSelect=` of the pair.
    """
    matchstart._CARD_READY.pop((chan, index), None)
    boardrules._MATCH_HANDS.pop((chan, index), None)
    boardrules._MATCH_TURN.pop((chan, index), None)
    boardrules._MATCH_BOARD.pop((chan, index), None)
    boardrules._MATCH_OBJECTS.pop((chan, index), None)
    scoring._MATCH_COMBO.pop((chan, index), None)
    matchstart._TURN_RAND.pop((chan, index), None)
    matchmaking._MATCH_BOTS.pop((chan, index), None)
    matchmaking._BOT_SYNTH.discard((chan, index))
    placement._PENDING_BATTLE.pop((chan, index), None)
    # An unsettled PvP escrow is dropped WITHOUT refunding here -- each
    # member's refund rides their own durable staked_wager (@GameExit /
    # _stake_recover), and refunding both places would pay twice.
    pots._PVP_STAKES.pop((chan, index), None)
    # WARNING: A REMATCH IS A NEW GAME TO THE MATCH-HOLD CLOCK TOO. `_MATCH_BEGAN`
    # is stamped at the FIRST `@GameReady=` and a rematch sends no new one, so
    # after enough consecutive rematches the epoch aged past
    # POL_TM_MATCH_HOLD_S and `expire_silent_seats` released the seats UNDER
    # the live game -- the table republished as free, and a stranger reserving
    # it re-opens the stale-roster-hijack class (09-02 review M2).
    if (chan, index) in matchmaking._MATCH_BEGAN:
        matchmaking._MATCH_BEGAN[(chan, index)] = time.time()
    webwatch._live_matches_write()
    _MATCH_CONTINUE.pop((chan, index), None)
    _CONTINUE_READY.pop((chan, index), None)
