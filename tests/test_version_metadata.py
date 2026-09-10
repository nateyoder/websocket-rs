"""The fork's version scheme has two sources that must stay in step.

``pyproject.toml`` carries the released version as ``<upstream base>.postN``;
``Cargo.toml`` carries only the upstream base, because Cargo requires semver and
semver cannot spell ``.postN``. Nothing in the build enforces the relationship,
so a one-sided rebase would silently leave the crate reporting a base the wheel
was never built from -- and that base is exactly what ``__version__`` falls back
to for a source checkout. These tests are the enforcement.
"""

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# "<base>.postN" -- the base is what Cargo.toml must also carry.
_FORK_VERSION = re.compile(r"^(?P<base>\d+\.\d+\.\d+)\.post(?P<patch>[1-9]\d*)$")


def _load(name):
    with (_ROOT / name).open("rb") as fh:
        return tomllib.load(fh)


def test_project_version_follows_fork_scheme():
    version = _load("pyproject.toml")["project"]["version"]
    assert _FORK_VERSION.match(version), (
        f"pyproject version {version!r} does not follow the fork scheme "
        "'<upstream base>.postN' documented in CHANGELOG.md"
    )


def test_cargo_version_is_the_upstream_base_of_the_project_version():
    project_version = _load("pyproject.toml")["project"]["version"]
    cargo_version = _load("Cargo.toml")["package"]["version"]
    match = _FORK_VERSION.match(project_version)
    assert match is not None, project_version
    assert cargo_version == match.group("base"), (
        f"Cargo.toml version {cargo_version!r} is not the upstream base of the "
        f"released version {project_version!r}. Bumping one without the other "
        "makes websocket_rs.__version__ report a wrong base in a source "
        "checkout, where it falls back to CARGO_PKG_VERSION."
    )


def test_distribution_name_is_the_fork_name():
    # __init__.py looks this exact name up in the installed metadata; a rename
    # that missed it would send every install down the fallback path.
    assert _load("pyproject.toml")["project"]["name"] == "websocket-rs-nateyoder"
    source = (_ROOT / "websocket_rs" / "__init__.py").read_text(encoding="utf-8")
    assert '_dist_version("websocket-rs-nateyoder")' in source


# --- Release URLs pinned in the docs -----------------------------------------
#
# This fork is distributed as GitHub release assets, so every install command in
# the docs pins a tag and a version. Nothing in the build regenerates them, so a
# release that bumps pyproject.toml without sweeping the docs leaves every
# documented command installing the *previous* release. These tests are the
# sweep, enforced.

_DOC_FILES = (
    "README.md",
    "README.zh-TW.md",
    "README.pypi.md",
    "docs/API.md",
    "docs/TLS-BACKENDS.md",
)

_TAGGED_URL = re.compile(
    r"https://github\.com/nateyoder/websocket-rs/releases/"
    r"(?:expanded_assets|download)/v(?P<version>[\w.]+)"
)
_PINNED_REQUIREMENT = re.compile(r"websocket-rs-nateyoder(?:\[[\w,-]+\])?==(?P<version>[\w.]+)")


def _doc_sources():
    for name in _DOC_FILES:
        # Explicit encoding: README.zh-TW.md is UTF-8 with CJK text, and
        # read_text() would otherwise decode it with the locale default --
        # cp1252 on the Windows runners, which raises UnicodeDecodeError.
        yield name, (_ROOT / name).read_text(encoding="utf-8")


def test_release_urls_in_docs_point_at_the_current_version():
    expected = _load("pyproject.toml")["project"]["version"]
    stale = [
        (name, m.group("version"))
        for name, text in _doc_sources()
        for m in _TAGGED_URL.finditer(text)
        if m.group("version") != expected
    ]
    assert not stale, (
        f"release URLs pin a tag other than v{expected}: {stale}. Bumping the "
        "version means sweeping the docs; see the release checklist in "
        "docs/PUBLISHING.md."
    )


def test_documented_install_commands_pin_the_current_version():
    expected = _load("pyproject.toml")["project"]["version"]
    stale = [
        (name, m.group("version"))
        for name, text in _doc_sources()
        for m in _PINNED_REQUIREMENT.finditer(text)
        if m.group("version") != expected
    ]
    assert not stale, (
        f"install commands pin a version other than {expected}: {stale}"
    )


def test_every_documented_find_links_install_is_version_pinned():
    # The tag in a --find-links URL selects which release is *offered*; it does
    # not constrain what the resolver *picks*, because the default index stays
    # enabled. An unpinned command is therefore not pinned at all.
    unpinned = []
    for name, text in _doc_sources():
        for line in text.splitlines():
            if "--find-links" not in line and "expanded_assets" not in line:
                continue
            if "websocket-rs-nateyoder" not in line:
                continue
            if "find-links = [" in line:  # the [tool.uv] table; pin is on the requirement
                continue
            if not _PINNED_REQUIREMENT.search(line):
                unpinned.append((name, line.strip()))
    assert not unpinned, f"unpinned --find-links install commands: {unpinned}"
