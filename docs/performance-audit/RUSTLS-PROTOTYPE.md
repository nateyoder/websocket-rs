# Same-thread rustls prototype

> **Status: landed, off by default.** The prototype now lives in
> `src/native_client/rustls_transport.rs` behind the `rustls-transport` cargo
> feature and is reachable as `tls_backend="rustls"`. It is built and tested
> in CI so it does not rot, but no build ships it enabled, for the reason
> below. Build it locally with `make test-rustls`.
>
> Re-measured against the shipped `tls_backend="auto"` build over 15 paired
> rounds, the same trade-off appears with much tighter intervals: **+5.80%**
> [+5.47, +7.61] at 256 B (14/15 rounds) and **−3.30%** [−3.99, −2.90] at
> 8 KiB (1/15). The direction below is confirmed; the magnitudes here came
> from a noisier session and should not be quoted. See
> [`shipped-tls-rustls-vs-auto.json`](shipped-tls-rustls-vs-auto.json) and
> [`../TLS-BACKENDS.md`](../TLS-BACKENDS.md).

Implemented an opt-in `tls_backend="rustls"` in the native client. It keeps raw TCP on the asyncio/uvloop event-loop thread, performs TLS with rustls, and feeds decrypted Rust buffers directly into the existing Rust WebSocket parser. There are no Tokio tasks or cross-thread message handoffs in this path. The default backend is unchanged.

## Result

**Do not replace the aiofastnet prototype with this implementation by default.** The longer direct comparison shows an unstable small-message benefit and a material 8 KiB regression. A Rust TLS implementation is feasible, but Rust alone does not guarantee a faster end-to-end client.

| Payload | websocket-rs + aiofastnet median requests/s | rustls median requests/s | Paired rustls change (95% bootstrap CI) | Wins |
|---|---:|---:|---|---:|
| tls/256 | 23,415 | 28,271 | +3.66% [+0.39, +69.12] | 11/15 |
| tls/8192 | 20,754 | 18,063 | -13.48% [-31.23, -1.76] | 3/15 |

Fifteen alternating paired rounds, 3 seconds measured per cell plus 0.5 seconds warmup, fresh client processes, CPython 3.13.14, uvloop, macOS ARM64. Neutral local Rust TLS echo server, compression disabled, verified TLS, one outstanding binary request, owned-byte response validation every message. Handshake/configuration and cleanup excluded. No CPU affinity. Percentages are medians of paired ratios, not ratios of displayed medians.

Host variability is substantial: the 256-byte confidence interval is too wide to estimate a stable improvement magnitude. Treat the small-message result as provisional even though its percentile interval is narrowly above zero. Absolute rates differ from previous runs and must not be compared across sessions. The 8 KiB regression is the practical reason not to select this implementation by default. Client CPU medians are 17.87 to 15.78 microseconds/request at 256 B and 23.28 to 25.68 at 8 KiB; these are per-arm medians, not paired CPU effect estimates.

The initial seven-round three-arm run retained unchanged picows 2.1.1 with aiofastnet 1.1.0. Rustls vs websocket-rs + aiofastnet was +9.63% [-3.27, +68.42] at 256 B and -2.19% [-10.66, +2.36] at 8 KiB; neither resolved an improvement. Picows remained faster at 8 KiB in that run. The longer follow-up compared only the two websocket-rs builds to reduce time between paired measurements; picows was not disabled, modified, or slowed.

## Validation

- 185 existing Python tests passed with the staged prototype (default backend), checking regressions to existing behavior.
- 19 Rust unit tests passed; clippy with warnings denied and cargo fmt check passed.
- Rustls-specific TLS checks passed for certificate rejection, fragmented text, protocol ping/pong and ordered 32-message bursts.
- On both uvloop and standard asyncio, cancellation and timeout each released three connections at each of the TLS and WebSocket handshake phases (12 per loop).
- Both loops passed a 16 MiB burst with a delayed reader, sends from receive callbacks, and rejection of a trusted certificate with an incorrect hostname.
- Ruff check/format passed for the benchmark and verification scripts.

These checks are not a complete TLS conformance or platform matrix. They do not establish production feed p99 latency or memory under load. Existing unrelated audit findings remain unresolved.

## Scope and remaining work

The prototype accepts `rustls_ca_file` as an explicit trust store or uses the repository's cached native-root configuration. It rejects `ssl.SSLContext` with rustls instead of silently ignoring settings. It does not implement client-certificate authentication, arbitrary SSLContext options, full transport introspection, or a tested TLS-over-proxy matrix. The private transport returns a TLS marker to prevent the existing raw-socket send optimization from bypassing encryption.

There are still copies through rustls's buffered reader, a reusable plaintext Vec and owned message payloads, plus outbound ciphertext materialization for the asyncio transport. Reducing those copies or trying rustls's unbuffered interface are possible next experiments; this run does not establish that copies caused the regression. A buffer-lifetime redesign needs its own tests. The prototype inherits existing NativeClient queue policy; it is not a backpressure/memory-limit fix.

## Reproduction and artifacts

Build this working tree with `PYO3_PYTHON="$PWD/.venv/bin/python" .venv/bin/maturin build --release --locked --out /tmp/ws-audit/wheels`, then copy the release library to `/tmp/ws-audit/rustls.so`. The aiofastnet baseline is the previously built `tls_aiofastnet.patch` variant of a006d393bcf0601b553459dcd97e6592fb674187. Build hashes are in `rustls-builds.json`.

```sh
.venv/bin/python tests/compat_perf_audit.py --baseline /tmp/ws-audit/tls_aiofastnet.so --candidate /tmp/ws-audit/rustls.so --candidate-backend rustls --transports tls --pairs-only --rounds 15 --duration 3 --output rustls-vs-aiofastnet-confirmation.json
```

Stage the prototype with `bench_ab._stage_build` and run `tests/tls_transport_check.py STAGE rustls` and `tests/rustls_lifecycle_check.py STAGE uvloop` / `STAGE asyncio` for the additional checks.

Raw measurements: `rustls-vs-aiofastnet.json`, `rustls-vs-aiofastnet-confirmation.json`. Correctness logs: `rustls-functional.json`, `validation-rustls.txt`, `validation-rustls-unit.txt`. Complete experimental patch: `patches/rustls_same_thread.patch`. Production integration is not being enabled by default.
