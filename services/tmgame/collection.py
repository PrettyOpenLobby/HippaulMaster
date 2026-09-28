"""A member's card collection file: loading, storing, granting, offers, deck reports, deck slots and
deck names.
"""
import os
import re
import tmblob
from .deps import tmsave
from . import cardshop, common, opener, purse, shopdoors


#: `/Ans=` must be POSITIVE -- 0x003a92f0 returns it and its caller `blez`es on
#: the result. Env-overridable because the value may be an id rather than an OK.
#: `POL_TM_INIT=0` silences the reply again (back to the captured-and-silent
#: behaviour) without a code change, which is the clean way to A/B it.
#: CARD PERSISTENCE -- and the client hands us both halves unprompted.
#:
#: The problem: TM.dll has no save-write API. It only ever calls
#: `sqMgCpLoadPlayerSaveData`, so `U/g/TM0DataFile` is a file SE's server owned
#: and served; the collection itself lives at `0x5246F10` with its count at
#: `0x52464CC` and is never written back anywhere. A purchase therefore dies with
#: the process, which is exactly what live testing hit: five cards and a deck at
#: 05:03, gone by the next launch.
#:
#: What makes this tractable is that BOTH halves already cross our wire:
#:
#:     @Card=   OUR OWN reply to `@Buy=` -- the cards we granted, with stats
#:     @Decks=  the CLIENT reporting its deck back to us, unprompted, whenever
#:              it changes. Measured 2026-08-17:
#:                @Decks=/C=5@0=/D=220|55|0|78|80|18|0@1=/D=215|90|2|...
#:                @Decks=/C=0@DN1=/S=59454148        <- "YEAH", the deck's NAME
#:              (msgid 0x22, and it has no arm -- it is the
#:              client TELLING us, so it wants recording, not answering.)
#:
#: So we can record a player's collection exactly, with no RE of the save format
#: at all. That is what this does, and it is the half with no unknowns.
#:
#: VERIFIED: DELIVERY IS SOLVED, AND IT WAS NEVER A MESSAGE (2026-08-18). The paragraph
#: above is right that TM.dll has no save-WRITE api and wrong about what follows
#: from that: the client LOADS its collection from `U/g/TM0DataFile`, so the save
#: is the delivery channel in the only direction that exists. `services/tmsave.py`
#: carries the measured layout -- count u16 at +0x3C, 1000 records of 12 from
#: +0x148 -- and `_collection_to_save` writes it on every collection change.
#: `POL_TM_SAVE_WRITE=0` reverts to record-only.
#:
#: What made this hard to see: an earlier reading concluded "the collection is never loaded
#: from the save" from a scan for ABSOLUTE references into the save buffer, which
#: cannot see an indexed loop off a register -- and that is exactly how the card
#: region is walked. The check that broke the deadlock was not a better scan, it
#: was noticing that otherwise every retail player would have logged in with zero
#: cards until they visited the shop.
#:
#: WARNING: AND IT EXPLAINS THE INFLATED RECORDS. Serving a zero header re-cleared the
#: shop's "already bought" flags every launch, so the FREE Pauper's Pack was
#: buyable for ever and the recorder banked cards no real player could hold (30
#: on member 1). Preserving the header is not cosmetic -- it is what stops that.
#:
#: `POL_TM_CARD_RESTORE=1`'s unsolicited `@Card=` push is left in place but is no
#: longer the plan: it was the least-invented guess when the save looked closed,
#: and the save is not closed. Recording stays unconditional because it costs
#: nothing and cannot be wrong.
_COLLECTION_SUFFIX = ".tm_collection.json"


def _collection_file(member_id):
    """The record a member's collection is kept under (tmblob.py): scope the
    member, path `tm_collection.json`, the old file name's two halves. Its own
    path, so it can never collide with a POL resource the member stores."""
    if member_id is None:
        return None
    return "%s%s" % (member_id, _COLLECTION_SUFFIX)


def _collection_load(member_id):
    name = _collection_file(member_id)
    if not name:
        return {}
    try:
        data = tmblob.read_json(name)
    except tmblob.errors():
        return {}
    return data if isinstance(data, dict) else {}


def _save_resource_file(member_id):
    """The record of the member's `U/g/TM0DataFile`, named the way the core's
    `_resource_file` names it -- member-scoped, because a save is user-owned
    and is deliberately NOT in `_SUBJECT_KEYED_PATHS`. The core serves this
    same row."""
    if member_id is None:
        return None
    return "%s.U_g_TM0DataFile.bin" % member_id


def _collection_to_save(member_id, cards):
    """Write the collection into the member's save, so it survives a relaunch.

    THIS IS THE DELIVERY HALF, and it is no longer an open question: the client
    loads its collection FROM the save (`services/tmsave.py` has the measured
    layout and the chain that proves it). Recording without this step is what
    made a purchase die with the process.

    WARNING: THE HEADER IS PRESERVED, NEVER REBUILT. Money, rank and the shop's
    "already bought" flags all live in +0x00..0x147, and the Pauper's Pack bit is
    among them. Dropping that header is what let the FREE pack be re-bought on
    every launch and inflated the recorded collections past anything a real
    player could hold. So an existing save is read and used as the base; if there
    is none, a zero header is the only honest starting point, and the first
    purchase is what fills it.
    """
    if tmsave is None or os.environ.get("POL_TM_SAVE_WRITE", "1") != "1":
        return False
    name = _save_resource_file(member_id)
    if not name:
        return False
    # Everything that does not come from the save itself is worked out FIRST,
    # outside the save's lock: `money_of` may open the member's account, and
    # that store rewrites this very save.
    #
    # MONEY GOES IN THE SAVE, not in SHOPINIT. `/M=` does not feed the shop's
    # Money display -- measured 2026-08-18 by serving `/M=4321` and watching
    # the screen stay at 87 across a shop exit and re-entry. The balance
    # comes from the save header at `tmsave.MONEY_OFF`, which we had been
    # writing as zeros, which is why every launch started the player at 0.
    if _deck_slots_on():
        # THE DECK SLOTS: byte 8 of every record is the card's position
        # (255 = none), never the CardPrm column -- see DECK_SLOT_COUNT.
        _data = _collection_load(member_id) or {}
        _slots = _deck_slot_bytes(_data, cards)
        cards = [(list(c) + [0] * 8)[:7] + [_slots[i]]
                 for i, c in enumerate(cards)]
    money = purse.money_of(member_id)
    # WARNING: OFF BY DEFAULT since 2026-09-07: +0x104 is the FIRST DECK TAB's
    # name, not the shop's pack label -- a tester's card screen read
    # the week's champion there. `POL_TM_CHAMPION_SAVE_SLOT=1` restores
    # the stamp; the shop's "<X>'s Pack" still rides `@Init=/SN=`.
    # THE DECK SET NAMES, from the player's own `@Decks=` name report.
    fields = sorted(_deck_name_fields(member_id).items())
    if common._env_int("POL_TM_CHAMPION_SAVE_SLOT", 0):
        _cn = shopdoors._champion_name()
        if _cn:
            fields.append((shopdoors.SAVE_OFF_CHAMPION,
                           (shopdoors.SAVE_CHAMPION_WIDTH, _cn)))
    # ONE TRANSACTION, holding the save's lock, from reading the header to
    # writing the result: an `@Opt=` patch landing in between would otherwise
    # be overwritten with the header read before it. A read error raises out
    # of it and nothing is written -- rebuilding the save from a zero header
    # is how a player's settings get destroyed.
    try:
        with tmblob.locked(name) as conn:
            base = tmblob.read(name, conn=conn)
            try:
                _buf = bytearray(tmsave.build(cards, base, money=money))
                for _off, (_w, _val) in fields:
                    tmsave.write_field(_buf, _off, _w, _val)
            except ValueError as e:
                # encode_card refuses rows the client would SILENTLY drop. Say
                # which, and leave the existing save alone rather than
                # shipping a worse one.
                common._say("tm: save NOT rewritten for member %s -- %s"
                            % (member_id, e))
                return False
            tmblob.write(name, bytes(_buf), conn=conn)
    except tmblob.errors() as e:
        common._say("tm: could not write %s: %s" % (name, e))
        return False
    common._say("tm: member %s save rewritten -- %d card(s) at +0x%X, count at +0x%X"
          % (member_id, len(cards), tmsave.CARDS_OFF, tmsave.COUNT_OFF))
    return True


def _collection_store(member_id, data, sync_save=True):
    name = _collection_file(member_id)
    if not name:
        return False
    try:
        # One statement: a reader sees the old record or the new one. This is
        # the wallet/collection OF RECORD (it also carries the durable
        # staked_wager), and the file's old truncate-in-place write meant a
        # crash mid-write -- or two of this member's threads interleaving --
        # left corrupt JSON; `_collection_load` then returns {} and the NEXT
        # write persists the empty record: cards, money, stats silently reset
        # (09-02 review M3).
        tmblob.write_json(name, data, indent=1, sort_keys=True)
    except tmblob.errors() as e:
        common._say("tm: could not write %s: %s" % (name, e))
        return False
    # The JSON is our record; the SAVE is what the client actually reads. Keep
    # them in step here rather than at each call site, so nothing can record a
    # card without also delivering it. `sync_save=False` is for changes to
    # fields that do NOT live in the save (the `rank` stats block) -- rewriting
    # the save for those would be churn with no delivery in it.
    if sync_save:
        _collection_to_save(member_id, data.get("cards") or [])
    return True


#: HOW MANY OF A PACK THE CLIENT ACTUALLY KEEPS. **One.** Measured off the shop
#: scene's own state table (TM.dll rva 0x179690, eight states; image base
#: 0x04F90000) -- state 0 sends `@Buy=`, state 6 is the acquire:
#:
#:     0x17961D  mov  ecx, [0x52464CC]        the COLLECTION count
#:     0x179623  push 0x524AF70               src = STAGING SLOT 0, not a picked
#:                                            index -- there is no index here
#:     0x179637  call 0xB8180                 copies ONE 16-byte record
#:     0x17963C  inc  word [0x52464CC]        collection count += 1
#:     0x17964E  mov  word [0x52464D0], 0     and the rest of the pack is GONE
#:     0x179657  call 0x106E30(0x15)          only THEN is `@Get=` sent
#:
#: Two things fall out and both matter more than the arithmetic:
#:
#: 1. **`@Get=` is a NOTIFICATION, not a request.** The drain runs BEFORE the
#:    send, unconditionally, in the same basic block. Our silence on it costs
#:    nothing, which is why the client has always quit the shop cleanly. Nothing
#:    is waiting for a reply we have not written.
#: 2. **A pack grants exactly ONE card, and it is the FIRST in our `@Card=`
#:    list.** Recording all of `/S=5` therefore over-counted five-fold -- 18
#:    acquires across both logs had produced a 30-card file for a player who can
#:    hold at most 18. The wire agrees: every `@Buy=/No=0` in the logs is
#:    followed by exactly one `@Get=/No=1`, 18 times out of 19, and `/No=` never
#:    varies, so it is a count and not a chosen slot.
#:
#: WARNING: RETRACTED 2026-08-18 -- SCREEN-VERIFIED, AND IT WAS BACKWARDS. The check
#: named directly above was run: buy the Pauper's Pack, open the collection, and
#: the tester counts **FIVE** cards, not one. The whole pack is kept.
#:
#: So `@Get=/No=1` is ONE ACQUISITION EVENT, not one card, and every inference
#: built on reading it as a card count was wrong -- including "18 acquires can
#: hold at most 18 cards", which is what made the 30-card file look like 5x
#: over-counting when it was simply six packs. The static read of shop state 6
#: (rva 0x17961D, "copies staging slot 0 and zeroes the staging count") described
#: one iteration of a loop, not the whole transfer.
#:
#: WARNING: THE LESSON, because this cost a real debugging round: a static trace of one
#: state said one thing, the tester's screen said another, and the screen was
#: right both times it was consulted. `tetramaster.py` had ALREADY recorded the
#: contradicting observation -- "five cards and a deck at 05:03, gone by the next
#: launch" is in this file, above -- and it was read as a symptom of the loss bug
#: instead of as evidence about pack size.
#:
#: `POL_TM_COLLECTION_RECORD=acquire` restores the one-card behaviour.
#:
#: WARNING: AND "FIVE" WAS THE PAUPER'S PACK'S SIZE, NOT A PACK'S (2026-09-26). The
#: screen check above was a Pauper's Pack, which deals 5, and the number went
#: in as a constant. The Beginner's Pack (/No=1) deals TEN (PackPrm +0x09,
#: pinned in `_selftest_packs`): a player bought one, the client showed ten,
#: and `@Get=` kept the first five -- "ACQUIRED 5 of 10 offered". The whole pack
#: is kept, whatever its size. `POL_TM_PACK_KEEP_MAX=5` restores the old cap.
_ACQUIRED_PER_PACK = None


def _record_on_grant():
    return os.environ.get("POL_TM_COLLECTION_RECORD", "acquire").strip().lower() == "grant"


def _collection_add_cards(member_id, cards):
    """Record cards we just granted. Appends -- a pack is an acquisition, not a
    replacement, and the client's own collection grows the same way."""
    if member_id is None or not cards:
        return
    data = _collection_load(member_id)
    have = data.get("cards")
    data["cards"] = (have if isinstance(have, list) else []) + [list(c) for c in cards]
    if _collection_store(member_id, data):
        common._say("tm: member %s collection += %d card(s), %d total"
              % (member_id, len(cards), len(data["cards"])))


def _collection_remove_card(member_id, card_id, row=None):
    """Remove ONE card from the collection: exact-row match first, then by id.

    The take flow's deduction half: the loser's card goes to the
    winner. `row` is the 8-value wire row when the caller holds one -- an
    exact match removes the precise instance; a collection that has drifted
    from the client's still gives up its first copy of the id. Says loudly
    when nothing matches (nothing is then removed -- a take must never eat a
    different card).
    """
    if member_id is None:
        return False
    data = _collection_load(member_id)
    have = data.get("cards")
    if not isinstance(have, list) or not have:
        common._say("tm: member %s take: no collection to remove card %s from"
             % (member_id, card_id))
        return False
    idx = None
    if row is not None:
        want = [int(v) for v in row.split(b"|")]
        idx = next((i for i, c in enumerate(have) if list(c) == want), None)
    if idx is None:
        idx = next((i for i, c in enumerate(have)
                    if c and int(c[0]) == int(card_id)), None)
    if idx is None:
        common._say("tm: WARNING: member %s take: card %s is not in the collection "
             "(drifted?) -- nothing removed" % (member_id, card_id))
        return False
    gone = have.pop(idx)
    if _collection_store(member_id, data):
        common._say("tm: member %s collection -= card %s (taken; %d left)"
             % (member_id, gone, len(have)))
    return True


def _collection_offer(member_id, cards):
    """Remember the pack we just put on offer, WITHOUT crediting it.

    The client keeps the WHOLE pack (see `_ACQUIRED_PER_PACK`), but only once the
    player goes through with it: `@Get=` is the acquisition event, and a pack the
    player walks away from is never acquired at all. So the offer is held here
    and committed there -- that part of the split was right even though the
    number of cards it committed was not.
    """
    if member_id is None or not cards:
        return
    data = _collection_load(member_id)
    data["pending_pack"] = [list(c) for c in cards]
    _collection_store(member_id, data)


def _collection_commit_offer(member_id):
    """`@Get=` arrived: credit what the client actually kept, and clear the offer."""
    if member_id is None:
        return
    data = _collection_load(member_id)
    pack = data.pop("pending_pack", None)
    if not isinstance(pack, list) or not pack:
        common._say("tm: member %s sent @Get= with no pack on offer -- nothing credited"
              % member_id)
        _collection_store(member_id, data)
        return
    cap = common._env_int("POL_TM_PACK_KEEP_MAX", 0) or _ACQUIRED_PER_PACK
    kept = [list(c) for c in (pack[:cap] if cap else pack)]
    have = data.get("cards")
    data["cards"] = (have if isinstance(have, list) else []) + kept
    if _collection_store(member_id, data):
        common._say("tm: member %s ACQUIRED %d of %d offered, %d total"
              % (member_id, len(kept), len(pack), len(data["cards"])))


def _merge_deck_stats_into(member_id, data, cmd):
    """Merge the CURRENT stats of a `@Decks=` report's cards into `data["cards"]`.

    Mutates `data` in place; returns True if any stored card changed. The caller
    owns the single load/store, so this cannot race its own read-modify-write.

    WARNING: THE UPGRADE-PERSISTENCE FIX. A card's combat stats (attack / type /
    physical-def / magic-def) and its arrows upgrade through play. The client
    RENDERS and RE-REPORTS them in `@Decks=` (msgid 0x22, builder 0x109D8D,
    emitted verbatim from the deck's own 16-byte records) -- but it never
    AUTHORS them via a formula: the stat bytes are server-supplied and the
    client only recomputes the DERIVED power/display from them (disassembly
    2026-09-02: writers 0x105E85, deriver 0xB9410/0xB9520). The save the client
    reloads its whole collection from is authored from `data["cards"]`
    (`_collection_to_save`), so unless the reported stats are merged back, every
    upgrade is overwritten by the stored pre-upgrade row on the next save sync --
    the measured "the boost reverts on relaunch".

    WARNING: NO UPGRADE RULE IS INVENTED: we persist exactly what the client reports.
    `@Decks=` carries only the <=5 DECK cards, so only those rows are touched;
    newly won cards still arrive by the take / `@Card=` path. There is NO
    per-instance id on the wire -- a card is identified only by (id + stats) --
    so a report row is matched to a stored row by ID, preferring a stored row
    that already carries the reported stats (an idempotent re-report) over
    upgrading a same-id sibling. When a player holds duplicates of an id this
    cannot be perfect (the client itself cannot tell the copies apart), but the
    multiset of (id -> stats) is kept as close as the wire allows.

    WARNING: FIELD ORDER DIFFERS between the two shapes -- never copy positionally:
        `@Decks=` /D= (7):  id | atk | type | pdef | mdef | arrows | flag
        stored row    (8):  id | atk | type | pdef | mdef | POWER  | arrows | flag
    the stored `power` (idx 5) is a value the client recomputes on load
    (0xB9520), so it is left untouched; arrows and flag are REMAPPED.

    WARNING: DEFAULT OFF as of 2026-09-02, and it must stay off while card GROWTH is
    on. This merge assumed the CLIENT is authoritative for card stats (persist
    what it reports). The decomp settled the opposite: the SERVER owns the stats
    (it rolls and grows them; the client only renders absolute records). So a
    `@Decks=` report is just the client echoing stats WE gave it -- taking them
    back is a no-op at best, and once `_grow_used_cards` has raised a card the
    client has not reloaded yet, the client reports the OLD (lower) stats and
    this merge would UNDO the growth. `POL_TM_DECK_STAT_SYNC=1` re-enables it for
    an A/B, but do not run it together with `POL_TM_CARD_GROW`.
    """
    if not common._env_int("POL_TM_DECK_STAT_SYNC", 0):
        return False
    rows = cardshop._parse_card_list(cmd)          # a name-only /C=0 report parses to []
    if not rows:
        return False
    have = data.get("cards")
    have = [list(c) for c in have] if isinstance(have, list) else []
    consumed, changed, unknown = set(), [], []
    for r in rows:
        if len(r) < 6:                    # need at least id + 4 stats + arrows
            continue
        cid, stats, arrows = r[0], list(r[1:5]), r[5]
        # An exact same-(id, stats, arrows) stored row is this report row already
        # persisted -- consume it and change nothing (idempotent re-report).
        exact = next((i for i, row in enumerate(have)
                      if i not in consumed and row and row[0] == cid
                      and list(row[1:5]) == stats
                      and len(row) > 6 and row[6] == arrows), None)
        if exact is not None:
            consumed.add(exact)
            continue
        idx = next((i for i, row in enumerate(have)
                    if i not in consumed and row and row[0] == cid), None)
        if idx is None:
            unknown.append(cid)
            continue
        row = have[idx]
        while len(row) < 8:               # pad a short stored row out to 8 fields
            row.append(0)
        before = list(row)
        row[1], row[2], row[3], row[4] = stats     # the combat stats upgrade
        row[6] = arrows                            # arrows are per-instance too
        # row[5] (power, derived) and row[7] (flag, transient) left as stored.
        consumed.add(idx)
        if row != before:
            changed.append((cid, before[1:5] + [before[6]], stats + [arrows]))
    if changed:
        data["cards"] = have
        common._say("tm: member %s deck stats synced -- %d card(s) changed: %s"
             % (member_id, len(changed),
                "; ".join("#%d %s->%s" % (c, b, a) for c, b, a in changed)))
    if unknown:
        common._say("tm: member %s @Decks= reported %d id(s) not in the collection: "
             "%s -- deck/collection drift, stats not merged"
             % (member_id, len(unknown), unknown))
    return bool(changed)


#: WARNING: THE TWO 17-BYTE NAME SLOTS ARE THE DECK SET NAMES (2026-09-07, a tester's
#: screen). Save +0x104 renders as the card screen's FIRST deck tab and +0x115
#: as the second. The client reports a name it types as `@Decks=/C=0@DN<n>=
#: /S=<hex>` (stored under `deck_names`, never answered) and expects it back
#: in the save, which is why the tabs were blank (we wrote zeros) -- and why
#: they read the champion's name the Sunday the champion feature stamped the week's
#: #1 into +0x104 believing it the shop's "1st pack name" slot (RETRACTED: see
#: `SAVE_OFF_CHAMPION`). `POL_TM_DECK_NAMES=0` stops authoring them;
#: `POL_TM_DECK_NAME_BASE` is the `@DN<n>` index of the FIRST tab (default 1:
#: every report ever seen was `@DN1=`, and a player naming the first tab is the
#: likelier event -- unmeasured, flip it if the name lands on the wrong tab).
SAVE_OFF_DECK_NAMES = (0x104, 0x115)
SAVE_DECK_NAME_WIDTH = 17           #: 16 bytes + NUL


#: WARNING: THE 8TH CARD VALUE IS THE DECK SLOT (2026-09-07, a tester's deck screen).
#: Save record byte 8 (`tmsave` copy loop 0x10136D: rec+8 -> memory row +0xE;
#: the wire parser checks it `< 0x32` and indexes a word table) is the card's
#: position across BOTH deck sets: 0..24 = set 1 (5 decks x 5), 25..49 = set 2,
#: 255 = not in any deck. Measured: every card the tester "never put there"
#: sat at exactly the value we had been writing -- CardPrm field 6 (0, 1, 2, 3,
#: 6, 7, 12, 18, 21, 24, 29 on one tester's cards), which is NOT a slot. The client
#: reports a deck edit as `@Decks=/C=n@<i>=/D=id|atk|type|pdef|mdef|arrows|SLOT`
#: (the 7th value IS this byte, absolute) and reloads decks from the save, so the
#: server must (a) grant new cards with 255 and (b) write the reported slot
#: back. `POL_TM_DECK_SLOTS=0` restores the old column (the scatter).
#:
#: WARNING: UNMEASURED: whether the report's header `sh` names the deck (the name form
#: sends sh=1 for both tabs, so probably not). A report is per DECK (<=5 cards);
#: the decks it covers are inferred from its slots, and an EMPTY card report
#: (a deck cleared) therefore cannot say which deck -- logged, not applied.
DECK_SLOT_COUNT = 50
DECK_SLOT_NONE = 255


def _deck_slots_on():
    return common._env_int("POL_TM_DECK_SLOTS", 1)


def _new_card_slot(legacy):
    """The 8th value of a card we GRANT: no deck slot (255), or the old column."""
    return DECK_SLOT_NONE if _deck_slots_on() else legacy


def _note_deck_slots(data, rows, sh=None):
    """Merge one card-form `@Decks=` report into `data["deck_slots"]`
    ({"<slot>": [id, atk, type, pdef, mdef, arrows]}). Returns (placed, decks)."""
    slots = data.get("deck_slots")
    if not isinstance(slots, dict):
        # FIRST MAP for this member: seed it from the verbatim `deck` report
        # kept before slots were understood, or the card it placed (a tester's Al
        # Bhed Primer at slot 4) is dropped by the first real edit -- which
        # is what happened live at 03:05Z.
        slots = {}
        text = data.get("deck")
        if text and rows is not None:
            data["deck_slots"] = {}
            _note_deck_slots(data, cardshop._parse_card_list(
                str(text).encode("latin1", "replace")), None)
            slots = data.get("deck_slots") or {}
    slots = {str(k): list(v) for k, v in slots.items()}
    placed, removed = {}, []
    for r in rows:
        if len(r) < 7:
            continue
        try:
            slot = int(r[6])
        except (TypeError, ValueError):
            continue
        card = [int(v) for v in r[:6]]
        if slot == DECK_SLOT_NONE:
            # VERIFIED: A REMOVAL, measured 2026-09-07T03:17:51Z: clearing two cards
            # sent `/C=2@0=...|255@1=...|255` -- the cards, slot 255. Drop
            # every map entry naming that card (id + stats, else id).
            hit = [k for k, v in slots.items() if v and v[0] == card[0]
                   and list(v[1:6]) == card[1:6]]                 or [k for k, v in slots.items() if v and v[0] == card[0]]
            # one row takes ONE copy out (identical copies are separate cards)
            hit = hit[:1]
            for k in hit:
                slots.pop(k, None)
            removed.append((card[0], hit))
            continue
        if not 0 <= slot < DECK_SLOT_COUNT:
            continue
        placed[slot] = card
    # WARNING: A REPORT NEVER CLEARS A DECK. Measured 03:05Z/03:06Z: each placement
    # arrives ALONE (`/C=1`), so "a report replaces the deck it covers" would
    # drop the first card when a second joins the same deck. A placement
    # ADDS or MOVES: the card's previous entry (id + stats, else id) goes,
    # then the reported slot is set; a removal is a 255 row (above).
    decks = {s // 5 for s in placed}

    # WARNING: IDENTICAL COPIES ARE DIFFERENT CARDS (2026-09-25):
    # a member holding three identical #148s put all three in one deck and only
    # the last stayed -- each placement "moved" the previous identical entry.
    # A placement is a MOVE only while the member owns no more copies of that
    # exact card than the map already places; otherwise it is another copy.
    def _owned(v):
        n = 0
        for c in data.get("cards") or []:
            try:
                if (int(c[0]) == v[0] and [int(x) for x in c[1:5]] == list(v[1:5])
                        and int(c[6]) == v[5]):
                    n += 1
            except (TypeError, ValueError, IndexError):
                continue
        return n

    for s, v in placed.items():
        same = [k for k, w in slots.items() if w and w[0] == v[0]
                and list(w[1:6]) == v[1:6] and k != str(s)]
        if same:
            prev = same[:1] if len(same) >= max(1, _owned(v)) else []
        else:
            # same id, other stats: a card that changed (drift, an upgrade)
            # is replaced -- but only an entry whose exact card the member no
            # longer holds; same-id cards with other arrows are other cards
            prev = [k for k, w in slots.items() if w and w[0] == v[0]
                    and k != str(s) and not _owned(w)][:1]
        for k in prev:
            slots.pop(k, None)
        slots[str(s)] = v
    if removed:
        common._say("tm:   ...deck removal: %s" % ", ".join(
            "card %d out of slot(s) %s" % (c, ",".join(h) if h else "(none held)")
            for c, h in removed))
    elif not placed and rows is not None:
        common._say("tm:   ...a card-form @Decks= report with NO cards (sh=%r) -- nothing "
             "to place or remove" % (sh,))
    data["deck_slots"] = slots
    return placed, decks


def _deck_slot_bytes(data, cards):
    """Byte 8 for every stored card row, in order: its deck slot, else 255.

    A slot names a card by (id, stats); a stored row is matched by id,
    preferring one whose atk/pdef/mdef/arrows agree, each row used once.
    Falls back to the verbatim `deck` report when no slot map exists yet."""
    slots = data.get("deck_slots")
    if not isinstance(slots, dict):
        slots = {}
        text = data.get("deck")
        if text:
            _note_deck_slots(data, cardshop._parse_card_list(
                str(text).encode("latin1", "replace")), None)
            slots = data.get("deck_slots") or {}
    out = [DECK_SLOT_NONE] * len(cards)
    used = set()
    for s_str, v in sorted(slots.items(), key=lambda kv: int(kv[0])):
        try:
            slot = int(s_str)
        except ValueError:
            continue
        cid = int(v[0]) if v else -1
        cand = [i for i, row in enumerate(cards)
                if i not in used and row and int(row[0]) == cid]
        if not cand:
            continue
        best = next((i for i in cand if len(cards[i]) > 6 and len(v) >= 6
                     and [int(cards[i][1]), int(cards[i][3]), int(cards[i][4]),
                          int(cards[i][6])] == [v[1], v[3], v[4], v[5]]),
                    cand[0])
        used.add(best)
        out[best] = slot
    return out


def _deck_names(member_id):
    """{n: name bytes} from the member's stored `@DN<n>=/S=<hex>` report."""
    text = (_collection_load(member_id) or {}).get("deck_names") or ""
    out = {}
    for m in re.finditer(r"@DN(\d+)=/S=([0-9A-Fa-f]*)", str(text)):
        try:
            out[int(m.group(1))] = bytes.fromhex(m.group(2))
        except ValueError:
            pass
    return out


def _deck_name_fields(member_id):
    """{offset: (17, name)} for BOTH deck-set slots -- an unnamed tab gets
    zeros, so a stale string (the champion stamp) is cleared, not kept."""
    if member_id is None or not common._env_int("POL_TM_DECK_NAMES", 1):
        return {}
    base = common._env_int("POL_TM_DECK_NAME_BASE", 1)
    names = _deck_names(member_id)
    return {off: (SAVE_DECK_NAME_WIDTH,
                  (names.get(base + i) or b"")[:SAVE_DECK_NAME_WIDTH - 1])
            for i, off in enumerate(SAVE_OFF_DECK_NAMES)}


def _collection_set_deck(member_id, cmd, sh=None):
    """Record a `@Decks=` report: the verbatim body AND the cards' current stats.

    The raw body is kept as before (the client's own words for its own state;
    the two forms -- `/C=<n>` with `@<i>=/D=` cards, and `/C=0` with
    `@DN<n>=/S=<hex ascii>` names -- are stored under separate keys so a name
    report cannot erase the card list). NEW: the card form's stats are also
    merged into the collection so upgrades survive the save rewrite -- see
    `_merge_deck_stats_into`. One load/store covers both.
    """
    if member_id is None:
        return
    data = _collection_load(member_id)
    key = "deck_names" if b"/S=" in cmd and b"/D=" not in cmd else "deck"
    text = cmd.decode("latin1")
    slot_changed = False
    if key == "deck" and _deck_slots_on():
        _before_slots = dict(data.get("deck_slots") or {})
        _placed, _decks = _note_deck_slots(data, cardshop._parse_card_list(cmd), sh)
        slot_changed = (data.get("deck_slots") or {}) != _before_slots
        if _placed:
            common._say("tm: member %s deck %s <- %s"
                 % (member_id, ",".join(str(d + 1) for d in sorted(_decks)),
                    ", ".join("slot %d: card %d" % (s, v[0])
                              for s, v in sorted(_placed.items()))))
    if key == "deck_names":
        # WARNING: A NAME REPORT CARRIES ONLY THE TAB JUST EDITED (measured
        # 2026-09-07T02:42Z: `@DN1=/S=TEST` then, 5 s later, `@DN2=/S=RAWR`
        # alone -- and replacing the stored text lost tab 1). MERGE per
        # index, rebuilt in the client's own form so `_deck_names` reads it.
        merged = {}
        for src in (data.get("deck_names") or "", text):
            for m in re.finditer(r"@DN(\d+)=/S=([0-9A-Fa-f]*)", str(src)):
                merged[int(m.group(1))] = m.group(2)
        text = "@Decks=/C=0" + "".join("@DN%d=/S=%s" % (n, hx)
                                       for n, hx in sorted(merged.items()))
    deck_changed = data.get(key) != text
    if deck_changed:
        data[key] = text
    # Merge the deck cards' current stats into `cards` (idempotent, so it is safe
    # to run even when the verbatim body repeats). Name-only reports carry no
    # `/D=` cards and merge to nothing.
    stat_changed = (_merge_deck_stats_into(member_id, data, cmd)
                    if key == "deck" else False)
    if not (deck_changed or stat_changed or slot_changed):
        return                                  # the client repeats itself
    if _collection_store(member_id, data):
        if deck_changed:
            common._say("tm: member %s %s <- %s" % (member_id, key, text[:80]))


def _card_restore_enabled():
    """`restore=1` in the teach control file, else POL_TM_CARD_RESTORE.

    The control file is re-read per line and bind-mounted, so this experiment
    costs no restart and no session -- which matters, because whether an
    unsolicited `@Card=` reaches the collection depends on client-side timing we
    cannot see from here and will want to toggle mid-session. It is the same
    preference the class-L/class-R work landed on: express an experiment as a
    spec key, not a rebuild.

    WHY IT MIGHT NOT WORK, so the result is readable either way. `@Card=` is
    parsed at TM.dll 0x105E85 into a STAGING array (`0x524AF70`, slot
    `(count + 0x4B9)*16`, count at `0x52464D0`); the COLLECTION at `0x5246F10`
    is only filled by a separate arm at `0x16628`, which drains staging and
    zeroes it. So a push lands in staging immediately but shows up as "Owned"
    only once that arm runs. If Owned does not move, the answer is not a
    different `@Card=` -- it is the card service (`@CardReq=`/`@CardEnt=`).

    WARNING: AND THE PUSH CAN BE MIS-MATCHED. The parser gates on three equalities
    against the shop scene: sender id lo/hi (`[esi+0x28]`/`[esi+0x2c]`) and the
    shop number byte (`[esi+0x30]`). Ours satisfy all three, which also means a
    push left sitting in the queue could be picked up by the NEXT real `@Buy=`
    instead of its own reply. Turn it off once the question is answered.
    """
    opt = opener._teach_opts().get("restore")
    if opt is not None:
        return opt not in ("0", "", "off", "no")
    return os.environ.get("POL_TM_CARD_RESTORE", "0") == "1"


def _collection_cards(member_id):
    data = _collection_load(member_id)
    cards = data.get("cards")
    return [c for c in cards if isinstance(c, list) and len(c) == 8] \
        if isinstance(cards, list) else []
