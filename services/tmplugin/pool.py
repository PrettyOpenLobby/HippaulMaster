"""The character pool the client hands over (<CR>), and names and records looked up in it."""
import tmrank
from . import corenames


def _tm_pool_note(payload):
    """`<CR>` is the client handing us its CHARACTER POOL. Keep it.

    Measured 2026-08-20 off this very log -- three accounts, all the same shape:

        <CR>(0x000000003B9ACA66,1000000102) <AN>(1000000102) <CI>(1,Card Level 0)

    and `0x3B9ACA66 == 1000000102 == accounts._default_content_id(1, 2)`. So the
    identity the rankings row is matched on (TM.dll rva 0x17545E, row+0x10/+0x14
    against `0x52452A0`/`0x52452A4`) is the member's Tetra Master **Content ID**,
    and the client tells it to us every session.

    We RECORD it rather than only computing it, for the reason `tmroom.
    note_pol_id` records `/NN=` verbatim: a value echoed back cannot be wrong,
    and a derived one is only right until the thing it derives from moves. The
    computed value is still checked against it here, because that check is free
    and it is the one thing that would otherwise fail SILENTLY -- a mismatched
    cid does not error, it just tells the player "Did not rank" while their own
    name is on the screen.

    `<CI>`'s second value is the client's own status string (`Card Level 116`) --
    a live per-player stat nothing else here captures.
    """
    if tmrank is None or corenames.polpro is None:
        return
    try:
        groups = dict(corenames.polpro.parse(payload))
        if "CR" not in groups:
            return
        member = corenames._session_get("member_id")
        if not member:
            return
        cr = groups.get("CR") or []
        ci = groups.get("CI") or []
        cid = int(cr[0], 16) if cr and cr[0].lower().startswith("0x") else None
        csid = int(ci[0]) if ci and ci[0].lstrip("-").isdigit() else None
        # ASK THE DB, don't recompute (2026-08-23). `cid_for`'s fallback is the
        # RETRACTED computed mint, which is still right for every account that
        # predates the allocator and wrong for every account after it -- so
        # handing it the stored value is the difference between a real alarm and
        # a WARNING: line on every new player's first Tetra Master session.
        want = tmrank.cid_for(member, corenames._member_content_id(member,
                                                         tmrank.POOL_CONTENT_CODE))
        if cid is not None and cid != want:
            corenames.log("authserv", f"  WARNING: TM pool: member {member} calls itself {cid} "
                            f"but our Content ID mint says {want} -- the ranking "
                            f"row would be matched against the wrong identity. "
                            f"Recording what the CLIENT said; see tmrank.cid_for")
        if tmrank.note_pool(member, cid=cid, csid=csid,
                            cname=(cr[1] if len(cr) > 1 else None),
                            cinfo=(ci[1] if len(ci) > 1 else None)):
            corenames.log("authserv", f"  TM pool: member {member} cid={cid} csid={csid} "
                            f"{(ci[1] if len(ci) > 1 else '')!r} recorded for the "
                            f"ranking tally")
    except Exception as exc:
        corenames.log("authserv", f"  TM pool: could not record this <CR> ({exc!r}) -- the "
                        f"tally falls back to the Content ID mint")


def _pool_character_name(cid):
    """The TM pool's character name for `cid`, or None.

    WARNING: `tmrank`'s `cname` IS NOT A CHARACTER NAME, whatever it is called.
    `_tm_pool_note` fills it from `<CR>` value[1], and that value is the
    **decimal Content ID** -- the live prod pool is full of rows like
    `{"cid": 30000046, "cname": "30000046"}`. Trusting the field's NAME instead
    of reading what is in it is what put Content ID digits back in the Tetra
    Master char-list slot on 2026-08-23, undoing the very bug the +0x18 work had
    just fixed. (`tm-shipped-data-beats-the-docs`, one directory over.)

    So: a purely numeric value is the id echoed back, not a name, and this
    returns None for it. If SE's client ever does send a real name there, it
    will not be all digits and it will be used.
    """
    if tmrank is None or cid is None:
        return None
    try:
        for rec in (tmrank.observed_pools() or {}).values():
            if corenames.accounts.content_id_int(rec.get("cid")) != cid:
                continue
            name = (rec.get("cname") or "").strip()
            return name if name and not name.isdigit() else None
    except Exception:
        pass
    return None


def _pool_character(cid):
    """(name, info, member_id) for a Content ID from the character pool the
    client filled on `<CR>`, or None. The name goes through
    `_pool_character_name` (a purely numeric `cname` is the id echoed back,
    not a name)."""
    want = corenames.accounts.content_id_int(cid) if corenames.accounts else cid
    for mid, rec in (tmrank.observed_pools() or {}).items():
        if corenames.accounts and corenames.accounts.content_id_int(rec.get("cid")) == want:
            return (_pool_character_name(want), rec.get("cinfo") or None, mid)
    return None
