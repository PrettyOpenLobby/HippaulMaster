"""Stakes and pots: the durable stake record, the table pot, double-up and the PvP escrow."""
from . import collection, common, purse, pushqueue, ruleset, tablesettings, vscom


def _stake_persist(member_id, wager):
    """Record an in-flight wager stake DURABLY, beside the money it came from.

    The in-memory `_COM_GAME[key]['staked']` dies with the process, and deploy
    restarts mid-match ate two stakes in one afternoon (2026-08-22: members 3's
    135 and 100, both refunded by hand). The collection copy survives; the
    orphan sweep in `_stake_recover` refunds it when no live game claims it."""
    if member_id is None:
        return
    try:
        data = collection._collection_load(member_id)
        data["staked_wager"] = int(wager)
        collection._collection_store(member_id, data, sync_save=False)
    except Exception as e:
        common._say("tm: stake NOT persisted for member %s (%s)" % (member_id, e))


def _stake_clear(member_id):
    """The stake settled or refunded -- drop the durable copy."""
    if member_id is None:
        return
    try:
        data = collection._collection_load(member_id)
        if data.pop("staked_wager", None) is not None:
            collection._collection_store(member_id, data, sync_save=False)
    except Exception as e:
        common._say("tm: stake record NOT cleared for member %s (%s)" % (member_id, e))


def _stake_recover(member_id, why=""):
    """Refund a stake the process no longer knows -- the restart orphan.

    Called where a NEW game begins (@GameENC=) and on @GameExit=: a durable
    stake with no in-memory match behind it can only be a mid-match restart's
    leftovers, and the client's own abort path (0xBC701) refunded its local
    wallet, so the server must follow or the save sync eats the difference."""
    if member_id is None or not common._env_int("POL_TM_WAGER", 1):
        return
    try:
        entry = vscom._COM_GAME.get(pushqueue._push_key(member_id)) or {}
        if entry.get("staked"):
            return                      # a live game owns it; not an orphan
        if any(pushqueue._push_key(member_id) in (_stk or {})
               for _stk in _PVP_STAKES.values()):
            # A PvP escrow still holds it -- including a LEAVER's stake, which
            # stays in the pot for the seats still playing (`_seat_departed`).
            # Refunding here AND paying the pot out later would mint money.
            return
        data = collection._collection_load(member_id)
        orphan = data.pop("staked_wager", None)
        if not orphan:
            return
        data["money"] = max(0, int(data.get("money") or 0) + int(orphan))
        collection._collection_store(member_id, data)
        common._say("tm: VERIFIED: member %s had an ORPHANED stake of %d (a restart wiped "
             "the match that held it)%s -- refunded, balance %d"
             % (member_id, int(orphan), (" -- " + why) if why else "",
                data["money"]))
    except Exception as e:
        common._say("tm: orphaned-stake check failed for member %s (%s)"
             % (member_id, e))


#: VERIFIED: RETRACTED 2026-08-25, THE SAME NIGHT, BY READING THE BUILDER. The stake
#: is BACK ON: `@ComGame=/Rule=` occurrence 0 **is** the pot, and the banner
#: below (kept for the record) reasoned its way to the wrong answer from
#: position and coincidence.
#:
#: `0x106F87`..`0x106FE4`, the `@ComGame=` builder, emits occurrence 0 from
#: `[ebp+0xDC]` and clamps it first DOWN to **0x7D0 = 2000** and then UP to 0
#: before printing it -- and `[obj+0xDC]` is the POT (`0x52464BC`), with 2000
#: the same cap `POL_TM_WAGER_CAP` already carried. The `@ComGameInit=` parser
#: agrees from the other side: `0x102E05` stores `/R=` occurrence 0 to
#: `[esi+0xDC]`. Two functions, one slot, and a clamp that only makes sense
#: for money.
#:
#: WARNING: WHAT THE "BOTH PLAYERS SENT 135" EVIDENCE WAS WORTH: nothing, on its own.
#: It is a real observation with an innocent explanation (two players on the
#: same screen picking the same figure), and it was allowed to outweigh a
#: function nobody had opened. That is exactly the
#: open-the-function-not-just-the-address trap, and it cost a live feature for
#: an hour. The saved `bm` (2000 / 995) is the PRESET's own wager field, a
#: different tier from the live pot -- "looks more like a wager" was pattern
#: matching, not measurement.
#:
#: `POL_TM_WAGER_COM=0` still turns charging off if it ever needs an A/B.
#:
#: ---- the original, WRONG banner, kept so the reasoning stays auditable ----
#: WARNING: THE VS. COM STAKE IS OFF BY DEFAULT, AND IT IS OFF BECAUSE THE FIELD WE
#: WERE CHARGING IS NOT MEASURED TO BE A STAKE.
#:
#: `@ComGame=/Rule=` occurrence 0 was read as "the wager the player chose on
#: the COM select screen" and `_start`/settle arithmetic was built on it --
#: real gold, moved on every COM game. The wire does not support the reading.
#: Every `@ComGame=/Rule=` this server has ever logged opens with the SAME
#: number:
#:
#:     @ComGame=/Rule=135|1|1|1|3|0|0|0     member 3, wallet 39360
#:     @ComGame=/Rule=135|0|1|1|1|0|0|0     member 6, wallet  9600
#:     @ComGame=/Rule=0|1|1|1|3|0|0         member 3, the bare (no /Com=) form
#:
#: Two different players, wallets 4x apart, both sending 135. A per-player
#: stake cannot do that, and a money-DERIVED default cannot either -- the
#: tester's own measurement of the client's default is "10% of however much
#: you have in gold" (see `tmsave.DEFAULT_TABLE_PRESET_DERIVED`), which would
#: be 3936 and 960. Meanwhile the same two players' SAVED `bm` (tmsave +0xB8)
#: is 2000 and 995 -- and 995 is exactly 10% of the 9950 that client held.
#: **`bm` in the save behaves like the wager; `/Rule=` occ 0 does not.**
#:
#: So we were deducting an unverified constant and then telling the client its
#: pot was that constant, which is the live report: "at the end, it said
#: the wager was 0 even though it wasn't". Charging stops until the field is
#: read out of the `@ComGame=` builder (0x106F52 -> 0xA97C0) rather than
#: guessed from position. `POL_TM_WAGER_COM=1` re-enables it for that A/B.
#:
#: WARNING: SCOPED TO THE COM PATH ON PURPOSE. The PvP wager comes from somewhere
#: else entirely (`_PVP_STAKES`, filled at the accept quorum) and no evidence
#: here touches it -- `POL_TM_WAGER` / `POL_TM_WAGER_PVP` are unchanged. And
#: the `@GameExit=` REFUND stays live whatever this says, so a stake taken
#: before the flip is still given back.
def _com_wager_enabled():
    """Is the VS. COM stake allowed to move gold? Default NO -- see above."""
    return bool(common._env_int("POL_TM_WAGER", 1)
                and common._env_int("POL_TM_WAGER_COM", 1))

#: (chan, index) -> {push_key: (member_id, amount actually staked)} -- the
#: PvP wager ESCROW, filled at the accept quorum, settled at the result,
#: refunded by @GameExit/_stake_recover when a game never finishes. Its own
#: dict because the deal REASSIGNS `_MATCH_TURN[(chan, index)]`.
_PVP_STAKES = {}

#: (chan, index) -> the pot the table's NEXT game is played for. Seeded from
#: the table's `bm` rule and ESCALATED by Double Up -- see `_double_up_pot`.
_TABLE_POT = {}


def _wager_cap():
    """The hard ceiling on a pot. 0x10BF69 clamps to 0x7D0 and so does the
    `@ComGame=` builder (0x106F87), which is where `POL_TM_WAGER_CAP` came
    from; Double Up is the only thing that can climb to it from below."""
    return common._env_int("POL_TM_WAGER_CAP", 2000)


def _table_pot(chan, index, fields=None, reset=False):
    """The pot this table's next game is played for, clamped to the cap.

    Seeded from the table's own `bm` rule the first time it is asked for, then
    OWNED HERE, because Double Up moves it between games and nothing on the
    wire reports the new figure back (`@Continue=` has no pot field).
    """
    key = (chan, index)
    if reset:
        _TABLE_POT.pop(key, None)
    if key in _TABLE_POT:
        return _TABLE_POT[key]
    if fields is None:
        fields = tablesettings._table_rules_get(chan, index) or {}
    pot = max(0, min(int((fields or {}).get("bm", ruleset.TET_DEFAULTS["bm"]) or 0),
                     _wager_cap()))
    _TABLE_POT[key] = pot
    return pot


def _double_up_pot(chan, index, seats):
    """Escalate the pot for a REMATCH when the table's `du` rule is on.

    KEY: DOUBLE UP IS A POST-GAME STAKE ESCALATOR, NOT A PRE-GAME MULTIPLIER,
    which is the answer to "why not just wager double". Both `du` read sites
    (0x10BC4B and 0x10BF5C, two LAYOUTS of one panel) live inside `0x10B780`
    -- the **"Play again?" panel constructor**, `Playmes.BIN` 93, whose three
    callers are take-scene states 0x35/0x41/0x45 and whose accessors are all
    index 0x11 = `@Continue`. The transitions into 0x41 print their own names,
    `----GameDrawOver----` (0xBE713) and `----GameTradeOver----` (0xBEA5B), so
    the panel is reached AFTER the draw and AFTER the card trade.

    What it does there, exactly:

        0x10BF5C   if du == 1:  pot += pot        [0x52464BC] = pot * 2
                   if pot > 0x7D0: pot = 2000     the same cap we clamp to
        0x10CF91   eax = [0x52464A8]              the wallet
        0x10D9C9   if wallet < pot: du = 0        the rule switches ITSELF off
        0x10D191   ...and raises the "can't afford it" dialog on that compare

    KEY: THE 2000 CLAMP IS THE POINT OF THE RULE. The pot is hard-capped at 2000
    everywhere, so doubling is the only way to climb to the ceiling from a
    table set below it -- and it does nothing at all for a wager above 1000.

    WARNING: THE CLIENT'S CHECK IS AGAINST ITS OWN WALLET, so two players with
    different balances could disagree about whether the rule is still on. This
    server is the authority, and it takes the strict reading: the pot doubles
    only when EVERY seat can cover the doubled stake. Anything else would
    stake a player into an overdraft the client already refused to offer them.
    """
    key = (chan, index)
    fields = tablesettings._table_rules_get(chan, index) or {}
    pot = _table_pot(chan, index, fields)
    if not common._env_int("POL_TM_WAGER_DOUBLEUP", 1):
        return pot
    if not int(fields.get("du", ruleset.TET_DEFAULTS["du"]) or 0):
        return pot
    if pot <= 0:
        common._say("tm:   ...Double Up is on but the table's pot is 0 -- nothing to "
             "double (0x10BF5C doubles the pot, it does not invent one)")
        return pot
    want = min(pot * 2, _wager_cap())
    if want == pot:
        common._say("tm:   ...Double Up: the pot is already at the %d cap (0x10BF69) "
             "-- a rematch is played for the same %d" % (_wager_cap(), pot))
        return pot
    short = [(m, purse.money_of(m)) for m, _v in seats if purse.money_of(m) < want]
    if short:
        # 0x10D9C9's own arm, applied to the table rather than to one wallet.
        common._say("tm:   ...Double Up would take the pot %d -> %d, but %s cannot "
             "cover it -- the rule switches ITSELF off on that compare "
             "(0x10D9C9) and the rematch is played for %d"
             % (pot, want, "; ".join("member %s holds only %d" % (m, b)
                                     for m, b in short), pot))
        return pot
    _TABLE_POT[key] = want
    common._say("tm:   ...DOUBLE UP (rule du=1): the rematch pot doubles %d -> %d "
         "(cap %d, 0x10BF5C) -- the client's own panel has already done this "
         "arithmetic and nothing on the wire reports it back, so this is the "
         "half that has to agree" % (pot, want, _wager_cap()))
    return want


def _stake_pvp_pot(chan, index, seats, pot, why):
    """Take `pot` off every seat into the table's escrow. Returns the escrow.

    One place, because the accept quorum and the rematch must stake the same
    way or a rematch's refund path (`@GameExit` / `_stake_recover`) would not
    recognise what it is refunding.
    """
    if pot <= 0:
        return None
    stakes = {}
    for m2, _v2 in seats:
        put_in = min(pot, purse.money_of(m2))
        if put_in <= 0:
            continue
        purse._set_money(m2, purse.money_of(m2) - put_in,
                   "PvP stake in (table %s %s %d)" % (index, why, pot))
        _stake_persist(m2, put_in)
        stakes[pushqueue._push_key(m2)] = (m2, put_in)
    if stakes:
        _PVP_STAKES[(chan, index)] = stakes
    return stakes or None
