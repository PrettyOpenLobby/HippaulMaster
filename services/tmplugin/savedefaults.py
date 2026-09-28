"""A fresh player save's factory header."""
import os
import tetramaster
import tmsave
from . import corenames


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
    member = corenames._session_get("member_id")
    if member and tetramaster is not None:
        try:
            bal = int(tetramaster.money_of(member))
        except Exception as exc:                 # never fail a fetch over money
            corenames.log("lobby", f"  {path!r}: cannot read member {member}'s balance "
                         f"({exc!r}) -- serving the header without it")
        else:
            got = tmsave.write_field(out, tmsave.MONEY_OFF, 4, bal)
            if got:
                moved[tmsave.MONEY_OFF] = got
    if moved:
        corenames.log("lobby", f"  {path!r}: no stored save -- serving the FACTORY header "
                     f"({len(moved)} field(s): "
                     + ", ".join("+0x%03X=%d" % (o, nw)
                                 for o, (_, nw) in sorted(moved.items()))
                     + "). A zero header reads as SE/BGM Volume Off.")
    return bytes(out)
