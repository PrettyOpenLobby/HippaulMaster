"""A member's gil: the stored balance, opening an account, clamped writes."""
import os
from . import collection, common


#: *** MONEY WAS A CONSTANT, AND THE SHOP RESET IT ON EVERY VISIT. ***
#: `/M=` in SHOPINIT is the Money the card shop displays (Cardsh.BIN names the
#: field). It was served from `_SHOPINIT_FIELDS` as a flat 10000 on every
#: `@ShReq=`, so a player who sold a card or bought a pack watched it snap back
#: to 10000 the moment they re-entered the shop. Nothing persisted it, and
#: nothing could: the client never reports its money.
#:
#: `POL_TM_START_MONEY` is what a member starts with.
#:
#: WARNING: IT WAS 10000, AND THAT NUMBER WAS NEVER MEASURED -- IT WAS THE UNMEASURED
#: `_SHOPINIT_FIELDS` CONSTANT, KEPT ONLY SO THE ACCOUNT-OPENING CHANGE WOULD
#: NOT LOOK LIKE A BALANCE CHANGE. Keeping it turned every new member into a
#: 10000-gold grant, and 2026-08-25 is when two players watched it land:
#:
#:   tester,   02:47Z  "fought a COM opponent, quit, lost a card, and was given
#:                      10000 gil"
#:   tester,   02:50Z  "another user backed out of the game and was given 10000
#:                      money as well upon logging back in"
#:
#: Neither is a prize and neither has anything to do with quitting -- the log
#: shows no payout on either match (`/R=0`, no wager, no `money` line between
#: `@ComGame=` and `@GameExit=`). It is THIS constant, materialised by `money_of`
#: at the member's first shop visit and then DELIVERED, late, by whichever of the
#: two channels the player touched first:
#:
#:   `@ComGameInit=/M=`  0x102DA9 `mov dword [esi+0xC8], eax` -- a plain
#:                       assignment into the LIVE WALLET. `esi` is the save
#:                       struct at 0x52463E0 (callers do `mov ecx, 0x52463E0 ;
#:                       call 0x101A10`, and 0x101A36 is `mov esi, ecx`), and
#:                       +0xC8 is the money field the save parser fills from
#:                       file +0x34 at 0x10123B. So whatever we serve here
#:                       BECOMES the player's on-screen gold.
#:   the authored save   `_collection_to_save` writes `money_of` at
#:                       `tmsave.MONEY_OFF`, which is what the client reads at
#:                       launch -- hence "given 10000 upon logging back in".
#:
#: The shop's own `/M=` is NOT a third channel: on `@Init` it is parsed and
#: discarded (money there is `[esi+0xCC]`, the `@EcmInit`/AUCMONEY field), which
#: is why the grant only became visible at a COM game or a relaunch.
#:
#: VERIFIED: ZERO IS WHAT SE SHIPPED FOR, and its evidence is in SE's own data: the
#: shop ladder in `PackPrm.BIN` opens with the **FREE Pauper's Pack** (No=0,
#: price 0) before 1000 / 1500 / 9000 / 40000 / 100000. A free starter pack is
#: only a design if a new player has nothing -- it IS the bootstrap, and a
#: 10000 stipend makes it pointless. Nothing anywhere measures a starting
#: balance; 0 is the only number here that is not invented.
#:
#: WARNING: THIS ONLY STOPS THE MINTING. A member with a balance already on record
#: keeps it -- `money_of` materialises once and never re-reads this. Undoing
#: grants already made is a deliberate act on somebody's save, not a default.
def _start_money():
    try:
        return max(0, int(os.environ.get("POL_TM_START_MONEY", "0"), 0))
    except ValueError:
        return 0


def _money_materialise_enabled():
    return os.environ.get("POL_TM_MONEY_OPEN_ACCOUNT", "1") == "1"


def money_of(member_id):
    """This member's money -- the stored value, opening an account if there is none.

    WARNING: THE FALLBACK USED TO BE A REFUND MACHINE, AND THAT IS WHY THE SHOP HANDED
    OUT FREE GOLD. `_start_money()` was returned WITHOUT being stored, so a member
    whose balance had never been written read 10000 on every single call, for
    ever. Nothing the client spent could stick, because there was no account for
    it to stick to -- the next read simply answered 10000 again.

    Reported live, 2026-08-20: "why does every shop visit give the player free money?
    I don't think that's how square handled it". It isn't. SE's server owned the
    balance; ours only owned it once something happened to write one.

    MEASURED, the same afternoon: that member bid 50 gold on an auction, their
    client went to 9950 (and proved it -- the wager default is 10% of gold and it
    sent `bm=995`), while `money_of` still answered **10000** because their
    collection carried no `money` key at all. The 50 would have been refunded on
    the next serve.

    So the fallback now OPENS THE ACCOUNT: the starting balance is written once,
    and from then on this is stored state that debits and credits accumulate
    against. Nothing else had to change -- `_collection_to_save` already writes
    our balance into the save at `tmsave.MONEY_OFF`, sales already credit and
    packs already debit. They were all accumulating against a number that reset.

    WARNING: AND THE VALUE IT OPENS AT IS NOW **0**, not the 10000 this docstring used
    to promise was unchanged. That 10000 was the grant two players watched arrive
    on 2026-08-25 -- see the banner above `_start_money` for the two delivery
    channels and why the free Pauper's Pack is the bootstrap SE actually shipped.
    Materialising is still ONCE: a member with a balance on record is untouched.

    `POL_TM_MONEY_OPEN_ACCOUNT=0` restores the non-storing fallback, which is the
    control for "did opening accounts change anything".
    """
    if member_id is None:
        return _start_money()
    data = collection._collection_load(member_id)
    v = data.get("money")
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        pass
    start = _start_money()
    if not _money_materialise_enabled():
        return start
    data["money"] = start
    if collection._collection_store(member_id, data):
        common._say("tm: member %s had no balance on record -- opening the account at "
              "%d. Until now every read answered this and stored nothing, so "
              "anything the client spent came back on the next serve. A "
              "NON-ZERO figure here is a grant this server invented and will "
              "push into the client's wallet (@ComGameInit=/M= writes it "
              "straight to save-struct +0xC8) -- POL_TM_START_MONEY."
              % (member_id, start))
    return start


def _set_money(member_id, amount, why=""):
    """Persist a new balance. Clamped at zero -- a negative balance is a number
    the client has no way to display and we have no evidence it ever sees."""
    if member_id is None:
        return False
    data = collection._collection_load(member_id)
    was = money_of(member_id)
    data["money"] = max(0, int(amount))
    if not collection._collection_store(member_id, data):
        return False
    common._say("tm: member %s money %d -> %d%s"
          % (member_id, was, data["money"], (" (%s)" % why) if why else ""))
    return True
