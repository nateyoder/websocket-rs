"""Zero-copy receive: payloads sliced from the receive buffer must stay valid.

The BufferedProtocol receive path hands large payloads out as slices of the
buffer the loop wrote into, rather than copying each one. The safety property
that makes it sound is refcounting: while any message still references a region,
that region is never written again. These tests hold messages across subsequent
reads and check the bytes did not move under them -- the failure this change
would cause if the refcounting were wrong is silent data corruption, not a
crash, so it needs asserting rather than assuming.

Every test here feeds bytes through ``get_buffer`` / ``buffer_updated`` -- the
BufferedProtocol hooks that own the sliced-payload lifetime. ``data_received``
would exercise the unchanged PyBytes path instead, where the retention property
is trivially true, so it must not be used in this file.
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


def feed(ws, data):
    """Push bytes through the BufferedProtocol hooks this PR changes.

    ``get_buffer`` hands out the spare capacity of the Rust-side receive buffer,
    so a chunk larger than what is offered is split across several reads -- which
    is also what a real loop does.
    """
    offset = 0
    while offset < len(data):
        view = ws.get_buffer(-1)
        n = min(len(view), len(data) - offset)
        view[:n] = data[offset : offset + n]
        del view
        ws.buffer_updated(n)
        offset += n


async def drain(ws, count):
    return [await asyncio.wait_for(ws.recv(), 5) for _ in range(count)]


@pytest.mark.parametrize("size", [SMALL, BIG])
async def test_retained_message_survives_later_reads(size):
    """Hold a message, keep receiving, and check it did not change underneath."""
    ws, _stub = await connect_over_stub()
    try:
        first = bytes([0xAA]) * size
        feed(ws, data_frame(first))
        held = await asyncio.wait_for(ws.recv(), 5)
        assert bytes(held) == first

        # Enough subsequent traffic to force the receive buffer to be reused
        # and regrown several times over.
        for i in range(200):
            filler = bytes([i % 251]) * size
            feed(ws, data_frame(filler))
            assert bytes(await asyncio.wait_for(ws.recv(), 5)) == filler

        assert bytes(held) == first, "a retained payload was overwritten by later reads"
    finally:
        ws.close()


async def test_many_retained_messages_all_stay_distinct():
    """Retaining every message from a burst must not alias them together."""
    ws, _stub = await connect_over_stub()
    try:
        expected = [bytes([i % 251]) * BIG for i in range(64)]
        # One chunk carrying many frames: they share a backing buffer, which is
        # exactly the case where a slicing bug would alias them.
        feed(ws, b"".join(data_frame(p) for p in expected))
        held = await drain(ws, len(expected))
        assert [bytes(m) for m in held] == expected
    finally:
        ws.close()


async def test_payload_split_across_reads_is_reassembled():
    """A frame straddling two reads must survive the buffer handover."""
    ws, _stub = await connect_over_stub()
    try:
        payload = bytes(range(256)) * (BIG // 256)
        frame = data_frame(payload)
        cut = len(frame) // 3
        feed(ws, frame[:cut])
        feed(ws, frame[cut:])
        held = await asyncio.wait_for(ws.recv(), 5)
        assert bytes(held) == payload

        # The reassembled payload is the one whose backing region the next reads
        # are most likely to reclaim, so keep reading while holding it.
        for i in range(30):
            filler = bytes([i % 251]) * BIG
            feed(ws, data_frame(filler))
            assert bytes(await asyncio.wait_for(ws.recv(), 5)) == filler
        assert bytes(held) == payload, "a reassembled payload was overwritten by later reads"
    finally:
        ws.close()


@pytest.mark.parametrize("threshold", [0, 1, BIG // 2, 1 << 30])
async def test_threshold_does_not_change_delivered_bytes(threshold):
    """zero_copy_min_bytes is a memory/CPU dial, never a correctness one.

    0 slices everything, a huge value copies everything; the bytes the caller
    sees must be identical either way.
    """
    ws, _stub = await connect_over_stub(zero_copy_min_bytes=threshold)
    try:
        payloads = [bytes([i % 251]) * s for i, s in enumerate((SMALL, 1000, BIG, 200))]
        feed(ws, b"".join(data_frame(p) for p in payloads))
        held = await drain(ws, len(payloads))
        assert [bytes(m) for m in held] == payloads

        # Hold them across further reads: whatever the threshold does, a
        # delivered payload must never be rewritten.
        for i in range(30):
            filler = bytes([i % 251]) * BIG
            feed(ws, data_frame(filler))
            assert bytes(await asyncio.wait_for(ws.recv(), 5)) == filler
        assert [bytes(m) for m in held] == payloads, "delivered payloads changed after later reads"
    finally:
        ws.close()


async def test_memoryview_of_a_slice_stays_valid_across_reads():
    """A memoryview is the zero-copy consumer path; it must not dangle."""
    ws, _stub = await connect_over_stub()
    try:
        payload = bytes([0x5A]) * BIG
        feed(ws, data_frame(payload))
        msg = await asyncio.wait_for(ws.recv(), 5)
        view = memoryview(msg)
        for i in range(50):
            filler = bytes([i % 251]) * BIG
            feed(ws, data_frame(filler))
            await asyncio.wait_for(ws.recv(), 5)
        assert bytes(view) == payload
    finally:
        ws.close()


async def test_retained_message_survives_a_frame_reassembled_from_small_chunks():
    """The carry-over path must not write over a payload already handed out.

    A frame arriving in many small reads makes the buffer accumulate an unparsed
    tail across reads, which is the case where a payload sliced out of an earlier
    read is most likely to be trampled.
    """
    ws, _stub = await connect_over_stub()
    try:
        first = bytes([0xC3]) * BIG
        feed(ws, data_frame(first))
        held = await asyncio.wait_for(ws.recv(), 5)
        assert bytes(held) == first

        big = bytes(range(256)) * (4 * BIG // 256)
        frame = data_frame(big)
        for start in range(0, len(frame), 997):
            feed(ws, frame[start : start + 997])
        assert bytes(await asyncio.wait_for(ws.recv(), 5)) == big
        assert bytes(held) == first, "a retained payload was overwritten during reassembly"
    finally:
        ws.close()
