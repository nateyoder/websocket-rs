"""Native asyncio.Protocol-based WebSocket client (Rust pyclass).

Runs on the asyncio event loop thread — no tokio runtime, no cross-thread
wakeup. Frame codec in Rust with AVX2-vectorised masking. Targets parity with
or better than picows while remaining a pure Python-facing API.

All API-parity items vs the legacy ``async_client`` are implemented; the
legacy module is deprecated and will be removed in 2.0.
"""

from __future__ import annotations

import asyncio
import ssl as _ssl
from collections.abc import AsyncIterator, Callable
from types import TracebackType
from typing import Self

class WSMessage:
    """Zero-copy view over a received WebSocket frame payload.

    Implements the Python buffer protocol so ``memoryview(msg)``,
    ``struct.unpack_from(...)``, and ``msg[:n]`` slicing are cheap.
    ``bytes(msg)`` is the only operation that forces a copy.
    """

    def __len__(self) -> int: ...
    def __bytes__(self) -> bytes: ...
    def __getitem__(self, key: int | slice) -> int | bytes: ...
    def __eq__(self, other: object) -> bool: ...
    def __hash__(self) -> int: ...
    def __repr__(self) -> str: ...

class NativeClient:
    """WebSocket client integrated directly with asyncio.Protocol.

    Instances are returned by ``native_client.connect()``. Direct construction
    via ``NativeClient()`` is not supported.
    """

    def send(self, message: str | bytes) -> None:
        """Fire-and-forget send. Synchronous; encodes a frame straight into a
        ``PyBytes`` buffer and hands it to ``transport.write``. Raises
        ``RuntimeError`` if the connection is closed."""
        ...

    async def recv(self) -> WSMessage:
        """Wait for the next server message and return it as a zero-copy
        :class:`WSMessage`."""
        ...

    def ping(self, data: bytes | None = None) -> None:
        """Send a ping (opcode 0x9) control frame. Payload must be ≤125 bytes."""
        ...

    def ping_waiter(self, data: bytes | None = None) -> asyncio.Future[None]:
        """Send a Ping immediately; resolve on its matching Pong.

        Payloads are at most 125 bytes. Duplicate outstanding payloads raise
        ValueError; use fresh bytes per probe to distinguish delayed replies.
        Cancellation affects only this probe. Closure raises ConnectionError.
        """
        ...

    def close(self) -> None:
        """Send a close frame (best-effort) and close the underlying transport."""
        ...

    # Async iteration: ``async for msg in ws:``
    def __aiter__(self) -> AsyncIterator[WSMessage]: ...
    async def __anext__(self) -> WSMessage: ...

    # Async context manager: ``async with await connect(...) as ws:``
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
    @property
    def is_open(self) -> bool: ...
    @property
    def subprotocol(self) -> str | None: ...
    @property
    def close_received_code(self) -> int | None: ...
    @property
    def close_received_reason(self) -> str | None: ...
    @property
    def close_sent_code(self) -> int | None: ...
    @property
    def close_sent_reason(self) -> str | None: ...
    @property
    def close_code(self) -> int | None: ...
    @property
    def close_reason(self) -> str | None: ...
    @property
    def closed(self) -> bool:
        """True once the connection has been torn down (client or peer)."""
        ...
    # Addresses proxy transport.get_extra_info(); IPv6 peers report
    # 4-tuples (host, port, flowinfo, scope), not plain (host, port).
    @property
    def local_address(self) -> tuple[str | bytes, int, ...] | None: ...
    @property
    def remote_address(self) -> tuple[str | bytes, int, ...] | None: ...

    def pong(self, data: bytes | None = None) -> None:
        """Send a pong frame proactively; same 125-byte limit as ping."""
        ...

async def connect(
    uri: str,
    *,
    headers: list[tuple[str, str]] | None = None,
    subprotocols: list[str] | None = None,
    ssl_context: _ssl.SSLContext | None = None,
    connect_timeout: float | None = None,
    receive_timeout: float | None = None,
    proxy: str | None = None,
    compression: bool = False,
    on_message: Callable[[WSMessage], None] | None = None,
    tls_backend: str = "auto",
    rustls_ca_file: str | None = None,
    zero_copy_min_bytes: int | None = None,
) -> NativeClient:
    """Connect to ``uri`` (``ws://`` or ``wss://``) and complete the handshake.

    - ``headers`` adds arbitrary request headers (reserved names — Host,
      Upgrade, Connection, Sec-WebSocket-* — are filtered automatically).
    - ``subprotocols`` sets ``Sec-WebSocket-Protocol``; the negotiated value
      is then on :attr:`NativeClient.subprotocol`.
    - ``ssl_context`` overrides the default ``ssl.create_default_context()``
      used for ``wss://``. TLS is driven by the transport so the protocol
      sees decrypted bytes. Not accepted by ``tls_backend="rustls"``.
    - ``connect_timeout`` defaults to 10 seconds when omitted or ``None`` and
      wraps the full TCP+TLS+handshake sequence in ``asyncio.wait_for``;
      raises ``TimeoutError`` on expiry.
    - ``receive_timeout=None`` (the default) waits indefinitely. A numeric
      value wraps every ``recv()`` / ``async for`` step in
      ``asyncio.wait_for``; the backlog fast-path is not wrapped so already
      queued messages return immediately.
    - ``proxy`` accepts ``socks5://[user:password@]host:port``. Handshake
      runs in ``loop.run_in_executor`` so the event loop stays responsive;
      once the tunnel is up all traffic goes through the native
      zero-copy hot path. ``picows`` does not support proxies.
    - ``compression=True`` negotiates the ``permessage-deflate`` extension
      (RFC 7692) with ``server_no_context_takeover`` +
      ``client_no_context_takeover`` for bounded per-message memory. If the
      server doesn't echo the extension header the client silently falls
      back to sending uncompressed frames. ``picows`` exposes the RSV1 bit
      but does not compress or decompress for you.
    - ``tls_backend`` picks the transport that carries ``wss://`` traffic;
      it is ignored for ``ws://``.

      * ``"auto"`` (default) uses ``aiofastnet`` when it is importable and
        falls back to ``loop.create_connection`` otherwise. aiofastnet's
        OpenSSL transport measured ~+10% request throughput at both 256 B and
        8 KiB versus asyncio's SSLProtocol; install it with the ``fast-tls``
        extra. On Windows aiofastnet is never selected — it registers I/O with
        ``loop.add_reader``, which the default ProactorEventLoop does not
        implement — so ``"auto"`` always uses the stdlib path there.
      * ``"asyncio"`` pins the stdlib path.
      * ``"aiofastnet"`` requires aiofastnet and raises ``RuntimeError`` if it
        is not installed, for callers who would rather fail than silently run
        slower. It also raises ``RuntimeError`` on Windows, where the backend
        is unsupported, before any socket work happens.
      * ``"rustls"`` terminates TLS in Rust on the event-loop thread. It is in the
        published wheels; bare ``cargo`` builds need the ``rustls-transport``
        feature. It takes ``rustls_ca_file`` instead of
        ``ssl_context`` and does not implement client-certificate auth.
    - ``rustls_ca_file`` is a PEM trust store for ``tls_backend="rustls"``.
      Omit it to use the platform's native root certificates. Passing it with
      any other backend raises ``ValueError``.
    - ``on_message`` switches delivery to a synchronous callback invoked
      from the asyncio Protocol ``data_received`` path. When set, messages
      are NOT queued for :meth:`NativeClient.recv` — the callback receives
      each :class:`WSMessage` directly and must not ``await``. Leave as
      ``None`` for typical async/await usage.
    - ``zero_copy_min_bytes`` (default 4096) is the payload size at or above
      which a received payload is sliced out of the receive buffer instead of
      copied, saving a memcpy per message on the BufferedProtocol path. A
      slice keeps its whole backing chunk alive — tens of KiB per read — so a
      retained payload costs far more than its own length, and that
      amplification applies to every sliced payload, including ones well above
      the threshold; the threshold only bounds it below itself. Measured with
      one 5000 B message per read and 20,000 messages all retained: 374.9 MB
      max RSS at the default against 100 MB of live payload, versus 145.6 MB
      with everything copied. Lower it when payloads are consumed and dropped
      promptly; raise it past your typical message size (or convert payloads
      to ``bytes`` on receipt) if you queue raw payloads deeply.
    """
    ...


class NativeClientBuffered(NativeClient):
    """Subclass of :class:`NativeClient` exposing the BufferedProtocol hooks
    (``get_buffer`` + ``buffer_updated``), which let the loop write kernel
    data straight into an internal reusable buffer and skip the per-recv
    ``bytes`` allocation the plain Protocol path incurs. ~15% win on 64 KB
    pipelined throughput vs the base class.

    **That win is uvloop-only.** Whether the hooks are used at all is the
    loop's decision, and the two rules differ: uvloop duck-types, so it
    calls them; stdlib asyncio gates on
    ``isinstance(protocol, asyncio.BufferedProtocol)``
    (``selector_events.py``), and this is a pyclass rather than a subclass
    of it, so under plain asyncio the transport silently falls back to
    ``data_received`` and these hooks are never called. Nothing breaks —
    the fallback is the ordinary receive path — but the allocation saving
    does not apply. aiofastnet uses the same ``isinstance`` rule, with an
    ``is_buffered_protocol()`` escape hatch this class does not implement.

    Instances are produced transparently by :func:`connect` when the URI
    scheme is ``ws://`` (plain TCP). For ``wss://``, :func:`connect`
    returns a bare :class:`NativeClient`. Routing TLS through this class
    instead was measured on the aiofastnet transport and is neutral, so
    the simpler class stays: see ``FOLLOWUPS.md`` F10.

    Do not instantiate directly; always go through :func:`connect`.
    """
    ...
