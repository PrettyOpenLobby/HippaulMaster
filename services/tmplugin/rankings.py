"""The ranking lists: the class-R reply, the rank list blob and TM0RkData per client build."""
import os
import tmrank
from . import corenames, templates


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
    if tmrank is None or corenames.polpro is None:
        return None, False
    if os.environ.get("POL_TM_RANK", "1") != "1":
        return None, False
    try:
        groups = corenames.polpro.parse(payload)
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
            corenames.log("authserv", f"  rankings: <RR>({rank_id}) is not one of "
                            f"{sorted(tmrank.LISTS)} -- falling through")
            return None, False
        path = tmrank.path_for(rank_id)
        blob = _rank_list_blob(path)
        rows = len(blob) // tmrank.REC if blob else 0
        if rows <= 0:
            # `<LN>` 0 is not "an empty list", it is "close the screen with no
            # error" (rva 0x1750AE). Fall through to the static entry, which
            # names the one-row fixture.
            corenames.log("authserv", f"  rankings: nothing to serve for RankID {rank_id} "
                            f"-- falling through to the polpro.json entry")
            return None, False
        rows = min(rows, tmrank.MAX_ROWS)     # 0x8AF00: 0 < num <= 100
        menu, label, key, _ = tmrank.LISTS[rank_id]
        corenames.log("authserv", f"  rankings: <RR>({rank_id}) = {label} (menu {menu}, "
                        f"ranked on {key}) -> {path} with {rows} row(s)")
        return corenames.polpro.build([("RF", [path]), ("LN", [str(rows)])]), True
    except Exception as exc:
        corenames.log("authserv", f"  rankings: reply failed ({exc!r}) -- the polpro.json "
                        f"entry still stands")
        return None, False

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
    if not corenames._peer_is_ps2() or os.environ.get("POL_TM_RKDATA_PS2", "1") != "1":
        return data
    out = tmrank.to_ps2(data)
    corenames.log("lobby", f"  3:0 {path!r}: PS2 layout -- {len(data)}B PC header sliced "
                 f"to {len(out)}B (dropping the PC's unread +0x00)")
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
    return templates._tm_template_blob(path)           # services/tmdata/, shipped fallback
