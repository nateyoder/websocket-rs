type Field = tuple[int, str]
type FieldBatch = tuple[Field, ...]
type EntryBatch = tuple[FieldBatch, ...]
type KalshiBookEntry = tuple[str | None, str | None, str, float, float, str, str]
type KalshiBookBatch = tuple[str, str, str | None, tuple[KalshiBookEntry, ...]]
type KalshiWsLevel = tuple[int, int]
type KalshiWsBookBatch = tuple[
    str,
    int,
    int,
    str,
    str,
    tuple[KalshiWsLevel, ...],
    tuple[KalshiWsLevel, ...],
    str | None,
    int | None,
    int | None,
]

def decode(frame: bytes, /) -> tuple[FieldBatch, EntryBatch]:
    """Validate and decode one complete FIXT.1.1 frame.

    Values are lossless Latin-1 strings. Raises ValueError for malformed FIX
    framing, body length, checksum, the ordered standard header, tags,
    duplicates, MsgType-specific group delimiters, or tag-268 group counts.
    """

def decode_kalshi_book(frame: bytes, /) -> KalshiBookBatch:
    """Decode one validated Kalshi W/X frame into book-ready typed values.

    The result is ``(msg_type, sequence, snapshot_symbol, entries)``. Entries
    are ``(action, symbol, type, price, size, date, time)`` with native floats.
    Price and size accept FIX decimal syntax, not exponent notation.
    """

def decode_kalshi_ws_book(frame: bytes, /) -> KalshiWsBookBatch | None:
    """Decode a Kalshi WebSocket fixed-point book frame into scaled integers.

    Returns ``None`` for valid non-book JSON. Prices are ten-thousandths of a
    dollar and sizes are hundredths of a contract. Malformed JSON, book shapes,
    identities, sequences, sides, and fixed-point values raise ``ValueError``.
    """
