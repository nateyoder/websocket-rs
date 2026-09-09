
import asyncio as _asyncio
import socket as _socket
import sys as _sys
from functools import partial as _partial


def _recv_exact(sock, size, stage):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError(f"SOCKS5 proxy closed during {stage}")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _socks5_connect_blocking(proxy_host, proxy_port, user, password, target_host, target_port):
    """Blocking SOCKS5 CONNECT. Designed to run inside loop.run_in_executor so it
    never blocks the asyncio event loop. Returns a connected, non-blocking socket
    tunnelled through the proxy to (target_host, target_port)."""
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.connect((proxy_host, proxy_port))
        s.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        methods = b"\x00" if not user else b"\x00\x02"
        s.sendall(b"\x05" + bytes([len(methods)]) + methods)
        reply = _recv_exact(s, 2, "greeting")
        if reply[0] != 0x05:
            raise ConnectionError("SOCKS5 proxy rejected greeting")
        method = reply[1]
        if method == 0x02:
            if not user:
                raise ConnectionError("SOCKS5 proxy requires auth but none supplied")
            ub, pb = user.encode(), password.encode()
            s.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
            ar = _recv_exact(s, 2, "authentication")
            if ar[1] != 0x00:
                raise ConnectionError("SOCKS5 auth failed")
        elif method != 0x00:
            raise ConnectionError(f"SOCKS5 proxy selected unsupported method {method}")
        host_b = target_host.encode("idna")
        req = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + int(target_port).to_bytes(2, "big")
        s.sendall(req)
        hdr = _recv_exact(s, 4, "CONNECT reply")
        if hdr[1] != 0x00:
            raise ConnectionError(f"SOCKS5 CONNECT failed: status={hdr[1]}")
        atyp = hdr[3]
        if atyp == 0x01:
            _recv_exact(s, 4, "IPv4 bind address")
        elif atyp == 0x03:
            nlen = _recv_exact(s, 1, "domain bind length")[0]
            _recv_exact(s, nlen, "domain bind address")
        elif atyp == 0x04:
            _recv_exact(s, 16, "IPv6 bind address")
        else:
            raise ConnectionError(f"SOCKS5 returned unsupported ATYP {atyp}")
        _recv_exact(s, 2, "bind port")
        s.setblocking(False)
        return s
    except Exception:
        s.close()
        raise


def _parse_proxy_uri(proxy):
    # socks5://[user:password@]host:port
    from urllib.parse import unquote, urlsplit
    parts = urlsplit(proxy)
    if parts.scheme not in ("socks5", "socks5h"):
        raise ValueError(f"Only socks5:// proxies are supported (got {parts.scheme})")
    user = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else ""
    if not parts.hostname or not parts.port:
        raise ValueError("SOCKS5 proxy URI must include host and port")
    return parts.hostname, parts.port, user, password


# aiofastnet ships an OpenSSL-backed asyncio transport whose TLS path is
# measurably faster than asyncio's SSLProtocol (see
# docs/performance-audit/TLS-OPTIMIZATION.md). It is an optional dependency, so
# resolution is memoised and a missing or broken install degrades to asyncio
# rather than failing the connect.
_AIOFASTNET_UNRESOLVED = object()
_aiofastnet_create_connection = _AIOFASTNET_UNRESOLVED

# aiofastnet registers its socket with ``loop.add_reader``, which the Windows
# default event loop (ProactorEventLoop since 3.8) does not implement, so every
# wss:// connect would raise NotImplementedError. The gate is deliberately the
# whole platform rather than a probe of the running loop: Windows is untested
# here, and a narrower rule is what produced the bug in the first place.
_IS_WINDOWS = _sys.platform == "win32"


def _resolve_aiofastnet():
    """Return ``aiofastnet.create_connection``, or None when unavailable.

    Imported once per process; the result (including the None) is cached so
    the connect path never re-enters the import machinery.
    """
    global _aiofastnet_create_connection
    if _aiofastnet_create_connection is _AIOFASTNET_UNRESOLVED:
        try:
            import aiofastnet
            _aiofastnet_create_connection = aiofastnet.create_connection
        except Exception:
            _aiofastnet_create_connection = None
    return _aiofastnet_create_connection


def _select_create_connection(loop, is_tls, tls_backend):
    """Pick the create_connection used for this connection.

    Only wss:// is routed through aiofastnet: the plain-TCP path already runs
    on asyncio's BufferedProtocol fast path, and the measured win is in the TLS
    layer. "auto" prefers aiofastnet when importable, "aiofastnet" demands it,
    "asyncio" pins the stdlib path. On Windows aiofastnet is never selected:
    "auto" stays on asyncio and "aiofastnet" is an error.
    """
    if not is_tls or tls_backend == "asyncio":
        return loop.create_connection
    if _IS_WINDOWS:
        if tls_backend == "aiofastnet":
            raise RuntimeError(
                'tls_backend="aiofastnet" is not supported on Windows: aiofastnet '
                "drives its I/O through loop.add_reader, which the Windows default "
                "ProactorEventLoop does not implement. Use tls_backend=\"auto\" or "
                '"asyncio".'
            )
        return loop.create_connection
    create_connection = _resolve_aiofastnet()
    if create_connection is None:
        if tls_backend == "aiofastnet":
            raise RuntimeError(
                'tls_backend="aiofastnet" requires the aiofastnet package: '
                "pip install aiofastnet"
            )
        return loop.create_connection
    return _partial(create_connection, loop)


async def _connect_helper(loop, protocol_factory, host, port, is_tls, ssl_ctx,
                          proxy, req_bytes, handshake_fut, client, connect_timeout,
                          tls_backend="auto"):
    async def _do():
        create_connection = _select_create_connection(loop, is_tls, tls_backend)
        kwargs = {}
        if is_tls:
            kwargs["ssl"] = ssl_ctx
            kwargs["server_hostname"] = host
        if proxy:
            proxy_host, proxy_port, user, password = _parse_proxy_uri(proxy)
            sock = await loop.run_in_executor(
                None, _socks5_connect_blocking,
                proxy_host, proxy_port, user, password, host, port,
            )
            # Hand the already-connected socket to asyncio. TLS (if any) runs
            # on top of it; asyncio will perform the TLS handshake itself.
            kwargs["sock"] = sock
            transport, _proto = await create_connection(protocol_factory, **kwargs)
        else:
            transport, _proto = await create_connection(
                protocol_factory, host, port, **kwargs
            )
            try:
                s = transport.get_extra_info("socket")
                if s is not None:
                    s.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except Exception:
                pass
        if tls_backend == "rustls":
            # The TLS shim is the protocol asyncio drives; the upgrade request
            # has to be written through it so it gets encrypted, not to the raw
            # TCP transport underneath. connect() only passes "rustls" down when
            # it actually built a shim, so this cannot fire on a plain ws://
            # connection where _proto would be the NativeClient itself.
            transport = _proto
        try:
            transport.write(bytes(req_bytes))
            await handshake_fut
            return client
        finally:
            # Exception tracebacks may outlive the event-loop thread. Never
            # leave a native protocol or transport in these retained locals.
            transport = None
            _proto = None
    try:
        if connect_timeout is not None:
            return await _asyncio.wait_for(_do(), timeout=connect_timeout)
        return await _do()
    except BaseException:
        # TCP/TLS setup may fail before the upgrade Future has a consumer.
        handshake_fut.cancel()
        client.close()
        raise
    finally:
        # _do closes over these cells. Clearing them also detaches native
        # references from retained coroutine/timeout/cancellation tracebacks.
        client = None
        protocol_factory = None
        handshake_fut = None
        loop = None
        _do = None
