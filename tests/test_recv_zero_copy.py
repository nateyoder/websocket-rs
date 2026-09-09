"""Zero-copy receive: payloads sliced from the receive buffer must stay valid.

The BufferedProtocol receive path hands large payloads out as slices of the
buffer the loop wrote into, rather than copying each one. The safety property
that makes it sound is refcounting: while any message still references a region,
that region is never written again. These tests hold messages across subsequent
reads and check the bytes did not move under them -- the failure this change
would cause if the refcounting were wrong is silent data corruption, not a
crash, so it needs asserting rather than assuming.
"""

import asyncio

import pytest

from tests.test_control_frames import connect_over_stub

OP_BINARY = 0x2
# Above the default zero_copy_min_bytes, so these take the slicing path.
BIG = 8192
# Below it, so these take the copying path.
SMALL = 64


def data_frame(payload):
    n = len(payload)
    if n < 126:
        hdr = bytes([0x80 | OP_BINARY, n])
    elif n < 65536:
        hdr = bytes([0x80 | OP_BINARY, 126]) + n.to_bytes(2, "big")
    else:
        hdr = bytes([0x80 | OP_BINARY, 127]) + n.to_bytes(8, "big")
    return hdr + payload


async def drain(ws, count):
    return [await asyncio.wait_for(ws.recv(), 5) for _ in range(count)]


@pytest.mark.parametrize("size", [SMALL, BIG])
async def test_retained_message_survives_later_reads(size):
    """Hold a message, keep receiving, and check it did not change underneath."""
    ws, stub = await connect_over_stub()
    try:
        first = bytes([0xAA]) * size
        stub.protocol.data_received(data_frame(first))
        held = await asyncio.wait_for(ws.recv(), 5)
        assert bytes(held) == first

        # Enough subsequent traffic to force the receive buffer to be reused
        # and regrown several times over.
        for i in range(200):
            filler = bytes([i % 251]) * size
            stub.protocol.data_received(data_frame(filler))
            assert bytes(await asyncio.wait_for(ws.recv(), 5)) == filler

        assert bytes(held) == first, "a retained payload was overwritten by later reads"
    finally:
        ws.close()


async def test_many_retained_messages_all_stay_distinct():
    """Retaining every message from a burst must not alias them together."""
    ws, stub = await connect_over_stub()
    try:
        expected = [bytes([i % 251]) * BIG for i in range(64)]
        # One chunk carrying many frames: they share a backing buffer, which is
        # exactly the case where a slicing bug would alias them.
        stub.protocol.data_received(b"".join(data_frame(p) for p in expected))
        held = await drain(ws, len(expected))
        assert [bytes(m) for m in held] == expected
    finally:
        ws.close()


async def test_payload_split_across_reads_is_reassembled():
    """A frame straddling two reads must survive the buffer handover."""
    ws, stub = await connect_over_stub()
    try:
        payload = bytes(range(256)) * (BIG // 256)
        frame = data_frame(payload)
        cut = len(frame) // 3
        stub.protocol.data_received(frame[:cut])
        stub.protocol.data_received(frame[cut:])
        assert bytes(await asyncio.wait_for(ws.recv(), 5)) == payload
    finally:
        ws.close()


@pytest.mark.parametrize("threshold", [0, 1, BIG // 2, 1 << 30])
async def test_threshold_does_not_change_delivered_bytes(threshold):
    """zero_copy_min_bytes is a memory/CPU dial, never a correctness one.

    0 slices everything, a huge value copies everything; the bytes the caller
    sees must be identical either way.
    """
    ws, stub = await connect_over_stub(zero_copy_min_bytes=threshold)
    try:
        payloads = [bytes([i % 251]) * s for i, s in enumerate((SMALL, 1000, BIG, 200))]
        stub.protocol.data_received(b"".join(data_frame(p) for p in payloads))
        got = [bytes(m) for m in await drain(ws, len(payloads))]
        assert got == payloads
    finally:
        ws.close()


async def test_memoryview_of_a_slice_stays_valid_across_reads():
    """A memoryview is the zero-copy consumer path; it must not dangle."""
    ws, stub = await connect_over_stub()
    try:
        payload = bytes([0x5A]) * BIG
        stub.protocol.data_received(data_frame(payload))
        msg = await asyncio.wait_for(ws.recv(), 5)
        view = memoryview(msg)
        for i in range(50):
            filler = bytes([i % 251]) * BIG
            stub.protocol.data_received(data_frame(filler))
            await asyncio.wait_for(ws.recv(), 5)
        assert bytes(view) == payload
    finally:
        ws.close()
