"""The auction house: listing store, bids, notices, the expiry sweep, the class-A replies and Check
Out files.
"""
import os
import re
import struct
import time
import tetramaster
import tmauction
import tmroom
from . import corenames, fetches


def _auction_store_file():
    """This member's exhibit list -- the file the lobby band already serves.

    ONE FILE IS THE WHOLE STORE. `_resource_blob` hands these bytes to `3:0` and
    `_fetch_len` measures the same file, so the `<SN>` computed here cannot
    drift from what the client is given. Two different CONTAINERS answer those
    two bands, so a shared file is the only thing that can keep them in step --
    see the POL-5135 note on `_AUCTION_LIST_PATHS`.
    """
    return corenames._resource_file(fetches._EXHIBIT_LIST_PATH)


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
    return os.path.join(corenames.RESOURCE_DIR, "auction-%d.bids.bin" % int(auction_id))


def _auction_bids(auction_id):
    """Auction `n`'s bid rows, sorted, or b"" if nobody has bid."""
    try:
        with open(_auction_bids_file(auction_id), "rb") as f:
            blob = f.read()
    except OSError:
        return b""
    if len(blob) % tmauction.BID_REC:
        corenames.log("lobby", f"  auction: bids for {auction_id} are {len(blob)}B, not a "
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
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", fetches._EXHIBIT_LIST_PATH) + ".bin"
    for fn in sorted(glob.glob(os.path.join(corenames.RESOURCE_DIR, "*." + suffix))):
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
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", fetches._EXHIBIT_LIST_PATH) + ".bin"
    out = bytearray()
    for fn in sorted(glob.glob(os.path.join(corenames.RESOURCE_DIR, "*." + suffix))):
        try:
            with open(fn, "rb") as f:
                blob = f.read()
        except OSError:
            continue
        # A short tail is a torn or hand-edited file. Take the whole records and
        # say so rather than serving a fragment: `_fetch_len` measures what we
        # write, so a partial record would be a length the reader cannot use.
        if len(blob) % tmauction.REC:
            corenames.log("lobby", f"  auction: {os.path.basename(fn)} is {len(blob)}B, "
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
    listing had settled, the store was empty, Fox's new listing minted as
    auction 1 and inherited two stale test bids from an earlier settled listing,
    rendering "High bid 0 by PCTest (2 bids)" on a listing nobody had bid on --
    and at `<NC>` the sweep would have SOLD the card to PCTest for 100.
    """
    import glob
    used = tmauction.max_auction_id(_auction_all_rows())
    for fn in glob.glob(os.path.join(corenames.RESOURCE_DIR, "auction-*.bids.bin")):
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
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", fetches._EXHIBIT_LIST_PATH) + ".bin"
    out = bytearray()
    for fn in sorted(glob.glob(os.path.join(corenames.RESOURCE_DIR, "*." + suffix))):
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
    if corenames.accounts is None or which not in _AUCTION_NOTICES:
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
        corenames.log("authserv", f"  auction notice: {which!r} recipient "
                        f"{recipient_member!r} is not a member id -- skipped")
        return
    try:
        db = corenames.accounts.connect()
        try:
            hid = corenames._member_primary_handle(db, member)
        finally:
            db.close()
        if not hid:
            corenames.log("authserv", f"  auction notice: member {member} has no handle "
                            f"-- {which!r} notice not sent")
            return
        path = corenames._mail_mint(from_name, _TM_AUCTION_SENDER_GUID,
                          corenames.accounts.handle_guid(hid), subject, body)
        if path:
            corenames.log("authserv", f"  auction notice: {which!r} POL Message minted to "
                            f"member {member} (handle {hid}) from {from_name!r}")
    except Exception as e:                                    # pragma: no cover
        corenames.log("authserv", f"  auction notice: {which!r} to member {member} FAILED "
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
    suffix = re.sub(r"[^A-Za-z0-9._-]", "_", fetches._EXHIBIT_LIST_PATH) + ".bin"
    relisted = stuck_sold = stuck_unsold = sold = unsold = 0
    for fn in sorted(glob.glob(os.path.join(corenames.RESOURCE_DIR, "*." + suffix))):
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
                    corenames.log("authserv", f"  auction: {rec['ai']} winner "
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
                    corenames.log("authserv", f"  auction: {rec['ai']} sold but the "
                                    f"credit failed ({e}) -- holding the "
                                    f"listing rather than losing the card")
                    stuck_sold += 1
                    continue
                drop.append(i)
                changed = True
                sold += 1
                corenames.log("authserv", f"  auction: {rec['ai']} SOLD to "
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
                        corenames.log("authserv", f"  auction: {rec['ai']} -- outbid "
                                        f"notice to {len(_losers)} loser(s): "
                                        f"{[n for n,_ in _losers.values()]}")
                continue
            if rec["am"] > 0:
                row = blob[i * tmauction.REC:(i + 1) * tmauction.REC]
                out[i * tmauction.REC:(i + 1) * tmauction.REC] =                     tmauction.relist(row, now)
                changed = True
                relisted += 1
                corenames.log("authserv", f"  auction: {rec['ai']} did not sell -- "
                                f"relisting, {rec['am'] - 1} relist(s) left")
            else:
                # UNSOLD and out of relists -- the card goes back to its owner.
                seller = os.path.basename(fn).split(".", 1)[0]
                try:
                    tmauction.add_pending(seller, card=rec["ii"])
                except OSError as e:
                    corenames.log("authserv", f"  auction: {rec['ai']} unsold but the "
                                    f"return failed ({e}) -- holding")
                    stuck_unsold += 1
                    continue
                drop.append(i)
                changed = True
                unsold += 1
                corenames.log("authserv", f"  auction: {rec['ai']} UNSOLD -- card queued "
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
                corenames.log("authserv", f"  auction: cannot write the relist for "
                                f"{os.path.basename(fn)} ({e}) -- left as is")
    if stuck_sold or stuck_unsold:
        # One summary line, not one per listing per request: this fires on every
        # auction request and would otherwise bury the log.
        corenames.log("authserv", f"  auction: {stuck_sold} sold and {stuck_unsold} "
                        f"out-of-relist listing(s) are EXPIRED AND UNDELIVERED "
                        f"-- settlement needs the DI/BI format, so they are "
                        f"held, not dropped")


def _auction_take_listed_card(member, ii):
    """Remove the card a `<ER>` lists from the seller's stored collection.

    `ii` is the 16-value `<II>`; its card is `tmauction.card_row_from_ii` =
    [id, atk, type, pdef, mdef, power, arrows, flag]. The stored row's 8th value
    is the deck slot, so the match is on the first SEVEN values, then on the
    first copy of the id with the same four stats, then on the id alone (a
    collection that drifted from the client still gives up a copy). Returns
    the removed row, or None when the member holds no copy at all.
    """
    want = tmauction.card_row_from_ii(ii)
    data = tetramaster._collection_load(member)
    have = data.get("cards")
    if not isinstance(have, list) or not have:
        return None
    tests = (lambda c: list(c)[:7] == want[:7],
             lambda c: list(c)[:5] == want[:5],
             lambda c: int(c[0]) == int(want[0]))
    idx = None
    for t in tests:
        idx = next((i for i, c in enumerate(have) if c and t(c)), None)
        if idx is not None:
            break
    if idx is None:
        return None
    gone = list(have.pop(idx))
    tetramaster._collection_store(member, data)
    corenames.log("authserv", f"  auction: member {member} collection -= card {gone} "
                    f"(listed; {len(have)} left)")
    return gone


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
    if tmauction is None or corenames.polpro is None or not tmauction.enabled():
        return None, False
    groups = corenames.polpro.parse(payload)
    if not groups:
        return None, False
    lead = groups[0][0]
    member = corenames._session_get("member_id")
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
                corenames.log("authserv", f"  auction: <MP> with {groups[1][1]!r} is not a "
                                f"(lo,hi) pair -- serving the UNFILTERED list "
                                f"rather than guessing at the band")
        n = tmauction.count(rows)
        try:
            _write_resource(corenames._resource_file(_AUCTION_BROWSE_PATH), rows)
        except OSError as e:
            corenames.log("authserv", f"  auction: cannot stage the browse list ({e}) -- "
                            f"declining so the client gets <SF>, not a count "
                            f"it cannot fetch")
            return None, False
        corenames.log("authserv", f"  auction: browse{'' if band is None else ' %d..%d' % band}"
                        f" -> {n} listing(s) ({len(rows)}B) for member {member}")
        return corenames.polpro.build([("SS", [_AUCTION_BROWSE_PATH]),
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
            _write_resource(corenames._resource_file("U/g/TM0_BIDLIST"), rows)
        except OSError as e:
            corenames.log("authserv", f"  auction: cannot stage the bid-on list ({e}) -- "
                            f"declining, the static entry answers <SN>(0)")
            return None, False
        corenames.log("authserv", f"  auction: member {member} ({me!r}) has bid on {n} "
                        f"auction(s) ({len(rows)}B)")
        return corenames.polpro.build([("SS", ["U/g/TM0_BIDLIST"]), ("SN", [str(n)])]), True

    if lead == "SI" and len(groups) > 1 and groups[1][0] == "IO":
        rows = _auction_rows()
        n = tmauction.count(rows)
        corenames.log("authserv", f"  auction: member {member} has {n} listing(s) "
                        f"({len(rows)}B stored)")
        return corenames.polpro.build([("SS", [fetches._EXHIBIT_LIST_PATH]), ("SN", [str(n)])]), True

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
            corenames.log("authserv", f"  auction: <BH> with no usable <AI> "
                            f"({vals.get('AI')!r}) -- declining, <HF> answers")
            return None, False
        bids = _auction_bids(ai)
        n = tmauction.bid_count(bids)
        try:
            _write_resource(corenames._resource_file(_AUCTION_BIDHIST_PATH), bids)
        except OSError as e:
            corenames.log("authserv", f"  auction: cannot stage the bid history ({e}) -- "
                            f"declining so the client gets <HF>, not a count "
                            f"it cannot fetch")
            return None, False
        corenames.log("authserv", f"  auction: bid history for auction {ai} -> {n} bid(s) "
                        f"({len(bids)}B) for member {member}")
        # WARNING: `<SN>`(0) IS THE HONEST ANSWER AND MAY NOT BE A USABLE ONE. The
        # ranking list's `<LN>`(0) made its scene tear itself down before it
        # ever read the file, and nothing yet says this one behaves better --
        # an auction with no bids is exactly the case a player hits first. If
        # the bid form still will not open on zero, that is the same shape and
        # the fix is the same: serve one zero row and say so.
        return corenames.polpro.build([("HS", [_AUCTION_BIDHIST_PATH]),
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
            corenames.log("authserv", f"  auction: <BR> with unusable <AI>/<BM> "
                            f"({vals.get('AI')!r}/{vals.get('BM')!r}) -- "
                            f"declining, <BF> answers")
            return None, False
        found = _auction_find(ai)
        if found is None:
            corenames.log("authserv", f"  auction: <BR> for auction {ai}, which no store "
                            f"holds -- declining, <BF> answers")
            return None, False
        fn, blob, idx = found
        rec = tmauction.read_record(blob, idx)
        floor = tmauction.min_bid(rec)
        if bm < floor:
            # The CLIENT computes this same floor and pre-fills it, so a bid
            # under it means we and it disagree about the listing -- refuse
            # rather than record a number the seller's screen will contradict.
            corenames.log("authserv", f"  auction: <BR> {bm} on auction {ai} is under the "
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
            corenames.log("authserv", f"  auction: cannot record the bid ({e}) -- "
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
                    corenames.log("authserv", f"  auction {ai}: {_outbid['name']!r} "
                                    f"(member {_pm}) OUTBID -- {_outbid['amount']}"
                                    f" gil queued as a Bid Refund")
                else:
                    corenames.log("authserv", f"  auction {ai}: previous high bidder "
                                    f"{_outbid['name']!r} has no resolvable "
                                    f"member id -- {_outbid['amount']} gil refund "
                                    f"NOT queued; recover by hand")
        except Exception as _e:                              # pragma: no cover
            corenames.log("authserv", f"  auction {ai}: bid recorded but the escrow move "
                            f"FAILED ({_e}) -- the bid stands, gil accounting "
                            f"needs a hand")
        d = tmauction.read_record(updated)
        corenames.log("authserv", f"  auction: member {member} ({bidder!r}) bid {bm} on "
                        f"auction {ai}; <CM> now {d['cm']}")
        return corenames.polpro.build(tmauction.bs_groups(d)), True

    if lead == "ER":
        vals = {t: v for t, v in groups}
        ii = vals.get("II")
        if not ii or len(ii) != 16:
            # NOT OURS TO ANSWER. A malformed <II> means we would be storing a
            # card we cannot hand back intact, and <II> must survive verbatim.
            corenames.log("authserv", f"  auction: <ER> with a {len(ii or [])}-value <II> "
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
            corenames.log("authserv", f"  auction: <ER> fields unparsable ({e}) -- "
                            f"declining, the static <EF> answers")
            return None, False
        # WARNING: THE LISTED CARD LEAVES THE SELLER'S COLLECTION HERE (2026-09-25).
        # It never did: a SOLD auction gave the buyer the card at Check Out
        # while the seller kept theirs, so every sale duplicated a card
        # (measured: 9 cards, list one, self-bid, settle, collect -> 10). The
        # listing now holds the card; settlement already hands it to the
        # winner (cards-B) or back to the seller (cards-A, "Returned Card"). A
        # seller who does not own the card is refused with <EF>.
        # POL_TM_AUCTION_TAKE_CARD=0 restores the old behaviour.
        taken = None
        if os.environ.get("POL_TM_AUCTION_TAKE_CARD", "1") == "1"                 and member is not None:
            taken = _auction_take_listed_card(member, [int(x) for x in ii])
            if taken is None:
                corenames.log("authserv", f"  auction: member {member} listed card "
                                f"{ii[0]} that is not in their collection -- "
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
            corenames.log("authserv", f"  auction: cannot store the listing ({e}) -- "
                            f"declining so the client gets <EF>, not a lie")
            if taken is not None:
                tetramaster._collection_add_cards(member, [taken])
            return None, False
        d = tmauction.read_record(rec)
        corenames.log("authserv", f"  auction: member {member} listed card {d['ii'][0]} "
                        f"as auction {d['ai']} for {d['sp']} "
                        f"(ends {d['nc']}); {tmauction.count(blob)} listing(s)")
        return corenames.polpro.build(tmauction.es_groups(d)), True

    return None, False


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
        corenames.log("lobby", f"  auction: cannot compute band counts ({e}) -- serving "
                     f"the blob unpatched")
        return data
    out = bytearray(data)
    need = 4 * len(counts)
    if len(out) < need:
        out.extend(b"\x00" * (need - len(out)))
    for i, c in enumerate(counts):
        struct.pack_into("<I", out, 4 * i, min(int(c), 0xFFFFFFFF))
    if any(counts):
        corenames.log("lobby", f"  auction: Price List counts {counts} "
                     f"(All/Cheap/Affordable/Expensive/Exorbitant)")
    return bytes(out)


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
    if corenames._resource_stored(path, subject):
        return False
    _co_member = corenames._session_get("member_id")
    try:
        _co_pend = tmauction.pending(_co_member)
    except Exception:
        _co_pend = None
    if (_co_pend is not None and not _co_pend.get("money")
            and not _co_pend.get("cards")
            and not _co_pend.get("won")
            and not _co_pend.get("refund")):
        corenames.log("lobby", f"  3:0 {path!r}: member {_co_member} has NOTHING "
                     f"pending -- replying -650 File Not Found (type "
                     f"0x7e) so Check Out skips the stream entirely "
                     f"(the loader's bit stays clear)")
        return True
    return False
