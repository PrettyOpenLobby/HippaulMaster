"""The core names the title uses, bound from titles.core at import and again when the core rebinds.
"""
from titles import core as _core


#: Every core name the moved code reaches for. Bound at import and again when
#: the core (re)binds its handle; see titles.Core.__doc__ for what each is.
_CORE_NAMES = (
    "log", "NoPad", "PRESENCE", "ROOMS", "accounts", "polpro", "RESOURCE_DIR",
    "_game_notice_line", "_irc_host", "_session_get", "_session_sid",
    "_sess_member_id", "_member_content_id", "_live_rooms", "_room_of_member",
    "_resource_file", "_resource_read_file", "_resource_stored", "_fetch_subject",
    "_peer_is_ps2", "_mail_mint", "_member_primary_handle",
    "_session_handle_id", "_self_ip", "_member_display_name",
    "_advertise_configured",
)


def _rebind():
    for name in _CORE_NAMES:
        try:
            globals()[name] = getattr(_core, name)
        except AttributeError:
            globals()[name] = None


_rebind()
