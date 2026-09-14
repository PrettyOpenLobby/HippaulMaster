#!/usr/bin/env python3
"""Translate SE's Japanese Tetra Master portal pages in place, reviewably.

Square Enix never deployed `/pml/game/tetra/` on the Western content host -- a
live crawl of wh000.pol.com 404s the whole subtree -- so there is no "original
English" set of these pages to recover. What SE *did* ship in the West is the
artwork: all 177 images under `/pml/game/tetra/` that we serve are byte-identical
to the Western install CD's `viewer/data/pmlus/game/tetra/` set, and four real
English pages (guidebook/gdpm01, guidebook/gdst01, community/fortune/fopm01,
community/fortune/fost02) came out of the same tree when the pmlus cipher fell.

That leaves the remaining pages genuine-but-Japanese. This tool translates the
*visible* strings in them while leaving markup, layout, ids and artwork alone,
which is exactly the shape SE's own English pages have: English body text, and
the original Japanese authoring comments left in place.

    python tools/tetra_i18n.py extract            # build/refresh the string table
    python tools/tetra_i18n.py apply              # write translations into the pages
    python tools/tetra_i18n.py status             # how much is translated
    python tools/tetra_i18n.py revert             # restore from the .ja originals

Files are Shift-JIS (cp932) with no charset meta, matching SE's real English
tetra pages, and are rewritten as cp932 -- English is ASCII, so this is a no-op
for the encoding and every untranslated byte survives untouched.

WHAT COUNTS AS TRANSLATABLE

The page is tokenised into tags and text nodes.

  * a text node holding non-ASCII yields either each double-quoted literal in it
    that holds non-ASCII (that is how `<array>` button tables are written), or,
    if there are none, the whole trimmed node;
  * a tag yields each double-quoted attribute value holding non-ASCII, so
    `alt="..."`, `value="..."` and `<title>` copy are all caught;
  * PML comments are `<!...>` / `<!-- ... -->`, i.e. inside a tag with no quoted
    value, so they are never picked up. They stay Japanese on purpose.

Inline markup inside a string -- `&br;` line breaks, `^03` style-run codes, the
`$var+'...'` concatenations -- is part of the string and must be preserved by the
translation. `check` enforces the ones that are mechanically checkable.

THE STRING TABLE

`tools/tetra_i18n.json` maps the exact Japanese source string to its English
replacement. It is keyed by string, not by file+offset, so the same UI label
translates identically everywhere it appears and the table survives edits to the
pages. An empty value means "not translated yet" and `apply` leaves those alone.
"""
import argparse
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.normpath(
    os.path.join(HERE, "..", "www", "wh000.pol.com", "pml", "game", "tetra"))
DEFAULT_TABLE = os.path.join(HERE, "tetra_i18n.json")
ENCODING = "cp932"

#: Suffix for the pristine Japanese copy kept beside each translated page.
JA_SUFFIX = ".ja"

#: Tag / text tokeniser. PML tags are `<...>`; unlike XML a comment is just a tag
#: whose name starts with `!`, so this one regex covers both.
TOKEN_RE = re.compile(r"<[^>]*>|[^<]+", re.S)
QUOTED_RE = re.compile(r'"([^"]*)"')

#: Structural markup, which must survive translation unchanged in count, kind
#: and order: `&br;` line break, `&style=Bo15;` .. `&style;` style run,
#: `&pre=1;` preformatted, `&image=nakaguro;` inline bullet, and the `^03`
#: inline style-index code. Anything `&name;` / `&name=value;` that is not a
#: character entity is markup.
MARKUP_RE = re.compile(
    r"&(?!squo;|dquo;|quot;|amp;|lt;|gt;|nbsp;|#)\w+(?:=[\w.]+)?;|\^\d\d")

#: Character entities, which stand for one literal character and so may appear
#: in a translation that had none (English needs `&squo;` for an apostrophe --
#: SE's own English pages write "Today&squo;s Fortune").
ENTITY_OK = {"&squo;", "&dquo;", "&quot;", "&#34;", "&amp;", "&lt;", "&gt;", "&nbsp;"}
ENTITY_RE = re.compile(r"&#?[\w=]+;")


def has_jp(s):
    """True if the string carries any non-ASCII, i.e. anything to translate."""
    return any(ord(c) > 0x7F for c in s)


def pml_files(root):
    out = []
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            if n.endswith(".pml"):
                out.append(os.path.join(dirpath, n))
    return sorted(out)


def spans(text):
    """Yield (start, end) of every translatable string in a decoded page.

    Spans are the *inside* of the quotes / the trimmed text node, never the
    delimiters, so applying a replacement can never disturb the markup.
    """
    for tok in TOKEN_RE.finditer(text):
        s, e = tok.span()
        raw = tok.group()
        if raw.startswith("<"):
            # A tag: only quoted attribute values are copy. Comments have none.
            for q in QUOTED_RE.finditer(raw):
                if has_jp(q.group(1)):
                    yield s + q.start(1), s + q.end(1)
        else:
            if not has_jp(raw):
                continue
            # An <array> row is a list of quoted literals with only commas and
            # whitespace between them, so each literal is its own string. But a
            # prose node may *also* contain quoted terms (SE quote UI words:
            # `"Battle"`, `"Trigger"`), and there the sentence around them is
            # copy too -- so fall through to taking the whole node whenever any
            # Japanese sits outside the quotes.
            quoted = [q for q in QUOTED_RE.finditer(raw) if has_jp(q.group(1))]
            outside = QUOTED_RE.sub("", raw)
            if quoted and not has_jp(outside):
                for q in quoted:
                    yield s + q.start(1), s + q.end(1)
            else:
                # Whole node, minus the surrounding whitespace/indentation.
                lead = len(raw) - len(raw.lstrip())
                trail = len(raw) - len(raw.rstrip())
                if e - trail > s + lead:
                    yield s + lead, e - trail


def read_page(path):
    """Decode a page, preferring the pristine .ja copy once one exists."""
    src = path + JA_SUFFIX
    if not os.path.exists(src):
        src = path
    with open(src, "rb") as f:
        return f.read().decode(ENCODING), src


def load_table(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_table(path, table):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(table, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def collect(root):
    """string -> sorted list of pages it appears in."""
    seen = {}
    for path in pml_files(root):
        text, _ = read_page(path)
        rel = os.path.relpath(path, root).replace("\\", "/")
        for a, b in spans(text):
            seen.setdefault(text[a:b], set()).add(rel)
    return {k: sorted(v) for k, v in seen.items()}


def cmd_extract(args):
    found = collect(args.root)
    table = load_table(args.table)
    added = 0
    for s in found:
        if s not in table:
            table[s] = ""
            added += 1
    stale = [s for s in table if s not in found]
    save_table(args.table, table)
    print("pages scanned      : %d" % len(pml_files(args.root)))
    print("distinct strings   : %d" % len(found))
    print("new to the table   : %d" % added)
    print("untranslated       : %d" % sum(1 for v in table.values() if not v))
    if stale:
        print("no longer present  : %d (left in the table)" % len(stale))
    print("\ntable: %s" % args.table)


def cmd_status(args):
    found = collect(args.root)
    table = load_table(args.table)
    done = [s for s in found if table.get(s)]
    todo = [s for s in found if not table.get(s)]
    print("distinct strings : %d" % len(found))
    print("translated       : %d" % len(done))
    print("remaining        : %d" % len(todo))
    by_file = {}
    for s in todo:
        for f in found[s]:
            by_file[f] = by_file.get(f, 0) + 1
    if by_file:
        print("\nremaining, by page:")
        for f, n in sorted(by_file.items(), key=lambda kv: (-kv[1], kv[0])):
            print("  %4d  %s" % (n, f))


def cmd_check(args):
    """Flag translations that would break the page's inline markup."""
    table = load_table(args.table)
    bad = 0
    for ja, en in sorted(table.items()):
        if not en or en == ja:
            # An identity mapping is a deliberate pass-through: the string is
            # already SE's own English (their Fortune Cards page decorates its
            # heading with full-width tildes) and the page stays byte-identical.
            continue
        # `&sp=N;` is letter-spacing SE used to justify two-character Japanese
        # menu labels ("&#x5BFE;&sp=2;&#x6226;"). It is meaningless in English,
        # so a translation is free to drop it.
        drop = lambda xs: [x for x in xs if not x.startswith("&sp=")]
        want, got = drop(MARKUP_RE.findall(ja)), drop(MARKUP_RE.findall(en))
        if want != got:
            bad += 1
            print("MARKUP  %r\n     ja %s\n     en %s\n" % (ja[:60], want, got))
        unknown = [e for e in ENTITY_RE.findall(en)
                   if e not in ENTITY_OK and not MARKUP_RE.fullmatch(e)]
        if unknown:
            bad += 1
            print("UNKNOWN ENTITY %s in %r" % (unknown, en[:80]))
        if has_jp(en):
            bad += 1
            print("NON-ASCII in translation: %r" % en[:80])
        # A raw `"` is only a hazard where the source string is itself a quoted
        # literal -- which is exactly the case where the source has no raw `"`.
        if '"' in en and '"' not in ja:
            bad += 1
            print("QUOTE would terminate the literal: %r" % en[:80])
    print("checked %d translations, %d problem(s)"
          % (sum(1 for v in table.values() if v), bad))
    return 1 if bad else 0


def cmd_apply(args):
    table = load_table(args.table)
    if cmd_check(args):
        print("\nrefusing to apply while checks fail")
        return 1
    print()
    changed = 0
    for path in pml_files(args.root):
        text, src = read_page(path)
        out, last, hits = [], 0, 0
        for a, b in spans(text):
            en = table.get(text[a:b])
            if not en:
                continue
            out.append(text[last:a])
            out.append(en)
            last = b
            hits += 1
        if not hits:
            continue
        out.append(text[last:])
        new = "".join(out)
        # Keep the untouched Japanese original beside the page the first time
        # we rewrite it, so `revert` is exact and diffs stay reviewable.
        ja = path + JA_SUFFIX
        if not os.path.exists(ja):
            shutil.copyfile(src, ja)
        with open(path, "wb") as f:
            f.write(new.encode(ENCODING))
        changed += 1
        print("  %4d strings  %s" % (hits, os.path.relpath(path, args.root)))
    print("\n%d page(s) rewritten" % changed)
    return 0


def cmd_revert(args):
    n = 0
    for path in pml_files(args.root):
        ja = path + JA_SUFFIX
        if os.path.exists(ja):
            shutil.copyfile(ja, path)
            os.remove(ja)
            n += 1
    print("%d page(s) restored to Japanese" % n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=DEFAULT_ROOT, help="tetra portal root")
    ap.add_argument("--table", default=DEFAULT_TABLE, help="string table JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("extract", cmd_extract), ("status", cmd_status),
                     ("check", cmd_check), ("apply", cmd_apply),
                     ("revert", cmd_revert)):
        sub.add_parser(name, help=fn.__doc__ or name).set_defaults(fn=fn)
    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
