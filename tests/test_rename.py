"""scripts/rename.py gives the project another name in one step: it must change every spelling and every file name - and
nothing else, and it must never leave a half-renamed project behind.

The tests use a made-up project, so this file does not mention the real name and survives a rename itself.
"""
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "rename.py"
OLD, NEW = "demoapp", "linkup"


def load_script(path: Path = SCRIPT, name: str = "rename_script"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rename():
    return load_script()


PYPROJECT = ('[build-system]\nrequires = ["setuptools"]\n\n[tool.other]\nname = "wrong"\n\n[project]\nname = "demoapp"\n'
             'version = "1.0"\n\n[project.scripts]\ndemoapp = "demoapp.cli:main"\n')

TEXT_FILES = {
    "pyproject.toml": PYPROJECT,
    "README.md": "# Demoapp\n\nRun `demoapp init`, then set DEMOAPP_HOME.\n",
    "demoapp/__init__.py": '"""Demoapp helps with things."""\nHOME_VARIABLE = "DEMOAPP_HOME"\nCOOKIE = "demoapp_token"\n'
                           'HEADER = "X-Demoapp-Token"\n',
    "demoapp/cli.py": 'from demoapp import HOME_VARIABLE\n\n\ndef main():\n    print("demoapp 1.0")\n',
    "demoapp/demoapp_notes.md": "notes about demoapp\n",                  # a file with the name inside a folder with the name
    "demoapp/ui/static/index.html": "<title>Demoapp</title>\n",
    "tests/test_demoapp.py": "import demoapp\n",
    "docs/demoapp.md": "# The demoapp guide\n",
    "skills/demoapp/SKILL.md": "---\nname: demoapp\n---\n",
    ".github/workflows/tests.yml": "name: demoapp tests\n",
    ".gitignore": ".venv/\nlocaldata/\nsecret.txt\n*.log\n",
}

# Everything here mentions the old name and must come through a rename byte for byte.
UNTOUCHED = {
    ".git/config": b"[remote]\n\turl = https://example.invalid/demoapp.git\n",
    ".venv/lib/site.py": b"demoapp\n",
    "__pycache__/x.cpython-313.pyc": b"\x00demoapp",
    "docs/__pycache__/y.pyc": b"\x00demoapp",
    "node_modules/pkg/demoapp.js": b"demoapp\n",
    "demoapp.egg-info/PKG-INFO": b"Name: demoapp\n",       # a build leftover, not a source file
    "localdata/people.txt": b"demoapp\n",                   # listed in .gitignore by name: the user's own data
    "secret.txt": b"demoapp\n",
    "run.log": b"demoapp\n",
    "logo.png": b"\x89PNG\r\n\x1a\n\x00demoapp",
    "blob.bin": b"\x00\x01demoapp\x02",                     # contains a NUL: not text
    "latin1.txt": "caf\xe9 demoapp\n".encode("latin-1"),    # not UTF-8: not safe to rewrite
    "data.sqlite": b"SQLite format 3\x00demoapp",
}

EXPECTED = {
    "linkup/__init__.py": '"""Linkup helps with things."""\nHOME_VARIABLE = "LINKUP_HOME"\nCOOKIE = "linkup_token"\n'
                          'HEADER = "X-Linkup-Token"\n',
    "linkup/cli.py": 'from linkup import HOME_VARIABLE\n\n\ndef main():\n    print("linkup 1.0")\n',
    "linkup/linkup_notes.md": "notes about linkup\n",
    "linkup/ui/static/index.html": "<title>Linkup</title>\n",
    "tests/test_linkup.py": "import linkup\n",
    "docs/linkup.md": "# The linkup guide\n",
    "skills/linkup/SKILL.md": "---\nname: linkup\n---\n",
    ".github/workflows/tests.yml": "name: linkup tests\n",
    "README.md": "# Linkup\n\nRun `linkup init`, then set LINKUP_HOME.\n",
    ".gitignore": ".venv/\nlocaldata/\nsecret.txt\n*.log\n",
    "pyproject.toml": PYPROJECT.replace('name = "demoapp"', 'name = "linkup"').replace(
        'demoapp = "demoapp.cli:main"', 'linkup = "linkup.cli:main"'),
}


def put(root: Path, rel: str, content) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def snapshot(root: Path) -> dict:
    """Every path under the folder (not following links) with its bytes, 'dir', or its link target."""
    out = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = path.read_bytes()
    return out


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    for rel, content in {**TEXT_FILES, **UNTOUCHED}.items():
        put(root, rel, content)
    return root


# ---------------------------------------------------------------------------------------------------------------- a rename

def test_a_dry_run_reports_everything_and_changes_nothing(project, rename):
    before = snapshot(project)
    report = rename.rename_project(project, NEW, dry_run=True)
    assert snapshot(project) == before
    assert (report["old"], report["new"], report["dry_run"]) == (OLD, NEW, True)
    assert report["files"]["pyproject.toml"] == 3                      # name = "...", and the command, and its module
    assert report["files"]["demoapp/__init__.py"] == 4
    assert set(report["moves"]) == {("demoapp", "linkup"), ("docs/demoapp.md", "docs/linkup.md"),
                                    ("skills/demoapp", "skills/linkup"), ("tests/test_demoapp.py", "tests/test_linkup.py"),
                                    ("demoapp/demoapp_notes.md", "demoapp/linkup_notes.md")}
    assert report["moves"][-1] == ("demoapp", "linkup")                # a folder moves after what is inside it
    assert report["odd"] == []


def test_every_spelling_in_every_text_file_is_replaced_and_files_are_renamed(project, rename):
    report = rename.rename_project(project, NEW)
    assert report["dry_run"] is False
    after = snapshot(project)
    for rel, expected in EXPECTED.items():
        assert after[rel] == expected.encode("utf-8"), rel
    for gone in ("demoapp", "docs/demoapp.md", "skills/demoapp", "tests/test_demoapp.py", "demoapp/cli.py",
                 "linkup/demoapp_notes.md"):
        assert gone not in after
    # no file that was meant to change still says the old name, in any capitalisation
    for rel, content in after.items():
        if rel in EXPECTED:
            assert OLD not in content.decode("utf-8").lower(), rel


def test_only_the_project_table_decides_what_the_current_name_is(project, rename):
    assert rename.current_name(project) == OLD                       # not "wrong" from [tool.other]


def test_everything_that_is_not_the_projects_own_text_is_left_exactly_as_it_was(project, rename):
    rename.rename_project(project, NEW)
    after = snapshot(project)
    for rel, content in UNTOUCHED.items():
        assert after[rel] == content, rel
    assert "demoapp.egg-info" in after and "linkup.egg-info" not in after       # not renamed either


def test_plain_names_in_gitignore_are_honoured_but_paths_and_patterns_are_left_to_git(project, rename):
    put(project, ".gitignore", ".venv/\nlocaldata/\nsecret.txt\n*.log\ndocs/private/\n#demoapp\n!keep\n")
    put(project, "docs/private/note.md", "demoapp\n")                # a path pattern: not something this script interprets
    rename.rename_project(project, NEW)
    after = snapshot(project)
    assert after["localdata/people.txt"] == b"demoapp\n" and after["secret.txt"] == b"demoapp\n"
    assert after["docs/private/note.md"] == b"linkup\n"
    assert after[".gitignore"].decode().endswith("#linkup\n!keep\n")


def test_the_display_name_sets_the_capitals_used_in_prose(project, rename):
    rename.rename_project(project, NEW, display="LinkUp")
    after = snapshot(project)
    assert after["linkup/__init__.py"] == (b'"""LinkUp helps with things."""\nHOME_VARIABLE = "LINKUP_HOME"\n'
                                            b'COOKIE = "linkup_token"\nHEADER = "X-LinkUp-Token"\n')
    assert after["README.md"] == b"# LinkUp\n\nRun `linkup init`, then set LINKUP_HOME.\n"


@pytest.mark.parametrize("display", ["Link Up", "Other", "Link-Up", "", "LinkUp2"])
def test_a_display_name_that_is_not_the_same_letters_is_refused(project, rename, display):
    before = snapshot(project)
    with pytest.raises(rename.RenameError, match="display-name"):
        rename.rename_project(project, NEW, display=display)
    assert snapshot(project) == before


def test_other_capitalisations_are_reported_and_left_alone(project, rename):
    put(project, "docs/notes.md", "Use DemoApp for this and Demoapp for that.\n")
    report = rename.rename_project(project, NEW)
    assert report["odd"] == ["docs/notes.md"]
    assert (project / "docs/notes.md").read_text() == "Use DemoApp for this and Linkup for that.\n"


def test_a_project_renamed_with_its_own_capitals_can_be_renamed_again(project, rename):
    rename.rename_project(project, NEW, display="LinkUp")
    report = rename.rename_project(project, "zyxproj", display="ZyxProj")
    assert report["prose"] == ("LinkUp", "ZyxProj") and report["odd"] == []
    after = snapshot(project)
    assert after["zyxproj/__init__.py"] == (b'"""ZyxProj helps with things."""\nHOME_VARIABLE = "ZYXPROJ_HOME"\n'
                                             b'COOKIE = "zyxproj_token"\nHEADER = "X-ZyxProj-Token"\n')
    for rel, content in after.items():
        if rel.endswith((".py", ".md", ".html", ".toml", ".yml")):
            assert b"linkup" not in content.lower(), rel


def test_the_prose_spelling_is_whatever_the_project_mostly_uses(project, rename):
    for rel in ("README.md", "docs/demoapp.md", "demoapp/__init__.py", "demoapp/ui/static/index.html"):
        path = project / rel
        path.write_text(path.read_text().replace("Demoapp", "DemoApp"), encoding="utf-8")
    put(project, "docs/other.md", "Demoapp once.\n")
    report = rename.rename_project(project, NEW)
    assert report["prose"] == ("DemoApp", "Linkup")
    assert report["odd"] == ["docs/other.md"]                  # the minority spelling is reported, not guessed at
    assert (project / "README.md").read_text() == "# Linkup\n\nRun `linkup init`, then set LINKUP_HOME.\n"


def test_renaming_back_restores_every_byte(project, rename):
    before = snapshot(project)
    rename.rename_project(project, NEW)
    assert snapshot(project) != before
    rename.rename_project(project, OLD)
    assert snapshot(project) == before


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no executable bit")
def test_the_executable_bit_survives(project, rename):
    script = put(project, "docs/run-demoapp.sh", "#!/bin/sh\necho demoapp\n")
    script.chmod(0o755)
    rename.rename_project(project, NEW)
    moved = project / "docs" / "run-linkup.sh"
    assert moved.read_text() == "#!/bin/sh\necho linkup\n"
    assert moved.stat().st_mode & stat.S_IXUSR


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need extra privileges on Windows")
def test_symbolic_links_are_never_followed(project, tmp_path, rename):
    outside = tmp_path / "outside"
    put(outside, "notes.txt", "demoapp\n")
    (project / "docs" / "folder_link").symlink_to(outside, target_is_directory=True)
    (project / "docs" / "file_link.md").symlink_to(outside / "notes.txt")
    (project / "docs" / "demoapp_link.md").symlink_to(outside / "notes.txt")
    rename.rename_project(project, NEW)
    assert (outside / "notes.txt").read_text() == "demoapp\n"                  # nothing outside was edited through a link
    assert (project / "docs" / "folder_link").is_symlink() and (project / "docs" / "file_link.md").is_symlink()
    assert (project / "docs" / "linkup_link.md").is_symlink()                  # the link itself has the name, so it is renamed
    assert (project / "docs" / "linkup_link.md").read_text() == "demoapp\n"    # ... but what it points to is not


def test_the_script_never_edits_itself(project):
    copy = project / "scripts" / "rename.py"
    copy.parent.mkdir()
    shutil.copy(SCRIPT, copy)
    with copy.open("a", encoding="utf-8") as handle:
        handle.write("\n# mentions demoapp, Demoapp and DEMOAPP\n")
    before = copy.read_bytes()
    load_script(copy, "rename_copy").rename_project(project, NEW)
    assert copy.read_bytes() == before


# ---------------------------------------------------------------------------------------------- refusing before changing

@pytest.mark.parametrize("bad", ["", "a", "Linkup", "link-up", "link_up", "1link", "linkü", "x" * 25, "class", "json", "tests",
                                 "yaml", "pip", OLD, OLD + "x", "demo"])
def test_a_bad_new_name_is_refused_and_nothing_changes(project, rename, capsys, bad):
    before = snapshot(project)
    assert rename.main([bad, "--root", str(project)]) == 2
    assert "rename:" in capsys.readouterr().err
    assert snapshot(project) == before


def test_a_file_that_would_be_overwritten_stops_everything_before_the_first_change(project, rename):
    put(project, "docs/linkup.md", "already here\n")
    before = snapshot(project)
    with pytest.raises(rename.RenameError, match=r"docs.linkup\.md.* already exists"):
        rename.rename_project(project, NEW)
    assert snapshot(project) == before                  # not even the text files were touched


def test_an_existing_folder_with_the_new_name_is_never_merged_into(project, rename):
    put(project, "linkup/other.py", "x = 1\n")
    before = snapshot(project)
    with pytest.raises(rename.RenameError, match="already exists"):
        rename.rename_project(project, NEW, dry_run=True)       # a dry run finds the problem too
    assert snapshot(project) == before


def test_two_files_that_would_become_one_are_refused(project, rename):
    put(project, "docs/Demoapp.md", "capital\n")
    if len(list((project / "docs").glob("*emoapp.md"))) < 2:
        pytest.skip("this file system does not tell Demoapp.md from demoapp.md")
    with pytest.raises(rename.RenameError, match="would both become"):
        rename.rename_project(project, NEW, display="linkup")   # lower-case prose name: both spellings turn into linkup.md


def test_a_folder_that_is_not_a_project_is_refused(tmp_path, rename, capsys):
    assert rename.main([NEW, "--root", str(tmp_path)]) == 2
    assert "pyproject.toml" in capsys.readouterr().err
    (tmp_path / "pyproject.toml").write_text('[tool.other]\nname = "wrong"\n', encoding="utf-8")
    assert rename.main([NEW, "--root", str(tmp_path)]) == 2
    assert "[project]" in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------------------ interruptions

def test_a_run_that_stops_half_way_says_so_and_can_simply_be_repeated(project, tmp_path, rename, monkeypatch):
    clean = tmp_path / "clean"
    shutil.copytree(project, clean, symlinks=True)
    rename.rename_project(clean, NEW)

    real_rename, calls = Path.rename, []

    def flaky(self, target):
        calls.append(self)
        if len(calls) == 2:
            raise PermissionError("the disk is read-only")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(rename.RenameError, match="run the same command again"):
        rename.rename_project(project, NEW)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert rename.current_name(project) == OLD                        # still the old name: nothing says "done" yet
    rename.rename_project(project, NEW)                               # the same command finishes the job
    assert snapshot(project) == snapshot(clean)


def test_a_failed_write_leaves_the_original_and_no_temporary_file(tmp_path, rename, monkeypatch):
    target = tmp_path / "a.txt"
    target.write_text("old")

    def boom(*args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        rename.write_atomic(target, b"new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["a.txt"]


# ------------------------------------------------------------------------------------------------------- the command line

def test_the_command_line_says_what_it_would_do_and_what_it_did(project, rename, capsys):
    assert rename.main([NEW, "--root", str(project), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"{OLD} -> {NEW}" in out and "Would change" in out and "Would rename demoapp -> linkup" in out
    assert "Nothing was changed" in out
    assert (project / "demoapp").is_dir()

    assert rename.main([NEW, "--root", str(project), "--display-name", "LinkUp"]) == 0
    out = capsys.readouterr().out
    assert "Changed" in out and "Renamed demoapp -> linkup" in out and "Nothing was changed" not in out
    assert f"pip uninstall -y {OLD}" in out and f"{OLD} schedule uninstall" in out and f"~/.{OLD}" in out
    assert (project / "linkup").is_dir() and not (project / "demoapp").exists()


# ----------------------------------------------------------------------------------------------- the real project, renamed

def test_the_real_project_survives_a_rename(tmp_path, rename):
    """The promise behind 'rename in one step': nothing of the old name is left, and the renamed copy still runs."""
    old = rename.current_name(REPO)
    new = "zyxproj" if old != "zyxproj" else "qwxproj"
    about_renaming = {"tests/test_rename.py", "scripts/rename.py"}          # these two use made-up names on purpose
    copy = tmp_path / "copy"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "*.egg-info", "build", "dist", "node_modules", "*.sqlite*"))
    script = load_script(copy / "scripts" / "rename.py", "rename_of_the_copy")
    report = script.rename_project(copy, new)                                       # as a user would run it
    assert set(report["odd"]) <= about_renaming, "a spelling of the name that the script cannot convert crept into the project"

    ignore = script.read_ignore(copy)   # a person's own files (.gitignore: config.yaml, private/ ...) are never renamed
    leftovers = []
    for folder, dirs, files in os.walk(copy):
        dirs[:] = [d for d in dirs if d != "__pycache__" and not ignore.skips(d, is_dir=True)]   # bytecode is a by-product
        for name in dirs + files:
            path = Path(folder) / name
            shown = path.relative_to(copy).as_posix()
            if not path.is_dir() and ignore.skips(name, is_dir=False):
                continue
            if old in name.lower():
                leftovers.append(shown)
            elif path.is_file() and shown not in about_renaming and path.suffix not in (".png", ".ico", ".gif", ".jpg", ".zip", ".pdf"):
                if old.encode() in path.read_bytes().lower():
                    leftovers.append(shown)
    assert not leftovers, f"still mentions {old!r}: {leftovers}"

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    version = subprocess.run([sys.executable, "-m", new, "--version"], cwd=copy, env=env, capture_output=True, text=True,
                             timeout=120)
    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith(new + " ")

    imports = ("import importlib, pkgutil, sys; sys.path.insert(0, sys.argv[1]); package = importlib.import_module(sys.argv[2]); "
               "names = [m.name for m in pkgutil.walk_packages(package.__path__, package.__name__ + '.')]; "
               "[importlib.import_module(n) for n in names]; print(len(names))")
    imported = subprocess.run([sys.executable, "-I", "-c", imports, str(copy), new], env=env, capture_output=True, text=True,
                              timeout=120)
    assert imported.returncode == 0, imported.stderr
    assert int(imported.stdout) > 20                                        # every module of the package, not just the first

    collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o",
                                "addopts="], cwd=copy, env=env, capture_output=True, text=True, timeout=300)
    assert collected.returncode == 0, collected.stdout[-2000:] + collected.stderr[-2000:]
