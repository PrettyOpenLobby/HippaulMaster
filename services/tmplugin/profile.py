"""The member profile fields for content id 2 (card level, title, average rank)."""
import re
from . import corenames


# ---------------------------------------------------------------------------
# the member profile (the Viewer's content profile for content id 2)
# ---------------------------------------------------------------------------
_TM_CARD_LEVEL, _TM_TITLE, _TM_AVG_RANK = 16, 24, 9


def profile_fields(cid, member_id):
    """`{schema slot: value}` for the Tetra Master content profile: Card
    Level (16), Average Rank (9) and Title (24), only when held. See the
    core's _content_game_fields for the slot map and why unset stays unset.
    """
    out = {}
    try:
        import tmrank
        pool = tmrank.observed_pools()
        want = corenames.accounts.content_id_int(cid)
        level = None
        # `member_id` reaches us from the handle link, which a Content ID
        # the client sent but nothing has linked yet does NOT have. The
        # pool is KEYED BY MEMBER ID, so the row that gives us the card
        # level also names whose it is -- use it rather than dropping the
        # stats for exactly the players who have been playing.
        for pool_key, row in (pool or {}).items():
            if int(row.get("cid") or 0) == want:
                if member_id is None:
                    member_id = pool_key
                lvl = row.get("card_level")
                if lvl is None:
                    # Fall back to the client's own `<CI>` string. It reads
                    # "Card Level 116", and it is what the CLIENT told us --
                    # rows recorded before `card_level` was split out carry
                    # only this.
                    m = re.search(r"(\d+)", str(row.get("cinfo") or ""))
                    lvl = m.group(1) if m else None
                if lvl is not None:
                    level = int(lvl)
                    out[_TM_CARD_LEVEL] = level
                break
        # AVERAGE RANK and TITLE. Both come from the career stats the match
        # path keeps in the collection's `rank` block, and both are served
        # only when that block has a game in it -- a player who has never
        # played has no average, and the title is a FUNCTION of the average,
        # so guessing one fabricates the other.
        collection = tmrank.collection_of(member_id) or {}
        stats = collection.get("rank") or {}
        games = int(stats.get("games") or 0)
        avg = int(stats.get("avg_rank") or 0)
        if games and avg:
            # Slot 9 is a 64-byte STRING (type 1 in `prof_002.pib`, and the
            # same widget kind as Player Name), so the SERVER decides the
            # formatting. Two decimals is the game's own rendering of this
            # fixed point everywhere it appears -- `Average Rank 0.48` on
            # Player Data, `2.96` on the ranking rows.
            out[_TM_AVG_RANK] = "%d.%02d" % (avg // 100, avg % 100)
            try:
                import tm_title
                # The SAME three inputs, in the SAME units, the client's own
                # picker uses -- money from the collection (which is what
                # the save's +0x34 is written from), card level from the
                # pool the client sent, average rank from the career block.
                # Feeding it anything else would show a title the game
                # itself would not.
                money = int(collection.get("money") or 0)
                idx = tm_title.title_index(money, level or 0, avg)
                if idx:
                    out[_TM_TITLE] = idx
            except Exception as exc:
                corenames.log("lobby", f"content profile: no title for member "
                             f"{member_id} ({exc!r}) -- leaving it unset, "
                             f"which the Viewer draws as Unknown")
    except Exception as exc:
        corenames.log("lobby", f"content profile: Tetra Master fields for {cid} failed "
                     f"({exc!r}) -- leaving them unset")
    return out
