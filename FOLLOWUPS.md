# Follow-ups

Deferred items with a stub here so they stay auditable. Each entry flips to
DONE (with the closing PR) once merged.

- [ ] F8: `build_handshake` splices user headers and subprotocols into raw
  request bytes without token/CRLF validation — `subprotocols=["a\r\nX-Evil: y"]`
  injects handshake headers on the native client. Pre-existing on main; the
  sync path validates via `HeaderValue::from_str` since 0.7.8. Fix = shared
  token validation before splicing.
  Source: PR #45 correctness review (confidence 15, not a regression).

- [ ] F9: The rustls transport wins at 256 B (+5.80% [+5.47, +7.61], 14/15
  rounds) and loses at 8 KiB (−3.30% [−3.99, −2.90], 1/15) against
  `tls_backend="auto"`, which is what keeps it behind the `rustls-transport`
  feature. Lower per-message overhead, higher per-byte cost. The
  path still copies through rustls's buffered reader, a reusable plaintext `Vec`
  and owned message payloads, and materializes outbound ciphertext for the
  asyncio transport. Next experiments = rustls's unbuffered interface and
  eliminating those copies. Nothing yet establishes that copies *caused* the
  regression; a buffer-lifetime redesign needs its own tests. This is the
  blocker for doing TLS and framing in one Rust package.

- [ ] F10: `wss://` returns a bare `NativeClient`, not `NativeClientBuffered`,
  because asyncio's SSLProtocol delivers ≤16 KiB chunks and BufferedProtocol was
  net-negative there. aiofastnet exports `aiofn_is_buffered_protocol`, so its
  TLS transport may support the buffered path with different chunking. Untested
  — measure before assuming the old conclusion carries over.

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
