"""The seven /R= rule values: table defaults, the VS. game and VS. COM field orders."""
import os
from . import protocol


#: The seven `/R=` occurrences, in parse order.
#:
#: WARNING: INFERRED, NOT MEASURED, and the one mapping here a live run should check
#: first. `@Tet=` carries exactly seven rule fields (`TABLE_INFO_TET`, the order
#: 0x87090 parses the client's own settings in) and both GameInit messages carry
#: exactly seven `/R=`; that one-to-one count is the whole of the argument. Their
#: destinations differ in WIDTH, which is the corroboration: occurrence 0 lands
#: as a DWORD at [obj+0xDC] and 1..5 as BYTES at [obj+0xE0..0xE4], so the first
#: rule is the wide one -- `bm` in `@Tet=`'s order. Occurrence 6 goes to
#: [obj+0xE9] in `@VsGameInit` and [obj+0xD9] in `@ComGameInit`.
#: `POL_TM_VSGAME_RULES` overrides the lot with a comma-separated list.
VSGAME_RULE_KEYS = protocol.TABLE_INFO_TET

#: VERIFIED: THE CLIENT'S OWN DEFAULTS, TAKEN OFF THE WIRE -- not invented, and not the
#: zeros we used to send for a table nobody had configured.
#:
#: The server owner, 2026-08-20: "at some point we'll have to fix the default rules for
#: these rooms so they match the game defaults". They already told us what those
#: are. In one session the SAME client sent both halves:
#:
#:   code 0x24 `@Save=` (the stored preset)  Game=1 Tbl=0 al=0 au=0 lu=0 ll=0
#:                                           bm=0 ca=1 cb=1 co=0 du=0 gs=1
#:                                           in=0 pa=0 st=1 tl=3
#:   code 0x14 (Confirm)                     al=100 au=300 lu=99999 ll=0
#:                                           bm=0 ca=1 cb=1 co=0 du=0 gs=1
#:                                           in=0 pa=0 st=1 tl=3
#:
#: The `@Tet=` seven are **byte-identical in both**, so the player never touched
#: them -- they are what the client seeded itself with. Only the `@Tab=` numeric
#: ranges differ (the preset had never been stored, hence its zeros).
#:
#: WARNING: Corroborated independently, which is why this is not just "what one player
#: happened to pick": an earlier wire reading measured `au=300/al=100` ACCEPTED and
#: `au=0/al=0` ALWAYS REFUSED. Zeros are not merely un-default here, they are
#: rejected.
#:
#: WARNING: SCOPE: this is the fallback for a table whose owner has set nothing, and it
#: only reaches `/R=` on the GameInit. The authored `b/g/PTL` fixture still
#: carries its own rule block, and the open question -- whether `au`/`al` live
#: anywhere in the 104-byte table record at all -- is untouched by this.
TET_DEFAULTS = {"bm": 0, "du": 0, "st": 1, "cb": 1, "ca": 1, "gs": 1, "tl": 3}

#: The `@Tab=` half, same measurement. Not used by `/R=` (which is `@Tet=` only);
#: recorded here because it is the other half of the same answer and the next
#: person to serve `@Save=` back will need it.
TAB_DEFAULTS = {"in": 0, "lu": 99999, "ll": 0, "au": 300, "al": 100,
                "co": 0, "pa": 0, "pw": 0}


def _rule_seven(fields):
    """The seven `/R=` values for a table, from the settings its owner chose."""
    over = os.environ.get("POL_TM_VSGAME_RULES")
    if over:
        out = []
        for part in over.split(","):
            try:
                out.append(int(part.strip(), 0))
            except ValueError:
                out.append(0)
        return (out + [0] * 7)[:7]
    fields = fields or {}
    return [int(fields.get(k.decode(), TET_DEFAULTS.get(k.decode(), 0)) or 0)
            for k in VSGAME_RULE_KEYS]


#: The COM `@ComGameInit=` `/R=` field order the CLIENT PARSER (0x102D21) reads,
#: which is NOT the @Tet order `VSGAME_RULE_KEYS` uses. Measured 2026-09-02:
#: occ0->[obj+0xDC]=bm, occ1->[0xE0]=st, occ2->[0xE1]=cb, occ3->[0xE2]=ca,
#: occ4->[0xE3]=tl, occ5->[0xE4]=du (occ6->[0xD9]=d9 is written, not rendered).
#: `gs` is DROPPED on the COM path. Sending @Tet order here MANGLED the VS. COM
#: rules screen: `/R=0|0|1|1|1|1|3` (@Tet: bm,du,st,cb,ca,gs,tl) is read as
#: bm=0, st=0 (Special Tile OFF), cb=1, ca=1, tl=1 (Time Limit 0:30), du=1 --
#: the tester's exact wrong-defaults screen (Special Tile Off / 0:30 vs the
#: factory On / 3:00), 2026-09-02. In COM order the same defaults are
#: `/R=0|1|1|1|3|0|0` = Special Tile On, Time Limit 3:00.
COM_RULE_KEYS = (b"bm", b"st", b"cb", b"ca", b"tl", b"du")


def _com_rule_seven(fields):
    """The seven `@ComGameInit=` `/R=` values in the CLIENT PARSER's order.

    A COM game's rules land on a DIFFERENT slot order than `@Tet=`/`_rule_seven`
    (see `COM_RULE_KEYS`): six named fields plus a trailing d9=0 (occ6, written
    to [obj+0xD9] but not rendered). Use this -- never `_rule_seven` -- for any
    `@ComGameInit=` `/R=` we build from defaults. `POL_TM_VSGAME_RULES` still
    overrides the lot (given in this same COM order).
    """
    over = os.environ.get("POL_TM_VSGAME_RULES")
    if over:
        out = []
        for part in over.split(","):
            try:
                out.append(int(part.strip(), 0))
            except ValueError:
                out.append(0)
        return (out + [0] * 7)[:7]
    fields = fields or {}
    vals = [int(fields.get(k.decode(), TET_DEFAULTS.get(k.decode(), 0)) or 0)
            for k in COM_RULE_KEYS]
    return (vals + [0])[:7]      # + d9 (occ6), keeping seven values


#: The `/R=` field order the @VsGameInit / @WatchInfo client PARSER reads
#: (arm 0x102476), which is NOT the @Tet order `VSGAME_RULE_KEYS` aliased.
#:
#: WARNING: THIS IS THE PvP "the turn timer is always 30 s / Special Tile is off"
#: BUG, 2026-09-03. `_rule_seven` emitted `/R=` in `@Tet=` order
#: (bm,du,st,cb,ca,gs,tl), but @VsGameInit lands its seven on the SAME match
#: object as @ComGameInit -- occ0->[0xDC], occ1..5->[0xE0..0xE4] -- and the
#: game engine reads the TURN TIMER from [0xE3] = occ 4 (measured on the COM
#: path 2026-09-02; same engine, same struct). @Tet order put `tl` at occ 6,
#: so the client read `ca` (=1) at occ4 as the timer -> **0:30 every game**,
#: and `du` (=0) at occ1 as Special Tile -> **OFF**. The bug's very existence
#: proves the timer is NOT at occ6 ([0xE9]), or the old order would have shown
#: 3:00. So the fix is the SAME reorder that repaired VS. COM: `tl` at occ4.
#:
#: The first six positions are exactly `COM_RULE_KEYS`; they differ only at
#: occ6, which @VsGameInit writes to [0xE9] (COM writes [0xD9]). `gs` is the
#: remaining @Tet field and the likely occupant -- but [0xE9]'s match-meaning
#: is UNMEASURED and does NOT affect the timer. `_vsgame_rule_seven(None)` is
#: [0,1,1,1,3,0,1] = Special Tile On, Time Limit 3:00, the factory screen.
#: `POL_TM_VSGAME_RULES` overrides the lot, given in THIS order.
VSGAME_RULE_KEYS_PARSED = (b"bm", b"st", b"cb", b"ca", b"tl", b"du", b"gs")


def _vsgame_rule_seven(fields):
    """The seven `@VsGameInit=`/`@WatchInfo=` `/R=` values in the CLIENT
    PARSER's slot order (`VSGAME_RULE_KEYS_PARSED`) -- never `_rule_seven`'s
    `@Tet=` order, which mis-slots the timer and Special-Tile rules."""
    over = os.environ.get("POL_TM_VSGAME_RULES")
    if over:
        out = []
        for part in over.split(","):
            try:
                out.append(int(part.strip(), 0))
            except ValueError:
                out.append(0)
        return (out + [0] * 7)[:7]
    fields = fields or {}
    return [int(fields.get(k.decode(), TET_DEFAULTS.get(k.decode(), 0)) or 0)
            for k in VSGAME_RULE_KEYS_PARSED]
