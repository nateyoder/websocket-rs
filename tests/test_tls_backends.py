"""TLS coverage for the native client across every selectable ``tls_backend``.

The rest of the suite only exercises ``ws://``, so nothing here overlaps with it.
Each backend runs the same battery against a real ``wss://`` peer, which also
pins the property that matters most for the aiofastnet and rustls transports:
``NativeClient`` must never take its raw-socket send fast path on a TLS
connection. A leak there writes plaintext frames straight to the fd, the peer
fails to decrypt, and every round-trip assertion below fails.
"""

import asyncio
import shutil
import ssl
import subprocess
import sys

import pytest

from websocket_rs.native_client import connect

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the TLS harness shells out to openssl and uses a POSIX-only server fixture",
)


def _have(name):
    try:
        __import__(name)
    except ImportError:
        return False
    return True


HAVE_AIOFASTNET = _have("aiofastnet")

# These deadlines exist to turn a hang into a failure, not to assert latency:
# every operation below completes in milliseconds on an idle loopback. Keep
# them generous so a loaded CI runner does not produce a spurious failure.
RECV_TIMEOUT = 30


def _rustls_available():
    """True when the extension was built with the `rustls-transport` feature.

    Argument validation runs before ``connect`` touches the event loop, so this
    probe never opens a socket: without the feature it raises ValueError naming
    the feature, and with it we get some other failure instead.
    """
    try:
        connect("wss://127.0.0.1:1/", tls_backend="rustls")
    except ValueError as exc:
        return "cargo feature" not in str(exc)
    except Exception:
        return True
    return True


@pytest.fixture(scope="session")
def certs(tmp_path_factory):
    """Self-signed localhost cert/key, generated per session.

    Generated rather than committed so the suite needs no `make tls-certs` step
    and no checked-in private key.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required to generate the test certificate")
    d = tmp_path_factory.mktemp("certs")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-days", "1", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "extendedKeyUsage=serverAuth",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _backend_kwargs(backend, cert):
    """Connect kwargs that trust `cert` under the given backend.

    rustls does not accept an ``ssl.SSLContext``; it takes a PEM trust file.
    """
    if backend == "rustls":
        return {"tls_backend": "rustls", "rustls_ca_file": str(cert)}
    return {"tls_backend": backend, "ssl_context": ssl.create_default_context(cafile=str(cert))}


BACKENDS = [
    pytest.param("auto", id="auto"),
    pytest.param("asyncio", id="asyncio"),
    pytest.param(
        "aiofastnet",
        id="aiofastnet",
        marks=pytest.mark.skipif(not HAVE_AIOFASTNET, reason="aiofastnet is not installed"),
    ),
    pytest.param(
        "rustls",
        id="rustls",
        marks=pytest.mark.skipif(
            not _rustls_available(), reason="built without the rustls-transport feature"
        ),
    ),
]


class _Peer:
    """A wss:// echo peer that also drives the checks needing server-side action."""

    def __init__(self, cert, key, handler=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self._ctx = ctx
        self._handler_override = handler
        self.done = None
        self._server = None
        self.url = None

    async def __aenter__(self):
        from websockets.asyncio.server import serve

        self.done = asyncio.get_running_loop().create_future()
        self._server = await serve(
            self._handler_override or self._handler,
            "127.0.0.1",
            0,
            ssl=self._ctx,
            compression=None,
        ).__aenter__()
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"wss://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        await self._server.__aexit__(*exc)

    async def _handler(self, ws):
        try:
            # A fragmented text message: the client must reassemble it.
            await ws.send(["frag", "mented"])
            for i in range(32):
                assert await ws.recv() == str(i)
                await ws.send(str(i))
            # 256 KiB spans many TLS records, so this covers record reassembly.
            big = await ws.recv()
            assert big == b"\xa5" * (256 * 1024)
            await ws.send(big)
            self.done.set_result(True)
            await ws.wait_closed()
        except Exception as exc:  # surfaced by the test via `await peer.done`
            if not self.done.done():
                self.done.set_exception(exc)


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tls_answers_a_server_ping(backend, certs):
    """The client must answer a server Ping with a Pong on the TLS receive path.

    Kept apart from the round trip below on purpose. Answering a Ping while the
    client has a send burst in flight has been seen to stall on a heavily loaded
    host — on plain ``ws://`` as well, so it is not a property of any TLS backend
    (FOLLOWUPS F13). Isolating it means that flake cannot take the rest of the
    TLS coverage with it.
    """
    cert, key = certs

    async def ping_peer(ws):
        await asyncio.wait_for(await ws.ping(b"tls-control"), RECV_TIMEOUT)
        await ws.send(b"ponged")
        await ws.wait_closed()

    async with _Peer(cert, key, handler=ping_peer) as peer:
        ws = await connect(peer.url, connect_timeout=10, **_backend_kwargs(backend, cert))
        try:
            assert bytes(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT)) == b"ponged"
        finally:
            ws.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tls_round_trip(backend, certs):
    """Fragments, ordering and a multi-record payload, per backend."""
    cert, key = certs
    async with _Peer(cert, key) as peer:
        ws = await connect(peer.url, connect_timeout=10, **_backend_kwargs(backend, cert))
        try:
            assert bytes(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT)) == b"fragmented"
            for i in range(32):
                ws.send(str(i))
            for i in range(32):
                assert bytes(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT)) == str(i).encode()
            payload = b"\xa5" * (256 * 1024)
            ws.send(payload)
            assert bytes(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT)) == payload
            await asyncio.wait_for(peer.done, RECV_TIMEOUT)
        finally:
            ws.close()


@pytest.mark.parametrize("backend", BACKENDS)
async def test_tls_rejects_untrusted_certificate(backend, certs):
    """A backend that silently skipped verification would be a security regression."""
    cert, key = certs
    async with _Peer(cert, key) as peer:
        # No trust material of any kind: every backend falls back to system
        # roots, which do not sign this certificate.
        with pytest.raises((ssl.SSLCertVerificationError, ssl.SSLError, ConnectionError)):
            await connect(peer.url, connect_timeout=10, tls_backend=backend)


@pytest.fixture
def helper():
    """The private Python module holding the create_connection selection policy."""
    import websocket_rs  # noqa: F401  (import registers the helper module)

    return sys.modules["websocket_rs._native_connect_helper"]


@pytest.mark.skipif(not HAVE_AIOFASTNET, reason="aiofastnet is not installed")
async def test_auto_routes_tls_through_aiofastnet(helper, certs):
    """`auto` is only worth defaulting to if it actually picks the fast transport."""
    cert, _key = certs
    calls = []
    real = helper._resolve_aiofastnet()
    assert real is not None

    async def spy(*args, **kwargs):
        calls.append(True)
        return await real(*args, **kwargs)

    saved = helper._aiofastnet_create_connection
    helper._aiofastnet_create_connection = spy
    async def idle(ws):
        """The spy test only needs the handshake, so it must not fail on an
        immediate client close the way the full battery handler would."""
        await ws.wait_closed()

    try:
        async with _Peer(*certs, handler=idle) as peer:
            ws = await connect(
                peer.url,
                connect_timeout=10,
                ssl_context=ssl.create_default_context(cafile=str(cert)),
            )
            ws.close()
    finally:
        helper._aiofastnet_create_connection = saved
    assert calls, "tls_backend='auto' did not route wss:// through aiofastnet"


def test_plain_tcp_never_uses_aiofastnet(helper):
    """ws:// keeps the asyncio BufferedProtocol path; only TLS is rerouted."""
    loop = asyncio.new_event_loop()
    try:
        for backend in ("auto", "asyncio", "aiofastnet", "rustls"):
            assert (
                helper._select_create_connection(loop, False, backend) == loop.create_connection
            )
    finally:
        loop.close()


def test_asyncio_backend_pins_the_stdlib_path(helper):
    loop = asyncio.new_event_loop()
    try:
        assert helper._select_create_connection(loop, True, "asyncio") == loop.create_connection
    finally:
        loop.close()


def test_auto_falls_back_when_aiofastnet_is_missing(helper):
    """A missing optional dependency must degrade, not break the connect."""
    loop = asyncio.new_event_loop()
    saved = helper._aiofastnet_create_connection
    helper._aiofastnet_create_connection = None
    try:
        assert helper._select_create_connection(loop, True, "auto") == loop.create_connection
        with pytest.raises(RuntimeError, match="aiofastnet"):
            helper._select_create_connection(loop, True, "aiofastnet")
    finally:
        helper._aiofastnet_create_connection = saved
        loop.close()


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="tls_backend must be one of"):
        connect("wss://127.0.0.1:1/", tls_backend="openssl")


def test_rustls_ca_file_requires_the_rustls_backend():
    with pytest.raises(ValueError, match="rustls_ca_file"):
        connect("wss://127.0.0.1:1/", rustls_ca_file="/nonexistent.pem")


def test_the_published_build_includes_rustls():
    """pyproject enables rustls-transport for every maturin build, and downstream code
    pins tls_backend="rustls". A build without it must fail here, not quietly skip the
    rustls cells below."""
    assert _rustls_available()


@pytest.mark.skipif(not _rustls_available(), reason="built without the rustls-transport feature")
def test_rustls_rejects_an_ssl_context():
    with pytest.raises(ValueError, match="ssl_context"):
        connect(
            "wss://127.0.0.1:1/",
            tls_backend="rustls",
            ssl_context=ssl.create_default_context(),
        )


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("asyncio", id="asyncio"),
        pytest.param(
            "aiofastnet",
            id="aiofastnet",
            marks=pytest.mark.skipif(not HAVE_AIOFASTNET, reason="aiofastnet is not installed"),
        ),
    ],
)
async def test_tls_through_a_socks5_proxy(backend, certs):
    """wss:// over SOCKS5 hands a pre-connected socket to create_connection.

    That is a different call shape from the host/port form — ``sock=`` plus
    ``ssl=`` plus ``server_hostname=`` — so swapping the transport under it is
    exactly where proxied TLS would break without anyone noticing. rustls is
    excluded: it has no proxy path yet (FOLLOWUPS F11).
    """
    import threading

    from tests.bench_socks5_handshake import _serve_socks5

    cert, _key = certs
    ready = threading.Event()
    bound = []
    threading.Thread(
        target=_serve_socks5, args=(0, ready), kwargs={"bound": bound}, daemon=True
    ).start()
    assert ready.wait(timeout=5), "SOCKS5 proxy did not start"
    proxy_port = bound[0]

    async with _Peer(*certs, handler=lambda ws: ws.send(b"proxied")) as peer:
        ws = await connect(
            peer.url,
            proxy=f"socks5://127.0.0.1:{proxy_port}",
            connect_timeout=10,
            **_backend_kwargs(backend, cert),
        )
        try:
            assert bytes(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT)) == b"proxied"
        finally:
            ws.close()


@pytest.mark.skipif(not _rustls_available(), reason="built without the rustls-transport feature")
async def test_rustls_backend_is_inert_on_plain_tcp():
    """ws:// has no TLS layer to replace, so the backend must be ignored, not
    half-applied: the handshake write still goes to the raw transport, and no
    TLS shim is built to route it through."""
    from websockets.asyncio.server import serve

    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    async with serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await connect(f"ws://127.0.0.1:{port}", tls_backend="rustls", connect_timeout=10)
        try:
            ws.send(b"plain")
            assert bytes(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT)) == b"plain"
        finally:
            ws.close()


@pytest.mark.skipif(not _rustls_available(), reason="built without the rustls-transport feature")
async def test_rustls_delivers_on_message_callbacks(certs):
    """The rustls shim owns callback delivery for every record it decrypts.

    Messages parsed out of decrypted plaintext are only queued during the parse;
    the shim's own flush is what hands them to ``on_message``. Nothing else in
    the suite drives the callback path over rustls, so dropping that flush would
    otherwise go unnoticed. The server writes both frames in a single raw
    ``transport.write`` so they arrive in one record and one ``data_received``.
    """
    cert, key = certs
    seen = []
    done = asyncio.get_running_loop().create_future()

    def on_message(message):
        seen.append(bytes(message))
        if len(seen) == 2 and not done.done():
            done.set_result(True)

    async def burst(ws):
        ws.transport.write(b"\x81\x05first\x81\x06second")
        await ws.wait_closed()

    async with _Peer(cert, key, handler=burst) as peer:
        ws = await connect(
            peer.url,
            connect_timeout=10,
            on_message=on_message,
            **_backend_kwargs("rustls", cert),
        )
        try:
            await asyncio.wait_for(done, RECV_TIMEOUT)
        finally:
            ws.close()
    assert seen == [b"first", b"second"]
