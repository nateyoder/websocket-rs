"""State-machine contracts for the native Kalshi WebSocket book."""

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


def _snapshot(
    yes: list[tuple[int, int]],
    no: list[tuple[int, int]],
    *,
    sequence: int = 1,
) -> bytes:
    return _wire(
        {
            "type": "orderbook_snapshot",
            "sid": 7,
            "seq": sequence,
            "msg": {
                "market_ticker": "KXTEST-26",
                "market_id": "market-id",
                "yes_dollars_fp": [[_price(price), _size(size)] for price, size in yes],
                "no_dollars_fp": [[_price(price), _size(size)] for price, size in no],
            },
        }
    )


def _delta(side: str, price: int, delta: int, *, sequence: int) -> bytes:
    return _wire(
        {
            "type": "orderbook_delta",
            "sid": 7,
            "seq": sequence,
            "msg": {
                "market_ticker": "KXTEST-26",
                "market_id": "market-id",
                "side": side,
                "price_dollars": _price(price),
                "delta_fp": _size(delta),
            },
        }
    )


def _expected(
    yes: dict[int, int], no: dict[int, int], depth: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    bids = tuple(sorted(yes.items(), reverse=True)[:depth])
    asks = tuple(sorted((10_000 - price, size) for price, size in no.items())[:depth])
    return bids, asks


@st.composite
def _uncrossed_books_and_transitions(draw):
    boundary = draw(st.integers(0, 10_000))
    yes = draw(
        st.dictionaries(st.integers(0, boundary), st.integers(1, 100_000), max_size=20)
    )
    no = draw(
        st.dictionaries(
            st.integers(0, 10_000 - boundary),
            st.integers(1, 100_000),
            max_size=20,
        )
    )
    transitions = draw(
        st.lists(
            st.one_of(
                st.tuples(
                    st.just("yes"),
                    st.integers(0, boundary),
                    st.integers(0, 100_000),
                ),
                st.tuples(
                    st.just("no"),
                    st.integers(0, 10_000 - boundary),
                    st.integers(0, 100_000),
                ),
            ),
            max_size=30,
        )
    )
    return yes, no, transitions


@given(
    depth=st.integers(1, 8),
    case=_uncrossed_books_and_transitions(),
)
@example(
    depth=1,
    case=({4_000: 100}, {1_000: 200}, [("yes", 4_000, 0), ("yes", 4_000, 300)]),
)
def test_native_book_matches_full_model_through_empty_and_refilled_levels(
    depth, case
):
    yes, no, transitions = case
    state = fix.KalshiWsBookState(publication_depth=depth, use_yes_price=False)
    result = state.apply(_snapshot(list(yes.items()), list(no.items())))
    assert result is not None
    first_result = result
    assert result == _expected(yes, no, depth)

    current = {"yes": dict(yes), "no": dict(no)}
    for sequence, (side, price, target) in enumerate(transitions, start=2):
        ladder = current[side]
        previous = ladder.get(price, 0)
        result = state.apply(_delta(side, price, target - previous, sequence=sequence))
        if target == 0:
            ladder.pop(price, None)
        else:
            ladder[price] = target
        assert result is not None
        assert result == _expected(current["yes"], current["no"], depth)
        assert all(size > 0 for rows in result for _, size in rows)

    assert first_result == _expected(yes, no, depth)


def test_native_book_lifecycle_and_sequence_fail_closed():
    state = fix.KalshiWsBookState(
        publication_depth=1,
        use_yes_price=False,
        enforce_sequence=True,
    )
    state.apply(_snapshot([(4_000, 100)], []))
    assert state.baseline_ready("market-id")

    with pytest.raises(ValueError, match="sequence gap"):
        state.apply(_delta("yes", 4_000, 1, sequence=3))
    assert not state.baseline_ready("market-id")

    with pytest.raises(ValueError, match="fresh snapshot"):
        state.apply(_delta("yes", 4_000, 1, sequence=4))

    state.apply(_snapshot([(4_000, 100)], [], sequence=5))
    state.invalidate_tickers({"KXTEST-26"})
    assert not state.baseline_ready("market-id")
    state.apply(_snapshot([(4_000, 100)], [], sequence=6))
    state.reset()
    assert not state.baseline_ready("market-id")


def test_native_book_rejects_crossed_and_negative_books():
    state = fix.KalshiWsBookState(publication_depth=1, use_yes_price=False)

    with pytest.raises(ValueError, match="crossed book"):
        state.apply(_snapshot([(9_000, 100)], [(2_000, 100)]))
    state.apply(_snapshot([(4_000, 100)], []))
    with pytest.raises(ValueError, match="negative"):
        state.apply(_delta("yes", 4_000, -101, sequence=2))
    assert not state.baseline_ready("market-id")


def test_native_book_supports_yes_ask_wire_convention():
    state = fix.KalshiWsBookState(publication_depth=2, use_yes_price=True)

    result = state.apply(_snapshot([(4_000, 100)], [(6_000, 200), (7_000, 300)]))

    assert result is not None
    assert result == (((4_000, 100),), ((6_000, 200), (7_000, 300)))


@pytest.mark.parametrize("depth", [0, -1, True, 1.5, "1"])
def test_native_book_requires_a_positive_integer_depth(depth):
    with pytest.raises((TypeError, ValueError), match="publication_depth"):
        fix.KalshiWsBookState(publication_depth=depth, use_yes_price=False)
