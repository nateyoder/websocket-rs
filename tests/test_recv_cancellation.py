"""Cancelled receive waiters must not swallow messages or accumulate.

Driven over the stub transport from test_control_frames, so every case is
deterministic: no sockets, no timers, no load sensitivity.

`recv()` parks an asyncio Future in the client's `pending_recv` queue. Anything
that cancels that Future -- an explicit `cancel()`, a `receive_timeout`
expiring, or `asyncio.wait_for` around it -- leaves a done Future parked there.
The next frame to arrive must skip it and reach a live receiver.
"""

import asyncio
import gc
import weakref

import pytest

from tests.test_control_frames import connect_over_stub

OP_TEXT = 0x1


def text_frame(payload: bytes):
    """A server->client text frame. Server frames are never masked."""
    assert len(payload) <= 125, "short payloads only; keeps the header one byte"
    return bytes([0x80 | OP_TEXT, len(payload)]) + payload


async def test_message_after_a_cancelled_recv_reaches_the_next_receiver():
    """The regression: a cancelled waiter used to consume and drop the frame."""
    ws, stub = await connect_over_stub()
    try:
        abandoned = ws.recv()
        abandoned.cancel()
        await asyncio.sleep(0)

        stub.protocol.data_received(text_frame(b"hello"))

        received = await asyncio.wait_for(ws.recv(), 1)
        assert bytes(received) == b"hello"
    finally:
        ws.close()


async def test_message_survives_a_run_of_cancelled_waiters():
    """One live receiver behind many dead ones still gets the frame."""
    ws, stub = await connect_over_stub()
    try:
        for _ in range(16):
            fut = ws.recv()
            fut.cancel()
        await asyncio.sleep(0)
        live = ws.recv()

        stub.protocol.data_received(text_frame(b"payload"))

        assert bytes(await asyncio.wait_for(live, 1)) == b"payload"
    finally:
        ws.close()


async def test_cancelled_waiter_does_not_consume_a_backlogged_message():
    """With no live receiver, the frame must be queued rather than discarded."""
    ws, stub = await connect_over_stub()
    try:
        abandoned = ws.recv()
        abandoned.cancel()
        await asyncio.sleep(0)

        stub.protocol.data_received(text_frame(b"queued"))

        # Nobody was waiting, so this must come from the backlog.
        assert bytes(await asyncio.wait_for(ws.recv(), 1)) == b"queued"
    finally:
        ws.close()


async def test_recv_timeout_does_not_drop_the_next_message():
    """The real-world trigger: a receive_timeout cancels the parked Future.

    A client polling with a timeout would otherwise lose exactly one message per
    expiry, silently.
    """
    ws, stub = await connect_over_stub()
    try:
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await asyncio.wait_for(ws.recv(), 0.01)

        stub.protocol.data_received(text_frame(b"after-timeout"))

        assert bytes(await asyncio.wait_for(ws.recv(), 1)) == b"after-timeout"
    finally:
        ws.close()


async def test_ordering_is_preserved_across_cancellations():
    """Skipping dead waiters must not reorder or duplicate delivery."""
    ws, stub = await connect_over_stub()
    try:
        dead = ws.recv()
        dead.cancel()
        await asyncio.sleep(0)
        first, second = ws.recv(), ws.recv()

        stub.protocol.data_received(text_frame(b"1") + text_frame(b"2"))

        assert bytes(await asyncio.wait_for(first, 1)) == b"1"
        assert bytes(await asyncio.wait_for(second, 1)) == b"2"
    finally:
        ws.close()


async def test_cancelled_waiters_do_not_accumulate_without_traffic():
    """Idle cancellation must not grow the queue without bound.

    Skipping dead entries when a frame arrives fixes delivery but not this: a
    connection that is polled with timeouts and receives nothing would retain
    one Future per expiry.
    """
    ws, stub = await connect_over_stub()
    try:
        # Weak references measure what actually matters -- whether the client
        # still holds the Future -- without adding introspection API for a test.
        seen = []
        for _ in range(5000):
            fut = ws.recv()
            fut.cancel()
            seen.append(weakref.ref(fut))
            del fut
        await asyncio.sleep(0)
        gc.collect()
        retained = sum(1 for ref in seen if ref() is not None)
        assert retained < 100, f"{retained} of 5000 cancelled waiters still retained"
    finally:
        ws.close()
