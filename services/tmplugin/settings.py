"""The title's release defaults (set into the environment at import) and where its files are."""
import os
from . import deps


HERE = os.path.dirname(os.path.abspath(deps.FACADE_FILE))

#: THE PROVEN CONFIGURATION AS DEFAULTS. These are the values the live service
#: ran Tetra Master with (its compose enumerated every knob; the ones below
#: are the ones it set). A deployment overrides any of them in its
#: environment; nothing else needs setting. The knobs are read where they
#: are used (search the name), each with its reason.
RELEASE_DEFAULTS = {
    "POL_TM_EVENT_START": "-1",      # -1/-1 = no event: the event board and
    "POL_TM_EVENT_END": "-1",        # countdown stay off until a window is set
    "POL_TM_CV_INIT": "excinit",
    "POL_TM_EINIT_RT": "1",
    "POL_TM_TRADE_CARD": "1",
    "POL_TM_TRADE_CARD_MAX": "10",
    "POL_TM_GAMEML_DELAY_MS": "0",
    "POL_TM_BOARD_OBJECTS": "1",     # the board's blocks and special tiles
    "POL_TM_SCRAMBLE": "1",
    "POL_TM_WAGER_DOUBLEUP": "1",
    "POL_TM_WAGER_REMATCH": "1",
    "POL_TM_COMBO": "defeated",
    "POL_TM_ABANDON_CLEANUP": "1",
    "POL_TM_DECK_NAMES": "1",
    "POL_TM_DECK_NAME_BASE": "1",
    "POL_TM_CHAMPION_SAVE_SLOT": "0",
    "POL_TM_DECK_SLOTS": "1",
    "POL_TM_TAKE_PVP": "1",
    "POL_TM_GETAWAY_TO": "all",
    "POL_TM_ROTATING_STEP": "1",
    "POL_TM_READY_HOLD_LOSER": "0",
    "POL_TM_CONTINUE_PARTIAL": "0",
}
for _k, _v in RELEASE_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

#: The reply templates ship with the title: config/polpro.json in a checkout,
#: /app/polpro.json in the image (the Dockerfile copies it beside the code).
POLPRO_SPEC_CANDIDATES = (
    os.path.normpath(os.path.join(HERE, os.pardir, "config", "polpro.json")),
    os.path.join(HERE, "polpro.json"),
)
