"""A run never starts with broken code.

On 2026-10-01 a round of edits deleted two functions that were still being called. The code still compiled, so nothing
noticed, and every single application of the next run would have ended in "name is not defined". This check reads the
bot's own source before each run and looks for exactly that kind of mistake:

  * a name that is used somewhere in a module but defined nowhere in it;
  * a reference from one module of the bot to something in another (sub.apply, auth.handle, mailbox.find_confirmation)
    that the other module does not have.

It takes a fraction of a second. When it finds something, the run stops before any job is touched and says what to fix.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

PKG = Path(__file__).resolve().parent
EXTRA_NAMES = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__builtins__", "__version__"}


def _defined(tree: ast.AST) -> set:
    """Every name a module binds anywhere (functions, classes, imports, assignments, arguments, loop and 'as' targets)."""
    names = set(dir(builtins)) | EXTRA_NAMES
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
    return names


def _top_level(tree: ast.Module) -> set:
    """What a module offers to others: everything bound at its top level (also inside top-level if / try / with blocks)."""
    out = set(EXTRA_NAMES)

    def visit(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(n.name)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            out.add(x.id)
            elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for part in ("body", "orelse", "finalbody"):
                    visit(getattr(n, part, []) or [])
                for h in getattr(n, "handlers", []) or []:
                    visit(h.body)
    visit(tree.body)
    return out


def _aliases(tree: ast.AST, modules: set) -> dict:
    """{name used in this module: module of the bot it stands for}, e.g. {'sub': 'submit', 'S': 'submit', 'auth': 'auth'}."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level >= 1 and not n.module:          # from . import submit as sub, auth
            for a in n.names:
                if a.name in modules:
                    out[a.asname or a.name] = a.name
        elif isinstance(n, ast.ImportFrom) and n.module == "autoapply":             # from autoapply import submit as S
            for a in n.names:
                if a.name in modules:
                    out[a.asname or a.name] = a.name
        elif isinstance(n, ast.Import):                                              # import autoapply.writer as W
            for a in n.names:
                if a.name.startswith("autoapply.") and a.asname and a.name.split(".", 1)[1] in modules:
                    out[a.asname] = a.name.split(".", 1)[1]
    return out


def problems(paths: list[Path] | None = None, pkg: Path | None = None) -> list[str]:
    """Human-readable list of broken references in the bot's own code (empty when everything is in place)."""
    files = sorted((pkg or PKG).glob("*.py"))
    trees, offers = {}, {}
    out = []
    for f in files:
        try:
            trees[f.stem] = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            out.append(f"{f.name}: does not parse (line {e.lineno}: {e.msg})")
    for name, tree in trees.items():
        offers[name] = _top_level(tree)
    modules = set(trees)
    scan = dict(trees)
    for extra in paths or []:                              # e.g. the test files: they call into the bot too
        try:
            scan["@" + str(extra)] = ast.parse(Path(extra).read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as e:
            out.append(f"{extra}: {e}")
    for name, tree in scan.items():
        label = (name[1:] if name.startswith("@") else name + ".py")
        known = _defined(tree)
        seen = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known and n.id not in seen:
                seen.add(n.id)
                out.append(f"{label} line {n.lineno}: '{n.id}' is used but is not defined anywhere in the file")
        alias = _aliases(tree, modules)
        local_rebinds = {x.id for x in ast.walk(tree) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias and n.value.id not in local_rebinds:
                mod = alias[n.value.id]
                key = (n.value.id, n.attr)
                if n.attr not in offers.get(mod, set()) and key not in seen and not n.attr.startswith("__"):
                    if isinstance(n.ctx, ast.Store):
                        continue                              # setting a module attribute (tests do this to stub things out)
                    seen.add(key)
                    out.append(f"{label} line {n.lineno}: {n.value.id}.{n.attr} is used, but {mod}.py has no '{n.attr}'")
    return out


if __name__ == "__main__":
    import sys
    found = problems([Path(p) for p in sys.argv[1:]])
    print("\n".join(found) if found else "code check: every name and cross-module reference resolves")
    sys.exit(1 if found else 0)
