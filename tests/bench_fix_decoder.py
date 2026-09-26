"""Deterministic local timing harness for ``websocket_rs.fix.decode``.

The frame, warmup count, sample count, and decodes per sample are fixed so
results from two builds are directly comparable on the same idle machine.
This is intentionally separate from pytest: it reports timing rather than
asserting a machine-dependent threshold.
"""

from statistics import median
from time import perf_counter_ns

from websocket_rs import fix

SOH = b"\x01"
WARMUP = 20_000
SAMPLES = 11
DECODES_PER_SAMPLE = 100_000


def frame():
    body = (
        b"35=W\x0149=KALSHI\x0156=CLIENT\x0134=17\x0152=20260926-21:00:00.000\x01"
        b"55=FED-26\x01268=2\x01269=0\x01270=0.4100\x01271=17\x01"
        b"272=20260926\x01273=21:00:00.123\x01"
        b"269=1\x01270=0.5900\x01271=23\x01272=20260926\x01273=21:00:00.124\x01"
    )
    prefix = b"8=FIXT.1.1\x019=" + str(len(body)).encode() + SOH + body
    return prefix + f"10={sum(prefix) % 256:03d}".encode() + SOH


def main():
    payload = frame()

    def decode_and_book():
        fields, entries = fix.decode(payload)
        header = dict(fields)
        book = {dict(entry)[269]: (dict(entry)[270], dict(entry)[271]) for entry in entries}
        return header[55], book

    def decode_typed_and_book():
        _msg_type, _sequence, symbol, entries = fix.decode_kalshi_book(payload)
        book = {entry[2]: (entry[3], entry[4]) for entry in entries}
        return symbol, book

    operations = (
        ("decode", lambda: fix.decode(payload)),
        ("decode+canonical-book", decode_and_book),
        ("decode_kalshi_book", lambda: fix.decode_kalshi_book(payload)),
        ("decode_kalshi_book+canonical", decode_typed_and_book),
    )
    for _name, operation in operations:
        for _ in range(WARMUP):
            operation()

    for name, operation in operations:
        samples = []
        for _ in range(SAMPLES):
            started = perf_counter_ns()
            for _ in range(DECODES_PER_SAMPLE):
                operation()
            samples.append((perf_counter_ns() - started) / DECODES_PER_SAMPLE)

        print(
            f"fix.{name}: median={median(samples):.1f} ns/frame "
            f"min={min(samples):.1f} max={max(samples):.1f} "
            f"frame={len(payload)}B samples={SAMPLES} n={DECODES_PER_SAMPLE}"
        )


if __name__ == "__main__":
    main()
