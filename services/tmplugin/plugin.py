"""The TetraMaster titles.Title subclass and register(), the hooks the core calls."""
import os
import titles
import tetramaster
import tmrank
from . import (
    auction, bandglue, chat, corenames, envelope, events, fetches, manifests, pool, profile,
    ptl, rankings, roster, savedefaults, settings, templates, zones,
)


class TetraMaster(titles.Title):
    tag = b"TM0"
    content_code = 2
    fetch_pathlen = fetches.FETCH_PATHLEN
    resource_init = manifests.RESOURCE_INIT
    polpro_spec_files = tuple(p for p in settings.POLPRO_SPEC_CANDIDATES if os.path.isfile(p))

    def core_bound(self):
        corenames._rebind()

    def describe(self):
        return tetramaster.describe()

    # --- the auth band ---
    def notice(self, cls, payload, text, target, nick, srv, sess):
        return envelope.notice(cls, payload, text, target, nick, srv, sess)

    def polpro_reply(self, cls, payload):
        # THE DELTA STREAM ANSWERS `<DR>` BEFORE THE TEMPLATE DOES (class L);
        # the RANKING LISTS (R) and the AUCTION (A) answer themselves for the
        # same reason -- which list, and whose count, is a VALUE the static
        # template cannot express. Declining falls through to the template.
        if cls == b"L":
            return roster._roster_delta_reply(payload)
        if cls == b"R":
            return rankings._tm_rank_reply(payload)
        if cls == b"A":
            return auction._tm_auction_reply(payload)
        return None, False

    def polpro_noted(self, cls, payload, target):
        # THE ROSTER: `<DE>`/`<PD>` carry this member's own record; recorded
        # whether or not there was a reply. THE CHARACTER POOL: the client is
        # telling us who it is. THE ROOM PEER: every class-L line is addressed
        # to the room's roster peer, whose nick folds to the guid table peers
        # index against.
        roster._roster_note(payload, self.tag)
        if cls == b"P":
            pool._tm_pool_note(payload)
        roster._roster_note_room_peer(target)

    def roster_sequence(self):
        return roster._roster_sequence()

    def part_echo(self, nick, srv, sess):
        return bandglue._part_echo(nick, srv, sess)

    def room_parted(self, chan):
        roster._roster_note_departure(chan)

    def room_notice(self, body, sess, nick):
        rebroadcast = chat._tm_chat_rebroadcast(body)
        if rebroadcast is not None:
            return ("rebroadcast", rebroadcast)
        filled = chat._tm_chat_fill_name(body, sess, nick)
        if filled is not None:
            return ("filled", filled)
        return None

    def rooms_changed(self, state):
        roster._roster_sync_presence(state)

    def session_closed(self, member_id):
        roster._roster_retire_on_close(member_id)

    def band_role(self, cmd_txt):
        return bandglue._band_role(cmd_txt)

    def idle_pushes(self, member_id, peers):
        return tetramaster.idle_pushes(member_id, peers)

    def requeue_pushes(self, member_id, items, why=""):
        tetramaster.requeue_pushes(member_id, items, why=why)

    # --- the lobby band ---
    def resource_length(self, path):
        return fetches.resource_length(path)

    def resource_nodata(self, path, subject):
        return auction._checkout_empty(path, subject)

    def resource_live(self, path, n, req_pt):
        # THE TOURNAMENT'S LOBBY LISTS, built per fetch for the members in
        # POL_TM_EVENT_ZONE_MEMBERS: the event zone's one room, then the
        # Event List row. None for everyone and everything else.
        subject = corenames._fetch_subject(req_pt) if corenames._fetch_subject is not None else 0
        ev = events._tm_event_room_list(path, n, subject)
        if ev is None:
            ev = events._tm_event_list(path, n)
        return ev

    def resource_template(self, path):
        # THE PUBLISHED RANKING TALLY OUTRANKS THE SHIPPED FIXTURE, looked up
        # through the SAME function that answered `<LN>` on the other band.
        if tmrank.is_list_path(path) or path == tmrank.RKDATA:
            return rankings._rank_list_blob(path)
        return templates._tm_template_blob(path)

    def resource_patch(self, path, data, subject):
        # THE SAME PATCHES FOR A STORED BLOB AND A TEMPLATE, IN THE SAME ORDER:
        # the template branch once skipped the live roster and served the
        # shipped tables with a permanently EMPTY member list.
        data = ptl._ptl_with_live_roster(path, data)
        data = zones._lobby_counts_live(path, data, subject)
        data = zones._zone_host_live(path, data)
        data = events._zl_event_zone(path, data)
        data = zones._zl_name_for_build(path, data)
        data = auction._auc_counts_live(path, data)
        data = rankings._rkdata_for_build(path, data)
        return data

    def store_patch(self, path, data):
        # A fresh save must carry the FACTORY option header rather than the
        # zeros that read as "every volume Off".
        return savedefaults._tm_save_defaults(path, data)

    # --- the member profile ---
    def profile_fields(self, cid, member_id):
        return profile.profile_fields(cid, member_id)

    def polpro_profile(self, cid, name, member_id):
        # THE `<PO>` POPUP'S THREE SLOTS: Card Level (16), Title (24) and
        # Average Rank (9), from the ONE producer the Viewer's content profile
        # also reads, so the two screens cannot disagree about a player. The
        # slot numbers are the `.pib` schema's generic tail (see the core's
        # `<PG>` handler for why only slots 9 onward transfer).
        game = profile.profile_fields(cid, member_id)
        fields = {}
        if profile._TM_CARD_LEVEL in game:
            fields[16] = int(game[profile._TM_CARD_LEVEL])
        if profile._TM_TITLE in game:
            fields[24] = int(game[profile._TM_TITLE])
        if profile._TM_AVG_RANK in game:
            fields[9] = str(game[profile._TM_AVG_RANK])
        return fields, name, member_id

    def character_name(self, cid):
        return pool._pool_character_name(cid)

    def character_display_name(self, cid, content_id, handle_name):
        """The name in the character list (1:3, +0x18): the character pool's
        name, else the handle name, else the Content ID's digits. NEVER empty:
        this string is the VS. COM seat banner. POL_CHAR_NAME_HANDLE=0 drops
        the handle-name step."""
        pooled = pool._pool_character_name(cid)
        if pooled:
            return pooled
        if handle_name and os.environ.get("POL_CHAR_NAME_HANDLE", "1") == "1":
            return handle_name
        return content_id

    def character(self, cid):
        return pool._pool_character(cid)

    def describe_line(self, body):
        return tetramaster.describe_line(body)


def register():
    # Tetra Master's own tables (tmstore, services/tm_migrations/), applied
    # when the core loads the title, before the first game line needs them
    if os.environ.get("POL_DATABASE_URL", "").strip():
        import tmstore
        tmstore.migrate_at_start("tmtitle")
    return titles.register(TetraMaster())
