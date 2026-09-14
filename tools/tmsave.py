#!/usr/bin/env python3
"""CLI front end for `services/tmsave.py` -- author / decode `U/g/TM0DataFile`.

WARNING: THE LAYOUT LIVES IN `services/`, NOT HERE, and that is deliberate:
`docker-compose.yml` mounts `./services:/app:ro` and does NOT mount `tools/`, so
anything `tetramaster.py` has to call at run time must be a sibling of it.
`_collection_store` regenerates the save on every purchase, which makes this one
of those. Keeping a second copy of the offsets here is how they would drift.

The whole measured layout, the record map and the two silent-reject gates are
documented in `services/tmsave.py`; read that, not this.

    python tools/tmsave.py --dump  data/resources/16.U_g_TM0DataFile.bin
    python tools/tmsave.py --from-collection data/resources/16.tm_collection.json \\
                           --base data/resources/16.U_g_TM0DataFile.bin --out NEW.bin
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "services"))

from tmsave import (                                        # noqa: E402
    TOTAL, COUNT_OFF, CARDS_OFF, REC, REC_MEM, MAX_CARDS, COUNT_CAP, TYPE_MAX,
    build, decode, decode_card, encode_card, dump, main,
)

if __name__ == "__main__":
    main()
