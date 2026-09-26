"""Contract tests for the native, single-crossing FIXT.1.1 decoder."""

import pytest

from websocket_rs import fix

SOH = b"\x01"


def fix_frame(*body_fields):
    body = b"".join(str(tag).encode() + b"=" + value + SOH for tag, value in body_fields)
    prefix = b"8=FIXT.1.1\x019=" + str(len(body)).encode() + SOH + body
    return prefix + f"10={sum(prefix) % 256:03d}".encode() + SOH


def replace_field(frame, old, new):
    assert frame.count(old) == 1
    return frame.replace(old, new)


def test_decodes_ordered_scalars_and_268_entries_in_one_batch():
    frame = fix_frame(
        (35, b"W"),
        (49, b"KALSHI"),
        (55, b"FED-26"),
        (268, b"2"),
        (269, b"0"),
        (270, b"0.4100"),
        (271, b"17"),
        (269, b"1"),
        (270, b"0.5900"),
        (271, b"23"),
    )

    fields, entries = fix.decode(frame)

    assert fields == (
        (8, "FIXT.1.1"),
        (9, str(len(frame.split(SOH, 2)[2].rsplit(b"10=", 1)[0]))),
        (35, "W"),
        (49, "KALSHI"),
        (55, "FED-26"),
        (268, "2"),
        (10, frame[-4:-1].decode()),
    )
    assert entries == (
        ((269, "0"), (270, "0.4100"), (271, "17")),
        ((269, "1"), (270, "0.5900"), (271, "23")),
    )


def test_decodes_message_without_repeating_group():
    frame = fix_frame((35, b"0"), (112, b"request=42"))
    fields, entries = fix.decode(frame)
    assert fields[2:-1] == ((35, "0"), (112, "request=42"))
    assert entries == ()


def test_decodes_multi_entry_incremental_group_led_by_279():
    frame = fix_frame(
        (35, b"X"),
        (268, b"2"),
        (279, b"0"),
        (55, b"FED-26"),
        (269, b"0"),
        (270, b"0.4100"),
        (271, b"17"),
        (279, b"1"),
        (55, b"FED-26"),
        (269, b"1"),
        (270, b"0.5900"),
        (271, b"23"),
    )

    fields, entries = fix.decode(frame)

    assert fields[2:-1] == ((35, "X"), (268, "2"))
    assert entries == (
        ((279, "0"), (55, "FED-26"), (269, "0"), (270, "0.4100"), (271, "17")),
        ((279, "1"), (55, "FED-26"), (269, "1"), (270, "0.5900"), (271, "23")),
    )


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda frame: replace_field(frame, b"FIXT.1.1", b"FIX.4.4"), "BeginString"),
        (lambda frame: b"35=W\x01" + frame, "tag 8"),
        (lambda frame: frame[:-1], "complete"),
        (lambda frame: frame + b"garbage", "complete"),
        (lambda frame: replace_field(frame, b"9=5\x01", b"9=4\x01"), "BodyLength"),
        (lambda frame: replace_field(frame, b"10=", b"11="), "checksum trailer"),
        (lambda frame: frame[:-4] + b"999\x01", "checksum"),
    ],
)
def test_rejects_invalid_framing_length_and_checksum(mutate, match):
    frame = fix_frame((35, b"0"))
    with pytest.raises(ValueError, match=match):
        fix.decode(mutate(frame))


@pytest.mark.parametrize("bad_tag", [b"", b"A", b"01", b"0", b"4294967296"])
def test_rejects_malformed_tags(bad_tag):
    body = b"35=W\x01" + bad_tag + b"=value\x01"
    prefix = b"8=FIXT.1.1\x019=" + str(len(body)).encode() + SOH + body
    frame = prefix + f"10={sum(prefix) % 256:03d}".encode() + SOH
    with pytest.raises(ValueError, match="tag"):
        fix.decode(frame)


@pytest.mark.parametrize("count", [b"", b"-1", b"x", b"01", b"4097"])
def test_rejects_malformed_268_count(count):
    frame = fix_frame((35, b"W"), (268, count))
    with pytest.raises(ValueError, match="268"):
        fix.decode(frame)


@pytest.mark.parametrize(
    "body_fields, match",
    [
        (((35, b"W"), (35, b"X")), "duplicate tag 35"),
        (((35, b"W"), (268, b"1"), (269, b"0"), (270, b"1"), (270, b"2")), "duplicate tag 270"),
        (((35, b"X"), (268, b"1"), (279, b"0"), (55, b"A"), (55, b"B")), "duplicate tag 55"),
        (((35, b"W"), (268, b"2"), (269, b"0")), "expected 2"),
        (((35, b"X"), (268, b"2"), (279, b"0"), (269, b"0")), "expected 2"),
        (((35, b"W"), (268, b"1"), (270, b"1")), "begin with tag 269"),
        (((35, b"X"), (268, b"1"), (269, b"0")), "begin with tag 279"),
        (((35, b"W"), (268, b"1"), (279, b"0")), "begin with tag 269"),
        (((35, b"0"), (268, b"0")), "MsgType W or X"),
        (((35, b"W"), (269, b"0")), "without tag 268"),
        (((35, b"W"), (268, b"0"), (269, b"0")), "expected 0"),
        (((35, b"W"), (268, b"1"), (269, b"0"), (268, b"1")), "tag 268"),
    ],
)
def test_fails_closed_on_duplicate_tags_and_group_count_errors(body_fields, match):
    with pytest.raises(ValueError, match=match):
        fix.decode(fix_frame(*body_fields))


def test_same_tags_are_allowed_in_distinct_entries():
    frame = fix_frame((35, b"W"), (268, b"2"), (269, b"0"), (270, b"1"), (269, b"1"), (270, b"2"))
    _, entries = fix.decode(frame)
    assert entries[0][1][0] == entries[1][1][0] == 270


def test_values_use_lossless_latin1_strings():
    fields, _ = fix.decode(fix_frame((35, b"0"), (58, b"\x80\xff")))
    assert fields[-2] == (58, "\x80\xff")
    assert fields[-2][1].encode("latin-1") == b"\x80\xff"


def test_decodes_book_ready_kalshi_snapshot():
    frame = fix_frame(
        (35, b"W"),
        (34, b"42"),
        (55, b"FED-26"),
        (268, b"2"),
        (269, b"0"),
        (270, b"0.4100"),
        (271, b"17.5"),
        (272, b"20260926"),
        (273, b"21:00:00.123"),
        (269, b"1"),
        (270, b"0.5900"),
        (271, b"23"),
        (272, b"20260926"),
        (273, b"21:00:00.124"),
    )

    assert fix.decode_kalshi_book(frame) == (
        "W",
        "42",
        "FED-26",
        (
            (None, None, "0", 0.41, 17.5, "20260926", "21:00:00.123"),
            (None, None, "1", 0.59, 23.0, "20260926", "21:00:00.124"),
        ),
    )


def test_decodes_book_ready_multi_entry_kalshi_incremental():
    frame = fix_frame(
        (35, b"X"),
        (34, b"43"),
        (268, b"2"),
        (279, b"1"),
        (55, b"FED-26"),
        (269, b"0"),
        (270, b"0.4100"),
        (271, b"18"),
        (272, b"20260926"),
        (273, b"21:00:01.123"),
        (279, b"2"),
        (55, b"OTHER-26"),
        (269, b"1"),
        (270, b"0.5900"),
        (271, b"0"),
        (272, b"20260926"),
        (273, b"21:00:01.124"),
    )

    assert fix.decode_kalshi_book(frame) == (
        "X",
        "43",
        None,
        (
            ("1", "FED-26", "0", 0.41, 18.0, "20260926", "21:00:01.123"),
            ("2", "OTHER-26", "1", 0.59, 0.0, "20260926", "21:00:01.124"),
        ),
    )


@pytest.mark.parametrize(
    "body_fields, match",
    [
        (((35, b"W"), (55, b"FED"), (268, b"0")), "tag 34"),
        (((35, b"W"), (34, b"1"), (268, b"0")), "tag 55"),
        (
            (
                (35, b"W"),
                (34, b"1"),
                (55, b"FED"),
                (268, b"1"),
                (269, b"2"),
                (270, b".5"),
                (271, b"1"),
                (272, b"20260926"),
                (273, b"00:00:00.000"),
            ),
            "MDEntryType<269>",
        ),
        (
            (
                (35, b"X"),
                (34, b"2"),
                (268, b"1"),
                (279, b"3"),
                (55, b"FED"),
                (269, b"0"),
                (270, b".5"),
                (271, b"1"),
                (272, b"20260926"),
                (273, b"00:00:00.000"),
            ),
            "MDUpdateAction<279>",
        ),
        (
            (
                (35, b"X"),
                (34, b"2"),
                (268, b"1"),
                (279, b"1"),
                (269, b"0"),
                (270, b".5"),
                (271, b"1"),
                (272, b"20260926"),
                (273, b"00:00:00.000"),
            ),
            "tag 55",
        ),
    ],
)
def test_kalshi_book_decoder_requires_message_specific_fields(body_fields, match):
    with pytest.raises(ValueError, match=match):
        fix.decode_kalshi_book(fix_frame(*body_fields))


@pytest.mark.parametrize(
    "tag, value, match",
    [
        (270, b"nan", "finite"),
        (270, b"inf", "finite"),
        (270, b"-0.01", r"\[0, 1\]"),
        (270, b"1.01", r"\[0, 1\]"),
        (270, b"not-a-number", "numeric"),
        (271, b"nan", "finite"),
        (271, b"inf", "finite"),
        (271, b"not-a-number", "numeric"),
        (271, b"-1", "non-negative"),
        (271, b"0", "positive size"),
    ],
)
def test_kalshi_book_decoder_validates_native_numeric_values(tag, value, match):
    values = {270: b".5", 271: b"1"}
    values[tag] = value
    frame = fix_frame(
        (35, b"W"),
        (34, b"1"),
        (55, b"FED"),
        (268, b"1"),
        (269, b"0"),
        (270, values[270]),
        (271, values[271]),
        (272, b"20260926"),
        (273, b"00:00:00.000"),
    )
    with pytest.raises(ValueError, match=match):
        fix.decode_kalshi_book(frame)


@pytest.mark.parametrize("missing_tag", [269, 270, 271, 272, 273])
def test_kalshi_book_decoder_requires_every_book_entry_field(missing_tag):
    entry = (
        (269, b"0"),
        (270, b".5"),
        (271, b"1"),
        (272, b"20260926"),
        (273, b"00:00:00.000"),
    )
    frame = fix_frame(
        (35, b"W"),
        (34, b"1"),
        (55, b"FED"),
        (268, b"1"),
        *(field for field in entry if field[0] != missing_tag),
    )
    with pytest.raises(ValueError, match=f"tag {missing_tag}"):
        fix.decode_kalshi_book(frame)


def test_kalshi_book_decoder_preserves_generic_fail_closed_validation():
    frame = fix_frame(
        (35, b"W"),
        (34, b"1"),
        (55, b"FED"),
        (268, b"1"),
        (269, b"0"),
        (270, b".5"),
        (270, b".6"),
        (271, b"1"),
        (272, b"20260926"),
        (273, b"00:00:00.000"),
    )
    with pytest.raises(ValueError, match="duplicate tag 270"):
        fix.decode_kalshi_book(frame)


@pytest.mark.parametrize("not_bytes", [bytearray(b"frame"), memoryview(b"frame"), "frame"])
def test_requires_one_complete_bytes_frame(not_bytes):
    with pytest.raises(TypeError):
        fix.decode(not_bytes)
    with pytest.raises(TypeError):
        fix.decode_kalshi_book(not_bytes)
