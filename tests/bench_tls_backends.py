#!/usr/bin/env python3
"""Paired interleaved benchmark of the wss:// TLS backends in one build.

bench_ab.py compares two builds of the extension; this compares two values of
``tls_backend=`` inside a single build, which is the actual question behind
making ``"auto"`` prefer aiofastnet. Same methodology as bench_ab: alternating
paired rounds against one server generation, median of per-round ratios with a
percentile bootstrap interval, fresh interpreter per cell. Its statistics and
server orchestration are imported rather than reimplemented.

    make tls-certs
    cargo build --release --bin ws_echo_server_tls
    python tests/bench_tls_backends.py                      # asyncio vs auto
    python tests/bench_tls_backends.py --candidate rustls --rounds 15

An interval straddling zero means the run did not resolve the effect, not that
the effect is zero. Absolute rates are not comparable across sessions.
"""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import sys
import time
from pathlib import Path

import bench_ab
from bench_ab import GATE, MIN_USEFUL_ROUNDS, REPO, _start_server, summarise

CERT = REPO / "tests/certs/cert.pem"
DEFAULT_SIZES = (256, 8 * 1024)
BACKENDS = ("auto", "asyncio", "aiofastnet", "rustls")


def _run_cell(size: int, duration: float, backend: str, port: int) -> dict:
    """One request-response cell, on one backend, in this interpreter.

    Verification stays on: the audit's numbers were taken with a verified peer,
    and turning it off would measure a configuration nobody deploys. Handshake
    cost sits outside the timed region either way.
    """
    import asyncio

    import uvloop

    uvloop.install()

    from websocket_rs.native_client import connect

    if backend == "rustls":
        kwargs = {"tls_backend": "rustls", "rustls_ca_file": str(CERT)}
    else:
        kwargs = {"tls_backend": backend, "ssl_context": ssl.create_default_context(cafile=str(CERT))}

    async def drive(ws, payload, seconds):
        count = 0
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            ws.send(payload)
            await ws.recv()
            count += 1
        return count

    async def main():
        payload = b"a" * size
        ws = await connect(f"wss://127.0.0.1:{port}", **kwargs)
        try:
            for _ in range(bench_ab.WARMUP):
                ws.send(payload)
                await ws.recv()
            await drive(ws, payload, bench_ab.PREWARM_SECONDS)  # discarded
            start = time.perf_counter()
            count = await drive(ws, payload, duration)
            return count, time.perf_counter() - start
        finally:
            ws.close()

    count, elapsed = asyncio.run(main())
    return {"size": size, "count": count, "elapsed": elapsed, "rps": count / elapsed}


def _measure(size: int, duration: float, backend: str, port: int) -> float:
    """Spawn a fresh interpreter for one cell.

    Per-process state — the memoised aiofastnet lookup, allocator heaps,
    bytecode specialization — must not carry between arms.
    """
    code = (
        "import json,sys;"
        f"sys.path.insert(0,{str(REPO / 'tests')!r});"
        "import bench_tls_backends as b;"
        f"print(json.dumps(b._run_cell({size},{duration},{backend!r},{port})))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"cell failed ({backend}):\n{out.stderr[-2000:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])["rps"]


def run(args) -> dict:
    results = {s: {"baseline": [], "candidate": []} for s in args.sizes}
    for round_index in range(args.rounds):
        for size in args.sizes:
            port = args.port + (round_index * len(args.sizes) + args.sizes.index(size)) % 40
            server = _start_server("tls", port)
            try:
                # Alternate ordering so host drift cannot systematically favour
                # either backend.
                arms = [("baseline", args.baseline), ("candidate", args.candidate)]
                if round_index % 2:
                    arms.reverse()
                for key, backend in arms:
                    results[size][key].append(_measure(size, args.duration, backend, port))
            finally:
                server.terminate()
                server.wait()
        print(f"round {round_index + 1}/{args.rounds}", file=sys.stderr, flush=True)
    return {size: summarise(v["baseline"], v["candidate"]) for size, v in results.items()}


def report(summary: dict, baseline: str, candidate: str) -> None:
    print(f"\ntransport=tls  client=native  request-response  {baseline} -> {candidate}\n")
    header = f"{'size':>9}  {baseline:>10}  {candidate:>10}  {'median':>8}  {'95% CI':>18}  {'wins':>6}  gate"
    print(header)
    print("-" * len(header))
    for size, s in sorted(summary.items()):
        label = f"{size // 1024} KiB" if size >= 1024 else f"{size} B"
        ci = f"[{s['ci_low_pct']:+.2f}, {s['ci_high_pct']:+.2f}]"
        print(
            f"{label:>9}  {s['baseline_median_rps']:>10.0f}  {s['candidate_median_rps']:>10.0f}  "
            f"{s['paired_median_pct']:>+7.2f}%  {ci:>18}  {s['wins']:>3}/{s['rounds']:<2}  "
            f"{'PASS' if s['meets_gate'] else '-'}"
        )
    print(f"\nGate is +{GATE * 100:.0f}% on the median. Quote the interval and round count with it.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-b", "--baseline", choices=BACKENDS, default="asyncio")
    p.add_argument("-c", "--candidate", choices=BACKENDS, default="auto")
    p.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    p.add_argument("--rounds", type=int, default=15)
    p.add_argument("--duration", type=float, default=3.0)
    p.add_argument("--port", type=int, default=8930)
    p.add_argument("--json", type=Path)
    args = p.parse_args(argv)
    args.sizes = [int(s) for s in args.sizes.split(",")]
    if args.baseline == args.candidate:
        p.error("baseline and candidate are the same backend")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if not CERT.exists():
        raise SystemExit("tests/certs/cert.pem missing. Run: make tls-certs")
    if args.rounds < MIN_USEFUL_ROUNDS:
        print(
            f"warning: {args.rounds} rounds resolves nothing near the +{GATE * 100:.0f}% gate. "
            f"Use at least {MIN_USEFUL_ROUNDS}, and 15+ to report.",
            file=sys.stderr,
        )
    summary = run(args)
    report(summary, args.baseline, args.candidate)
    if args.json:
        args.json.write_text(json.dumps({str(k): v for k, v in summary.items()}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
