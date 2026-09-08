# Ping acknowledgment design and measurements

Issue #1 adds caller-owned protocol liveness without placing heartbeat work on
ordinary application-message delivery. Measurements below are local evidence,
not a claim of universal optimality or a substitute for CI correctness checks.

## Alternatives considered

1. **Scan pending Futures before each Ping.** This initial draft was compact and
   used fewer Python bytes per outstanding probe, but did O(n) Python `done()`
   calls per registration: O(n²) work to register a concurrent batch. Canceled
   Futures remained retained until another probe, matching Pong, or closure.
2. **Indexed cancellation cleanup, always scheduled.** A weak callback removes
   only its probe in expected O(1) time. It frees canceled waiters without more
   traffic, but scheduling that callback for every successful Pong adds CPU,
   event-loop work, and temporary memory.
3. **Indexed cleanup detached on acknowledgment (chosen).** The same cancellation
   callback is removed before completing a matching Pong. This preserves prompt
   cancellation cleanup while avoiding a redundant event-loop callback on success.
4. **Custom awaitable/Future wrapper.** Rejected without implementation: an extra
   await layer and cancellation protocol add complexity and overhead compared with
   the native asyncio Future API. A second reverse index with a shared callback
   would trade callback objects for another hash table and lookup; it was not
   necessary to remove the observed bottleneck.

The chosen implementation also:

- Allocates the registry lazily, adding only an optional pointer to clients that
  never use acknowledgment probes.
- Copies payload bytes once into a shared key and encodes the outgoing frame
  directly into Python bytes, avoiding a temporary frame allocation/copy.
- Handles a single acknowledgment without allocating a temporary acknowledgment
  vector; a multi-Pong batch can use a vector.
- Shrinks oversized registry tables after bursts, amortizing reclamation while
  retaining a small table for subsequent heartbeats.
- Invalidates the native-send empty-buffer cache for control writes so application
  sends cannot bypass a buffered Ping/Pong. Correct wire ordering takes priority
  over skipping a necessary transport-buffer check.

## Isolated CPU and Python allocation results

Seven alternating-order subprocess rounds on macOS arm64, CPython 3.12.13,
release builds. The original repository baseline is
`6680a3b3e7b8b5dc88be854e4309bb96614cd306`; the other variants are local drafts.
The deterministic transport removes sockets and peer latency from these results.
Registration uses nine batches per concurrency level and discards two warmups.

| Metric (median) | Scanning draft | Always-scheduled cleanup | Chosen cleanup |
| --- | ---: | ---: | ---: |
| CPU ns per complete immediate acknowledgment | 503 | 1,131 | 481 |
| Registration ns/probe, 32 outstanding | 1,000 | 344 | 344 |
| Registration ns/probe, 256 outstanding | 4,676 | 332 | 320 |
| Registration ns/probe, 1,024 outstanding | 17,030 | 348 | 346 |
| Python bytes per outstanding probe at 4,096 | 153 | 272 | 272 |
| Python bytes retained after canceling all 4,096 | 592,019 | 1,132 | 1,132 |

At 1,024 probes, the chosen/scanning paired CPU ratio is 0.0202 (bootstrap
95% CI 0.0195–0.0212), about 49× less registration CPU in this workload.
For immediate acknowledgments, the chosen/scanning ratio is 0.988
(0.9466–1.0064), so this run does not resolve an improvement for single probes.
The chosen/always-scheduled acknowledgment CPU ratio is 0.4302 (0.4051–0.4393).

The memory tradeoff is explicit: live pending probes cost more Python memory,
while canceled probes are reclaimed promptly. `tracemalloc` excludes Rust
allocations, including keys and registry tables. Median process peak RSS was
29.9 MB for the original baseline, 31.3 MB for scanning, 44.5 MB for always
scheduled, and 34.0 MB for chosen cleanup. These are whole-process high-water
marks, not retained connection memory. The Rust table is separately bounded by
amortized shrinking; no claim of lower total peak memory is made.

## Loopback latency and application throughput

Ping RTT: seven alternating-order rounds, 500 warmups then 5,000 probes per
build, asyncio client and the repository's Rust echo servers. These are local
loopback measurements, not WAN latency forecasts.

| Transport / metric | Scanning | Always scheduled | Chosen |
| --- | ---: | ---: | ---: |
| TCP median RTT, µs | 34.54 | 37.00 | 34.58 |
| TCP p95 RTT, µs | 48.04 | 50.25 | 47.63 |
| TLS median RTT, µs | 40.35 | 40.27 | 41.88 |
| TLS p95 RTT, µs | 52.96 | 53.25 | 54.75 |

TLS was noisy and the chosen implementation was slightly slower in this sample;
these results do not support a claim of improved network latency.

Ordinary application request/response throughput used the existing paired
`tests/bench_ab.py` harness, uvloop, 15 rounds, 0.2-second measurement cells and
the harness's warmups. Comparison is against the original repository baseline.

| Transport / payload | Paired median throughput change | Bootstrap 95% CI |
| --- | ---: | ---: |
| TCP / 256 B | +0.41% | −1.36% to +1.52% |
| TCP / 8 KiB | +0.75% | −0.03% to +1.30% |
| TLS / 256 B | +0.73% | −8.89% to +13.00% |
| TLS / 8 KiB | −0.48% | −3.88% to +1.52% |

No application-throughput effect was resolved. In particular, the broad TLS
intervals do not prove that small regressions are absent. No Linux, large-frame,
or production traffic performance claim is made from this local run.

## Reproduction

Build each extension separately with `maturin build --release`, retaining its
extension binary. Run builds in alternating order in fresh subprocesses; do not
benchmark while compiling or running other benchmarks.

```sh
python tests/bench_ping_ack.py /path/to/extension.so
python tests/bench_ping_ack.py /path/to/extension.so --uri ws://127.0.0.1:18871
python tests/bench_ping_ack.py /path/to/extension.so --uri wss://127.0.0.1:18872
python tests/bench_ab.py -b /path/to/baseline.so -c /path/to/candidate.so \
  --sizes 256,8192 --rounds 15 --duration 0.2 --transport plain
```

Start `ws_echo_server` and `ws_echo_server_tls` for Ping RTT tests; generate the
local TLS certificate with `make tls-certs`. The application harness starts its
own servers. Repeat its command with `--transport tls` for TLS.

Raw measurements: [ping-ack-performance.json](ping-ack-performance.json).

## Review: singleton versus one Vec

For R1-F4, compared commit `93f8573` with an otherwise identical release build
that collects every acknowledgment in one `Vec<PendingPing>`. On the same
Apple Silicon host, ran `tests/bench_ping_ack.py <extension>` in 15 alternating
process pairs. Raw samples are in `ping-ack-review-performance.json`.
The actual Pong acknowledgment path measured median 475 ns with the singleton
slot versus 510 ns with the Vec. The median paired ratio was 0.935 (20,000
paired bootstrap resamples, seed 1, 95% interval 0.908–0.963), a 6.5% CPU
reduction. Application-only CPU had ratio 1.010 (0.991–1.021), with no resolved
change. These are local microbenchmarks, not network latency guarantees.

Retain the singleton slot: it avoids allocating a Vec for the common one-Pong
batch, and this direct measurement exceeds the review's suggested 2% threshold.
Application-only echo throughput does not exercise acknowledgment collection.
This comparison isolates the collection choice before the subsequent review
correctness fixes; it does not claim a before/after result for those fixes.
