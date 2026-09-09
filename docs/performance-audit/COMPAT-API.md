# Picows compatibility API quick comparison

websocket-rs native main a006d393bcf0601b553459dcd97e6592fb674187 (0.7.11), picows 2.1.1 for both APIs, CPython 3.13.14, uvloop, macOS ARM64. Seven interleaved rounds, fresh client processes, neutral repository Rust echo servers. Each cell has 0.5 seconds warmup and 2 seconds measurement. Plain TCP and verified TLS; compression disabled; one outstanding binary request. All clients materialize owned bytes and verify every response after stripping the server's 24-byte benchmark header. Connections/handshakes and cleanup are outside measurement. GC disabled during measurement. No CPU affinity.

| Transport / bytes | native RPS | core RPS | compatibility RPS | Paired compatibility vs native (95% bootstrap CI) |
|---|---:|---:|---:|---|
| plain/256 | 48,894 | 47,043 | 45,052 | -7.36% [-10.64, -6.06] |
| plain/8192 | 41,243 | 40,517 | 40,720 | -1.45% [-1.76, -0.85] |
| tls/256 | 36,044 | 39,036 | 39,813 | +10.96% [+10.38, +14.68] |
| tls/8192 | 27,564 | 30,482 | 29,139 | +9.71% [+1.53, +10.76] |

RPS columns are independent arm medians; percentage changes are medians of paired round ratios, so they need not equal ratios of displayed medians. Bootstrap intervals capture this short run's variation, not all host or deployment uncertainty.

Compatibility vs core paired changes: plain 256 B -3.72% [-6.18, +0.88]; plain 8 KiB -0.22% [-0.62, +0.55]; TLS 256 B +2.33% [+0.78, +3.98]; TLS 8 KiB -0.93% [-9.54, +0.06]. This does not show a consistent compatibility-layer penalty across cells. No causal explanation for the small TLS advantage over core was isolated.

Client CPU median microseconds/request: TLS 256 B native 14.08, compatibility 12.32; TLS 8 KiB native 18.63, compatibility 16.88. CPU excludes the server. This is not a measurement of memory, tail latency, production feed processing, or saturated throughput. Binary RR traffic omits JSON parsing, application queues, text decoding, and burst behavior. Picows 2.1.3 was tested for functionality separately; these performance numbers are for 2.1.1.

Reproduce: `.venv/bin/python tests/compat_perf_audit.py`. Requires the audit baseline `/tmp/ws-audit/main.so`, existing release echo server binaries and tests/certs certificate/key. The script stages the extension without changing production files. Raw results: `compat-api.json`. Ruff check and format check passed.
