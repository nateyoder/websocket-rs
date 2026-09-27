"""Contracts for the native Kalshi WebSocket fixed-point book decoder."""

import json

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from websocket_rs import fix


def _price(units: int) -> str:
    return f"{units // 10_000}.{units % 10_000:04d}"


def _size(units: int) -> str:
    sign = "-" if units < 0 else ""
    absolute = abs(units)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def _wire(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


@given(
    yes=st.lists(
        st.tuples(st.integers(0, 10_000), st.integers(1, 100_000_000)),
        max_size=40,
        unique_by=lambda row: row[0],
    ),
    no=st.lists(
        st.tuples(st.integers(0, 10_000), st.integers(1, 100_000_000)),
        max_size=40,
        unique_by=lambda row: row[0],
    ),
    sid=st.integers(1, 2**31 - 1),
    sequence=st.integers(1, 2**53 - 1),
)
@example(yes=[(0, 1), (10_000, 100_000_000)], no=[], sid=1, sequence=1)
def test_snapshot_preserves_scaled_fixed_point_values(yes, no, sid, sequence):
    frame = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": sequence,
        "msg": {
            "market_ticker": "KXTEST-26",
            "market_id": "market-id",
            "yes_dollars_fp": [[_price(price), _size(size)] for price, size in yes],
            "no_dollars_fp": [[_price(price), _size(size)] for price, size in no],
        },
    }

    decoded = fix.decode_kalshi_ws_book(_wire(frame))

    assert decoded == (
        "orderbook_snapshot",
        sid,
        sequence,
        "KXTEST-26",
        "market-id",
        tuple(yes),
        tuple(no),
        None,
        None,
        None,
    )


@given(
    side=st.sampled_from(["yes", "no"]),
    price=st.integers(0, 10_000),
    delta=st.integers(-100_000_000, 100_000_000),
    sequence=st.integers(1, 2**53 - 1),
)
@example(side="yes", price=50, delta=-1, sequence=1)
def test_delta_preserves_sign_and_scaled_fixed_point_values(side, price, delta, sequence):
    frame = {
        "type": "orderbook_delta",
        "sid": 7,
        "seq": sequence,
        "msg": {
            "market_ticker": "KXTEST-26",
            "market_id": "market-id",
            "price_dollars": _price(price),
            "delta_fp": _size(delta),
            "side": side,
        },
    }

    decoded = fix.decode_kalshi_ws_book(_wire(frame))

    assert decoded == (
        "orderbook_delta",
        7,
        sequence,
        "KXTEST-26",
        "market-id",
        (),
        (),
        side,
        price,
        delta,
    )


@pytest.mark.parametrize(
    "value",
    [
        {"type": "trade", "sid": 1, "seq": 1, "msg": {}},
        {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta"}},
        ["not", "an", "object"],
    ],
)
def test_non_book_json_returns_none(value):
    assert fix.decode_kalshi_ws_book(_wire(value)) is None


def test_book_type_with_wrong_scalar_types_fails_closed():
    frame = {
        "type": "orderbook_delta",
        "sid": "not-an-integer",
        "seq": 1,
        "msg": {},
    }

    with pytest.raises(ValueError, match="sid"):
        fix.decode_kalshi_ws_book(_wire(frame))


@pytest.mark.parametrize(
    "changes, match",
    [({"sid": 0}, "positive"), ({"seq": 0}, "positive"), ({"market_ticker": ""}, "identity")],
)
def test_book_identity_and_sequence_must_be_usable(changes, match):
    frame = {
        "type": "orderbook_snapshot",
        "sid": changes.get("sid", 1),
        "seq": changes.get("seq", 1),
        "msg": {
            "market_ticker": changes.get("market_ticker", "KXTEST-26"),
            "market_id": "market-id",
        },
    }

    with pytest.raises(ValueError, match=match):
        fix.decode_kalshi_ws_book(_wire(frame))


def test_empty_snapshot_may_omit_both_ladders():
    frame = {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 1,
        "msg": {"market_ticker": "KXTEST-26", "market_id": "market-id"},
    }

    decoded = fix.decode_kalshi_ws_book(_wire(frame))

    assert decoded is not None
    assert decoded[5:7] == ((), ())


@pytest.mark.parametrize(
    "value, match",
    [
        (b"not-json", "JSON"),
        (_wire({"type": "orderbook_delta"}), "sid"),
        (
            _wire(
                {
                    "type": "orderbook_delta",
                    "sid": 1,
                    "seq": 1,
                    "msg": {
                        "market_ticker": "KXTEST-26",
                        "market_id": "market-id",
                        "price_dollars": "0.00001",
                        "delta_fp": "1.00",
                        "side": "yes",
                    },
                }
            ),
            "price_dollars",
        ),
    ],
)
def test_book_frames_fail_closed(value, match):
    with pytest.raises(ValueError, match=match):
        fix.decode_kalshi_ws_book(value)
