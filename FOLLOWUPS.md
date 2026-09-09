# Follow-ups

Deferred items with a stub here so they stay auditable. Each entry flips to
DONE (with the closing PR) once merged.

- [ ] F8: `build_handshake` splices user headers and subprotocols into raw
  request bytes without token/CRLF validation — `subprotocols=["a\r\nX-Evil: y"]`
  injects handshake headers on the native client. Pre-existing on main; the
  sync path validates via `HeaderValue::from_str` since 0.7.8. Fix = shared
  token validation before splicing.
  Source: PR #45 correctness review (confidence 15, not a regression).

- [ ] F9: The rustls transport wins at 256 B (+6.02% [+5.77, +7.68], 14/15
  rounds) and loses at 8 KiB (−2.71% [−2.92, −2.05]) against
  `tls_backend="auto"`, which is what keeps it behind the `rustls-transport`
  feature. Lower per-message overhead, higher per-byte cost. Figures are
  post-PR #7, which removed the per-message memset in the plaintext read
  (previously +5.80% / −3.30%); a re-measurement at 9 rounds reproduced the
  signs with intervals too wide to confirm the point estimates, and the host is
  intermittently noisy, so treat either run as directional.
  **Attribution.** The remaining 8 KiB gap profiles as memmove: 535 samples
  against aiofastnet's 294, i.e. the two staging copies the *buffered* rustls
  API makes (`read_tls` into rustls's own buffer, then a copy back out).
  Plaintext still copies again into owned message payloads, and outbound
  ciphertext is still materialized for the asyncio transport.
  **Measured negative, do not re-run without a new hypothesis:** swapping the
  crypto provider ring → aws-lc-rs measured +6.35% / −3.51%, indistinguishable
  from ring, with comparable AES kernels in the profile. The provider is not
  the gap.
  **Unbuffered rustls is not yet worth it.** Stable rustls 0.23.40 does *not*
  decrypt in place: `ReadTraffic` holds `_incoming_tls` unused "for forwards
  compatibility" and `next_record()` pops an owned `Vec` off
  `received_plaintext`, with a source comment marking in-place decryption as
  future work. So the unbuffered rewrite would remove only the ciphertext
  staging copy — roughly 1 percentage point — and none of the plaintext
  copies, at a cost of ~400 lines of state machine. Revisit when rustls ships
  in-place decryption. This is the blocker for doing TLS and framing in one
  Rust package.

- [x] F10: **Measured, not a win. Do not re-run without a new hypothesis.**
  Routing `wss://` through `NativeClientBuffered` on the aiofastnet transport
  (which decrypts straight into the protocol's buffer, unlike asyncio's
  SSLProtocol) is neutral: request/response +0.53% / +0.20% / −0.81% at
  256 B / 8 KiB / 64 KiB, and pipelined at window 32, +0.53% [+0.12, +0.95] at
  8 KiB and −0.19% at 64 KiB. 15 paired rounds each; nothing approaches the +2%
  gate. The first attempt measured nothing at all, because aiofastnet gates on
  `isinstance(protocol, asyncio.BufferedProtocol)` and this pyclass is not a
  subclass, so the buffered path never engaged; the numbers above come from a
  build adding `is_buffered_protocol()`, which aiofastnet honours, and which
  genuinely takes the path. That method was not kept — it is API surface for a
  neutral result.

- [ ] F14: `NativeClientBuffered`'s zero-copy receive path is **uvloop-only**.
  uvloop duck-types the BufferedProtocol hooks; stdlib asyncio gates on
  `isinstance(protocol, asyncio.BufferedProtocol)` (`selector_events.py`), and
  this is a pyclass rather than a subclass, so under plain asyncio the transport
  falls back to `data_received` and the hooks are dead code. Verified with an
  instrumented duck-typed protocol: stdlib asyncio called only `data_received`;
  uvloop called `get_buffer`/`buffer_updated`. Nothing is broken, but the
  documented ~15% pipelined win is unavailable to anyone not on uvloop.
  Recovering it needs the class to actually satisfy `isinstance`:
  `#[pyclass(subclass)]` plus a Python-level subclass of
  `asyncio.BufferedProtocol` that `connect()` instantiates for `ws://`. Not
  attempted — the repo's benchmarks and this project's deployment target both
  use uvloop, so the win accrues to other users, and the change touches object
  construction on every connect.

- [ ] F11: `tls_backend="rustls"` has no SOCKS5 path. `_connect_helper` hands
  the pre-connected socket to the selected `create_connection`, which
  `tests/test_tls_backends.py::test_tls_through_a_socks5_proxy` covers for the
  asyncio and aiofastnet backends; the rustls shim is excluded there because it
  drives its own handshake over whatever transport it is given and that
  combination is untested.

- [ ] F12: Audit priority 1 and 2 findings still open: receive/write queues have
  no byte-based backpressure (1,024 x 64 KiB queues ~64 MiB per connection, and
  `resume_writing` drains the whole queue despite a renewed pause), small
  messages pin whole input chunks (2 KiB of payload retaining ~32 MiB), the
  receive scratch buffer is grow-only, and there is no compression level or
  per-message policy. The canceled-receive-waiter finding is fixed. See
  [REPORT.md](docs/performance-audit/REPORT.md).

- [ ] F13: A server Ping was observed going unanswered while the client had a
  send burst in flight, stalling both peers until their deadlines (4 of 6
  consecutive `tests/test_tls_backends.py -k round_trip` runs, then it stopped
  recurring on the same binary with no change). **Not reproduced since, and the
  two leading hypotheses are disproven.** Investigated rather than guessed at;
  what is ruled out:

  - *Pong withheld by the paused write queue* — disproven. Over a stub transport
    the Pong is written straight through even with 32 frames queued behind a
    paused transport, and the queued frames survive in order.
  - *Pong bypassed by the raw-fd send cache* — disproven by construction:
    `encode_control_into` clears `buf_known_empty` (`client.rs:530`), so every
    control write already invalidates the native-send fast path.
  - *Corrupted Pong payload*, which peers match pings by and which would stall
    them silently — disproven: 37,800 pongs across every length 0–125 echoed
    byte-for-byte.
  - *Load* — 1,700 iterations of the exact scenario under a concurrent
    benchmark plus 14 CPU burners produced zero failures, on both the default
    and `rustls-transport` builds, over `ws://` and `wss://`.

  `tests/test_control_frames.py` pins all of the above deterministically, so a
  regression in any of them now fails there instead of as a load-dependent
  flake. `tests/test_tls_backends.py::test_tls_answers_a_server_ping` is kept
  separate from the round-trip test so a recurrence cannot mask the rest of the
  TLS coverage. Remaining suspects are outside the client: ephemeral-port reuse
  on loopback under the benchmark's connection churn, or `websockets`
  server-side ping bookkeeping. Next step = capture a failing run with a packet
  trace rather than reasoning from the client side, since the client side is now
  substantially excluded.

- [ ] F15: **Hypothesis falsified. The regression, if it exists, is not the
  per-read freeze.** The suspicion was that a read below `zero_copy_min` pays
  `split_to().freeze()` and gets nothing back, because every payload on such a
  pass is copied anyway. That fix was implemented -- skip the freeze when
  `recv_buf.len() < zero_copy_min` (gated on the buffer length rather than this
  read's `nbytes`, so a small read completing a large carried-over partial frame
  keeps the fast path), parsing in place via `mem::take` and compacting as
  before -- and measured **neutral** against head at the default threshold, 15
  alternating paired rounds on an idle host: 300 B -0.90% [-2.04, +1.19] 6/15;
  800 B +1.40% [-1.45, +3.03] 9/15; 2 KiB -1.37% [-10.13, +0.59] 6/15. Win
  counts at coin-flip. The code was reverted rather than shipped: it adds a
  second parse path for no measured gain.

  Note the two runs that produced the original -3% signal were both taken on a
  contended host, and a profile diff at 300 B showed no freeze-shaped cost (the
  candidate did *less* zeroing, `__bzero` -111). The sign test across those two
  runs (3/11 twice, combined p~0.013) was computed over contaminated inputs:
  the statistic was sound, the data was not. Treat the regression itself as
  unconfirmed, not merely unexplained.

  What is confirmed, across two independent runs, is that the threshold is worth
  far more than whatever it guards against. Slicing wins at every size measured:

  - loaded host, 11 paired rounds, `zero_copy_min_bytes=0` vs the pre-change
    baseline: 300 B +4.71% [+0.34, +8.29] 9/11; 800 B +17.30% [+7.49, +24.18]
    10/11; 5 KiB +21.29% [+14.08, +38.74] 11/11.
  - idle host, 15 paired rounds, same binary at 0 vs the 4096 default: 300 B
    +9.58% [+7.01, +13.15] 14/15; 800 B +15.43% [+11.01, +21.76] 14/15;
    2 KiB +11.90% [+6.56, +18.88] 12/15.

  A consumer that copies or drops payloads promptly should set
  `zero_copy_min_bytes=0` and stop thinking about this entry. Anyone reopening
  it should first reproduce the regression on an idle host with 15+ rounds
  before hunting a cause; the freeze is ruled out.

  Note this affects `ws://` only. `wss://` returns a bare `NativeClient` whose
  `data_received` path already slices from the incoming `PyBytes`, so neither
  the threshold nor this entry applies to TLS connections.
