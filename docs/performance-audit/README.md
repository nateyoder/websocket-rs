# Performance audit — 0.7.11

An audit of `a006d393` (0.7.11) plus the two TLS transport experiments that came
out of it. Everything here is measurement, not design: patches under
[`patches/`](patches/) are the isolated builds each number was taken with, not
production code.

## What to read

| Document | Subject |
|---|---|
| [REPORT.md](REPORT.md) | The audit proper: canceled-receiver retention, queue backpressure, zero-copy retention amplification, compression policy, buffer reclamation. Ranked by priority. |
| [TLS-OPTIMIZATION.md](TLS-OPTIMIZATION.md) | Routing `wss://` through aiofastnet. **Shipped** — now the default. |
| [RUSTLS-PROTOTYPE.md](RUSTLS-PROTOTYPE.md) | Terminating TLS in Rust on the event-loop thread. **Landed off by default** — slower at 8 KiB. |
| [COMPAT-API.md](COMPAT-API.md) | The picows compatibility API measured against the native one. |

## What has been acted on

Only the TLS transport work. See [`../TLS-BACKENDS.md`](../TLS-BACKENDS.md) for
what shipped and [`../../FOLLOWUPS.md`](../../FOLLOWUPS.md) for what is tracked
next. The REPORT.md findings — the priority 1 and 2 items in particular — are
open.

## Reading the numbers

Every percentage is the median of *paired* round ratios with a seeded 20,000-
resample percentile bootstrap interval, so it will not equal the ratio of the
displayed per-arm medians. All of it is loopback traffic on one macOS ARM64 host,
short runs, mostly one connection per cell. None of it establishes production WAN
throughput, p99 latency under load, or behaviour on x86. Absolute rates are not
comparable across sessions.
