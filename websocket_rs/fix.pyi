type Field = tuple[int, str]
type FieldBatch = tuple[Field, ...]
type EntryBatch = tuple[FieldBatch, ...]
type KalshiBookEntry = tuple[str | None, str | None, str, float, float, str, str]
type KalshiBookBatch = tuple[str, str, str | None, tuple[KalshiBookEntry, ...]]

def decode(frame: bytes, /) -> tuple[FieldBatch, EntryBatch]:
    """Validate and decode one complete FIXT.1.1 frame.

    Values are lossless Latin-1 strings. Raises ValueError for malformed FIX
    framing, body length, checksum, tags, duplicates, MsgType-specific group
    delimiters, or tag-268 group counts.
    """

def decode_kalshi_book(frame: bytes, /) -> KalshiBookBatch:
    """Decode one validated Kalshi W/X frame into book-ready typed values.

    The result is ``(msg_type, sequence, snapshot_symbol, entries)``. Entries
    are ``(action, symbol, type, price, size, date, time)`` with native floats.
    """
