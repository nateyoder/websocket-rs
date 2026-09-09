# TLS backends for the native client

`websocket_rs.connect()` terminates `wss://` through one of several transports.
The choice is a `tls_backend=` keyword; it is ignored for `ws://`, which always
uses asyncio's `BufferedProtocol` path.

| `tls_backend` | Transport | Availability |
|---|---|---|
| `"auto"` (default) | aiofastnet when importable, otherwise asyncio | always |
| `"asyncio"` | `loop.create_connection` + `ssl.SSLProtocol` | always |
| `"aiofastnet"` | aiofastnet, or `RuntimeError` if not installed | needs `aiofastnet` |
| `"rustls"` | same-thread rustls, TLS terminated in Rust | needs a build with the `rustls-transport` cargo feature |

## Why `auto` prefers aiofastnet

[aiofastnet](https://pypi.org/project/aiofastnet/) is an OpenSSL-backed asyncio
transport. Only the TLS path is routed through it; the WebSocket implementation
is untouched.

`tls_backend="asyncio"` against `tls_backend="auto"` in the shipped build, 15
alternating paired rounds, 3 measured seconds per cell after a discarded warmup,
fresh interpreter per cell, verified TLS against the repository's Rust echo
server, CPython 3.13.14 + uvloop, macOS ARM64:

| TLS payload | asyncio median req/s | auto median req/s | Paired improvement (95% bootstrap CI) | auto wins |
|---|---:|---:|---|---:|
| 256 B | 37,857 | 42,607 | **+13.10%** [+10.86, +15.93] | 15/15 |
| 8 KiB | 29,527 | 32,929 | **+11.70%** [+10.90, +13.00] | 15/15 |

Reproduce with `python tests/bench_tls_backends.py --rounds 15`; raw summary in
[`performance-audit/shipped-tls-auto-vs-asyncio.json`](performance-audit/shipped-tls-auto-vs-asyncio.json).
The independent audit that motivated the change measured +10.60% and +9.32% on
an isolated patch build, with client CPU falling 13.06 → 10.77 µs/request at
256 B and 17.28 → 15.14 at 8 KiB:
[`performance-audit/TLS-OPTIMIZATION.md`](performance-audit/TLS-OPTIMIZATION.md).

Percentages are medians of paired round ratios, so they do not equal the ratio of
the displayed medians. These are loopback numbers from one host and one
request-response workload. They do not establish production WAN throughput, p99
latency, or memory under load, and absolute rates are not comparable across
sessions.

## Installing it

```bash
pip install 'websocket-rs[fast-tls]'
```

aiofastnet is an *optional* dependency. With `tls_backend="auto"` a missing or
broken install degrades silently to the stdlib path — the connect still succeeds,
just slower. Callers who would rather fail than quietly run slow should pass
`tls_backend="aiofastnet"`, which raises `RuntimeError` when it is unavailable.

Resolution is memoised per process, so the import never runs on the connect path
more than once.

## Custom TLS configuration

`ssl_context=` works identically under `"auto"`, `"asyncio"` and `"aiofastnet"`:

```python
import ssl
import websocket_rs

ctx = ssl.create_default_context(cafile="corp-root.pem")
ws = await websocket_rs.connect("wss://internal.example/ws", ssl_context=ctx)
```

`"rustls"` does not accept an `ssl.SSLContext` — it rejects one rather than
silently ignoring your trust settings. Use `rustls_ca_file=` (a PEM bundle), or
omit it to use the platform's native roots.

## The experimental rustls backend

`tls_backend="rustls"` keeps TLS on the asyncio event-loop thread and terminates
it in Rust, feeding decrypted buffers straight into the Rust WebSocket parser —
no Tokio tasks, no cross-thread handoff, no Python plaintext buffers. It is the
first step toward doing TLS and WebSocket framing in one Rust package.

**It is off by default because it is not yet a win overall.** Measured against
`tls_backend="auto"` in the shipped build, same harness and 15 paired rounds:

| Payload | auto median req/s | rustls median req/s | Paired change (95% CI) | rustls wins |
|---|---:|---:|---|---:|
| 256 B | 42,652 | 45,510 | **+5.80%** [+5.47, +7.61] | 14/15 |
| 8 KiB | 32,809 | 31,793 | **−3.30%** [−3.99, −2.90] | 1/15 |

Both intervals are tight and they disagree in sign: rustls has the lower
per-message overhead and the higher per-byte cost. That is the shape you would
expect from the copies the prototype still makes — through rustls's buffered
reader, a reusable plaintext `Vec`, owned message payloads, and materialized
outbound ciphertext — so eliminating those, or moving to rustls's unbuffered
interface, is the next experiment. Nothing here proves the copies *cause* the
regression.

The independent audit measured the same shape against an aiofastnet patch build,
more extremely and far less precisely: +3.66% [+0.39, +69.12] at 256 B and
−13.48% [−31.23, −1.76] at 8 KiB. Reproduce with
`python tests/bench_tls_backends.py --baseline auto --candidate rustls --rounds 15`;
raw summary in
[`performance-audit/shipped-tls-rustls-vs-auto.json`](performance-audit/shipped-tls-rustls-vs-auto.json),
full prototype notes in
[`performance-audit/RUSTLS-PROTOTYPE.md`](performance-audit/RUSTLS-PROTOTYPE.md).

Build and test it with:

```bash
make test-rustls
```

Known gaps: no client-certificate authentication, no arbitrary `SSLContext`
options, no transport introspection beyond what it forwards, and no tested
TLS-over-SOCKS5 matrix.

## Safety note

`NativeClient` has a send fast path that writes frames straight to the socket
file descriptor, bypassing the transport. It must never engage on a TLS
connection or it would put plaintext on the wire. The guard is in
`connection_made`: the fd is only borrowed when `get_extra_info("ssl_object")` is
`None`. aiofastnet's SSL transport returns a real `ssl_object`, and the rustls
shim returns a marker for the same reason. Every round-trip cell in
`tests/test_tls_backends.py` fails loudly if either guard breaks, because the
peer cannot decrypt what it receives.
