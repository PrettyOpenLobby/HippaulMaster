#!/usr/bin/env python3
"""tm_boardart_bake.py -- bake the Tetra Master board's art (services/boardtm.py)
from the PC client's own texture packs (gW*.dat, plain PNG inside).

No SE screenshot of the Rankings or Auction screen exists, and the client
draws both from generic chrome plus text, so the board is composed from that
chrome (the server owner's rule, 2026-09-12: "design from the art"):

    backdrop.png      gW101 menu_bg0..5 -- the six tiles of TM's menu backdrop
                      (the gothic hall), assembled into its 640x448 screen
    panel.png         the sheet: the slate of gW101 `menu` (the clean patch
    side.png          between its painted "Card Data" and "Time Limit"),
                      mirror-tiled, with a light torn rim like the menu's own
    mask_main.png     the sheets' deckled edges, 2x (edge_mask below)
    mask_side.png
    header.png        gW201 `hn`, the wood plank, three-sliced to the header bar
    sep.png           gW101 `menu`'s thin wood bar, three-sliced to a separator
    tab_s0/s1/s3.png  gW101 `score_bd`'s clean board as the tab buttons: normal,
                      pressed (2 px lower), current (the flat highlight)
    rank_1..3.png     gW101 `rank`'s gold 1st / 2nd / 3rd
    arrow.png         gW000 `arrow`, frame 3 (normal) and 4 (lit): page buttons
    arrow_hi.png
    card_000..249.png the card art, cad00000..00249 from gW001..003
    board.json        the layout both the page and render.png draw by, and
                      the client's own English strings for every label

Every word on the board is real text (tools/tm_boardfont_build.py), set live.

Run on the machine with the PC tree, then commit the output:

    python tools/tm_boardart_bake.py
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.pardir, "services", "boardart", "tm")
sys.path.insert(0, HERE)
import tmclientfiles                                             # noqa: E402

#: set by main() from --client: the Tetra Master install's data/ directory
CLIENT_DATA = [None]

#: the client's own English (tmbuild-en/Ranking.BIN and Aucti.BIN, decoded with
#: tmclientfiles.load). Ours only where noted.
STRINGS = {
    "rank": "Rank", "last_week": "Last Week", "player": "Player Name",
    "vs_rating": "VS. Rating", "prize_money": "Prize Money",
    "no_info": "No rankings information.", "hidden": "-------",
    "cards": "Cards", "high_bid": "High Bid", "bidder": "Bidder",
    "time_left": "Time Left", "d": "d", "h": "h", "opening": "Opening Bid",
    "bands": ["All Cards", "Cheap Cards", "Affordable Cards", "Expensive Cards",
              "Exorbitant Cards"],
    # OURS: the client has no word for these
    "games": "Games", "seller": "Seller", "sold_for": "Sold For",
    "winner": "Winner", "ended": "Ended", "sold": "Recently Sold",
    "live_note": "Live standings, not yet published.",
    "no_cards": "No cards are up for auction.", "no_sales": "No recent sales.",
}

#: tab captions, in the Rankings menu's order (Ranking.BIN; Prize Center is
#: the prize shop and has no list), then OUR live tab and the auction
TABS = ["VS. Rating", "Top 30", "Best Rookies", "Grand Total", "Weekly Total",
        "This Week", "Auction"]
#: the window titles for those tabs
TITLES = ["VS. Rating", "Top 30", "Best Rookies", "Grand Total", "Weekly Total",
          "This Week So Far", "Auction"]

#: OURS: the board's layout in the game's 640x448 coordinates. ONE x and ONE
#: alignment per column, shared by the header label and its values (the Jan
#: board's rule, chosen by the server owner 2026-09-12).
LAYOUT = {
    "screen": [640, 448],
    "title": {"x": 320, "baseline": 45, "size": 30, "max_width": 270, "spacing": 1,
              "top": [255, 224, 116], "bottom": [236, 108, 16],
              "stroke": [58, 24, 6], "stroke_px": 3,
              "shadow": [0, 0, 0, 0.6], "shadow_dx": 1, "shadow_dy": 2},
    "arrows": {"prev": [128, 16, 54, 29], "next": [458, 16, 54, 29]},
    "header": {"rect": [16, 58, 608, 28], "img": "header.png"},
    "label": {"size": 16, "fill": [246, 216, 128], "shadow": [36, 18, 4]},
    # medal_h: the gold 1st / 2nd / 3rd drawn this tall, not their native 22
    # (server owner, 2026-09-13: "kinda big relative to everything else")
    "rows": {"y": 88, "pitch": 26, "count": 10, "size": 15, "medal_h": 16,
             "fill": [238, 228, 204], "dim": [170, 160, 140]},
    "auction_rows": {"y": 88, "pitch": 52, "count": 5, "thumb": [36, 48]},
    "sep": {"rect": [16, 348, 608, 10], "img": "sep.png"},
    "info": {"rect": [20, 358, 600, 19], "size": 13, "fill": [214, 190, 130]},
    "status": {"rect": [20, 377, 600, 18], "size": 13, "fill": [184, 172, 146]},
    # pad: the clear space each side of a tab's caption; the shared caption
    # size is whatever the longest one needs to keep it (server owner,
    # 2026-09-13: the long names sat "a bit too tightly" on the boards)
    "tabs": {"x0": 20, "y": 400, "w": 84, "h": 36, "gap": 2, "size": 14, "pad": 12,
             "fill": [[236, 222, 190], [255, 226, 120], [214, 196, 160]]},
    "columns": {
        "rank": {"rank": [56, "right"], "last": [104, "center"],
                 "name": [152, "left"], "value": [604, "right"]},
        "live": {"rank": [56, "right"], "name": [84, "left"], "games": [372, "right"],
                 "rating": [486, "right"], "prize": [604, "right"]},
        "auction": {"card": [22, "left"], "name": [66, "left"], "seller": [236, "left"],
                    "price": [420, "right"], "who": [436, "left"], "left": [604, "right"]},
    },
    "side": {"w": 220, "gap": 14},
}


def _packs(*names):
    """{asset name: (PNG bytes, RGBA image)} for the PNG assets of the named
    PC packs in the client's data/; the first group carrying a name wins
    (gW000 ships two copies of its chrome)."""
    from PIL import Image
    out = {}
    for p in names:
        path = os.path.join(CLIENT_DATA[0], p + ".dat")
        for name, blob in tmclientfiles.png_assets(path).items():
            if name not in out:
                out[name] = (blob, Image.open(io.BytesIO(blob)).convert("RGBA"))
    return out


def _comps(im, thr=8, minarea=12):
    """Connected alpha components (x, y, w, h), in reading order."""
    a = im.getchannel("A")
    W, H = im.size
    px, seen, out = a.load(), set(), []
    for y in range(H):
        for x in range(W):
            if px[x, y] > thr and (x, y) not in seen:
                st, x0, x1, y0, y1, c = [(x, y)], x, x, y, y, 0
                seen.add((x, y))
                while st:
                    cx, cy = st.pop()
                    c += 1
                    x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
                    for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                        if 0 <= nx < W and 0 <= ny < H and px[nx, ny] > thr and (nx, ny) not in seen:
                            seen.add((nx, ny))
                            st.append((nx, ny))
                if c >= minarea:
                    out.append((x0, y0, x1 - x0 + 1, y1 - y0 + 1))
    return sorted(out, key=lambda c: (c[1] // 8, c[0]))


def _trim(im, horizontal_only=False):
    bb = im.getchannel("A").getbbox()
    if not bb:
        return im
    return im.crop((bb[0], 0, bb[2], im.height) if horizontal_only else bb)


def _watch_art(A, save):
    """The WATCH screen's pieces (tm.example.com/watch, 2026-09-13): the
    game's own match board and everything drawn on it. Returns board.json's
    "watch" section, every file with its size so the page can lay out before
    an image has loaded."""
    from PIL import Image
    sizes = {}

    def keep(im, name):
        save(im, name)
        sizes[name] = list(im.size)
        return name
    boards = {}
    # the two boards (gW101 masu_4 / masu_5, four tiles each, 360x448) and
    # their cell frames, measured off the copper lines
    for tiles, stem, xs, ys in (
            ("16", "masu_4", [22, 100, 178, 256, 334], [37, 127, 217, 307, 397]),
            ("25", "masu_5", [8, 76, 144, 212, 280, 348], [22, 102, 182, 262, 342, 422])):
        cv = Image.new("RGBA", (360, 448), (0, 0, 0, 255))
        for part, xy in zip("abcd", [(0, 0), (256, 0), (0, 256), (256, 256)]):
            cv.alpha_composite(A[stem + part][1], xy)
        boards[tiles] = {"img": keep(cv.convert("RGB"), "board%s.png" % tiles),
                         "x": xs, "y": ys, "cols": len(xs) - 1}
    card = (1, 1, 81, 97)                        # the 80x96 card inside its 88x104 cell
    bases = [keep(A[n][1].crop(card), "base_%s.png" % c)
             for n, c in (("base_b", "b"), ("base_r", "r"), ("base_g", "g"))]
    oc = A["on_card1"][1]
    digit_boxes = [(3, 105, 18, 22), (27, 105, 14, 22), (50, 105, 19, 22), (74, 105, 19, 22),
                   (97, 105, 20, 22), (122, 105, 18, 22), (146, 105, 18, 22),
                   (169, 105, 19, 22), (194, 105, 19, 22), (218, 105, 18, 22)]
    pt = A["point"][1]
    score = {c: [keep(_trim(pt.crop((i * 24, top, i * 24 + 24, top + 32)), True),
                      "p%s%d.png" % (c, i)) for i in range(10)]
             for c, top in (("b", 0), ("r", 32), ("g", 64))}
    d1 = A["res_drw1"][1].crop((0, 0, 144, 72))
    d2 = A["res_drw2"][1].crop((0, 0, 144, 72))
    draw = Image.new("RGBA", (288, 72), (0, 0, 0, 0))
    draw.alpha_composite(d1, (0, 0))
    draw.alpha_composite(d2, (144, 0))           # "DRA" + "AW": the two halves tile
    e0 = A["effec_00"][1]
    frames = [c for c in _comps(e0) if c[1] < 100 and c[3] >= 17]
    boom = (sorted([c for c in frames if c[1] < 48], key=lambda c: c[0])      # growing
            + sorted([c for c in frames if c[1] >= 48], key=lambda c: c[0]))  # fading
    return {
        "boards": boards, "bases": bases,
        "back": keep(A["base_bk"][1].crop(card), "base_bk.png"),
        "blocks": {"plain": keep(A["base_st1"][1].crop(card), "block_st.png"),
                   "chance": keep(A["block_ch"][1].crop(card), "block_ch.png"),
                   # the rotating block's FRAME (the green orb is baked in); its
                   # arrow and glow are separate layers -- see "rot" below
                   "rot": keep(A["through"][1].crop((0, 0, 82, 98)), "block_rot.png")},
        # the rotating block (0x8009, draw branch 0xB721F): the frame above, the
        # 30x30 arrow (88,0) with its base on the centre, turned to
        # ((phase-4)&7)*45 deg clockwise from up (0xC1A9C) -- where the ray
        # fires -- and the 40x40 glow (92,60) pulsing additively (0xB76DF)
        "rot": {"arrow": keep(A["through"][1].crop((88, 0, 118, 30)), "rot_arrow.png"),
                "glow": keep(A["through"][1].crop((92, 60, 132, 100)), "rot_glow.png")},
        # WHO GOES FIRST, the reel (states 0x1B..0x1D, drawn at 0xD4F0A..0xD5477):
        # 56x200 reel frames (2 players a..f, 3 players a..i -- 3 frames a
        # digit), the arm with its pointer, the tassel, two ornaments
        "roulette": {
            "reel2": [keep(A["roure_2" + c][1].crop((0, 0, 56, 200)), "reel2%s.png" % c) for c in "abcdef"],
            "reel3": [keep(A["roure_3" + c][1].crop((0, 0, 56, 200)), "reel3%s.png" % c) for c in "abcdefghi"],
            "arm": keep(A["roure_a1"][1].crop((0, 32, 207, 103)), "rl_arm.png"),
            "ornA": keep(A["roure_a1"][1].crop((0, 0, 95, 31)), "rl_orn_a.png"),
            "ornB": keep(A["roure_a1"][1].crop((96, 0, 191, 31)), "rl_orn_b.png"),
            "tassel": keep(A["roure_a2"][1].crop((0, 0, 95, 216)), "rl_tassel.png")},
        "hilight": keep(A["hilight"][1].crop((1, 1, 85, 101)), "hilight.png"),
        "digits": [keep(oc.crop((x, y, x + w, y + h)), "dy%d.png" % i)
                   for i, (x, y, w, h) in enumerate(digit_boxes)],
        "combo": keep(oc.crop((128, 160, 240, 192)), "combo.png"),
        "score": score,
        "plates": {c: keep(A["name"][1].crop((4, y, 159, y + 20)), "plate_%s.png" % c)
                   for c, y in (("n", 1), ("b", 25), ("r", 49), ("g", 73))},
        "start": keep(_trim(A["game_st"][1]), "game_start.png"),
        # GAME START as the client builds it (0xFA160): "GAME" (0,0)-(248,72)
        # slides in from the left, "START!" (0,72)-(248,144) from the right
        "start_halves": [keep(A["game_st"][1].crop((0, 0, 248, 72)), "gs_game.png"),
                         keep(A["game_st"][1].crop((0, 72, 248, 144)), "gs_start.png")],
        # the WIN banner's two layers (0xCCC40): the face (0,0,247,79), drawn
        # additive, over its shadow (0,80,247,87) at a third of the alpha
        "win_layers": [keep(A["res_win"][1].crop((0, 0, 247, 79)), "win_face.png"),
                       keep(A["res_win"][1].crop((0, 80, 247, 167)), "win_shadow.png")],
        "win": keep(_trim(A["res_win"][1].crop((0, 0, 248, 80))), "win.png"),
        "draw": keep(_trim(draw), "draw.png"),
        "boom": [keep(e0.crop((x, y, x + w, y + h)), "boom%d.png" % i)
                 for i, (x, y, w, h) in enumerate(boom)],
        "hand": keep(_trim(A["cursor"][1].crop((0, 0, 40, 40))), "hand.png"),
        # gW000 on_card1, measured with _comps: the three SPECIAL TILE icons
        # (keyed by the ability they hand out -- 1 attack up, 2 defense up,
        # 3 all arrows; tmbattle SPECIAL_CODES 3/4/5), the card face's stat
        # glyphs (0-9, a star, P M X A) and the eight arrow tips the game draws
        # ON the card, clockwise from north like the arrow bits
        # the FULL 74x70 cell the client draws, not a tight crop: u = 80*(type-1),
        # v = 0 (TM.dll 0xB49BC..0xB4C5E on a card, 0xB6DFD on an empty tile)
        "icons": {str(ab): keep(oc.crop((80 * (ab - 1), 0, 80 * (ab - 1) + 74, 70)),
                                "sp_%d.png" % ab) for ab in (1, 2, 3)},
        # the battle numbers as the card renderer draws them (0xB5C34): digit d
        # is the 24x24 cell at (d*24, 104)
        "num": [keep(oc.crop((d * 24, 104, d * 24 + 24, 128)), "nm%d.png" % d) for d in range(10)],
        # the COMBO count's digits: 32x32 at u = (d%6)*32, v = (d//6 + 4)*32
        # (state 14, 0xCFFC7); the word itself is `combo` (128,160,112x32)
        "combo_digits": [keep(oc.crop(((d % 6) * 32, (d // 6 + 4) * 32,
                                       (d % 6) * 32 + 32, (d // 6 + 4) * 32 + 32)),
                              "cd%d.png" % d) for d in range(10)],
        # the renderer's own CELLS, not tight crops: TM.dll 0xB528E..0xB52CD
        # draws stat glyph g from u = g*16, v = 72..88 (16x16); 0xB4F58..0xB4FB8
        # draws arrow bit b from u = b*16 + 1, v = 89..105. Positions: see
        # "card_face" below.
        "glyphs": {ch: keep(oc.crop((g * 16, 72, g * 16 + 16, 88)), "st_%s.png" % nm)
                   for g, (ch, nm) in enumerate(zip(
                       "0123456789*PMXA",
                       [str(i) for i in range(10)] + ["star", "p", "m", "x", "a"]))},
        "card_arrows": [keep(oc.crop((b * 16 + 1, 89, b * 16 + 17, 105)), "ca%d.png" % b)
                        for b in range(8)],
        # where the renderer puts them, as offsets from the card's CENTRE in
        # its 80x96 space, each a 16x16 quad centred there. Stat glyph centres:
        # the table at VA 0x51C3430 (layout kind 0; kinds 1/2 differ only in y:
        # 21 / 28). Arrow centres: 0x51C339C, kind 0 = 37 across, 45 down.
        # Which kind the 5x5 board uses is not traced; kind 0 is drawn everywhere.
        "card_face": {"stats_x": [-18, -6, 6, 18], "stats_y": 26, "arrow_dx": 37, "arrow_dy": 45,
                      "cell": 16},
        "fx": _fx_frames(A, keep),
        "sizes": sizes,
    }


def _fx_frames(A, keep):
    """The chance / rotating block animations (gW101 group 1015), cut on the
    grids TM.dll draws them with: 0xF219A..0xF21C1 takes frame f from column
    f % per_row, row f // per_row. Which effect uses which sheet is the
    effect constructor 0xF0480's switch (its jump table 0xF1314):
      power down (state 0x12, 0xD08A0): dark smoke, then the reaper (type 14)
      power up   (0x14, 0xD0BDB): white smoke, the girl (15), creatures (16)
      take       (0x16, 0xD110C): smoke, crystals (6), creatures per neighbour
                 in its owner's colour (19)
      scramble   (0x19, 0xD18B1): smoke, the big orb (9), the redeal
      rotating block (0x1E..0x20): a burst (22), the bolt (23), a flash (24)
    Frame sizes from the constructor; the crystal rows are this bake's pick
    of effec_04, not a traced cell."""
    def grid(sheet, name, w, h, n, per_row, y0=0, x0=0):
        im = A[sheet][1]
        return [keep(im.crop((x0 + (i % per_row) * w, y0 + (i // per_row) * h,
                              x0 + (i % per_row) * w + w, y0 + (i // per_row) * h + h)),
                     "fx_%s_%02d.png" % (name, i)) for i in range(n)]
    bolt = A["effec_08"][1]
    return {
        # the battle burst, type 1 (state 9, 0xCF543): effec_00's 48x48 cells,
        # 5 per row, 12 frames at 2 ticks each
        "hit": grid("effec_00", "hit", 48, 48, 12, 5),
        "smoke": grid("effec_01", "smoke", 48, 48, 12, 5),
        "reaper": grid("effec_03", "reaper", 80, 80, 6, 3),
        "girl": grid("effec_07", "girl", 80, 80, 6, 3),
        "up": grid("effec_06", "up", 60, 56, 4, 4),
        "take": grid("effec_06", "take", 40, 44, 6, 6, y0=56),
        "shards": grid("effec_04", "shards", 32, 32, 8, 8, y0=32),
        "orb": grid("effec_04", "orb", 64, 64, 2, 2, y0=96),
        "burst": grid("effec_05", "burst", 64, 64, 4, 4),
        # the ray, type 23: effec_08's 128x40 cells, 2 per row
        "bolt": [keep(bolt.crop(((i % 2) * 128, (i // 2) * 40, (i % 2) * 128 + 128,
                                 (i // 2) * 40 + 40)), "fx_bolt_%02d.png" % i) for i in range(6)],
        # take's bouncing chips, types 6/7: effec_04 from (128, 96)
        "chips": grid("effec_04", "chips", 32, 32, 4, 4, y0=96, x0=128),
    }


def _labels(save):
    """The words the game floats over a card, from the ENGLISH client's
    on_card2 (the JP pack paints them in Japanese; same layout -- checked
    2026-09-13). Rects from TM.dll: power down (96,136,112x24) state 24,
    power up (0,136,90x24), ColorShift (0,48,72x24) state 23, Scramble
    (0,24,76x24) state 26; a card played on a special tile, state 1
    (0xCE304): Offense Up (80,24,101x24), Defense Up (80,48,113x24),
    MAX Arrows! (72,0,128x24). Returns ({key: file}, sizes)."""
    from PIL import Image
    path = os.path.join(CLIENT_DATA[0], "gW101.dat")
    blob = tmclientfiles.png_assets(path).get("on_card2")
    if blob is None:
        raise SystemExit("gW101.dat carries no on_card2 sheet")
    sheet = Image.open(io.BytesIO(blob)).convert("RGBA")
    out, sizes = {}, {}
    for key, (x, y, w, h) in (("power_down", (96, 136, 112, 24)), ("power_up", (0, 136, 90, 24)),
                              ("take", (0, 48, 72, 24)), ("scramble", (0, 24, 76, 24)),
                              ("sp1", (80, 24, 101, 24)), ("sp2", (80, 48, 113, 24)),
                              ("sp3", (72, 0, 128, 24))):
        fn = "lbl_%s.png" % key
        save(sheet.crop((x, y, x + w, y + h)), fn)
        out[key], sizes[fn] = fn, [w, h]
    return out, sizes


def _faces(src):
    """The PlayOnline PORTRAIT sheets, copied verbatim into boardart/faces/:
    a profile's `z_ficon` = sheet * 8 + tile picks tile `tile` of
    `hnf<sheet>.png` (app.dll 0x4a7d199). The
    plain-PNG set decoded from the Viewer (192 sheets); boardtm's /face.png
    cuts one tile out. Returns the number of sheets."""
    import shutil
    dst = os.path.join(os.path.dirname(OUT), "faces")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for fn in sorted(os.listdir(src)):
        if fn.startswith("hnf") and fn.endswith(".png"):
            shutil.copyfile(os.path.join(src, fn), os.path.join(dst, fn))
            n += 1
    return n


def _com_art(A, save):
    """The VS. COM opponents: {"<Com= value>": {"name", "face"}}. The client's
    `/Com=` is the PlPrm.BIN record index, and TM.dll's select screen uses the
    same index for the record (0xECA72), the portrait `fac%05d` (0xECA94, in
    gW080) and the unlock check (0xECAE0). Record 0 is the locked `???`.
    Names come from the US PC client's PlPrm.BIN (+0x0C)."""
    path = os.path.join(CLIENT_DATA[0], "PlPrm.BIN")
    names = {rec: txt.split(b"\x00")[0].decode("latin1").strip()
             for rec, off, txt in tmclientfiles.slot_strings(tmclientfiles.load(path)) if off == 0x0C}
    out, sizes = {}, {}
    for ci in range(1, 31):
        face = A.get("fac%05d" % ci)
        if face is None or not names.get(ci):
            continue
        fn = "face_%02d.png" % ci
        save(face[1], fn)
        sizes[fn] = list(face[1].size)
        out[str(ci)] = {"name": names[ci], "face": fn}
    return out, sizes


def _screen(A, stem):
    """The six tiles of a 640x448 screen (256+256+128 by 256+192)."""
    from PIL import Image
    cv = Image.new("RGBA", (640, 448), (0, 0, 0, 255))
    for i, xy in enumerate([(0, 0), (256, 0), (512, 0), (0, 256), (256, 256), (512, 256)]):
        cv.alpha_composite(A["%s%d" % (stem, i)][1], xy)
    return cv


def edge_mask(w, h, seed, scale=2, radius=11, base=1.5, amp=2.4, jitter=0.45):
    """The page's sheet EDGE for a w x h panel, as an RGBA mask (white, alpha
    = paper) at `scale`x: a rounded rectangle whose edge wanders inward by
    `base` + a slow wave of up to `amp` px + a fine `jitter`, feathered by a
    pixel -- a deckled paper edge, so the window and the sidebar read as
    parchment lying on the felt, not two rectangles.
    The waves close exactly round the perimeter, so there is no seam."""
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    W, H, R, step = w * scale, h * scale, radius * scale, 2.0

    def line(x0, y0, x1, y1, nx, ny):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        return [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n, nx, ny) for i in range(n)]

    def arc(cx, cy, a0, a1):
        n = max(4, int(abs(a1 - a0) * R / step))
        return [(cx + R * math.cos(a), cy + R * math.sin(a), math.cos(a), math.sin(a))
                for a in (a0 + (a1 - a0) * i / n for i in range(n))]

    path = (line(R, 0, W - R, 0, 0, -1) + arc(W - R, R, -math.pi / 2, 0)
            + line(W, R, W, H - R, 1, 0) + arc(W - R, H - R, 0, math.pi / 2)
            + line(W - R, H, R, H, 0, 1) + arc(R, H - R, math.pi / 2, math.pi)
            + line(0, H - R, 0, R, -1, 0) + arc(R, R, math.pi, 1.5 * math.pi))
    dist, s = [], 0.0
    for i, (x, y, _nx, _ny) in enumerate(path):
        if i:
            s += math.hypot(x - path[i - 1][0], y - path[i - 1][1])
        dist.append(s)
    total = s + math.hypot(path[0][0] - path[-1][0], path[0][1] - path[-1][1])
    # whole cycles round the perimeter: periods of roughly 60..300 px
    waves = [(max(1, round(total / scale / rnd.uniform(60, 300))), rnd.uniform(0, 2 * math.pi),
              rnd.uniform(0.3, 1.0)) for _ in range(4)]
    norm = sum(a for _k, _p, a in waves)
    poly = []
    for (x, y, nx, ny), d in zip(path, dist):
        wave = sum(a * (0.5 + 0.5 * math.sin(2 * math.pi * k * d / total + p))
                   for k, p, a in waves) / norm
        inset = max(0.5, base + amp * wave + rnd.uniform(-jitter, jitter)) * scale
        poly.append((x - nx * inset, y - ny * inset))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).polygon(poly, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(0.7 * scale))
    white = Image.new("L", (W, H), 255)
    return Image.merge("RGBA", (white, white, white, m))




def _slate(menu, w, h, seed):
    """The sheet: menu's slate, mirror-tiled, with a light torn rim that
    follows the deckled mask the page cuts it with, and a soft inner shade."""
    from PIL import Image, ImageChops, ImageFilter
    patch = menu.crop((10, 106, 154, 192)).convert("RGB")
    patch = patch.resize((patch.width * 2, patch.height * 2), Image.BICUBIC)
    pw, ph = patch.size
    tile = Image.new("RGB", (pw * 2, ph * 2))
    tile.paste(patch, (0, 0))
    tile.paste(patch.transpose(Image.FLIP_LEFT_RIGHT), (pw, 0))
    tile.paste(patch.transpose(Image.FLIP_TOP_BOTTOM), (0, ph))
    tile.paste(patch.transpose(Image.ROTATE_180), (pw, ph))
    cv = Image.new("RGB", (w, h))
    for y in range(0, h, tile.height):
        for x in range(0, w, tile.width):
            cv.paste(tile, (x, y))
    mask2x = edge_mask(w, h, seed=seed)
    a = mask2x.getchannel("A").resize((w, h), Image.LANCZOS)
    inner = a.filter(ImageFilter.MinFilter(5))
    rim = ImageChops.subtract(a, inner).point(lambda v: int(v * 0.6))
    shade = ImageChops.invert(a.filter(ImageFilter.MinFilter(13)).filter(
        ImageFilter.GaussianBlur(9))).point(lambda v: int(v * 0.55))
    cv = Image.composite(Image.new("RGB", (w, h), (0, 0, 0)), cv, shade)
    cv = Image.composite(Image.new("RGB", (w, h), (206, 196, 176)), cv, rim)
    return cv, mask2x


def _three_slice(im, w, h, cap):
    """A plank stretched to w x h, its two ends kept."""
    from PIL import Image
    im = im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS)
    c = min(cap, im.width // 3)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(im.crop((0, 0, c, h)), (0, 0))
    out.alpha_composite(im.crop((im.width - c, 0, im.width, h)), (w - c, 0))
    out.alpha_composite(im.crop((c, 0, im.width - c, h)).resize((w - 2 * c, h), Image.LANCZOS), (c, 0))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--client", required=True,
                    help="your Tetra Master install (the directory holding data/)")
    ap.add_argument("--faces", help="a directory of hnf*.png portrait sheets decoded "
                                    "from your Viewer; without it the board draws no portraits")
    args = ap.parse_args()
    data = os.path.join(args.client, "data")
    if not os.path.isfile(os.path.join(data, "gW000.dat")):
        raise SystemExit("no data/gW000.dat under %s" % args.client)
    CLIENT_DATA[0] = data
    from PIL import Image, ImageEnhance
    os.makedirs(OUT, exist_ok=True)
    A = _packs("gW000", "gW101", "gW201", "gW001", "gW002", "gW003", "gW080")
    save = lambda im, n: im.save(os.path.join(OUT, n), optimize=True)   # noqa: E731

    save(_screen(A, "menu_bg").convert("RGB"), "backdrop.png")
    menu = A["menu"][1]
    for n, (w, h, seed) in (("panel", (640, 448, 5)), ("side", (220, 448, 9))):
        sheet, mask = _slate(menu, w, h, seed)
        save(sheet, n + ".png")
        save(mask, "mask_main.png" if n == "panel" else "mask_side.png")

    hdr = LAYOUT["header"]["rect"]
    save(_three_slice(A["hn"][1].crop((1, 1, 178, 33)), hdr[2], hdr[3], 22), "header.png")
    sep = LAYOUT["sep"]["rect"]
    save(_three_slice(menu.crop((1, 25, 149, 37)), sep[2], sep[3], 16), "sep.png")

    T = LAYOUT["tabs"]
    board = A["score_bd"][1].crop((6, 73, 161, 137)).resize((T["w"], T["h"] - 4), Image.LANCZOS)
    for st, (dy, k) in {0: (0, 1.0), 1: (2, 0.8), 3: (0, 1.32)}.items():
        cv = Image.new("RGBA", (T["w"], T["h"]), (0, 0, 0, 0))
        b = board.copy()
        rgb = ImageEnhance.Brightness(b.convert("RGB")).enhance(k)
        if st == 3:
            rgb = Image.blend(rgb, Image.new("RGB", rgb.size, (255, 196, 90)), 0.12)
        rgb.putalpha(b.getchannel("A"))
        cv.alpha_composite(rgb, (0, dy + 1))
        save(cv, "tab_s%d.png" % st)

    for i, box in enumerate(((178, 1, 215, 23), (177, 25, 223, 47), (177, 49, 220, 71))):
        save(A["rank"][1].crop(box), "rank_%d.png" % (i + 1))
    save(A["arrow"][1].crop((1, 2, 55, 31)), "arrow.png")
    save(A["arrow"][1].crop((1, 33, 55, 62)), "arrow_hi.png")

    # the card BASE a card is drawn on in the game (gW000 base_b, the player's
    # blue; 80x96 inside its 88x104 cell): the Discord listing's card sits on it
    save(A["base_b"][1].crop((1, 1, 81, 97)), "card_frame.png")
    for cid in range(250):
        blob = A["cad%05d" % cid][0]
        with open(os.path.join(OUT, "card_%03d.png" % cid), "wb") as fh:
            fh.write(blob)

    watch = _watch_art(A, save)
    watch["coms"], face_sizes = _com_art(A, save)
    watch["sizes"].update(face_sizes)
    watch["labels"], label_sizes = _labels(save)
    watch["sizes"].update(label_sizes)
    out = dict(LAYOUT)
    out.update({
        "watch": watch,
        "about": "Tetra Master board art + layout, baked by tools/tm_boardart_bake.py "
                 "from the PC client's gW*.dat packs",
        "tabs_caption": TABS, "titles": TITLES, "strings": STRINGS,
        "ranks": ["rank_1.png", "rank_2.png", "rank_3.png"],
        "layers": {"backdrop": "backdrop.png", "panel": "panel.png", "side": "side.png",
                   "header": "header.png", "sep": "sep.png", "arrow": "arrow.png",
                   "arrow_hi": "arrow_hi.png",
                   "tab": {"s0": "tab_s0.png", "s1": "tab_s1.png", "s3": "tab_s3.png"}},
        "fonts": {"title": "tm-title.ttf", "text": "tm-text.ttf", "bold": "tm-text-bold.ttf"},
    })
    with open(os.path.join(OUT, "board.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    n = sum(1 for _ in os.listdir(OUT))
    size = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT))
    print("baked %d files, %d KB, into %s" % (n, size // 1024, os.path.normpath(OUT)))
    if args.faces:
        print("copied %d portrait sheets into boardart/faces" % _faces(args.faces))
    else:
        print("no --faces directory: the board will draw no portraits")


if __name__ == "__main__":
    main()
