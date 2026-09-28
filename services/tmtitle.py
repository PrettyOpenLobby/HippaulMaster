"""Tetra Master as a title plugin for the OpenLobby core.

The code lives in the `tmplugin` package, one module per concern
(tmplugin/__init__.py lists them; CONTRIBUTING.md has a reading order). This is
the name everything else uses (`POL_TITLES=tmtitle` in the core, and the tools
and tests) and a compatibility facade: `tmtitle.<name>` resolves every name of
the old single-file module, reading or writing, to the module that owns it, so
a test that swaps a helper out (`R._live_rooms = lambda: live`) still reaches
the code that calls it.

The facade and its module list were generated with the package in commit
f8ac59d and are kept by hand since: a new top-level name that the core, tools
or tests reach as `tmtitle.<name>` gets a line in `_OWNERS`.
"""
import sys
import types

# ONE COPY OF THIS MODULE. `python tmtitle.py` runs this file as `__main__`,
# and a later `import tmtitle` would otherwise load a second facade object.
if __name__ == "__main__":
    sys.modules.setdefault("tmtitle", sys.modules[__name__])

from tmplugin import (  # noqa: E402,F401
    deps,
    settings,
    corenames,
    fetches,
    roster,
    auction,
    rankings,
    pool,
    chat,
    envelope,
    manifests,
    zones,
    ptl,
    savedefaults,
    events,
    templates,
    profile,
    bandglue,
    plugin,
)

# Which tmplugin module owns each name that `tmtitle.<name>` reaches.
_OWNERS = {
    'FETCH_PATHLEN': 'fetches',
    'HERE': 'settings',
    'NoPad': 'corenames',
    'POLPRO_SPEC_CANDIDATES': 'settings',
    'PRESENCE': 'corenames',
    'PTL_DECLARED': 'fetches',
    'RELEASE_DEFAULTS': 'settings',
    'RESOURCE_DIR': 'corenames',
    'RESOURCE_INIT': 'manifests',
    'ROOMS': 'corenames',
    'TM_SAVE_PATH': 'savedefaults',
    'TetraMaster': 'plugin',
    '_AUCTION_BIDHIST_PATH': 'auction',
    '_AUCTION_BROWSE_PATH': 'auction',
    '_AUCTION_LIST_PATHS': 'fetches',
    '_AUCTION_LIST_REC': 'fetches',
    '_AUCTION_NOTICES': 'auction',
    '_CHECKOUT_PATHS': 'auction',
    '_CORE_NAMES': 'corenames',
    '_EVD_COUNT_OFF': 'events',
    '_EVD_MASK_OFF': 'events',
    '_EVENT_ZONE_ID': 'events',
    '_EVL_HDR': 'events',
    '_EVL_REC': 'events',
    '_EVL_TOTAL': 'events',
    '_EXHIBIT_LIST_PATH': 'fetches',
    '_PTL_EVENT_HOST_FROM': 'ptl',
    '_PTL_TEMPLATE_SERIAL': 'ptl',
    '_RANK_LIST_PATH': 'rankings',
    '_RANK_LIST_REC': 'rankings',
    '_RL_COUNT_OFF': 'events',
    '_RL_F_CHAN': 'events',
    '_RL_F_CHAN_END': 'events',
    '_RL_F_NAME': 'events',
    '_RL_F_NAME_END': 'events',
    '_RL_HDR': 'events',
    '_RL_REC': 'events',
    '_TM0CQL_INIT': 'manifests',
    '_TM0CVML_INIT': 'manifests',
    '_TM0IML_INIT': 'manifests',
    '_TM0SML_INIT': 'manifests',
    '_TM_AUCTION_SENDER_GUID': 'auction',
    '_TM_AVG_RANK': 'profile',
    '_TM_CARD_LEVEL': 'profile',
    '_TM_COM_HOST': 'manifests',
    '_TM_SERVICE_PEER': 'manifests',
    '_TM_TITLE': 'profile',
    '_ZL_COUNT_OFF': 'zones',
    '_ZL_F_HOST': 'zones',
    '_ZL_F_ID': 'zones',
    '_ZL_F_NAME': 'zones',
    '_ZL_HDR': 'zones',
    '_ZL_MAX': 'zones',
    '_ZL_REC': 'zones',
    '_ZONE_OF': 'zones',
    '_advertise_configured': 'corenames',
    '_auc_counts_live': 'auction',
    '_auction_all_rows': 'auction',
    '_auction_bids': 'auction',
    '_auction_bids_file': 'auction',
    '_auction_count_rows': 'auction',
    '_auction_find': 'auction',
    '_auction_next_id': 'auction',
    '_auction_notice': 'auction',
    '_auction_rows': 'auction',
    '_auction_store_file': 'auction',
    '_auction_sweep': 'auction',
    '_auction_take_listed_card': 'auction',
    '_auction_with_bid_counts': 'auction',
    '_band_role': 'bandglue',
    '_checkout_empty': 'auction',
    '_client_host': 'zones',
    '_core': 'deps',
    '_fetch_subject': 'corenames',
    '_game_notice_line': 'corenames',
    '_irc_host': 'corenames',
    '_k': 'settings',
    '_live_rooms': 'corenames',
    '_lobby_counts_live': 'zones',
    '_mail_mint': 'corenames',
    '_member_content_id': 'corenames',
    '_member_display_name': 'corenames',
    '_member_primary_handle': 'corenames',
    '_note_zone_presence': 'zones',
    '_notice_fill': 'auction',
    '_part_echo': 'bandglue',
    '_peer_is_ps2': 'corenames',
    '_pool_character': 'pool',
    '_pool_character_name': 'pool',
    '_ptl_event_hosts': 'ptl',
    '_ptl_unknown_base': 'ptl',
    '_ptl_with_live_roster': 'ptl',
    '_rank_list_blob': 'rankings',
    '_rebind': 'corenames',
    '_resource_file': 'corenames',
    '_resource_read_file': 'corenames',
    '_resource_stored': 'corenames',
    '_rkdata_for_build': 'rankings',
    '_rl_name_ps2': 'zones',
    '_room_of_member': 'corenames',
    '_roster_delta_reply': 'roster',
    '_roster_note': 'roster',
    '_roster_note_departure': 'roster',
    '_roster_note_guid': 'roster',
    '_roster_note_room_peer': 'roster',
    '_roster_retire_on_close': 'roster',
    '_roster_retire_stale': 'roster',
    '_roster_sequence': 'roster',
    '_roster_sync_presence': 'roster',
    '_self_ip': 'corenames',
    '_sess_member_id': 'corenames',
    '_session_get': 'corenames',
    '_session_handle_id': 'corenames',
    '_session_sid': 'corenames',
    '_tm_auction_reply': 'auction',
    '_tm_chat_fill_name': 'chat',
    '_tm_chat_rebroadcast': 'chat',
    '_tm_event_active': 'events',
    '_tm_event_info': 'events',
    '_tm_event_list': 'events',
    '_tm_event_missions': 'events',
    '_tm_event_room_list': 'events',
    '_tm_event_room_name': 'events',
    '_tm_event_test_member': 'events',
    '_tm_pool_note': 'pool',
    '_tm_rank_reply': 'rankings',
    '_tm_save_defaults': 'savedefaults',
    '_tm_template_blob': 'templates',
    '_v': 'settings',
    '_write_resource': 'auction',
    '_zl_event_zone': 'events',
    '_zl_name_for_build': 'zones',
    '_zl_with_live_host': 'zones',
    '_zone_host_live': 'zones',
    '_zone_lease': 'zones',
    '_zone_occupants': 'zones',
    'accounts': 'corenames',
    'json': 'deps',
    'log': 'corenames',
    'notice': 'envelope',
    'os': 'deps',
    'polpro': 'corenames',
    'profile_fields': 'profile',
    're': 'deps',
    'register': 'plugin',
    'resource_length': 'fetches',
    'struct': 'deps',
    'sys': 'deps',
    'tetramaster': 'deps',
    'time': 'deps',
    'titles': 'deps',
    'tmauction': 'deps',
    'tmfixtures': 'deps',
    'tmrank': 'deps',
    'tmroom': 'deps',
    'tmsave': 'deps',
}
_MODULES = {
    'deps': deps,
    'settings': settings,
    'corenames': corenames,
    'fetches': fetches,
    'roster': roster,
    'auction': auction,
    'rankings': rankings,
    'pool': pool,
    'chat': chat,
    'envelope': envelope,
    'manifests': manifests,
    'zones': zones,
    'ptl': ptl,
    'savedefaults': savedefaults,
    'events': events,
    'templates': templates,
    'profile': profile,
    'bandglue': bandglue,
    'plugin': plugin,
}


class _Facade(types.ModuleType):
    """`tmtitle.<name>` reads and writes go to the owning tmplugin module."""

    def __getattr__(self, name):
        mod = _OWNERS.get(name)
        if mod is None:
            raise AttributeError(f"module 'tmtitle' has no attribute {name!r}")
        return getattr(_MODULES[mod], name)

    def __setattr__(self, name, value):
        mod = _OWNERS.get(name)
        if mod is None:
            super().__setattr__(name, value)
            return
        setattr(_MODULES[mod], name, value)
        if mod == "deps":
            # an imported name (tmsave, tmbattle, ...) is a copy in every
            # module that imported it; a patch has to reach each copy
            for other in _MODULES.values():
                if other is not deps and hasattr(other, name):
                    setattr(other, name, value)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(_OWNERS))


sys.modules[__name__].__class__ = _Facade

