"""The card trade (0xA2): the trade board entry, offers, selections and the swap between two
collections.
"""
import os
import re
import time
from . import collection, common, matchstart, protocol, pushqueue, ruleset


#: push_key -> {"t": accept stamp, "served": count}. Armed by an accepted
#: trade (`note_trade_accept`, called from the responders relay), consumed by
#: the `@Init=` arm. Auth-band only, like _EVENTSHOP_PENDING, so a module
#: dict is safe.
_TRADE_PENDING = {}


def note_trade_accept(*members):
    """Both sides of an accepted `@TrAns=`: serve the trade board ENTRY.

    Each entry is `(member_id, partner_member_id, partner_login_nick)` -- a
    bare member_id records the pair but authors nothing (selftest / callers
    without the pairing).

    VERIFIED: LIVE-CORRECTED 2026-08-22 by a tester's dump `pol (32).DMP` (sqMg recv ring).
    The from-the-MEMBERS-LIST trade does NOT use the
    @TrID/@TrEntAns/@Init(0xA2,24) handshake: that dump shows the accepter
    RECEIVED our @TrEntAns(0xA1) and @Init(0xA2,24) -- framed correctly from the
    partner, delivery/relay all working -- and IGNORED both, returning to the
    room heartbeat, never sending @TrID. So TM.dll scene 0x15590 (the @TrID
    handshake, which needs a 'D' table at state 0) is the SEATED/at-a-table
    trade, a different feature. The static-RE claim that the pushes were
    "guid-dropped" is also falsified -- they land fine.

    What the members-list accepter DOES poll after @TrAns=/Ans=1 is **(0xA2,1)**
    (measured live, b51b3422), and the 20:22Z run -- `(0xA2,1) @EventGameInit`
    + exactly ONE `(0xA2,26)` @Card list -- brought the board up polling 25/26
    without crashing. The crash (`__fastfail`, minidump-confirmed) was a stack
    buffer overflow in the client's sqMg formatter on the SECOND `(0xA2,26)`
    record (the /E=0 terminator or /P=1 partner list). So: serve (0xA2,1) + ONE
    (0xA2,26), never a second 26 (that is why responders relays 25/27 but NOT
    26). POL_TM_TRADE_SERVE=0 disables.

    WARNING: **DEFAULT FLIPPED TO OFF, 2026-08-24.** This authored serve is the
    artefact of the static model that has now been falsified twice over -- the
    game band (channel-2 watchdog flat at 0 across three live runs) and the seat
    ('D' row; seating both players changed nothing). Live, its records land
    AFTER the client has already torn the trade down: measured 12:08:13.769Z,
    the accepter's order is `@Tr=` filed -> gate set -> `@TrAns` sent ->
    RecvCLEAR -> CalcPlayerData -> **gate zeroed** -> only then our
    @EventGameInit + both @Card lists arrive. Nothing is left to receive them,
    and the static read says the board wants each client's OWN relayed list
    anyway, never a server-authored one.
    So the accept path now serves the VS-shaped go flag instead (responders.py,
    `POL_TM_TRADE_GO`), which is the one push ever measured to ADVANCE the
    scene. `POL_TM_TRADE_SERVE=1` restores this for an A/B; the two are
    deliberately separable so a run only ever changes one of them.
    """
    now = time.time()
    for entry in members:
        entry = entry if isinstance(entry, tuple) else (entry,)
        m, pm, partner, role = (tuple(entry) + (None, None, "acc"))[:4]
        k = pushqueue._push_key(m)
        if k is None:
            continue
        # PARTIAL: RE-SERVE STATE (2026-08-24, measured with [tmlog] narration): the
        # client's @TrAns consumer runs a RecvCLEAR that PURGES (0xA2,*) from
        # the store -- probe-watched eating our pre-delivered @EventGameInit
        # (lookup #9951, wildcard FOUND -> deleted). The purge follows @TrAns
        # CONSUMPTION, which trails delivery by an unpredictable few seconds,
        # so no fixed serve delay can win the race. Instead the serve REPEATS
        # on the member's next few @Pongs (`_trade_reserve_tick`): copies that
        # land pre-purge die harmlessly, the first post-purge copy sticks.
        # The board polls its entry gate for minutes, so a late copy is fine.
        _TRADE_PENDING[k] = {"t": now, "served": 0, "last": now,
                             "left": common._env_int("POL_TM_TRADE_RESERVE_N", 4),
                             "pm": pm, "partner": partner, "role": role}
        if partner is None or os.environ.get("POL_TM_TRADE_SERVE", "0") != "1":
            continue
        try:
            served = _trade_entry_lines(m, pm, role)
            for why, line in served:
                pushqueue._queue_push(m, line, why=why, source=partner)
            common._say("tm: member %s trade board queued (role=%s, %d line(s)), "
                 "source=partner %r; up to %d @Pong re-serves armed -- the "
                 "client's @TrAns RecvCLEAR purge eats pre-purge copies by "
                 "design, a post-purge copy must land for the board."
                 % (m, role, len(served), partner,
                    common._env_int("POL_TM_TRADE_RESERVE_N", 4)))
        except Exception as exc:
            common._say("tm: member %s trade board NOT queued (%r)" % (m, exc))


def _trade_entry_lines(m, pm, role="acc"):
    """The (why, line) serves for one member's trade-board entry.

    ROLE-AWARE (2026-08-24, [tmlog] narration): the ACCEPTER's post-accept
    scene polls (0xA2,1) -- it gets @EventGameInit + the card list. The
    INITIATOR consumes @TrAns and goes straight to the 25/26 waiter (42
    consecutive (0xA2,26) polls measured, ZERO (0xA2,1) polls), so an init
    would sit unconsumed in its ~24-slot ring; it gets the list ONLY.

    (0xA2,26) LINE LENGTH is load-bearing (2026-08-23/24, first live A/B):
    the 20-card 637-byte line fastfailed the client every time (dumps 933148/
    936252/942416); the 10-card ~340-byte line was served twice with ZERO
    crashes. Suspected mechanism: the client's sqMg logger second stage
    (vsnprintf 0x1E3E88 -> callback -> sprintf \"%s\") overruns a fixed buffer
    at ~630+ bytes and scribbles the caller's SEH node -- fastfail 0x15 on the
    ODS raise. Native client lists chunk at <=15 cards = always under. Do NOT
    raise POL_TM_TRADE_CARD_MAX past 15 before the static trace sizes the
    real threshold. POL_TM_TRADE_CARD=0 drops the list serve entirely.
    """
    lines = []
    if role != "ini":
        # (0xA2,1) @EventGameInit -- the accepter's measured board-entry gate.
        # VS-init family body (recipient-first: each client is player 0 in its
        # own copy), default rules, framed from the partner so the cmd-1 arm's
        # session-pair stamp matches subsequent peer traffic.
        body = matchstart._vsgame_body([(m, 0), (pm, 0)], 0, ruleset._rule_seven(None))
        name = os.environ.get("POL_TM_TRADE_INIT_NAME",
                              "EventGameInit").encode()
        body = b"@" + name + b"=" + body.split(b"=", 1)[1]
        lines.append(("trade board entry (0xA2,1) @%s -- the accepter's "
                      "measured post-accept poll" % name.decode(),
                      protocol.encode_code(0xA2 | (1 << 16)) + body))
    if common._env_int("POL_TM_TRADE_CARD", 1):
        lines.append(("trade board card list (0xA2,26) /P=0, <=10 cards -- "
                      "the recipient's own collection",
                      _trade_card_line(m)))
        # BOTH SIDES (2026-08-24, [tmlog] 01:11Z run): consuming the complete
        # /P=0 list satisfied the 25-wait and left the scene polling for ONE
        # MORE 26 -- the PARTNER'S list. The 21:39Z "/P=1 partner list errors
        # the scene instantly" retraction was measured in the 637-byte era;
        # the length model retires it. Two ~340 B complete lists are safe.
        if pm is not None:
            lines.append(("trade board card list (0xA2,26) /P=1, <=10 cards "
                          "-- the PARTNER's collection",
                          _trade_card_line(pm, p=1)))
    return lines


def _trade_reserve_tick(member_id):
    """Re-serve the trade-board entry on this member's @Pong, while armed.

    The client's @TrAns consumer purges (0xA2,*) once, at a moment we cannot
    see; re-serving on the next few heartbeats guarantees a post-purge copy.
    Bounded: POL_TM_TRADE_RESERVE_N copies (default 4), >=3s apart, 30s TTL
    (POL_TM_TRADE_RESERVE_TTL). Ring pressure stays trivial next to the 24-slot
    ring, and no match runs during a trade.
    """
    k = pushqueue._push_key(member_id)
    st = _TRADE_PENDING.get(k) if k is not None else None
    if not st or st.get("partner") is None:
        return
    if os.environ.get("POL_TM_TRADE_SERVE", "0") != "1":
        return
    now = time.time()
    if now - st["t"] > common._env_int("POL_TM_TRADE_RESERVE_TTL", 45):
        _TRADE_PENDING.pop(k, None)
        return
    if st.get("left", 0) <= 0 or now - st.get("last", 0) < 3.0:
        return
    st["left"] -= 1
    st["last"] = now
    try:
        # Re-serves carry the CARD LIST only, whatever the role: [tmlog]
        # measured (2026-08-24) that NEITHER role polls (0xA2,1) -- both park
        # in the 25/26 waiter -- so re-serving the init would only stack
        # unconsumed copies in the client's ~24-slot ring.
        served = _trade_entry_lines(member_id, st["pm"], "ini")
        for why, line in served:
            pushqueue._queue_push(member_id, line, why=why + " [re-serve]",
                        source=st["partner"])
        common._say("tm: member %s trade board card list RE-SERVED (%d left) -- "
             "racing the client's one-shot @TrAns purge"
             % (member_id, st["left"]))
    except Exception as exc:
        common._say("tm: member %s trade re-serve failed (%r)" % (member_id, exc))


#: THE TRADE ACTUALLY MOVES CARDS, AND UNTIL 2026-08-25 IT DID NOT.
#: The board completed live at 01:02:12Z and the tester confirmed minutes
#: later: *"can confirm the trade did not persist"*. Collections here are
#: SERVER-AUTHORITATIVE -- `_collection_load` reads the per-member JSON and
#: `_collection_to_save` writes it into the save the client loads from -- so a
#: swap that happens only on the two clients evaporates at the next launch.
#:
#: Both halves are on the wire and we already relay them, so nothing new has to
#: be asked for:
#:
#:   (0xA2,26) @Card=/P=n/E=<total>/S=<start>/C=<count>@N<i>=/D=a|b|c|d|e|f
#:                      each side's OFFER, chunked; complete when S+C == E
#:   (0xA2,27) @Data=/P=n/M=<mode>/D=<30 bits>
#:                      that side's SELECTION
#:
#: KEY: A SELECTION INDEXES THE **PARTNER'S** OFFER, NOT YOUR OWN -- so it marks
#: what you RECEIVE. Settled by elimination on the completed 01:02Z trade:
#:
#:   m6 (P=0) offered 4 cards, selected 1|0|0|0...   -> takes m3's card 0
#:   m3 (P=1) offered 1 card,  selected 1|1|1|1|0... -> takes m6's cards 0..3
#:
#: m3 published ONE card and selected FOUR. If `/D=` indexed your own offer that
#: is impossible, so it indexes the partner's. It also means uneven trades are
#: normal and intended -- that run was four cards for one, which is what the
#: tester saw on screen.
#:
#: A wire card is 6 fields and a stored row is 8; `tmsave.encode_card` pads
#: short records with zeros, and `8|9|0|9|5|26` clears all three of its
#: rejection gates (id fits u16, type < TYPE_MAX, attack+pdef+mdef > 0), so the
#: wire form is storable as-is once padded.
_TRADE_SELECT_SLOTS = 30


def _trade_state(member_id):
    k = pushqueue._push_key(member_id)
    return _TRADE_PENDING.setdefault(k, {}) if k is not None else {}


def trade_partner_of(member_id):
    """This member's trade partner, from EITHER side of the binding.

    WARNING: A ONE-SIDED LOOKUP IS NOT ENOUGH, and a live run proved it (2026-08-25
    01:52:03Z). The pair was armed for members 3 and 6 at 01:51:51, and twelve
    seconds later the `@TrEnt=` handler logged **"partner member None"** -- so
    the partner copy of the grant never went out, and the role-1 client, which
    never sends `@TrEnt` itself, parked at "Now Loading" (board state 2,
    pct=80) exactly as it did before any of this was fixed. The `@Quit` disarm
    then printed "disarmed members 6 and None", confirming one entry had lost
    its `pm` while the other was fine.

    `note_trade_accept` writes BOTH directions, so the binding is redundant by
    construction -- there is no reason to fail when the other half is sitting
    right there. So: try our own entry first, and if it has no `pm`, scan for
    whoever names US. Empty entries (which `_trade_state` can create when a
    stray 0xA2 arrives outside a trade) are skipped rather than trusted.
    """
    k = pushqueue._push_key(member_id)
    if k is not None:
        pm = (_TRADE_PENDING.get(k) or {}).get("pm")
        if pm is not None:
            return pm
    for other, st in _TRADE_PENDING.items():
        if other is None or not isinstance(st, dict):
            continue
        pm = st.get("pm")
        if pm is None:
            continue
        if pushqueue._push_key(pm) == k and k is not None:
            # `other` names us as its partner -> `other` IS our partner.
            for cand in (st.get("pm_self"), other):
                if cand is not None:
                    return _unkey(cand)
    return None


def _unkey(k):
    """A push key back to a member id where possible (keys are ids today)."""
    try:
        return int(k)
    except (TypeError, ValueError):
        return k


def note_trade_offer(member_id, body):
    """Accumulate one `(0xA2,26) @Card=` chunk as this member's OFFER."""
    try:
        m = re.search(rb"@Card=/P=(\d+)/E=(\d+)/S=(\d+)/C=(\d+)", body)
        if not m:
            return
        total, start = int(m.group(2)), int(m.group(3))
        st = _trade_state(member_id)
        st["seat"] = int(m.group(1))        # this side's own /P= seat
        offer = st.setdefault("offer", {})
        if total == 0:                      # the /E=0 terminator
            st["offer_total"] = st.get("offer_total", 0)
            return
        st["offer_total"] = total
        for om in re.finditer(rb"@N(\d+)=/D=([0-9|]+)", body):
            idx = start + int(om.group(1))
            vals = [int(x) for x in om.group(2).split(b"|") if x != b""]
            if vals:
                offer[idx] = vals
    except Exception as e:
        common._say("tm: trade offer parse failed for member %s: %r" % (member_id, e))


def note_trade_select(member_id, body):
    """Record one `(0xA2,27) @Data=` selection bitmap for this member."""
    try:
        m = re.search(rb"@Data=/P=(\d+)/M=(\d+)/D=([0-9|]+)", body)
        if not m:
            return
        bits = [int(x) for x in m.group(3).split(b"|") if x != b""]
        st = _trade_state(member_id)
        st["seat"] = int(m.group(1))
        st["sel"] = bits[:_TRADE_SELECT_SLOTS]
        st["sel_mode"] = int(m.group(2))
    except Exception as e:
        common._say("tm: trade select parse failed for member %s: %r" % (member_id, e))


def _card_key(vals):
    """Identity for matching a WIRE card against a STORED row.

    WARNING: THE TWO SHAPES DO NOT LINE UP, and assuming they did is what made the
    first live run refuse with "m6 is missing a card it offered". Measured
    against member 6's real collection, all four offered cards agreeing:

        wire    [id, atk, type, pdef, mdef,     X]          6 fields
        stored  [id, atk, type, pdef, mdef, ?,  X,  ?]      8 fields
                                            5   6   7

    `wire[5] == stored[6]` in every case (26, 16, 14, 177) -- the wire DROPS
    stored[5] and stored[7]. Comparing the first six therefore never matches.

    WARNING: And the unit test did not catch it because the test built its collections
    by zero-padding wire cards, so both sides of the comparison shared the
    code's wrong assumption. A fixture derived from the code under test proves
    nothing; this one is now built from a real collection dump.

    Because the wire cannot carry stored[5]/[7], a received card is transferred
    as the GIVER'S STORED ROW rather than rebuilt from the wire -- lossless, and
    the reason `take()` returns the rows it popped.
    """
    v = list(vals)
    if len(v) >= 7:                      # a stored row
        return tuple(v[0:5]) + (v[6],)
    return tuple((v + [0] * 6)[:6])      # a wire card


def apply_trade_swap(a, b):
    """Move the agreed cards between two members' collections. Report a string.

    ALL-OR-NOTHING. If either side's offer is incomplete, or a card a member
    promised is not actually in their collection, NOTHING is written and the
    reason is returned. A half-applied trade would silently destroy cards, which
    is far worse than a trade that visibly did not take.
    """
    sa, sb = _trade_state(a), _trade_state(b)
    if sa.get("swapped") or sb.get("swapped"):
        return "already applied"
    oa, ob = sa.get("offer") or {}, sb.get("offer") or {}
    ta, tb = sa.get("offer_total"), sb.get("offer_total")
    if ta is None or tb is None:
        return "no offer recorded (m%s=%r m%s=%r)" % (a, ta, b, tb)
    if len(oa) != ta or len(ob) != tb:
        return ("offer INCOMPLETE -- m%s has %d/%s, m%s has %d/%s; refusing"
                % (a, len(oa), ta, b, len(ob), tb))
    ca, cb = sa.get("sel"), sb.get("sel")
    if ca is None or cb is None:
        return "no selection recorded (m%s=%s m%s=%s)" % (
            a, ca is not None, b, cb is not None)

    # a's selection indexes b's offer -> what a RECEIVES, and vice versa.
    a_gets = [ob[i] for i in range(min(len(ca), _TRADE_SELECT_SLOTS))
              if ca[i] and i in ob]
    b_gets = [oa[i] for i in range(min(len(cb), _TRADE_SELECT_SLOTS))
              if cb[i] and i in oa]
    if not a_gets and not b_gets:
        return "both selections empty -- nothing to move"

    da, db = collection._collection_load(a), collection._collection_load(b)
    la = [c for c in (da.get("cards") or []) if isinstance(c, list)]
    lb = [c for c in (db.get("cards") or []) if isinstance(c, list)]

    def take(pool, wanted, who):
        """Remove one instance of each wanted card; None if any is missing."""
        rest = list(pool)
        gone = []
        for w in wanted:
            key = _card_key(w)
            for j, have in enumerate(rest):
                if _card_key(have) == key:
                    gone.append(rest.pop(j))
                    break
            else:
                common._say("tm: trade REFUSED -- member %s does not hold %r, which "
                     "it offered. Nothing written." % (who, w))
                return None, None
        return rest, gone

    la2, a_gives = take(la, b_gets, a)
    if la2 is None:
        return "m%s is missing a card it offered" % a
    lb2, b_gives = take(lb, a_gets, b)
    if lb2 is None:
        return "m%s is missing a card it offered" % b

    pad = lambda v: (list(v) + [0] * 8)[:8]
    la2 += [pad(c) for c in b_gives]
    lb2 += [pad(c) for c in a_gives]

    da["cards"], db["cards"] = la2, lb2
    if not collection._collection_store(a, da):
        return "m%s collection write FAILED -- m%s untouched" % (a, b)
    if not collection._collection_store(b, db):
        common._say("tm: WARNING: m%s was written and m%s FAILED -- collections are now "
             "ASYMMETRIC, fix by hand" % (a, b))
        return "m%s collection write FAILED AFTER m%s succeeded" % (b, a)
    sa["swapped"] = sb["swapped"] = True
    return ("m%s gave %d and received %d; m%s gave %d and received %d"
            % (a, len(a_gives), len(b_gives), b, len(b_gives), len(a_gives)))


def note_trade_cancel(*member_ids):
    """A relayed `@TrCan=`/`@TrCanAck=` disarms the serve for both sides."""
    for m in member_ids:
        _TRADE_PENDING.pop(pushqueue._push_key(m), None)


#: The board's card array is 30 slots. Arm 0x1067B5 writes card i to slot
#: (/S + i) with NO bounds check (`esi + (S+i+0x4d7)*16`), and the sibling
#: cleanup loop caps the valid index at 0x1e = 30. So /S + count MUST be <= 30
#: or the write runs past the array and corrupts the session object.
TRADE_BOARD_SLOTS = 30


def _trade_card_line(member_id, p=0):
    """One member's collection as the trade board's (0xA2, cmd 26) `@Card=`.

    Arm 0x1067B5 reads `@Card=/P=/E=/S=/C=` then `@N<i>=/D=<8 vals piped>` and
    writes card i to board slot **/S + i** (`esi + (S+i+0x4d7)*16`).

    WARNING: THE CRASH FIX (2026-08-22, minidump-root-caused). The old body was
    `/E=1/S=<n>/C=<n>` -- it put the card COUNT in /S=, the START index. For a
    20-card collection that told the client to write cards into slots 20..39,
    overflowing the 30-slot array (slots 30..39), corrupting the session object
    and __fastfailing pol.exe (0xC0000409). Size-dependent: <=10 cards stayed in
    bounds, which is why one early run "worked". Correct: **/S=0**, /E= the real
    total, and CAP at the 30 the board can hold (a bigger collection needs
    chunking -- /S=0/C=8, /S=8/C=8, ... -- not yet built). /S=0/C=n/E=n also
    satisfies the completion check (S+C == E -> list marked complete).

    `p` -> `/P=` player index (0 = recipient), UNMEASURED but non-fatal.
    """
    # PARTIAL: /E=<n>/S=0/C=<n> -- the COMPLETE form (2026-08-24). The incomplete
    # /E=1/S=n form was kept out of fear of a "completion crash" (dump 933148,
    # /E=20/S=0/C=20 fastfailed), but the length model retired that reading:
    # 933148's line was 637 BYTES -- the crash was length, not completion (the
    # incomplete /E=1/S=20 form at the same length ALSO crashed, dump 936252;
    # completion was never varied independently of length). And the incomplete
    # form is now MEASURED not to advance the board: [tmlog] narration
    # (2026-08-24 00:40:46Z) shows the 26-waiter consuming our /E=1/S=10/C=10
    # list and going straight back to polling 25/26 -- S+C != E reads as
    # "partial chunk, wait for the rest", forever. Native chunking: /E= total,
    # /S= start, /C= this chunk; complete when S+C == E. At the 10-card cap
    # (~340 B) the line is length-safe, so completion gets its first fair
    # test; if a genuine completion crash exists it will write a dump.
    # DEFAULT CAP 10 (length): the crash tracks the LOGGED LINE LENGTH (~630+
    # bytes = 20 cards). 10 cards ~= 340 bytes, safely under. The client's own
    # native list builder chunks at <=15/message -- the ceiling to respect if
    # this cap is ever raised.
    _cap = max(0, min(TRADE_BOARD_SLOTS, common._env_int("POL_TM_TRADE_CARD_MAX", 10)))
    cards = (collection._collection_cards(member_id) or [])[:_cap]
    n = len(cards)
    body = protocol.encode_code(0xA2 | (26 << 16)) \
        + b"@Card=/P=%d/E=%d/S=0/C=%d" % (p, n, n)
    for i, v in enumerate(cards):
        body += b"@N%d=/D=%s" % (i, b"|".join(b"%d" % x for x in v))
    return body


def _trade_pending_serve(member_id):
    """The trade-board serve due on THIS `@Init=`, or []. TTL + serve cap.

    The cap exists because the game band REDIALS in a churn (~6-15s lives,
    measured 18:46Z: 20350 closed, 42159 closed, 45848 held) and every
    redial re-handshakes -- each fresh `@Init=` re-serves so the newest
    board still gets its list, but the client's receive store is one global
    ring of ~24 slots (ring pressure once ate a delivered @PutCard),
    so the re-serve cannot be unbounded.
    """
    # WARNING: RETRACTED MODEL, DEFAULT OFF. This served the trade card list on the
    # game-band @Init= (code 0x80). Static RE 2026-08-22: a trade NEVER opens
    # the game band (clients handshake it only for real game launches), and the
    # board takes each client's OWN relayed list, not a server-authored one. So
    # this path is dead for a trade; kept behind POL_TM_TRADE_INIT_CARDSERVE=1
    # only for a deliberate A/B. POL_TM_TRADE_SERVE now gates the 0xA1/0xA2,24
    # HANDSHAKE (note_trade_accept), a different thing.
    if os.environ.get("POL_TM_TRADE_INIT_CARDSERVE", "0") != "1":
        return []
    k = pushqueue._push_key(member_id)
    st = _TRADE_PENDING.get(k) if k is not None else None
    if st is None or os.environ.get("POL_TM_TRADE_SERVE", "0") != "1":
        return []
    if time.time() - st["t"] > common._env_int("POL_TM_TRADE_SERVE_TTL", 120):
        _TRADE_PENDING.pop(k, None)
        return []
    cap = common._env_int("POL_TM_TRADE_SERVE_MAX", 4)
    if st["served"] >= cap:
        common._say("tm: member %s trade-board serve SUPPRESSED (cap %d reached "
             "within the TTL) -- game band churning; the board never held "
             "long enough to consume a serve" % (member_id, cap))
        return []
    st["served"] += 1
    line = _trade_card_line(member_id)
    common._say("tm: member %s @Init= answered WITH the trade board card list "
         "(serve %d/%d, %d card(s)) -- riding the game-band @InitAns batch "
         "so it lands before the ~15s idle-close"
         % (member_id, st["served"], cap, line.count(b"@N")))
    return [line]
