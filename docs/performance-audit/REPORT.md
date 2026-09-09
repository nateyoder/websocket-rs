# Performance audit of main

Audited commit: `a006d393bcf0601b553459dcd97e6592fb674187` (0.7.11), fetched from `nateyoder/websocket-rs` on 2026-09-08 Pacific time. Production sources were restored after building experiments; this audit does not install or recommend merging all experimental patches.

## Conclusions and priority

The highest-priority opportunities are **bounded queues and cancellation cleanup**, **selective copying to prevent input-buffer retention**, and **an explicit compression policy**. Published benchmark rankings were also cross-checked using the repository’s original benchmark functions; see the final section for important hardware and workload differences. The existing native API already avoids the deprecated client's expensive thread bridge. More SIMD or additional inlining is not the first change I would make on this evidence.

The findings below distinguish a reproduced problem, a measured experimental improvement, and an unproven implementation idea. This is an inventory of the candidates found in this review, not a claim that every possible optimization has been exhausted.

| Priority | Area | Evidence | Recommended direction |
|---|---|---|---|
| 1 | Canceled receive waiters | 50,000 cancellations retain ~6.5 MiB of Python allocations; subsequent message is discarded. Reproduced over real asyncio and uvloop connections. | Remove canceled waiters and skip already-completed waiters without consuming messages. |
| 1 | Receive/write backpressure | 1,024 × 64 KiB queued messages use ~64 MiB of payload storage per connection. Resume drains all 1,024 writes despite an immediate renewed pause. | Byte-based high/low watermarks; awaitable drain or explicit overflow error; stop flushing when paused. |
| 2 | Small messages pin large input chunks | 2 KiB useful payload retains ~32 MiB of Python allocations; selective-copy prototype reduces it to ~11 KiB. | Copy small payloads; preserve zero-copy for larger ones. Tune against realistic message/chunk ratios. |
| 2 | Compression CPU and event-loop stalls | Level 1 reduces 1 MiB random-message send CPU from 14.92 ms to 2.74 ms; repeated data from 790 µs to 68.6 µs. | Expose level and per-message compression choice; choose by CPU, bandwidth and latency budget. |
| 2 | Receive scratch high-water retention | One 8 MiB message leaves a 16 MiB receive buffer after 1,000 small messages. | Reclaim after inactivity or sustained small traffic; release scratch on teardown. |
| 3 | Avoid unnecessary payload materialization | At 1 MiB, `bytes(msg)` takes 13.30 µs versus 0.104 µs for `memoryview(msg)`. | Parse directly from the buffer when an independent immutable copy is unnecessary. |
| 3 | Existing callback API | 64 KiB/window 32: +7.83% paired network throughput, 95% bootstrap interval [+2.23%, +12.60%]. | Offer callback examples for high-throughput consumers; do not promise the same benefit for small messages. |
| 3 | Deprecated asyncio/Tokio bridge | Native versus legacy, 256 B/window 1: +131.85% throughput [+121.59%, +144.23%]. | Migrate compatible users to the already-public native API before optimizing the legacy implementation. |
| 4 | Blocked-send duplicate encoding | Reuse prototype: 1 MiB CPU rate +26.6%, but 64 KiB **−18.9%**. | Do not merge unconditionally; investigate a size threshold and real congested sockets. |
| 4 | Chunked/fragmented receive copying | 1 MiB aligned input: 0.280 µs; 16 KiB chunks: 21.03 µs; two fragments: 30.41 µs. | Profile partial-buffer growth and fragment assembly; these workload costs are not an established patch speedup. |

## Measurement design and limits

- Host: macOS 26.6.2, ARM64, 16 logical CPUs, 128 GiB RAM; CPython 3.13.14, uvloop 0.22.1, rustc 1.98.0. Exact versions and SHA-256 hashes are in [environment.json](environment.json).
- All binaries use the repository release profile (`opt-level=3`, fat LTO, one codegen unit), built through maturin. Each candidate changes one area and starts from the same main source. [Patches](patches/) are experiments, not production-ready fixes.
- CPU suite: 15 alternating four-arm blocks, one fresh interpreter per build per block. Each cell warms up, then reports the median of five process-CPU samples. Assertions validate receive lengths and fully decode/match outbound masked/compressed payloads. Raw arrays and iteration counts are preserved. The shared baseline and a candidate are in the same block, not necessarily adjacent.
- Network suite: 15 alternating paired rounds per scenario, fresh client interpreters, same repository echo-server generation for both arms. Plain TCP and TLS run on loopback. Each worker performs a discarded warmup run. Replies include the server's 24-byte envelope; full payload equality is checked outside timing, and size/head/tail are checked inside timing. Pipeline windows and sample counts are explicit in the script. No CPU affinity is available on this macOS host.
- Reported speedup is `baseline time / candidate time - 1` (or candidate/baseline throughput). It is **not** the percentage reduction in latency. Intervals are seeded, 20,000-resample percentile bootstrap intervals for median paired ratios. Ratios of separate displayed medians need not equal the paired statistic.
- Memory probes repeat three times in fresh processes. Python allocation figures are from `tracemalloc`; RSS is current process resident memory from psutil. Rust allocations are absent from tracemalloc. Allocator caching means freeing live objects need not reduce RSS immediately. No RSS result alone is called a leak.
- These are exploratory comparisons, without multiple-comparison correction. Narrow confidence intervals for large CPU effects are compelling here; small effects and p99 differences need confirmation on deployment hardware. Per-cell p99 medians are descriptive, not statistically established tail-latency improvements.
- Loopback, short runs, one host and mostly one connection per cell cannot establish production WAN/TLS throughput, sustained multicore scaling, or x86 AVX-512 effects. Compression measurements are isolated send CPU, not bandwidth-constrained network benchmarks.

## Reproduced load and memory findings

### 1. Canceled receivers retain memory and drop subsequent messages

`recv()` and `__anext__()` append Futures to `pending_recv` without cancellation cleanup. Both fast and slow message delivery pop one waiter. `set_future_result()` ignores an already-done Future, so the message is lost rather than passed to the next live waiter.

The controlled memory test leaves **6,800,712 Python bytes** after 50,000 cancellations in its first run. A subsequently injected message does not reach a live receiver. Separate real-connection tests reproduce this with **both asyncio and uvloop** using one canceled receiver, one live receiver, and one echoed message. This is a correctness problem as well as a memory/latency problem; it can affect receive timeouts because those cancel the underlying Future too.

Fixing it needs cancellation cleanup when no new data arrives, plus delivery that skips stale waiters. Merely skipping canceled entries on the next message still leaves idle cancellation accumulation. Preserve ordering and custom-Future reentrancy, and cover both the fast parser and ProtocolCore paths. The ping registry already provides a useful weak-reference cleanup pattern, but a receive implementation still needs its own correctness tests.

Source: `src/native_client/client.rs:845–876`, `892–919`, `1551–1574`, `1621–1633`, `1657–1671`.

### 2. Per-frame caps do not bound aggregate queued memory

The native client does not apply receive backpressure to `backlog`. The paused write queue stores complete encoded Python bytes and accepts further `send()` calls. In the controlled load test:

| Messages × 64 KiB | Receive RSS increase, first run | Paused-write live Python increase, first run |
|---|---:|---:|
| 128 | 8.20 MiB | 8.07 MiB |
| 512 | 32.31 MiB | 32.09 MiB |
| 1,024 | 64.41 MiB | 64.11 MiB |

These numbers demonstrate accumulation under an intentionally stalled consumer; they are not proof of a leak after draining. The full raw results include allocator-retained RSS after draining.

`resume_writing()` takes the entire queue and writes every item without checking whether `pause_writing()` fired again. A deterministic transport that pauses on its first write still receives **all 1,024 queued writes**. The first measured drain took 1.73 ms even with a transport that discards data. Real transport buffering and TLS encryption can add cost; that extra cost was not measured here.

Introduce **byte** limits, not just message counts: a few large frames can dominate memory. Receive high/low watermarks can drive `pause_reading()`/`resume_reading()`. Outbound flow needs an awaitable drain mechanism or a documented overflow outcome. No finite buffer can absorb a sustained producer/consumer mismatch indefinitely. Keep control frames and close handling responsive, preserve queue order after reentrant writes, and use a bounded flush budget where fairness requires it.

Source: `client.rs:782–786`, `628–647`, `1657–1671`; aggregate state queues near `client.rs:80–145`.

### 3. Zero-copy retention amplification

Each zero-copy `WSMessage` owns a reference to the whole incoming `PyBytes`. Retaining a small frame can therefore retain large neighboring frames from the same receive chunk. The test constructs 128 chunks, each containing a 16-byte retained message and a discarded 256 KiB sibling.

- Main: 2,048 useful bytes retain **33,573,466 live Python bytes** (~32.02 MiB); first-run RSS rises ~34.66 MiB.
- Copy ≤1,024-byte payloads: **11,226 live Python bytes**, first-run RSS ~1.05 MiB.
- Python accounting excludes the candidate's small Rust payload allocations. It still proves the large Python chunks are released; the independent RSS comparison corroborates the large reduction.

The main test fails an explicit <1 MiB retention budget; the isolated small-copy build passes it. The CPU result for aligned 16-byte messages is inconclusive (+0.2% rate; interval spans zero). TLS network results are also inconclusive: 256 B/window 1 **+1.49% [−3.80%, +4.28%]**, 64 KiB/window 32 **+0.77% [−0.05%, +2.84%]**. This is evidence for a memory optimization, not a speed claim or proof of zero regression.

The 1 KiB cutoff is an experiment policy, not an established optimal threshold. A payload-to-owner-size rule could be better; no such rule was tested. Tiny-frame traffic delivered in already-small chunks may benefit less.

Source: `client.rs:286–306`, `1231–1269`; experiment: [small_copy.patch](patches/small_copy.patch).

### 4. Grow-only receive/send scratch

On main, a completed 8 MiB receive leaves `get_buffer()` exposing **16,777,216 bytes**, still present after 1,000 subsequent 16-byte messages. Geometric growth crosses a power-of-two boundary because the frame includes a header. The state also has grow-only `send_buf` logic, though this audit does not separately quantify its retained capacity.

The receive-reclaim prototype reduces the exposed empty receive buffer to **65,536 bytes** on a later `get_buffer()` call. The deterministic capacity test passes with the candidate. RSS does not immediately fall on this allocator, so the proven gain is live buffer capacity/reusability, not immediate OS memory return.

A 1 MiB continuous request/response test gives **−0.77% throughput [−6.68%, +7.40%]**: the cost of aggressive reclamation is unresolved. A production policy should retain useful steady-state capacity and shrink after inactivity or sustained smaller traffic, with hysteresis. Never reallocate while a transport buffer view is outstanding. Consider teardown reclamation too, while preserving the API's ability to drain already-received messages after close.

Source: `client.rs:810–819`, `1437–1469`; [recv_reclaim.patch](patches/recv_reclaim.patch).

## CPU and latency opportunities

### 5. Compression policy matters much more than small codec tweaks

Native `send()` creates a fresh compressor at the default level, compresses synchronously on the event-loop thread, then masks and sends the result. The per-message fresh context is deliberate: the code documents a decoder-reset correctness problem. Do not replace fresh contexts with resets without adversarial compression tests.

| Payload | Main send CPU | Level-1 send CPU | Paired CPU-rate speedup | Encoded frame bytes, main → level 1 |
|---|---:|---:|---:|---:|
| 256 B repeated | 4.54 µs | 4.21 µs | +7.8% | 21 → 21 |
| 256 B random | 8.67 µs | 7.27 µs | +20.7% | 270 → 270 |
| 64 KiB repeated | 54.67 µs | 8.87 µs | +518.7% | 84 → 325 |
| 64 KiB random | 708.47 µs | 176.73 µs | +301.4% | 65,566 → 65,609 |
| 1 MiB repeated | 790.38 µs | 68.63 µs | +1,058.0% | 1,041 → 4,825 |
| 1 MiB random | 14.92 ms | 2.74 ms | +445.2% | 1,048,761 → 1,049,561 |

For 1 MiB random data, this is roughly **82% less send CPU**, but it still blocks the event-loop thread for milliseconds. The input is deterministic pseudorandom bytes, not a production traffic corpus. Incompressible data also becomes larger. Default compression is already off; the opportunity applies when callers enable it.

Expose a configurable level and a per-message choice to avoid compressing small/already-compressed data. An adaptive classifier or off-thread compressor adds overhead, ordering and cancellation complexity and remains untested. Decompression also allocates output and runs synchronously; preserve both inflated-message and frame limits. The full confidence intervals are in [cpu-summary.json](cpu-summary.json), and every compressed cell validates a complete decompression round trip.

Source: `client.rs:713–756`, `protocol.rs:278–312`; [compression_fast.patch](patches/compression_fast.patch).

### 6. Preserve views when the consumer can use them

On the same 1 MiB `WSMessage`, materializing `bytes(msg)` costs **13.30 µs** versus **0.104 µs** for creating a memoryview. At 64 KiB: **0.854 µs** versus **0.103 µs**. These are warmed, repeated operations on the same input. The view operation does not do the work of making an independent copy; this is a consumer/API usage choice, not a faster implementation of identical semantics.

Use `memoryview(msg)` and `struct.unpack_from` where appropriate. `msg[:N]` currently copies the slice; it is fine for small metadata but should not be used as a supposed zero-copy full-payload conversion. A view retains its backing storage, so combine this advice with the retention findings above. For sync/legacy clients, returning Python bytes already imposes a receive copy; changing that return type would be an API change and was not prototyped.

Source: `client.rs:318–320`, `330–346`; `sync_client.rs:508–511`; `async_client.rs:78`.

### 7. Callback delivery versus await delivery

At 64 KiB/window 32/plain TCP, the existing `on_message` API improves paired throughput **7.83% [2.23%, 12.60%]**. Separate medians: 48,122 → 52,931 messages/s; client CPU 20.49 → 18.50 µs/message. Median-of-cell p99 RTT: 1,009 → 997 µs, a descriptive difference only.

An additional 15-round run of the repository’s **original** window-100 / N=5,000 benchmark also favors callbacks modestly on this host: +3.03% inverse-mean-RTT ratio [2.00%, 11.11%]. This disagrees with the published WSL2 table’s await-versus-callback ordering, so neither ordering should be treated as universal. See [published-callback.json](published-callback.json).

At 256 B/window 32, the paired effect is **+0.04% [−7.62%, +17.21%]**. Do not claim a small-message win from the ratio of separate medians. Callback consumers must stay short and nonblocking; long callbacks can impair other connections. A batched receive API could reduce per-message Python overhead further, but was not implemented or measured.

Source: `client.rs:1383–1414`, `1657–1663`.

### 8. Prefer the existing native API to the deprecated thread bridge

For 256 B/window 1/plain TCP, native versus legacy gives **+131.85% throughput [121.59%, 144.23%]**. Separate medians: 18,791 → 44,283 messages/s, p50 RTT 47.38 → 19.44 µs, client CPU 41.33 → 10.32 µs/message. For 64 KiB/window 32 the gain is much smaller: **+3.05% [1.79%, 6.76%]**.

The legacy path copies sends into `Vec`, queues commands, flushes with `SinkExt::send`, and on a waiting receive spawns work that reattaches to Python and calls `call_soon_threadsafe`. Native delivery avoids the cross-runtime bridge. This comparison measures the whole public API path, including its different return type; it does not isolate the contribution of each copy, lock, or wakeup.

The native API is already the package default. This opportunity is for remaining legacy users. For users who must retain legacy semantics, candidate work includes bounded command batching/feed+flush, immutable send ownership like the sync implementation, and coalesced receive wakeups. None has a measured standalone benefit in this audit. The single actor also awaits channel sends inside its select loop; test control-frame responsiveness under a full receive channel before changing capacities.

An initial network run stopped after six complete rounds on a legacy 64 KiB worker error; stderr was not retained by that initial harness, so the cause is unknown. Its partial data and parent exception are preserved separately and are **not pooled** into the reported results. The final failure-recording 15-round run completed every cell without a worker failure. No legacy deadlock or reliability defect is claimed from the preliminary failure.

Source: `async_client.rs:180–184`, `216–272`, `361–405`, `409–480`.

### 9. Blocked-send reuse must be size-sensitive

After `native_send()` returns an error/EAGAIN, main builds and masks a new Python frame even though a complete masked frame is already in `send_buf`. The prototype copies that scratch frame into Python bytes instead.

A real nonblocking socketpair is filled to EAGAIN; the fallback transport deliberately discards its output to isolate CPU. All emitted frames are unmasked and verified.

| Payload | Main | Reuse | Paired CPU-rate effect, 95% interval |
|---|---:|---:|---:|
| 64 KiB | 2.770 µs | 3.390 µs | **−18.9% [−19.7%, −15.9%]** |
| 1 MiB | 41.855 µs | 32.935 µs | **+26.6% [24.9%, 29.3%]** |

Fused masking is already efficient; an additional copy strategy is not automatically better. This experiment does not measure actual congested-network throughput, different socket buffer sizes, partial writes, or CPU architectures. Keep the candidate experimental until an appropriately guarded version wins those workloads.

Source: `client.rs:804–841`; [eagain_reuse.patch](patches/eagain_reuse.patch).

### 10. Fragmentation and partial-frame buffering deserve profiling

For a 1 MiB payload, isolated warmed receive CPU is 0.280 µs for one aligned immutable chunk, 21.03 µs when delivered in 16 KiB chunks, and 30.41 µs for two WebSocket fragments delivered together. The aligned case can construct an O(1) view; chunked and fragmented cases necessarily do more assembly. Do not interpret the ratio as a realizable 100× application speedup.

The slow path copies incoming chunks into `BytesMut`. First-fragment handling then copies again into a newly allocated fragment buffer; continuations extend that buffer. Candidate changes include transferring the first fragment when ownership allows, geometric/informed reserve policies, and avoiding copy-to-assembly-to-output chains. A compressed receive also passes through this path. Those specific changes need representative split-boundary, interleaved control-frame and maximum-message tests before a speed claim.

Source: `client.rs:1297–1380`; `protocol.rs:111–169`.

### 11. Cold paths: handshake scans and TLS context creation

A controlled incomplete-handshake probe feeds the actual parser 1 KiB chunks before terminating the header. CPU grows from **59 µs at 16 KiB**, to **612 µs at 64 KiB**, to **9.153 ms at 256 KiB**. Delivering the 256 KiB prefix at once instead takes **265 µs**. This is consistent with repeated full-buffer scans. The prefix is intentionally malformed, and the current parser accepts it once the accept header arrives; these are robustness/cost probes, not representative HTTP traffic or a claim of normal handshake latency. Add a handshake byte cap and incremental delimiter scanning, with fragmented-header and rejection tests.

Creating a default Python SSLContext costs a median **4.50 ms** on this host (15 batches of 20 calls), compared with sub-microsecond access to an existing context. Native TLS connect creates this context synchronously when the caller does not supply one. Caller-managed reuse can avoid this setup cost during reconnect bursts; an end-to-end connection improvement has not been measured. Preserve certificate validation and policy separation; do not substitute an insecure context. Results are in [cold-paths.json](cold-paths.json), generated by [cold_perf_audit.py](../../tests/cold_perf_audit.py).

## Additional candidates: code evidence, not established gains

| Area | Code evidence and proposed experiment | Status |
|---|---|---|
| TLS context construction / reconnect storms | Native default TLS connect calls Python `ssl.create_default_context()` per connect (`mod.rs:162–169`); caller-supplied context is already supported. Measure fresh versus reused contexts and end-to-end reconnects. Do not cache across incompatible trust policies. | Context-only measurement below; end-to-end gain unproven. |
| Incomplete handshake accumulation | `find_header_end()` rescans the entire accumulated buffer (`codec.rs:229`, `protocol.rs:66`) and the native path has no header byte cap. Bound headers and resume scanning near the prior end. | Controlled scaling measurement below; not a normal handshake latency claim. |
| Per-frame object allocation / batching | A WSMessage, Bytes owner and often an awaitable/Future are created for each message. Test a bounded `recv_many()` API and batch callbacks with real consumers. | Not prototyped. |
| Reassembly/decompression working memory | First fragment copy, vector capacity guesses, temporary compressed output and inflated output can coexist. Allocation counters and realistic compression corpora are needed. Avoid the documented decoder reset pitfall. | Costs observed; replacement strategy unproven. |
| Send batching / syscall count | Native performs one send syscall per eligible message; sync and legacy flush each `send`. Test an explicit bounded batch API, preserving latency deadlines and control-frame priority. | Not prototyped; could hurt request/response latency. |
| Idle connection density | Plain BufferedProtocol asks for ≥64 KiB receive headroom. The README cross-check below measures 1,000 real connections, allocated capacity versus RSS. | See published-benchmark cross-check; do not equate capacity with RSS. |
| Lifecycle cleanup | Native peer-close/lost paths mark closed and settle Futures; some buffers and cached references remain while the object is retained. Test object lifetime and queued-message semantics before more aggressive cleanup. | Receive scratch retention reproduced; a general connection-object leak is **not** established. |
| Handshake/SOCKS timeout resource use | SOCKS uses blocking socket calls in an executor without a socket deadline; wrapping the await in `wait_for` does not inherently interrupt those calls. Test blackholed proxies, thread counts and cleanup. | Code-review hypothesis; no resource-exhaustion claim without that test. |
| SIMD, allocators, Arc/Rc, inlining, PGO | Masking already fuses copy+XOR, caches feature detection, pools mask keys and reuses scratch. Release already uses LTO. Test on actual x86/ARM targets with profiles before changing kernels/allocators. | No evidence here that these changes should take priority. |
| Sync API batching / copy-free receive | Exact immutable sends already borrow Python storage and signal handling avoids repeated timeout syscalls in ordinary reads. `recv()` still materializes Python bytes. Test an opt-in view API and `send_many()` against consumers. | Not prototyped; maintain signal and thread-safety semantics. |

## Validation and reproduction

The existing main Python suite passes **185 tests**; Rust unit tests pass **19 tests**. Baseline rustfmt and clippy pass. All four experimental builds also pass the existing **185-test** Python suite independently. Variant validation results are recorded in [validation.json](validation.json) and individual text logs. The deterministic desired-behavior probes deliberately **xfail** on main; they are not claimed as passing regressions. The selective-copy and reclaim variants pass their corresponding probes with `--runxfail`.

Tests and evidence:

- [CPU/memory driver](../../tests/perf_audit.py), [serialized orchestration](../../tests/run_perf_audit.py), [network driver](../../tests/network_perf_audit.py).
- [Deterministic and real-network reproductions](../../tests/probe_perf_findings.py), [main expected failures](probes-main.txt).
- [Variant builder](../../tests/build_perf_variants.py), [full-suite isolated validation](../../tests/validate_perf_variants.py).
- [CPU raw](cpu-raw.json), [CPU summary](cpu-summary.json), [memory raw](memory-raw.json), [network raw](network-raw.json), [network summary](network-summary.json).

From a clean checkout of the audited commit, with Python ≥3.12, Rust and uv installed (commands below target the audited macOS environment):

```sh
UV_CACHE_DIR=/tmp/uv-cache uv venv --python 3.13
UV_CACHE_DIR=/tmp/uv-cache uv pip install maturin pytest pytest-asyncio websockets uvloop psutil ruff
make build
mkdir -p /tmp/ws-audit
cp target/release/libwebsocket_rs.dylib /tmp/ws-audit/main.so
.venv/bin/python tests/build_perf_variants.py
.venv/bin/python tests/run_perf_audit.py --rounds 15
make bench-servers tls-certs
.venv/bin/python tests/network_perf_audit.py --rounds 15
WS_AUDIT_EXTENSION=/tmp/ws-audit/main.so .venv/bin/pytest tests/probe_perf_findings.py -q -rx
WS_AUDIT_EXTENSION=/tmp/ws-audit/small_copy.so .venv/bin/pytest tests/probe_perf_findings.py --runxfail -k small_message -q
WS_AUDIT_EXTENSION=/tmp/ws-audit/recv_reclaim.so .venv/bin/pytest tests/probe_perf_findings.py --runxfail -k reclaims -q
.venv/bin/python tests/validate_perf_variants.py
.venv/bin/python tests/cold_perf_audit.py
```

Run performance measurements serially with no simultaneous compiler/test workload. Linux uses `libwebsocket_rs.so`; the repository's server helper also assumes cores 0/1 are usable if taskset is installed, so adapt affinity to the allowed cpuset. Reproduction should pin the versions in `environment.json` for a close comparison. The added scripts are separate from pytest's default discovery so expensive benchmarks and deliberate failure probes do not enter ordinary CI.

## Cross-check against the upstream GitHub benchmarks

Sources: [upstream README](https://github.com/coseto6125/websocket-rs#-performance) and [benchmark documentation](https://github.com/coseto6125/websocket-rs/blob/main/docs/BENCHMARKS.md), also present in this audited checkout. The README explicitly says its older multi-client table is superseded for native-vs-picows comparisons. Its newer comparison is **v0.7.3 versus picows 2.1.1, Ryzen 9950X/WSL2, pinned cores, seven interleaved RR rounds**. The audit is **v0.7.11, ARM64 macOS, no affinity**. Those differences prohibit expecting identical absolute numbers or claiming that a local mismatch disproves the published run.

To test our own methodology rather than explain away a discrepancy, [published_perf_audit.py](../../tests/published_perf_audit.py) imports and calls the **unchanged original benchmark functions**. It uses their 50-message warmup, one-second discarded prepass, GC policy, ten-second RR duration, and the repository tokio-tungstenite server. Picows was pinned to **2.1.1** before the scored head-to-head run. Seven paired rounds cover 256 B and 1 MiB. The other two sizes and second server were not rerun; this is a cross-check, not replication of the entire headline matrix.

We also ran the original window-100/N=5,000 callback benchmark for 15 paired rounds, and a seven-round one-second native-versus-identical-native control. The latter gives +1.08% [−1.45%, +1.90%] at 256 B and −1.88% [−19.28%, +4.33%] at 1 MiB. Both include zero; the large-message control is noisy enough that small effects there must remain unresolved. It does not prove absence of bias in every workload.

Published idle-memory numbers concern **idle established connections**, whereas the earlier retention/backlog tests intentionally hold messages or stall consumers. They cannot be compared directly. [idle_perf_audit.py](../../tests/idle_perf_audit.py) separately opens 1,000 real uvloop-backed native connections without application traffic, after ten discarded warmup connections, and records both current RSS and peak RSS. Page size and receive capacity are recorded to avoid mistaking allocated capacity for resident memory.

The original published RR functions do not validate payload contents inside the timed loop; our custom audit does lightweight length/head/tail validation and timestamps each reply. That instrumentation, fixed counts versus fixed time, GC policy and the smaller pipeline window can change absolute throughput. Original-function reruns cross-check the overall measurements; deterministic content-validation and failure probes separately support the concrete findings.

The core audit findings do **not** depend on beating picows or matching the README’s absolute throughput. Canceled-message loss and memory retention have deterministic tests. Compression costs and blocked-send results compare the same workload across isolated builds. Claims about callback preference and broad library ranking are explicitly limited to the tested environment.

### Cross-check results

| Scenario | Published result | Original-function rerun on this Mac | Interpretation |
|---|---|---|---|
| Native vs picows, 256 B RR, tokio server | Native +0.4% | Native **+6.60%** paired throughput [3.93%, 14.74%]; separate medians 50,102 vs 47,166 RPS | Same broad competitive performance, different magnitude. |
| Native vs picows, 1 MiB RR, tokio server | Native +20.0% | Native **−6.73%** paired throughput [−29.97%, +1.95%]; wins 2/7 pairs | Published win **not reproduced**; local ranking unresolved. |
| Await vs callback, 64 KiB/window 100 | Await mean 0.91 ms, callback 1.00 ms | Await median-of-means 1.976 ms, callback 1.882 ms; paired inverse-mean ratio favors callback +3.03% [2.00%, 11.11%] | Ordering differs; avoid a universal callback recommendation. |
| 1,000 idle native connections | ~8.9 KB/connection | **19.32 KB/connection** median RSS increase across three fresh processes (range 19.30–19.37 KB) | Larger here; OS/allocator/page-size and revision differ. Not the loaded-memory scenario. |

The 1 MiB RR samples varied substantially even with ten-second measurement windows: native 2,049–4,239 RPS and picows 2,952–4,545 RPS. The cause of that variability was not isolated. Different hardware, OS and scheduling are plausible contributors, not demonstrated explanations. A stable deployment-matched host is needed before publishing a general large-message ranking. Notably, the ratio of separate 1 MiB medians would suggest a small native win; the paired result does not. The report uses the paired statistic consistently.

The idle test records **16 KiB OS pages** and **64 KiB receive-buffer capacity**, but only ~19.32 KB incremental RSS per connection. This directly illustrates why capacity is not RSS. The old ~8.9 KB Linux figure was not reproduced here, but the result does not establish that the original Linux measurement was wrong. [Idle raw measurements](idle.json).

The measurements therefore support the concrete local findings, **not** the project’s blanket “fastest” headline across environments. We verified release-build provenance, isolated imports, payload semantics, same-build controls, the original benchmark functions, and real-socket reproductions. These checks increase confidence in the observed results; they cannot remove host variability or substitute for testing the actual deployment platform.

Additional reproduction commands:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv pip install 'picows==2.1.1'
.venv/bin/python tests/published_perf_audit.py --mode callback --rounds 15
.venv/bin/python tests/published_perf_audit.py --mode aa --rounds 7 --duration 1
.venv/bin/python tests/published_perf_audit.py --mode rr --rounds 7 --duration 10
.venv/bin/python tests/idle_perf_audit.py
# Intentionally exits nonzero: expose the actual assertions beneath xfail.
WS_AUDIT_EXTENSION=/tmp/ws-audit/main.so .venv/bin/pytest tests/probe_perf_findings.py --runxfail -q --tb=short
```

[Published-method RR raw/summary](published-rr.json), [same-build control](published-aa.json), [original callback benchmark](published-callback.json), and [actual main assertion failures](probes-main-actual-failures.txt) are retained. The initial smoke CPU result and interrupted preliminary network run are preserved for provenance and excluded from the scored summaries.
