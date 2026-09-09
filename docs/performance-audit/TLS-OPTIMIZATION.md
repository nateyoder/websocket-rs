# websocket-rs TLS transport prototype

> **Status: shipped.** This experiment became the default `wss://` path.
> `tls_backend="auto"` uses aiofastnet when it is importable and falls back
> to asyncio otherwise; see [`../TLS-BACKENDS.md`](../TLS-BACKENDS.md). The
> numbers below are the measurement that motivated it, taken with the
> isolated `patches/tls_aiofastnet.patch` build described here — not with the
> shipped code, which adds backend selection around the same transport swap.

The isolated prototype routes native TLS connections through aiofastnet 1.1.0 rather than loop.create_connection. The Rust WebSocket implementation is unchanged. Plain connections retain their previous path. Picows 2.1.1 compatibility API retains its documented default aiofastnet transport throughout; it was not slowed or reconfigured for this experiment.

Seven interleaved rounds with fresh client processes and release builds, CPython 3.13.14, uvloop, macOS ARM64. Each cell uses 0.5 seconds warmup and 2 seconds measured binary request/response traffic against the same neutral Rust TLS server. Certificate verification enabled; compression disabled. Every response is materialized as owned bytes and checked. Connections and cleanup are outside timing. No CPU affinity. Same methodology as COMPAT-API.md.

| TLS payload | Baseline median requests/s | Prototype median requests/s | Unchanged picows median requests/s | Paired prototype improvement (95% bootstrap CI) |
|---|---:|---:|---:|---|
| 256 B | 40,359 | 46,260 | 46,711 | +10.60% [+9.84, +20.52] |
| 8 KiB | 29,698 | 32,089 | 32,575 | +9.32% [+3.64, +10.28] |

Percentages are medians of paired ratios, not ratios of arm medians. Picows vs prototype: +1.62% [-0.97, +4.88] at 256 B; +1.84% [+1.34, +8.53] at 8 KiB. The 256 B comparison does not resolve a winner; picows retains an advantage at 8 KiB in this run. Client CPU medians fall from 13.06 to 10.77 microseconds/request and from 17.28 to 15.14 respectively. No memory or production tail-latency claim follows from this experiment.

Validation: 185 existing Python tests passed against the staged prototype. Additional baseline/prototype TLS checks passed for fragmented text, answering protocol pings, ordered 32-message bursts, and rejection of untrusted certificates. Benchmark response verification passed in every cell. Ruff check/format passed for the new benchmark and TLS check scripts.

This is an experimental patch, not a finished dependency/API change. Production source was restored after building the isolated variant. Shipping requires deciding dependency and selection policy and validating TLS proxy behavior, cancellation/cleanup, backpressure, and deployment platforms. This demonstrates a benefit from transport integration without attributing it to one particular internal copy or scheduling mechanism. Existing unrelated audit findings remain unresolved.

Artifacts:
- `patches/tls_aiofastnet.patch`
- `tls-aiofastnet.json` (all raw rounds and summaries)
- `validation-tls-aiofastnet.txt`
- `tls-transport-check.json`

Reproduce after building the patch into `/tmp/ws-audit/tls_aiofastnet.so` with the audited main build at `/tmp/ws-audit/main.so`:

```sh
.venv/bin/python tests/compat_perf_audit.py --candidate /tmp/ws-audit/tls_aiofastnet.so --transports tls --output tls-aiofastnet.json
```

Picows documentation checked: https://github.com/tarasko/picows and https://picows.readthedocs.io/en/latest/reference.html. Its compatibility API follows documented connect/send/recv usage. The core owned-bytes comparison from the previous run is not a maximum zero-copy callback throughput test. The performance pin is 2.1.1; current documentation is 2.1.3, and matching default aiofastnet selection was also verified in installed 2.1.1 source.
