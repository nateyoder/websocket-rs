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
    source = (_ROOT / "websocket_rs" / "__init__.py").read_text()
    assert '_dist_version("websocket-rs-nateyoder")' in source
