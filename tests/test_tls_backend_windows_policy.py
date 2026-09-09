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
