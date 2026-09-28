#!/usr/bin/env python3
"""Split CrystalMaster's services/tetramaster.py into the services/tmgame/ package.

Derived from the tool that split OpenLobby's responders.py into its core/
package. It is deterministic: the same source and the same map give the same
package, so the split can be regenerated after new code has been merged into
the flat file.

    python split_tetramaster.py --src <flat tetramaster.py> --map split_tetramaster_map.txt
        --out <repo>/services/tmgame --facade <repo>/services/tetramaster.py
    python split_tetramaster.py --src ... --map ... --check    # report only, write nothing
    python split_tetramaster.py --src ... --ranges ranges.txt  # derive a map from line ranges

--src and --facade may be the same path: the flat file is read completely
before anything is written. To regenerate after the flat file changed, restore
the flat file first (`git show <commit>:services/tetramaster.py`).

How it works
- Every top-level statement of the flat module is assigned to one module of
  the package by the map (defs and module globals by NAME; unnamed statements
  by a `~prefix` of their first line). The comment block above a statement
  travels with it, so the banners and the per-function commentary survive.
- Inside a moved statement, every reference to a top-level name that now lives
  in ANOTHER module is rewritten to `<module>.<name>`. Scope analysis follows
  Python's rules (function scopes nest, class bodies do not), so a local that
  shadows a module name is left alone.
- Import-like statements (plain imports, and `try: import x / except: x = None`
  blocks) go to deps.py; a module that uses such a name gets the plain
  import, or `from .deps import x` for the optional ones.
- The flat module becomes a facade: `import tetramaster` keeps working for
  every tool and test, reads AND writes (`tm._say = fake`) are forwarded to the
  owning module, and `python tetramaster.py --selftest` still runs.

Changes over the OpenLobby split tool
- Names are not hardcoded: package = basename of --out, facade module =
  basename of --facade. The facade's own `if __name__ == "__main__":` block is
  kept (rewritten like any moved statement) instead of `core.main.main()`.
- The flat module's docstring is kept: it opens the package's __init__.py,
  after the module layout.
- `global X` is supported. A function that declares a name global which now
  lives in another module has its loads and stores rewritten to
  `<module>.X`, and the name is dropped from the `global` statement (the line
  goes if nothing is left). A name global in its own module is untouched.
  (tetramaster.py has eight `global` statements; three of them cross modules,
  two in the selftests and one where the flag sits far from its only user.)
- `globals()["X"]` with a constant key naming another module's name becomes
  `<module>.X`, and `globals().get("X"[, d])` becomes
  `getattr(<module>, "X"[, d])`. The selftests use both to swap a helper out.
- `__file__` in moved code meant the flat file: it becomes `deps.FACADE_FILE`,
  the facade's path, so data files beside it (services/tmdata/) are found as
  before.
- f-string positions: on Python < 3.12 the ast gives no reliable positions for
  names inside f-strings; such a name is located by searching its source line
  for the identifier as a whole word, and refused if that is ambiguous.
"""
import argparse
import ast
import collections
import io
import os
import re
import sys
import textwrap

BANNER = re.compile(r"^# -{20,} #\s*$")


# --------------------------------------------------------------------------- #
# Source model
# --------------------------------------------------------------------------- #
class Stmt:
    __slots__ = ("node", "names", "kind", "first", "last", "span_first", "text", "module")

    def __init__(self, node, names, kind, first, last):
        self.node = node
        self.names = names        # top-level names this statement binds
        self.kind = kind          # def | assign | import | optimport | other
        self.first = first        # first line of the statement proper
        self.last = last
        self.span_first = None    # first line including the comment block above
        self.text = None
        self.module = None


def bound_targets(target):
    out = []
    for n in ast.walk(target):
        if isinstance(n, ast.Name):
            out.append(n.id)
    return out


def classify(node):
    """(names, kind) for a top-level statement."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name], "def"
    if isinstance(node, ast.Assign):
        names = []
        for t in node.targets:
            names += bound_targets(t)
        return names, "assign"
    if isinstance(node, ast.AnnAssign):
        return bound_targets(node.target), "assign"
    if isinstance(node, ast.AugAssign):
        return [], "other"
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [(a.asname or a.name).split(".")[0] for a in node.names], "import"
    if isinstance(node, ast.Try):
        body_imports = all(isinstance(b, (ast.Import, ast.ImportFrom)) for b in node.body)
        if body_imports and node.handlers:
            names = []
            for b in node.body:
                names += [(a.asname or a.name).split(".")[0] for a in b.names]
            return names, "optimport"
    if isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
        # `if X: NAME = a / else: NAME = b` binds NAME at module level too
        return module_level_stores(node), "other"
    return [], "other"


def module_level_stores(node):
    """Names a compound top-level statement assigns in MODULE scope (not in
    the functions, classes, lambdas or comprehensions inside it), in order."""
    out = []

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                              ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                continue
            if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Store) and c.id not in out:
                out.append(c.id)
            walk(c)
    walk(node)
    return out


def load_source(path):
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    stmts = []
    for node in tree.body:
        names, kind = classify(node)
        first = node.lineno
        # decorators sit above the def line
        for d in getattr(node, "decorator_list", []):
            first = min(first, d.lineno)
        stmts.append(Stmt(node, names, kind, first, node.end_lineno))
    prev_end = 0
    for s in stmts:
        s.span_first = prev_end + 1
        s.text = "".join(lines[s.span_first - 1:s.last])
        prev_end = s.last
    trailing = "".join(lines[prev_end:])
    return text, lines, tree, stmts, trailing


# --------------------------------------------------------------------------- #
# The map
# --------------------------------------------------------------------------- #
def read_map(path):
    """{module: {"doc": str, "names": set, "prefixes": [str]}} in file order."""
    mods = collections.OrderedDict()
    cur = None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\[([A-Za-z_][A-Za-z0-9_]*)\]\s*(.*)$", line)
        if m:
            cur = m.group(1)
            mods[cur] = {"doc": m.group(2).strip(), "names": set(), "prefixes": []}
            continue
        if cur is None:
            raise SystemExit(f"{path}: entry before any [module] header: {line!r}")
        if line.startswith("~"):
            mods[cur]["prefixes"].append(line[1:].rstrip())
        else:
            for tok in line.split():
                mods[cur]["names"].add(tok)
    return mods


def assign_modules(stmts, mods, facade_prefixes):
    owner = {}
    for mod, spec in mods.items():
        for n in spec["names"]:
            if n in owner:
                raise SystemExit(f"map: {n} listed in both {owner[n]} and {mod}")
            owner[n] = mod
    unmapped = []
    for s in stmts:
        head = s.text.splitlines()[-(s.last - s.first + 1)] if s.text else ""
        first_line = "".join(s.text.splitlines(keepends=True)[s.first - s.span_first:s.first - s.span_first + 1]).rstrip()
        if s.kind in ("import", "optimport"):
            s.module = "deps"
            continue
        if any(first_line.startswith(p) for p in facade_prefixes):
            s.module = "__facade__"
            continue
        hit = None
        for mod, spec in mods.items():
            if any(first_line.startswith(p) for p in spec["prefixes"]):
                hit = mod
                break
        if hit is None and s.names:
            owners = {owner.get(n) for n in s.names}
            owners.discard(None)
            if len(owners) > 1:
                raise SystemExit(f"L{s.first}: names {s.names} map to several modules {owners}")
            if owners:
                hit = owners.pop()
        if hit is None:
            if s.kind == "other" and isinstance(s.node, ast.Expr) and isinstance(
                    getattr(s.node, "value", None), ast.Constant) and s is stmts[0]:
                s.module = "__facade__"      # the module docstring
                continue
            unmapped.append(s)
            continue
        s.module = hit
    return owner, unmapped


# --------------------------------------------------------------------------- #
# Scope analysis
# --------------------------------------------------------------------------- #
def local_bindings(scope_node):
    """Names bound directly in this function/lambda/comprehension scope."""
    bound = set()
    args = getattr(scope_node, "args", None)
    if args is not None:
        for a in args.posonlyargs + args.args + args.kwonlyargs:
            bound.add(a.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    if isinstance(scope_node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        for g in scope_node.generators:
            bound |= set(bound_targets(g.target))

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(c.name)
                for d in c.decorator_list:
                    walk(d)
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in c.args.defaults + c.args.kw_defaults:
                        if d is not None:
                            walk(d)
                continue
            if isinstance(c, ast.Lambda):
                for d in c.args.defaults + c.args.kw_defaults:
                    if d is not None:
                        walk(d)
                continue
            if isinstance(c, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                # the first iterable is evaluated in the enclosing scope
                walk(c.generators[0].iter)
                continue
            if isinstance(c, ast.Name) and isinstance(c.ctx, (ast.Store, ast.Del)):
                bound.add(c.id)
            elif isinstance(c, ast.ExceptHandler) and c.name:
                bound.add(c.name)
            elif isinstance(c, (ast.Import, ast.ImportFrom)):
                for a in c.names:
                    bound.add((a.asname or a.name).split(".")[0])
            elif isinstance(c, ast.NamedExpr):
                bound.add(c.target.id)
            elif isinstance(c, ast.MatchAs) and c.name:
                bound.add(c.name)
            elif isinstance(c, ast.MatchStar) and c.name:
                bound.add(c.name)
            elif isinstance(c, ast.MatchMapping) and c.rest:
                bound.add(c.rest)
            walk(c)

    if isinstance(scope_node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        for g in scope_node.generators:
            for cond in g.ifs:
                walk(cond)
            if g is not scope_node.generators[0]:
                walk(g.iter)
        if isinstance(scope_node, ast.DictComp):
            walk(scope_node.key)
            walk(scope_node.value)
        else:
            walk(scope_node.elt)
    else:
        walk(scope_node)
    # `global X` makes X module scope for this function even where it is
    # assigned; nested functions keep their own declarations
    return bound - declared_globals(scope_node)


def declared_globals(scope_node):
    """Names a function scope declares `global` (not counting nested scopes)."""
    out = set()

    def walk(n):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(c, ast.Global):
                out.update(c.names)
            walk(c)
    if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        walk(scope_node)
    return out


def global_name_refs(stmt_node):
    """Yield every Name node in the statement that resolves to MODULE scope."""
    out = []

    def visit(n, fn_scopes, in_class):
        # fn_scopes: list of sets of names bound in enclosing FUNCTION scopes
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for d in getattr(c, "decorator_list", []):
                    visit_expr(d, fn_scopes)
                for d in c.args.defaults + c.args.kw_defaults:
                    if d is not None:
                        visit_expr(d, fn_scopes)
                for a in c.args.posonlyargs + c.args.args + c.args.kwonlyargs:
                    if a.annotation is not None:
                        visit_expr(a.annotation, fn_scopes)
                if getattr(c, "returns", None) is not None:
                    visit_expr(c.returns, fn_scopes)
                inner = local_bindings(c)
                body = c.body if isinstance(c.body, list) else [c.body]
                for b in body:
                    visit(b, fn_scopes + [inner], False)
                    if isinstance(b, ast.Name):
                        resolve(b, fn_scopes + [inner])
                continue
            if isinstance(c, ast.ClassDef):
                visit_class(c, fn_scopes)
                continue
            if isinstance(c, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                visit(c.generators[0].iter, fn_scopes, in_class)
                if isinstance(c.generators[0].iter, ast.Name):
                    resolve(c.generators[0].iter, fn_scopes)
                inner = local_bindings(c)
                scopes = fn_scopes + [inner]
                parts = [g.ifs for g in c.generators] + [[g.iter] for g in c.generators[1:]]
                elts = [c.key, c.value] if isinstance(c, ast.DictComp) else [c.elt]
                for group in parts + [elts]:
                    for p in group:
                        visit(p, scopes, False)
                        if isinstance(p, ast.Name):
                            resolve(p, scopes)
                continue
            if isinstance(c, ast.Name):
                resolve(c, fn_scopes)
            visit(c, fn_scopes, in_class)

    def visit_expr(node, fn_scopes):
        if isinstance(node, ast.Name):
            resolve(node, fn_scopes)
        else:
            visit(node, fn_scopes, False)

    def resolve(name_node, fn_scopes):
        for sc in fn_scopes:
            if name_node.id in sc:
                return
        out.append(name_node)

    def visit_class(c, fn_scopes):
        for d in c.decorator_list:
            visit_expr(d, fn_scopes)
        for b in c.bases + [k.value for k in c.keywords]:
            visit_expr(b, fn_scopes)
        # names assigned directly in the class body are class attributes: they
        # shadow module names for the body's own statements, not for methods
        class_bound = set()
        for b in c.body:
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                class_bound.add(b.name)
                continue
            for n in ast.walk(b):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    class_bound.add(n.id)
        for b in c.body:
            if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                visit(ast.Module(body=[b], type_ignores=[]), fn_scopes, False)
            else:
                visit(b, fn_scopes + [class_bound], True)
                if isinstance(b, ast.Name):
                    resolve(b, fn_scopes + [class_bound])

    if isinstance(stmt_node, ast.Name):
        out.append(stmt_node)
        return out
    if isinstance(stmt_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        # the statement IS a function: its body runs in its own local scope
        for d in getattr(stmt_node, "decorator_list", []):
            visit_expr(d, [])
        for d in stmt_node.args.defaults + stmt_node.args.kw_defaults:
            if d is not None:
                visit_expr(d, [])
        for a in stmt_node.args.posonlyargs + stmt_node.args.args + stmt_node.args.kwonlyargs:
            if a.annotation is not None:
                visit_expr(a.annotation, [])
        if getattr(stmt_node, "returns", None) is not None:
            visit_expr(stmt_node.returns, [])
        inner = local_bindings(stmt_node)
        for b in stmt_node.body:
            visit(b, [inner], False)
        return out
    if isinstance(stmt_node, ast.ClassDef):
        visit_class(stmt_node, [])
        return out
    visit(stmt_node, [], False)
    return out


def inside_fstring(tree_stmt):
    """Set of id()s of Name nodes that sit inside f-strings (positions unreliable < 3.12)."""
    ids = set()
    for n in ast.walk(tree_stmt):
        if isinstance(n, ast.JoinedStr):
            for m in ast.walk(n):
                if isinstance(m, ast.Name):
                    ids.add(id(m))
    return ids


# --------------------------------------------------------------------------- #
# Rewriting
# --------------------------------------------------------------------------- #
def rewrite_statement(stmt, owner, lines, import_names, report):
    """Return the statement text with cross-module names qualified, plus the
    set of modules it references and the import-like names it uses."""
    edits = []          # (lineno, col, end_col, new); new=None deletes the line
    deps = set()
    used_imports = set()
    fstr = inside_fstring(stmt.node)
    skip = set()        # id()s of nodes already covered by a wider edit

    def span_edit(node, new):
        if node.lineno != node.end_lineno:
            report.append(f"L{node.lineno}: multi-line construct -- fix by hand")
            return
        edits.append((node.lineno, node.col_offset, node.end_col_offset, new))
        for m in ast.walk(node):
            skip.add(id(m))

    for n in ast.walk(stmt.node):
        # globals()["X"] -> <owner>.X
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Name) and n.value.func.id == "globals"
                and not n.value.args and isinstance(n.slice, ast.Constant)
                and isinstance(n.slice.value, str)):
            mod = owner.get(n.slice.value)
            if mod is None:
                report.append(f"L{n.lineno}: globals()[{n.slice.value!r}] names no top-level name")
            elif mod != stmt.module:
                span_edit(n, f"{mod}.{n.slice.value}")
                deps.add(mod)
        # globals().get("X"[, d]) -> getattr(<owner>, "X"[, d])
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and isinstance(n.func.value, ast.Call)
                and isinstance(n.func.value.func, ast.Name)
                and n.func.value.func.id == "globals" and not n.func.value.args
                and n.args and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str) and len(n.args) <= 2 and not n.keywords):
            key = n.args[0].value
            mod = owner.get(key)
            if mod is None:
                report.append(f"L{n.lineno}: globals().get({key!r}) names no top-level name")
            elif mod != stmt.module:
                line = lines[n.lineno - 1]
                dflt = (line[n.args[1].col_offset:n.args[1].end_col_offset]
                        if len(n.args) == 2 else "None")
                span_edit(n, f"getattr({mod}, {key!r}, {dflt})")
                deps.add(mod)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "globals":
            if id(n) not in skip:
                report.append(f"L{n.lineno}: globals() in a form this tool does not rewrite")
        elif isinstance(n, ast.Name) and n.id == "__file__":
            if stmt.module == "deps":
                report.append(f"L{n.lineno}: __file__ in deps -- fix by hand")
            else:
                span_edit(n, "deps.FACADE_FILE")
                deps.add("deps")
        elif isinstance(n, ast.Name) and n.id in ("__doc__", "__spec__", "__loader__"):
            report.append(f"L{n.lineno}: {n.id} in moved code -- fix by hand")
        elif isinstance(n, ast.Global):
            moved = [g for g in n.names if owner.get(g) not in (None, stmt.module)]
            if not moved:
                continue
            keep = [g for g in n.names if g not in moved]
            line = lines[n.lineno - 1]
            if keep:
                span_edit(n, "global " + ", ".join(keep))
            elif line.strip() == line[n.col_offset:n.end_col_offset].strip() and                     n.lineno == n.end_lineno:
                edits.append((n.lineno, 0, len(line), None))
            else:
                report.append(f"L{n.lineno}: `global` shares its line -- fix by hand")

    for nm in global_name_refs(stmt.node):
        if id(nm) in skip:
            continue
        ident = nm.id
        if ident in import_names:
            used_imports.add(ident)
            continue
        mod = owner.get(ident)
        if mod is None or mod == stmt.module:
            continue
        if isinstance(nm.ctx, ast.Store) and stmt.module != "__facade__":
            report.append(f"L{nm.lineno}: {stmt.module} STORES {ident} owned by {mod}")
        line = lines[nm.lineno - 1]
        c0, c1 = nm.col_offset, nm.end_col_offset
        if line[c0:c1] != ident:
            if id(nm) not in fstr:
                report.append(f"L{nm.lineno}:{c0} position mismatch for {ident} "
                              f"-- fix by hand: {line.strip()[:80]}")
                continue
            # Python < 3.12: f-string positions are unreliable. Find the name
            # as a whole word on its line; refuse if that is ambiguous.
            hits = [m.start() for m in re.finditer(r"(?<![\w.])%s(?!\w)" % re.escape(ident), line)]
            if len(hits) != 1:
                report.append(f"L{nm.lineno}: f-string name {ident} found {len(hits)} times "
                              f"on its line -- fix by hand: {line.strip()[:80]}")
                continue
            c0, c1 = hits[0], hits[0] + len(ident)
        edits.append((nm.lineno, c0, c1, f"{mod}.{ident}"))
        deps.add(mod)
    # apply edits right-to-left per line (an f-string name found by search may
    # be listed twice when the ast reports it more than once: keep one)
    text_lines = lines[stmt.span_first - 1:stmt.last]
    by_line = collections.defaultdict(set)
    for ln, c0, c1, new in edits:
        by_line[ln].add((c0, c1, new))
    for ln, eds in by_line.items():
        idx = ln - stmt.span_first
        if any(new is None for _c0, _c1, new in eds):
            text_lines[idx] = ""        # a `global` line with nothing left to declare
            continue
        s = text_lines[idx]
        last = None
        for c0, c1, new in sorted(eds, key=lambda e: e[0], reverse=True):
            if last is not None and c1 > last:
                report.append(f"L{ln}: overlapping edits -- fix by hand")
            s = s[:c0] + new + s[c1:]
            last = c0
        text_lines[idx] = s
    return "".join(text_lines), deps, used_imports


def module_docstring(doc):
    if not doc:
        return ""
    if len(doc) + 6 <= 100:
        return '"""' + doc + '"""\n'
    return '"""' + textwrap.fill(doc, 97) + '\n"""\n'


def wrap_import(head, names):
    """`from . import a, b, ...` in one line, or parenthesised within 100 columns."""
    line = head + ", ".join(names)
    if len(line) <= 100:
        return line + "\n"
    body = textwrap.fill(", ".join(names), 96, initial_indent="    ", subsequent_indent="    ",
                         break_on_hyphens=False)
    return head + "(\n" + body + ",\n)\n"


def import_lines_for(used, import_stmts, optional_names):
    """Emit the import statements a module needs, in the head's order."""
    plain = []
    optional = sorted(n for n in used if n in optional_names)
    seen = set()
    for names, text, is_from in import_stmts:
        want = [n for n in names if n in used and n not in optional_names]
        if not want:
            continue
        if is_from and len(names) > 1:
            mod = re.match(r"\s*from\s+(\S+)\s+import", text).group(1)
            plain.append(f"from {mod} import {', '.join(sorted(want))}\n")
        else:
            plain.append(text.strip() + "\n")
    out = "".join(plain)
    if optional:
        out += f"from .deps import {', '.join(optional)}\n"
    return out


FACADE_DOC = (
    "The code lives in the `{package}` package, one module per concern "
    "({package}/__init__.py lists them; CONTRIBUTING.md has a reading order). This is the "
    "name everything else uses ({used_by}) and a compatibility facade: "
    "`{facade}.<name>` resolves every name of the old single-file module, reading or "
    "writing, to the module that owns it, so a test that swaps a helper out "
    "({example}) still reaches the code that calls it."
)

FACADE_TEMPLATE = '''"""{summary}

{facade_doc}

The facade is generated by tools/split/split_tetramaster.py; the module list
below is its output.
"""
import sys
import types

# ONE COPY OF THIS MODULE. `python {facade}.py` runs this file as `__main__`,
# and a later `import {facade}` would otherwise load a second facade object.
if __name__ == "__main__":
    sys.modules.setdefault("{facade}", sys.modules[__name__])

from {package} import (  # noqa: E402,F401
{module_imports}
)

# Which {package} module owns each top-level name of the old {facade}.py.
_OWNERS = {{
{owners}
}}
_MODULES = {{
{modules_dict}
}}


class _Facade(types.ModuleType):
    """`{facade}.<name>` reads and writes go to the owning {package} module."""

    def __getattr__(self, name):
        mod = _OWNERS.get(name)
        if mod is None:
            raise AttributeError(f"module '{facade}' has no attribute {{name!r}}")
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

{main_block}'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--map")
    ap.add_argument("--out")
    ap.add_argument("--facade")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ranges", help="derive a map from 'lo hi module' lines and print it")
    # texts for the generated docstrings (the defaults are tetramaster.py's)
    ap.add_argument("--summary", default="The Tetra Master game server (PlayOnline content id 2).",
                    help="first line of the facade's docstring")
    ap.add_argument("--package-summary", default="The Tetra Master game server, one module per concern.",
                    help="first line of the package's __init__.py")
    ap.add_argument("--used-by", default="`import {facade}` in the title plugin, the tools\n"
                                         "and the tests; `python {facade}.py --selftest`",
                    help="who imports the facade, for its docstring")
    ap.add_argument("--example", default="`{facade}._say = quiet`",
                    help="a rebinding a test makes, for the facade's docstring")
    ap.add_argument("--doc-heading", default="Protocol notes",
                    help="heading of the flat module's docstring in __init__.py")
    args = ap.parse_args()

    text, lines, tree, stmts, trailing = load_source(args.src)

    if args.ranges:
        ranges = []
        for raw in open(args.ranges, encoding="utf-8"):
            raw = raw.split("#")[0].strip()
            if not raw:
                continue
            lo, hi, mod = raw.split()
            ranges.append((int(lo), int(hi), mod))
        per = collections.OrderedDict()
        for lo, hi, mod in ranges:
            per.setdefault(mod, [])
        for s in stmts:
            if s.kind in ("import", "optimport") or not s.names:
                continue
            for lo, hi, mod in ranges:
                if lo <= s.first <= hi:
                    per[mod].extend(s.names)
                    break
            else:
                print(f"# UNCOVERED L{s.first}: {s.names}", file=sys.stderr)
        for mod, names in per.items():
            print(f"[{mod}]")
            buf = ""
            for n in names:
                if len(buf) + len(n) + 1 > 96:
                    print(buf.rstrip())
                    buf = ""
                buf += n + " "
            if buf:
                print(buf.rstrip())
            print()
        return

    mods = read_map(args.map)
    if "deps" not in mods:
        mods["deps"] = {"doc": "", "names": set(), "prefixes": []}
    facade_prefixes = ["if __name__ == \"__main__\":"]
    owner, unmapped = assign_modules(stmts, mods, facade_prefixes)

    # import-like names and their statements
    import_stmts = []       # (names, text, is_from)
    optional_names = set()
    import_names = set()
    for s in stmts:
        if s.kind == "import":
            import_stmts.append((s.names, "".join(lines[s.first - 1:s.last]),
                                 isinstance(s.node, ast.ImportFrom)))
            import_names |= set(s.names)
        elif s.kind == "optimport":
            optional_names |= set(s.names)
            import_names |= set(s.names)
    # names bound in deps are owned by deps for the facade
    for n in import_names:
        owner.setdefault(n, "deps")

    report = []
    if unmapped:
        for s in unmapped:
            first_line = lines[s.first - 1].rstrip()
            report.append(f"UNMAPPED L{s.first}-{s.last} {s.kind} {s.names or ''}: {first_line[:90]}")

    # build module bodies
    bodies = collections.OrderedDict((m, []) for m in mods)
    bodies["deps"] = bodies.get("deps", [])
    mod_deps = collections.defaultdict(set)
    mod_imports = collections.defaultdict(set)
    facade_head = []
    stats = collections.Counter()
    for s in stmts:
        if s.module is None:
            continue
        if s.module == "__facade__":
            facade_head.append(s)
            continue
        if s.kind in ("import", "optimport"):
            if s.kind == "optimport" or s.module == "deps":
                bodies["deps"].append("".join(lines[s.span_first - 1:s.last]))
            continue
        new_text, deps, used = rewrite_statement(s, owner, lines, import_names, report)
        bodies[s.module].append(new_text)
        mod_deps[s.module] |= deps
        mod_imports[s.module] |= used
        stats[s.module] += 1

    # module-name collisions with local variables: a function in module A that
    # binds a local named like module B while A references B.<x>
    for s in stmts:
        if s.module in (None, "__facade__", "deps") or s.kind != "def":
            continue
        for fn in ast.walk(s.node):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                loc = local_bindings(fn)
                clash = loc & mod_deps[s.module]
                for c in clash:
                    # only a problem if the function itself references that module
                    refs = {owner.get(n.id) for n in global_name_refs(fn) if owner.get(n.id) != s.module}
                    if c in refs:
                        report.append(f"L{fn.lineno}: local {c!r} in {s.module}.{getattr(fn, 'name', '<lambda>')} "
                                      f"shadows module {c} that the function references")

    # import-time dependencies (module-level non-def statements using other modules)
    import_time = collections.defaultdict(set)

    def def_time_refs(node):
        """Names evaluated when a def/class statement itself executes."""
        exprs = list(getattr(node, "decorator_list", []))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exprs += [d for d in node.args.defaults + node.args.kw_defaults if d is not None]
            exprs += [a.annotation for a in node.args.posonlyargs + node.args.args
                      + node.args.kwonlyargs if a.annotation is not None]
            if node.returns is not None:
                exprs.append(node.returns)
        elif isinstance(node, ast.ClassDef):
            exprs += node.bases + [k.value for k in node.keywords]
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    exprs += def_time_exprs(b)
                else:
                    exprs.append(b)
        out = []
        for e in exprs:
            out += global_name_refs(e)
        return out

    def def_time_exprs(node):
        exprs = list(getattr(node, "decorator_list", []))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exprs += [d for d in node.args.defaults + node.args.kw_defaults if d is not None]
        return exprs

    for s in stmts:
        if s.module in (None, "__facade__", "deps"):
            continue
        refs = def_time_refs(s.node) if s.kind == "def" else global_name_refs(s.node)
        for nm in refs:
            mod = owner.get(nm.id)
            if mod and mod not in (s.module, "deps") and nm.id not in import_names:
                # a def reference at import time needs the other module fully loaded
                import_time[s.module].add(f"{mod}.{nm.id}")
    # cycles among import-time deps
    def reaches(a, b, seen=None):
        seen = seen or set()
        for c in {x.split(".")[0] for x in import_time.get(a, ())}:
            if c == b or (c not in seen and reaches(c, b, seen | {c})):
                return True
        return False
    for a, bs in import_time.items():
        for b in {x.split(".")[0] for x in bs}:
            if reaches(b, a):
                report.append(f"IMPORT-TIME CYCLE {a} <-> {b}")
            if a != "boot" and mod_deps.get(b, set()) - {"deps"}:
                # a module that needs another one COMPLETE at import must not be
                # reachable from it through the top-of-module imports; a LEAF
                # (it imports no sibling but deps) is always complete once its
                # own import returns, so using it is safe in any order
                report.append(f"IMPORT-TIME USE {a} -> {b} (move the statement or the name)")

    # the facade keeps the flat module's `if __name__ == "__main__":` block,
    # rewritten to reach the package; the docstring goes to __init__.py
    facade_main = []
    flat_doc = ""
    for s in facade_head:
        if s is stmts[0] and isinstance(s.node, ast.Expr):
            raw = "".join(lines[s.first - 1:s.last]).strip()
            q = raw[:3]
            flat_doc = raw[len(q):-len(q)]
            continue
        new_text, _fdeps, _used = rewrite_statement(s, owner, lines, import_names, report)
        facade_main.append(new_text.lstrip("\n"))

    print(f"statements: {len(stmts)}  modules: {len(bodies)}  unmapped: {len(unmapped)}")
    for m, c in stats.most_common():
        print(f"  {c:>4}  {m}  (uses: {', '.join(sorted(mod_deps[m]))})")
    if import_time:
        print("import-time dependencies:")
        for a, bs in import_time.items():
            print(f"  {a} -> {', '.join(sorted(bs))}")
    if report:
        print("\nREPORT (%d):" % len(report))
        for r in report:
            print("  " + r)
    if args.check or not args.out:
        return
    if unmapped or any(r.startswith(("IMPORT-TIME CYCLE", "UNMAPPED")) or "shadows module" in r
                       or "position mismatch" in r for r in report):
        raise SystemExit("refusing to write: fix the report first")

    os.makedirs(args.out, exist_ok=True)
    order = list(mods)  # map order = import order; deps first
    if "deps" in order:
        order.remove("deps")
    order.insert(0, "deps")
    if not mods["deps"]["doc"]:
        opt = sorted(optional_names)
        mods["deps"]["doc"] = "Imports shared by the package's modules" + (
            ", and the optional ones (%s), which are None when the import fails." % ", ".join(opt)
            if opt else ".")
    package = os.path.basename(os.path.normpath(args.out))
    facade_name = os.path.splitext(os.path.basename(args.facade or args.src))[0]
    uses_facade_file = any("deps.FACADE_FILE" in "".join(bodies[m]) for m in order)
    for m in order:
        spec = mods[m]
        buf = io.StringIO()
        buf.write(module_docstring(spec["doc"]))
        if m == "deps":
            buf.write("".join(bodies["deps"]))
            if uses_facade_file:
                buf.write("\n\n#: The flat module's path. Code that looked for data files beside it\n"
                          "#: (`os.path.dirname(__file__)`) looks beside the facade, as before.\n"
                          "FACADE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path."
                          "abspath(__file__))),\n"
                          f"                           \"{facade_name}.py\")\n")
        else:
            imp = import_lines_for(mod_imports[m], import_stmts, optional_names)
            buf.write(imp)
            deps = sorted(mod_deps[m])
            if deps:
                buf.write(wrap_import("from . import ", deps))
            buf.write("\n\n")
            buf.write("".join(bodies[m]).lstrip("\n"))
        content = buf.getvalue()
        if not content.endswith("\n"):
            content += "\n"
        with open(os.path.join(args.out, m + ".py"), "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    # package init: the layout, in map order, with each module's docstring
    with open(os.path.join(args.out, "__init__.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write('"""' + args.package_summary + "\n\n")
        width = max(len(m) for m in order) + 4
        for m in order:
            f.write(textwrap.fill(mods[m]["doc"], 100, initial_indent=f"    {m + '.py':<{width}} ",
                                  subsequent_indent=" " * (4 + width + 1)) + "\n")
        f.write(f'\n{facade_name}.py (one directory up) is the name the rest of the tree\n'
                'imports, and the compatibility facade over these modules.\n')
        if flat_doc:
            f.write("\n\n" + args.doc_heading + "\n" + "-" * len(args.doc_heading) + "\n")
            f.write(flat_doc)
        f.write('"""\n')
    # facade
    if args.facade:
        main_block = "".join(facade_main)
        owners_lines = "\n".join(f"    {n!r}: {m!r}," for n, m in sorted(owner.items()))
        facade = FACADE_TEMPLATE.format(
            module_imports="\n".join(f"    {m}," for m in order),
            owners=owners_lines,
            modules_dict="\n".join(f"    {m!r}: {m}," for m in order),
            package=package, facade=facade_name, main_block=main_block,
            summary=args.summary,
            facade_doc=textwrap.fill(FACADE_DOC.format(
                package=package, facade=facade_name,
                used_by=" ".join(args.used_by.replace("{facade}", facade_name).split()),
                example=args.example.replace("{facade}", facade_name)), 79,
                break_on_hyphens=False, break_long_words=False),
        )
        with open(args.facade, "w", encoding="utf-8", newline="\n") as f:
            f.write(facade)
    print("written:", args.out, "and", args.facade)
    problems = verify_scopes(args.out, order)
    for p in problems:
        print("  SCOPE: " + p)
    if problems:
        raise SystemExit("the written package has scope problems (above)")


def verify_scopes(out_dir, order):
    """No scope of a written module may bind a name that is also a sibling
    module the file imports: `purse.money_of()` inside a function with a local
    `purse` would call the local. Checked with symtable, which knows every
    Python scope (functions, lambdas, comprehensions, class bodies)."""
    import symtable
    problems = []
    siblings = set(order)
    for m in order:
        path = os.path.join(out_dir, m + ".py")
        src = open(path, encoding="utf-8").read()
        imported = set()
        for node in ast.parse(src).body:
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
                imported |= {a.asname or a.name for a in node.names}
        imported &= siblings
        if not imported:
            continue

        def walk(tab):
            if tab.get_type() != "module":
                for sym in tab.get_symbols():
                    if sym.get_name() in imported and (sym.is_local() or sym.is_parameter()) \
                            and not sym.is_global():
                        problems.append(f"{m}.py: {tab.get_type()} {tab.get_name()!r} "
                                        f"(line {tab.get_lineno()}) binds {sym.get_name()!r}, "
                                        f"a sibling module this file imports")
            for ch in tab.get_children():
                walk(ch)
        walk(symtable.symtable(src, path, "exec"))
    return problems


if __name__ == "__main__":
    main()
