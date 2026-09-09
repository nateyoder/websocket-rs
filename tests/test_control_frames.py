"""Deterministic control-frame tests driven over a stub transport.

No sockets, no event-loop timing: the client is driven as a bare
``asyncio.Protocol``, so ``pause_writing`` and the write channel are under the
test's control and every assertion is about what reached the transport.

These exist because a Ping going unanswered was observed intermittently against
real sockets and could not be reproduced there (FOLLOWUPS F13). They pin the
invariants that investigation checked, so a future regression in any of them
fails here — deterministically — instead of as a load-dependent flake.
"""

import asyncio
import base64
import hashlib
import os

import pytest

from websocket_rs.native_client import connect

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_PING = 0x9
OP_PONG = 0xA


def server_frame(opcode, payload=b""):
    """A server->client frame. Server frames are never masked (RFC 6455 §5.1)."""
    assert len(payload) <= 125, "control frames only in this helper"
    return bytes([0x80 | opcode, len(payload)]) + payload


def unmask(frame):
    """Return the payload of a client->server control frame.

    Client frames must be masked, so this also asserts the mask bit is set —
    an unmasked client frame is a protocol violation the peer must reject.
    """
    assert frame[1] & 0x80, "client frames must be masked"
    n = frame[1] & 0x7F
    key, body = frame[2:6], frame[6 : 6 + n]
    return bytes(b ^ key[i % 4] for i, b in enumerate(body))


def opcodes(frames):
    return [f[0] & 0x0F for f in frames]


class StubTransport:
    """A transport that records writes and answers the handshake inline."""

    def __init__(self):
        self.writes = []
        self.protocol = None
        self.closed = False

    # -- asyncio.Transport surface the client uses --
    def get_extra_info(self, name, default=None):
        # None for "ssl_object" and "socket" alike: no raw-fd fast path, so
        # every frame is observable here rather than going straight to a socket.
        return default

    def get_write_buffer_size(self):
        return 0

    def write(self, data):
        data = bytes(data)
        if data.startswith(b"GET "):
            key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
            accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
            self.protocol.data_received(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            return
        self.writes.append(data)

    def close(self):
        self.closed = True

    def abort(self):
        self.closed = True


async def connect_over_stub(**connect_kwargs):
    """Connect the native client to a StubTransport instead of a real socket.

    The returned client is a ``NativeClientBuffered``. Driving it with
    ``protocol.data_received(...)`` exercises the PyBytes receive path only; the
    BufferedProtocol path (``get_buffer`` / ``buffer_updated``, where payloads
    are sliced out of the Rust-owned receive buffer) is a different code path and
    has to be fed through those hooks instead -- see
    ``tests/test_recv_zero_copy.py::feed``.
    """
    loop = asyncio.get_running_loop()
    original = loop.create_connection
    stub = StubTransport()

    async def create_connection(protocol_factory, *args, **kwargs):
        stub.protocol = protocol_factory()
        stub.protocol.connection_made(stub)
        return stub, stub.protocol

    loop.create_connection = create_connection
    try:
        ws = await connect("ws://stub:1", **connect_kwargs)
    finally:
        loop.create_connection = original
    stub.writes.clear()  # drop the handshake request
    return ws, stub


async def test_server_ping_is_answered():
    ws, stub = await connect_over_stub()
    try:
        stub.protocol.data_received(server_frame(OP_PING, b"probe"))
        assert OP_PONG in opcodes(stub.writes), "server Ping went unanswered"
    finally:
        ws.close()


async def test_server_ping_is_answered_while_writing_is_paused():
    """A paused transport must not swallow the Pong.

    ``send()`` buffers into the client's own queue while paused; the Pong is
    written straight through. Both halves matter: the peer is blocked on the
    Pong and cannot drain the transport, so deferring it until resume would
    deadlock the connection.
    """
    ws, stub = await connect_over_stub()
    try:
        stub.protocol.pause_writing()
        for i in range(32):
            ws.send(b"app%d" % i)
        assert opcodes(stub.writes) == [], "sends must buffer internally while paused"

        stub.protocol.data_received(server_frame(OP_PING, b"probe"))
        assert opcodes(stub.writes) == [OP_PONG], "Pong was withheld by the paused write queue"
        assert unmask(stub.writes[0]) == b"probe"

        stub.protocol.resume_writing()
        assert opcodes(stub.writes) == [OP_PONG] + [OP_BINARY] * 32
    finally:
        ws.close()


async def test_queued_sends_survive_a_pong_written_past_them():
    """The Pong overtakes queued frames; none of them may be lost or reordered."""
    ws, stub = await connect_over_stub()
    try:
        stub.protocol.pause_writing()
        for i in range(32):
            ws.send(b"%d" % i)
        stub.protocol.data_received(server_frame(OP_PING))
        stub.protocol.resume_writing()
        app = [f for f in stub.writes if (f[0] & 0x0F) == OP_BINARY]
        assert [unmask(f) for f in app] == [b"%d" % i for i in range(32)]
    finally:
        ws.close()


@pytest.mark.parametrize("length", [0, 1, 4, 5, 15, 16, 17, 31, 32, 33, 63, 64, 65, 124, 125])
async def test_pong_echoes_the_ping_payload_exactly(length):
    """RFC 6455 §5.5.3: the Pong payload must be identical to the Ping's.

    A corrupted echo is invisible locally but leaves the peer waiting forever,
    since peers match pongs to pings by payload. Lengths straddle the masking
    kernel's vector-width boundaries.
    """
    ws, stub = await connect_over_stub()
    try:
        for _ in range(20):  # vary allocator/scratch state between probes
            payload = os.urandom(length)
            stub.writes.clear()
            stub.protocol.data_received(server_frame(OP_PING, payload))
            pongs = [f for f in stub.writes if (f[0] & 0x0F) == OP_PONG]
            assert pongs, f"no Pong for a {length}-byte Ping"
            assert unmask(pongs[0]) == payload
    finally:
        ws.close()


async def test_pings_interleaved_with_traffic_are_all_answered():
    """Every Ping gets a Pong, including several arriving in one chunk."""
    ws, stub = await connect_over_stub()
    try:
        chunk = b"".join(
            server_frame(OP_PING, b"p%d" % i) + server_frame(OP_TEXT, b"msg%d" % i)
            for i in range(8)
        )
        stub.protocol.data_received(chunk)
        pongs = [f for f in stub.writes if (f[0] & 0x0F) == OP_PONG]
        assert [unmask(f) for f in pongs] == [b"p%d" % i for i in range(8)]
    finally:
        ws.close()


async def test_ping_split_across_two_chunks_is_answered():
    """A control frame arriving in two reads must still be answered once whole."""
    ws, stub = await connect_over_stub()
    try:
        frame = server_frame(OP_PING, b"halves")
        stub.protocol.data_received(frame[:3])
        assert opcodes(stub.writes) == [], "answered a Ping before it was complete"
        stub.protocol.data_received(frame[3:])
        assert opcodes(stub.writes) == [OP_PONG]
        assert unmask(stub.writes[0]) == b"halves"
    finally:
        ws.close()
