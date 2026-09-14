#!/usr/bin/env python3
"""tm_boardfont_build.py -- the Tetra Master board's faces (services/boardtm.py).

Two free (SIL OFL 1.1) families, chosen against the client's own lettering
(2026-09-12, side by side with gW201 `head` and gW101 `prize`):

  * Almendra Bold for the TITLE (orange, as TM's "Game results" headings) and
    the gold LABELS (as its "Prize Money" / "Technical"): an uncial-flavoured
    calligraphic serif, the closest free match to both. Kaushan Script read too
    thin and slanted, Uncial Antiqua too wide.
  * M PLUS 1p (the Jan board's text face) for the rows: fixed-width digits, so
    the rank and money columns line up. Medium for cream ink, Bold for names.

Writes into services/boardart/tm/: tm-title / tm-text / tm-text-bold as .woff2
(the page) and .ttf (Pillow, for render.png / Discord; .ttf is not served), and
the licences. Build time only; needs fontTools + brotli.

    python tools/tm_boardfont_build.py Almendra-Bold.ttf OFL-Almendra.txt \\
        MPLUS1p-Medium.ttf MPLUS1p-Bold.ttf OFL-MPLUS1p.txt

Sources: https://github.com/google/fonts/tree/main/ofl/almendra
         https://github.com/google/fonts/tree/main/ofl/mplus1p
"""
import os
import shutil
import sys

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, os.pardir, "services", "boardart", "tm")
#: printable ASCII, plus the Last Week arrows and the separator dot the rows use
TEXT = list(range(0x20, 0x7f)) + [0x25B2, 0x25BC, 0x00B7]
TITLE = list(range(0x20, 0x7f))


def build(src, stem, unicodes):
    f = TTFont(src)
    opts = subset.Options()
    opts.layout_features = ["kern", "liga", "tnum", "palt"]
    opts.name_IDs = ["*"]
    opts.name_languages = ["*"]
    opts.notdef_outline = True
    opts.hinting = False
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=unicodes)
    sub.subset(f)
    ttf = os.path.join(ART, stem + ".ttf")
    f.flavor = None
    f.save(ttf)
    f = TTFont(ttf)
    f.flavor = "woff2"
    f.save(os.path.join(ART, stem + ".woff2"))
    for ext in (".ttf", ".woff2"):
        p = os.path.join(ART, stem + ext)
        print("  %-20s %7d B" % (os.path.basename(p), os.path.getsize(p)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 5:
        raise SystemExit(__doc__)
    title, title_lic, medium, bold, text_lic = argv
    os.makedirs(ART, exist_ok=True)
    build(title, "tm-title", TITLE)
    build(medium, "tm-text", TEXT)
    build(bold, "tm-text-bold", TEXT)
    shutil.copyfile(title_lic, os.path.join(ART, "OFL-Almendra.txt"))
    shutil.copyfile(text_lic, os.path.join(ART, "OFL-MPLUS1p.txt"))


if __name__ == "__main__":
    main()
