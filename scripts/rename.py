#!/usr/bin/env python3
"""Give the project a different name, in one step.

    python scripts/rename.py linkup --dry-run      # see what would change
    python scripts/rename.py linkup                # do it
    python scripts/rename.py linkup --display-name LinkUp

It reads the current name from ``pyproject.toml`` and, in every text file of the project (code, tests, docs, examples,
workflows, metadata), replaces

    current   ->  linkup      the package, the command, file and folder names, the scheduler names, ...
    Current   ->  Linkup      the name as it is written in prose (``--display-name`` chooses the capitals: LinkUp)
    CURRENT   ->  LINKUP      environment variables such as ``<NAME>_HOME``

and renames the package folder and every file or folder whose name contains the old name. It never touches ``.git``,
anything your ``.gitignore`` lists by name (virtual environments, caches, your own data folder, ``config.yaml``, ``.env``),
symbolic links or binary files, and it leaves this script alone.

It checks everything first and changes nothing if a name is invalid or a file would be overwritten. If it is interrupted
half-way (a read-only file, a full disk), run the same command again to finish. Commit - or copy the folder - first so you can
see exactly what changed. Afterwards run the tests and reinstall::

    python -m pytest -m "not browser" && pip uninstall -y OLDNAME && pip install -e .

Rename **before you publish**. The name also decides the default data folder (``~/.<name>``), the scheduler entries and the
environment variable names, so people who already use the old name would have to move their folder.
"""
from __future__ import annotations

import argparse
import keyword
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist",
             "node_modules", ".tox", ".idea", ".vscode"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip",
                 ".gz", ".whl", ".pdf", ".egg-info")
NAME_OK = re.compile(r"^[a-z][a-z0-9]{1,23}$")
RESERVED = {"test", "tests", "scripts", "docs", "examples", "skills", "yaml", "requests", "bs4", "lxml", "playwright", "pytest",
            "setuptools", "pip", "numpy", "pandas"}
SELF = Path(__file__).resolve()
LAST = "pyproject.toml"          # written last: until it changes, the project still has its old name, so a stopped run can be repeated
TEMP_SUFFIX = ".rename-tmp"


class RenameError(Exception):
    pass


class Ignore(NamedTuple):
    """What the project's .gitignore lists *by plain name* (``.venv/``, ``config.yaml``, ``*.log``); anything fancier is not skipped."""
    folders: frozenset
    names: frozenset
    suffixes: tuple

    def skips(self, name: str, *, is_dir: bool) -> bool:
        return (name in self.names or name.endswith(self.suffixes) or (is_dir and (name in self.folders or name in SKIP_DIRS)))


def read_ignore(root: Path) -> Ignore:
    folders: set[str] = set()
    names: set[str] = set()
    suffixes: list[str] = list(SKIP_SUFFIXES)
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line[0] in "#!" or any(c in line for c in "?[]\\"):
            continue
        is_dir = line.endswith("/")
        line = line.rstrip("/")
        if "/" in line or not line:                      # a path, not a plain name: leave it to git
            continue
        if line.startswith("*.") and "*" not in line[1:]:
            suffixes.append(line[1:])
        elif "*" not in line:
            (folders if is_dir else names).add(line)
    return Ignore(frozenset(folders), frozenset(names), tuple(suffixes))


def current_name(root: Path) -> str:
    """The package name in the ``[project]`` table of pyproject.toml."""
    try:
        text = (root / LAST).read_text(encoding="utf-8")
    except OSError:
        raise RenameError(f"no pyproject.toml in {root} - run this from the project folder (or pass --root)") from None
    table = re.search(r"(?ms)^\[project\][ \t]*\r?\n(.*?)(?=^\[|\Z)", text)
    found = re.search(r"""(?m)^name\s*=\s*["']([^"']+)["']""", table.group(1)) if table else None
    if not found:
        raise RenameError("could not find the project name under [project] in pyproject.toml")
    return found.group(1)


def check_new_name(new: str, old: str) -> None:
    if not NAME_OK.fullmatch(new):
        raise RenameError("the new name must be 2-24 characters: lowercase letters and digits, starting with a letter "
                          "(it becomes a Python package, a command and a folder name)")
    if new == old:
        raise RenameError(f"the project is already called {old!r}")
    if keyword.iskeyword(new) or new in sys.stdlib_module_names or new in RESERVED:
        raise RenameError(f"{new!r} would clash with a Python module or a folder of this project - choose another name")
    if old in new or new in old:
        raise RenameError(f"{new!r} and {old!r} contain each other, so replacing one by the other could not be undone "
                          "cleanly - choose a clearly different name")


def check_display(display: str | None, new: str) -> str:
    """How the name is capitalised in prose: only the capitals may differ, because it also appears in headers and task names."""
    if display is None:
        return new.capitalize()
    if display.lower() != new:
        raise RenameError("--display-name must be the same letters as the name; only the capitals may differ "
                          "(LinkUp for linkup), because it also appears in HTTP headers and task names")
    return display


def spellings(old: str, new: str, prose_old: str, prose_new: str) -> list[tuple[str, str]]:
    """What to replace: the plain name, the name as written in prose (Linkup, say) and the upper-case name."""
    return [(old, new), (prose_old, prose_new), (old.upper(), new.upper())]


def prose_spelling(variants: Counter, old: str) -> str:
    """How the project writes its name in prose: the commonest spelling other than all-lower and all-upper case.

    Normally that is the capitalised name (Linkup), but a project that was renamed with ``--display-name LinkUp`` writes
    LinkUp, and should be renameable again just the same.
    """
    candidates = {spelling: n for spelling, n in variants.items() if spelling not in (old, old.upper())}
    if not candidates:
        return old.capitalize()
    return max(sorted(candidates), key=candidates.__getitem__)         # (the alphabetically first one wins a tie)


def rename_text(text: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in pairs:
        text = text.replace(old, new)
    return text


def walk(root: Path, ignore: Ignore):
    """Every file and folder of the project except what is ignored (never following symbolic links)."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not ignore.skips(d, is_dir=True) and not (Path(folder) / d).is_symlink())
        for name in sorted(dirs) + sorted(f for f in files if not ignore.skips(f, is_dir=False)):
            yield Path(folder) / name


def read_text(path: Path) -> str | None:
    """The content of a UTF-8 text file; None for anything else (binary files, other encodings)."""
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def plan_moves(root: Path, pairs: list[tuple[str, str]], ignore: Ignore) -> list[tuple[Path, Path]]:
    """Files and folders whose *name* mentions the old name, deepest first so that a parent is renamed after its children."""
    moves = []
    for path in sorted(walk(root, ignore), key=lambda p: (len(p.parts), str(p)), reverse=True):
        if path.resolve() == SELF:
            continue
        new_name = rename_text(path.name, pairs)
        if new_name != path.name:
            moves.append((path, path.with_name(new_name)))
    return moves


def check_conflicts(root: Path, moves: list[tuple[Path, Path]]) -> None:
    """Refuse - before anything is changed - to overwrite a file or to turn two files into one."""
    sources: dict[Path, Path] = {}
    for src, dst in moves:
        shown = dst.relative_to(root)
        if dst in sources:
            raise RenameError(f"{sources[dst].relative_to(root)} and {src.relative_to(root)} would both become {shown}")
        sources[dst] = src
        if os.path.lexists(dst):
            raise RenameError(f"cannot rename {src.relative_to(root)}: {shown} already exists")


def write_atomic(path: Path, data: bytes) -> None:
    """Replace a file's content so that it is either the old or the new version, never half of each."""
    temp = path.with_name(path.name + TEMP_SUFFIX)
    try:
        temp.write_bytes(data)
        shutil.copymode(path, temp)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def rename_project(root: Path, new: str, *, display: str | None = None, dry_run: bool = False) -> dict:
    root = root.resolve()
    old = current_name(root)
    check_new_name(new, old)
    prose_new = check_display(display, new)
    ignore = read_ignore(root)
    mentions = re.compile(re.escape(old), re.I)
    texts: list[tuple[Path, str]] = []                       # the text files that say the name, in any capitalisation
    variants: Counter = Counter()
    for path in walk(root, ignore):
        if not path.is_file() or path.is_symlink() or path.resolve() == SELF:
            continue
        text = read_text(path)
        found = mentions.findall(text) if text is not None else []
        if found:
            texts.append((path, text))
            variants.update(found)
    prose_old = prose_spelling(variants, old)
    pairs = spellings(old, new, prose_old, prose_new)
    known = {spelling for spelling, _ in pairs}
    edits: list[tuple[Path, bytes]] = []
    changed: dict[str, int] = {}
    odd: list[str] = []
    for path, text in texts:
        shown = path.relative_to(root).as_posix()
        if any(spelling not in known for spelling in mentions.findall(text)):
            odd.append(shown)
        count = sum(text.count(spelling) for spelling in known)
        if count:
            changed[shown] = count
            edits.append((path, rename_text(text, pairs).encode("utf-8")))
    moves = plan_moves(root, pairs, ignore)
    check_conflicts(root, moves)
    if not dry_run:
        try:
            for path, data in edits:
                if path != root / LAST:
                    write_atomic(path, data)
            for src, dst in moves:
                src.rename(dst)
            for path, data in edits:
                if path == root / LAST:
                    write_atomic(path, data)
        except OSError as exc:
            raise RenameError(f"stopped half-way: {exc}\nNothing is lost - fix the problem and run the same command again "
                              "to finish the job.") from exc
    return {"old": old, "new": new, "prose": (prose_old, prose_new), "files": changed, "odd": odd, "dry_run": dry_run,
            "moves": [(a.relative_to(root).as_posix(), b.relative_to(root).as_posix()) for a, b in moves]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rename the project (package, command, docs, tests) in one step.")
    parser.add_argument("name", help="the new name: lowercase letters and digits, e.g. linkup")
    parser.add_argument("--display-name", help="the same name with other capitals, for prose, e.g. LinkUp (default: Linkup)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="the project folder")
    args = parser.parse_args(argv)
    try:
        report = rename_project(Path(args.root), args.name, display=args.display_name, dry_run=args.dry_run)
    except RenameError as exc:
        print(f"rename: {exc}", file=sys.stderr)
        return 2
    old, new = report["old"], report["new"]
    verb = "Would change" if report["dry_run"] else "Changed"
    total = sum(report["files"].values())
    print(f"{old} -> {new}   (in prose: {report['prose'][0]} -> {report['prose'][1]})")
    print(f"{verb} {total} occurrence{'s' if total != 1 else ''} in {len(report['files'])} file"
          f"{'s' if len(report['files']) != 1 else ''}.")
    for src, dst in report["moves"]:
        print(f"{'Would rename' if report['dry_run'] else 'Renamed'} {src} -> {dst}")
    if report["odd"]:
        print("\nOther spellings of the old name were left alone - please check them by hand:")
        for name in report["odd"]:
            print(f"  {name}")
    if report["dry_run"]:
        print("\nNothing was changed (dry run).")
    else:
        print(f'\nNext:  python -m pytest -m "not browser"  and then  pip uninstall -y {old} ; pip install -e .\n'
              f"If you ever installed the daily schedule under the old name, run `{old} schedule uninstall` BEFORE "
              f"uninstalling it, and\nmove its data folder (~/.{old}) to ~/.{new} if you want to keep your people and history.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
