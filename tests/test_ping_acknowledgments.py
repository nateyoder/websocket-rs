"""Controlled wire peers for caller-owned native Ping acknowledgment deadlines."""

import asyncio
import base64
import contextlib
import hashlib
import ssl
import subprocess
import sys

import pytest

from websocket_rs import connect


def frame(opcode, payload=b""):
    assert len(payload) <= 125
    return bytes([opcode, len(payload)]) + payload


async def read_frame(reader):
    first, length = await reader.readexactly(2)
    assert length & 128, "client frames must be masked"
    length &= 127
    assert length <= 125
    mask = await reader.readexactly(4)
    payload = await reader.readexactly(length)
    return first, bytes(value ^ mask[i % 4] for i, value in enumerate(payload))


@pytest.fixture(scope="module")
def tls_contexts(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ping-tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost"],
        check=True, capture_output=True,
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    client.check_hostname = False
    return server, client


@pytest.fixture(params=["asyncio", "uvloop"])
def loop_factory(request):
    if request.param == "uvloop":
        return pytest.importorskip("uvloop").new_event_loop
    return asyncio.SelectorEventLoop


@contextlib.asynccontextmanager
async def peer(tls):
    connected = asyncio.get_running_loop().create_future()
    finished = asyncio.Event()

    async def accept(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(b":", 1) for line in request.split(b"\r\n")[1:] if b":" in line)
            key = headers[b"Sec-WebSocket-Key"].strip()
            accept_key = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept_key + b"\r\n\r\n")
            await writer.drain()
            connected.set_result((reader, writer))
            await finished.wait()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(accept, "127.0.0.1", 0, ssl=tls[0] if tls else None)
    port = server.sockets[0].getsockname()[1]
    ws = await connect(f"{'wss' if tls else 'ws'}://127.0.0.1:{port}", **({"ssl_context": tls[1]} if tls else {}))
    reader, writer = await connected
    try:
        yield ws, reader, writer
    finally:
        ws.close()
        finished.set()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("secure", [False, True])
def test_correlated_acknowledgments(loop_factory, tls_contexts, secure):
    async def scenario():
        async with peer(tls_contexts if secure else None) as (ws, reader, writer):
            # The peer responds before the caller starts awaiting the Future.
            first = ws.ping_waiter(b"first")
            assert await read_frame(reader) == (0x89, b"first")
            writer.write(frame(0x8A, b"first"))
            await asyncio.sleep(0.01)
            assert first.done()
            assert await first is None
            a, b = ws.ping_waiter(b"a"), ws.ping_waiter(b"b")
            with pytest.raises(ValueError, match="outstanding"):
                ws.ping_waiter(b"a")
            assert await read_frame(reader) == (0x89, b"a")
            assert await read_frame(reader) == (0x89, b"b")
            writer.write(frame(0x8A, b"wrong") + frame(0x8A, b"b"))
            await asyncio.wait_for(b, 1)
            assert not a.done()
            # Force slow fragmented-message parsing with interleaved controls.
            writer.write(frame(0x02, b"hello") + frame(0x89, b"server") + frame(0x8A, b"a") + frame(0x80, b"world"))
            await asyncio.wait_for(a, 1)
            assert bytes(await asyncio.wait_for(ws.recv(), 1)) == b"helloworld"
            assert await read_frame(reader) == (0x8A, b"server")
            assert ws.ping(b"legacy") is None
            assert await read_frame(reader) == (0x89, b"legacy")
            with pytest.raises(ValueError, match="125"):
                ws.ping_waiter(b"x" * 126)
            maximum = ws.ping_waiter(b"m" * 125)
            assert await read_frame(reader) == (0x89, b"m" * 125)
            writer.write(frame(0x8A, b"m" * 125))
            await asyncio.wait_for(maximum, 1)
            empty = ws.ping_waiter()
            assert await read_frame(reader) == (0x89, b"")
            writer.write(frame(0x8A))
            await asyncio.wait_for(empty, 1)
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
def test_deadline_and_cancellation(loop_factory, tls_contexts, secure, streaming):
    async def scenario():
        async with peer(tls_contexts if secure else None) as (ws, reader, writer):
            messages = []

            async def receive():
                while True:
                    messages.append(bytes(await ws.recv()))

            async def stream():
                while True:
                    writer.write(frame(0x82, b"market") + frame(0x8A, b"unsolicited"))
                    await asyncio.sleep(0.005)

            receiver = asyncio.create_task(receive())
            sender = asyncio.create_task(stream()) if streaming else None
            try:
                missing = ws.ping_waiter(b"missing")
                assert await read_frame(reader) == (0x89, b"missing")
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(missing, 0.05)
                assert missing.cancelled()
                if streaming:
                    assert len(messages) > 1
                assert not receiver.done()
                canceled = ws.ping_waiter(b"cancel")
                assert await read_frame(reader) == (0x89, b"cancel")
                canceled.cancel()
                next_probe = ws.ping_waiter(b"next")
                assert await read_frame(reader) == (0x89, b"next")
                writer.write(frame(0x8A, b"missing") + frame(0x8A, b"cancel"))
                await asyncio.sleep(0.01)
                assert not next_probe.done()
                writer.write(frame(0x8A, b"next") + frame(0x82, b"still usable"))
                await asyncio.wait_for(next_probe, 1)
                await asyncio.sleep(0.01)
                assert b"still usable" in messages
                assert not receiver.done()
            finally:
                for task in (receiver, sender):
                    if task:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("closure", ["local", "peer", "abort", "protocol"])
def test_closure_fails_waiters(loop_factory, tls_contexts, secure, closure):
    async def scenario():
        async with peer(tls_contexts if secure else None) as (ws, reader, writer):
            waits = [ws.ping_waiter(b"a"), ws.ping_waiter(b"b")]
            await read_frame(reader)
            await read_frame(reader)
            if closure == "local":
                ws.close()
            elif closure == "peer":
                writer.write(frame(0x88, b"\x03\xe8"))
            elif closure == "protocol":
                writer.write(frame(0x80, b"invalid continuation"))
            else:
                writer.transport.abort()
            for wait in waits:
                with pytest.raises(ConnectionError):
                    await asyncio.wait_for(wait, 1)
            with pytest.raises(ConnectionError):
                ws.ping_waiter(b"after close")
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(asyncio.wait_for(scenario(), 5))


def test_cancellation_reclaims_waiter_without_more_traffic(loop_factory):
    import gc
    import weakref

    async def scenario():
        async with peer(None) as (ws, reader, _writer):
            probe = ws.ping_waiter(b"cancel and release")
            await read_frame(reader)
            reference = weakref.ref(probe)
            probe.cancel()
            del probe
            await asyncio.sleep(0)
            gc.collect()
            assert reference() is None
            # A queued old cleanup must not remove a newer probe with same key.
            old = ws.ping_waiter(b"reuse")
            old.cancel()
            replacement = ws.ping_waiter(b"reuse")
            await asyncio.sleep(0)
            ws.data_received(frame(0x8A, b"reuse"))
            await asyncio.wait_for(replacement, 1)
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.skipif(sys.platform == "win32", reason="native socket send optimization is Unix-only")
@pytest.mark.parametrize("control", ["waiter", "ping", "pong", "automatic", "fragmented"])
@pytest.mark.parametrize("prefix", [0, 3])
def test_buffered_control_preserves_application_wire_order(loop_factory, control, prefix):
    import socket

    async def scenario():
        async with peer(None) as (ws, _reader, _writer):
            sending, receiving = socket.socketpair()
            receiving.setblocking(False)

            class BufferingTransport:
                def __init__(self):
                    self.pending = bytearray()

                def get_extra_info(self, name):
                    return sending if name == "socket" else None

                def get_write_buffer_size(self):
                    return len(self.pending)

                def write(self, data):
                    if not self.pending:
                        sending.sendall(data[:prefix])
                        data = data[prefix:]
                    self.pending.extend(data)

                def close(self):
                    pass

            transport = BufferingTransport()
            ws.connection_made(transport)
            try:
                ws.send(b"warm cache")
                assert receiving.recv(4096)
                probe = None
                if control == "waiter":
                    probe = ws.ping_waiter(b"control")
                elif control == "ping":
                    ws.ping(b"control")
                elif control == "pong":
                    ws.pong(b"control")
                else:
                    data = frame(0x89, b"control")
                    if control == "fragmented":
                        data = frame(0x02, b"start") + data
                    ws.data_received(data)
                queued_control = bytes(transport.pending)
                assert queued_control
                if prefix:
                    assert len(receiving.recv(4096)) == prefix
                ws.send(b"application")
                with pytest.raises(BlockingIOError):
                    receiving.recv(4096)
                assert transport.pending.startswith(queued_control)
                assert len(transport.pending) > len(queued_control)
                if probe:
                    probe.cancel()
            finally:
                ws.close()
                sending.close()
                receiving.close()
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("reentrant_close", [False, True])
def test_ping_write_failure_terminates_pending_probes(loop_factory, reentrant_close):
    async def scenario():
        async with peer(None) as (ws, _reader, _writer):
            class FailingTransport:
                writes = 0

                def get_extra_info(self, _name):
                    return None

                def write(self, _data):
                    self.writes += 1
                    if self.writes > 1:
                        if reentrant_close:
                            ws.connection_lost(ConnectionError("lost during write"))
                        raise OSError("write failed")

                def close(self):
                    pass

            ws.connection_made(FailingTransport())
            pending = ws.ping_waiter(b"pending")
            with pytest.raises(ConnectionError, match="Ping write failed"):
                ws.ping_waiter(b"failed")
            with pytest.raises(ConnectionError):
                await pending
            assert ws.closed
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(asyncio.wait_for(scenario(), 5))
