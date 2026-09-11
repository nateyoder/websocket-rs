"""Platform policy for the aiofastnet TLS backend.

aiofastnet drives its I/O through ``loop.add_reader``, which the Windows
default ProactorEventLoop does not implement, so selecting it there turns every
``wss://`` connect into a bare ``NotImplementedError``. The selection policy
therefore refuses aiofastnet on Windows outright.

Unlike ``tests/test_tls_backends.py`` (which needs a POSIX-only ``wss://``
fixture and is skipped on win32), this module exercises the pure selection
function and runs on every platform in CI, including the Windows matrix that
installs aiofastnet.
"""

import asyncio
import sys

import pytest


@pytest.fixture
def helper():
    import websocket_rs  # noqa: F401  (import registers the helper module)

    return sys.modules["websocket_rs._native_connect_helper"]


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def test_auto_never_selects_aiofastnet_on_windows(helper, loop):
    saved = helper._IS_WINDOWS
    helper._IS_WINDOWS = True
    try:
        assert helper._select_create_connection(loop, True, "auto") == loop.create_connection
    finally:
        helper._IS_WINDOWS = saved


def test_explicit_aiofastnet_on_windows_raises_naming_the_gap(helper, loop):
    saved = helper._IS_WINDOWS
    helper._IS_WINDOWS = True
    try:
        with pytest.raises(RuntimeError, match="ProactorEventLoop"):
            helper._select_create_connection(loop, True, "aiofastnet")
    finally:
        helper._IS_WINDOWS = saved


def test_windows_gate_matches_the_running_platform(helper):
    """The gate is a blanket platform check, not a probe of the event loop."""
    assert helper._IS_WINDOWS == (sys.platform == "win32")


def test_non_windows_keeps_the_aiofastnet_path(helper, loop):
    """Forcing the gate off must not change Linux/macOS behaviour."""
    saved = helper._IS_WINDOWS
    helper._IS_WINDOWS = False
    saved_cc = helper._aiofastnet_create_connection
    sentinel = object()

    def fake_create_connection(*args, **kwargs):  # pragma: no cover - never called
        return sentinel

    helper._aiofastnet_create_connection = fake_create_connection
    try:
        selected = helper._select_create_connection(loop, True, "auto")
        assert selected != loop.create_connection
        assert selected.func is fake_create_connection
    finally:
        helper._aiofastnet_create_connection = saved_cc
        helper._IS_WINDOWS = saved


def test_the_published_build_includes_rustls():
    """pyproject enables rustls-transport for every maturin build and downstream code
    pins tls_backend="rustls", so a build without it must fail CI on every platform.

    It lives here rather than in tests/test_tls_backends.py because that module is
    skipped on Windows, and a Windows wheel missing the feature would pass unnoticed.
    Argument validation runs before connect touches an event loop, so this opens no
    socket: without the feature connect raises ValueError naming it.
    """
    from websocket_rs.native_client import connect

    try:
        connect("wss://127.0.0.1:1/", tls_backend="rustls")
    except ValueError as exc:
        assert "cargo feature" not in str(exc), str(exc)
    except Exception:
        pass
