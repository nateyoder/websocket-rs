#!/usr/bin/env python3
"""Paired interleaved A/B benchmark for two builds of the extension.

Single-run numbers on a busy host are worthless: the same change measured
+8.02%, +4.62%, +2.40%, +2.20% and +2.69% across five sessions here. Believing
the first would have put a wrong figure in the CHANGELOG, which the repo's own
rule says must carry a median and a round count.

So every round runs both builds back to back on the same server process
generation, alternating which one goes first, and the statistic is the median of
the per-round ratios with a bootstrap confidence interval. Paired differencing
cancels the drift that makes unpaired medians swing; the interval reports what
is left.

    # build the two extensions you want to compare
    git stash && cargo build --release && cp target/release/libwebsocket_rs.so /tmp/base.so
    git stash pop && cargo build --release && cp target/release/libwebsocket_rs.so /tmp/cand.so

    python tests/bench_ab.py --baseline /tmp/base.so --candidate /tmp/cand.so
    python tests/bench_ab.py -b /tmp/base.so -c /tmp/cand.so --transport tls --sizes 1048576
    python tests/bench_ab.py -b /tmp/base.so -c /tmp/cand.so --rounds 21 --json out.json

Reading the result: the repo gates performance claims at +2% (see AGENTS.md).
Compare the median against that gate, and quote the interval and the round count
alongside it. An interval straddling zero means the run did not resolve the
effect, not that the effect is zero.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SIZES = (256, 8 * 1024, 100 * 1024, 1024 * 1024)
WARMUP = 50
# Discarded timing pre-pass per cell: warms bytecode specialization, allocator
# heaps, TCP window auto-tune, and any first-large-frame slow path in the server.
PREWARM_SECONDS = 1.0
GATE = 0.02

SERVERS = {"plain": "ws_echo_server", "tls": "ws_echo_server_tls"}


# ---------- worker: one cell, one build ----------


def _run_cell(size: int, duration: float, transport: str, port: int) -> dict:
    """Run one request-response cell against an already-running server."""
    import asyncio

    import uvloop

    uvloop.install()

    if transport == "tls":
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        uri, kwargs = f"wss://127.0.0.1:{port}", {"ssl_context": ctx}
    else:
        uri, kwargs = f"ws://127.0.0.1:{port}", {}

    from websocket_rs.native_client import connect

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
        ws = await connect(uri, **kwargs)
        try:
            for _ in range(WARMUP):
                ws.send(payload)
                await ws.recv()
            await drive(ws, payload, PREWARM_SECONDS)  # discarded
            start = time.perf_counter()
            count = await drive(ws, payload, duration)
            return count, time.perf_counter() - start
        finally:
            ws.close()

    count, elapsed = asyncio.run(main())
    return {"size": size, "count": count, "elapsed": elapsed, "rps": count / elapsed}


# ---------- parent: process orchestration ----------


def _stage_build(so: Path, into: Path) -> Path:
    """Copy the python package plus one built .so into a scratch dir.

    The extension is imported by name, so two builds cannot coexist in one
    process. Each cell therefore runs in its own interpreter with its own
    staged package on sys.path, which also keeps the working tree's own .so
    untouched while a comparison runs.
    """
    pkg = into / "websocket_rs"
    shutil.copytree(REPO / "websocket_rs", pkg, ignore=shutil.ignore_patterns("*.so", "__pycache__"))
    shutil.copyfile(so, pkg / "websocket_rs.abi3.so")
    return into


def _wait_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} never opened")


def _start_server(transport: str, port: int) -> subprocess.Popen:
    binary = REPO / "target" / "release" / SERVERS[transport]
    if not binary.exists():
        raise SystemExit(
            f"{binary} not built. Run:\n  cargo build --release --features echo-server-bin --bin {SERVERS[transport]}"
        )
    cmd = [str(binary), str(port)]
    if shutil.which("taskset"):
        cmd = ["taskset", "-c", "0", *cmd]  # server on core 0, client on core 1
    proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(port)
    except RuntimeError:
        proc.terminate()
        raise
    return proc


def _measure(staged: Path, size: int, duration: float, transport: str, port: int) -> float:
    """Spawn one interpreter that imports the staged build and runs one cell."""
    code = (
        "import json,os,sys;"
        f"sys.path.insert(0,{str(staged)!r});"
        f"sys.path.insert(0,{str(REPO / 'tests')!r});"
        "os.sched_setaffinity(0,{1}) if hasattr(os,'sched_setaffinity') else None;"
        "import bench_ab;"
        f"print(json.dumps(bench_ab._run_cell({size},{duration},{transport!r},{port})))"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"cell failed:\n{out.stderr[-2000:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])["rps"]


# ---------- statistics ----------


def bootstrap_ci(ratios: list[float], resamples: int = 20000, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap around the median of the paired ratios."""
    medians = sorted(statistics.median(random.choices(ratios, k=len(ratios))) for _ in range(resamples))
    return medians[int(resamples * alpha / 2)], medians[int(resamples * (1 - alpha / 2))]


def summarise(baseline: list[float], candidate: list[float]) -> dict:
    # strict: unequal arms means the pairing broke, which must not be averaged over.
    ratios = [c / b for b, c in zip(baseline, candidate, strict=True)]
    low, high = bootstrap_ci(ratios)
    return {
        "rounds": len(ratios),
        "baseline_median_rps": statistics.median(baseline),
        "candidate_median_rps": statistics.median(candidate),
        "paired_median_pct": (statistics.median(ratios) - 1) * 100,
        "ci_low_pct": (low - 1) * 100,
        "ci_high_pct": (high - 1) * 100,
        "wins": sum(r > 1 for r in ratios),
        "meets_gate": statistics.median(ratios) - 1 >= GATE,
    }


# ---------- driver ----------


def run(args) -> dict:
    results: dict[int, dict[str, list[float]]] = {s: {"baseline": [], "candidate": []} for s in args.sizes}
    with tempfile.TemporaryDirectory(prefix="bench_ab_") as tmp:
        staged = {
            "baseline": _stage_build(args.baseline, Path(tmp) / "baseline"),
            "candidate": _stage_build(args.candidate, Path(tmp) / "candidate"),
        }
        for round_index in range(args.rounds):
            for size in args.sizes:
                port = args.port + (round_index * len(args.sizes) + args.sizes.index(size)) % 40
                server = _start_server(args.transport, port)
                try:
                    # Alternate which build goes first so a drifting host cannot
                    # systematically favour either arm.
                    arms = ["baseline", "candidate"]
                    if round_index % 2:
                        arms.reverse()
                    for arm in arms:
                        results[size][arm].append(_measure(staged[arm], size, args.duration, args.transport, port))
                finally:
                    server.terminate()
                    server.wait()
            print(f"round {round_index + 1}/{args.rounds}", file=sys.stderr, flush=True)
    return {size: summarise(v["baseline"], v["candidate"]) for size, v in results.items()}


def report(summary: dict, transport: str) -> None:
    print(f"\ntransport={transport}  client=native  request-response\n")
    header = f"{'size':>9}  {'baseline':>10}  {'candidate':>10}  {'median':>8}  {'95% CI':>18}  {'wins':>6}  gate"
    print(header)
    print("-" * len(header))
    for size, s in sorted(summary.items()):
        label = f"{size // 1024} KiB" if size >= 1024 else f"{size} B"
        ci = f"[{s['ci_low_pct']:+.2f}, {s['ci_high_pct']:+.2f}]"
        gate = "PASS" if s["meets_gate"] else "-"
        print(
            f"{label:>9}  {s['baseline_median_rps']:>10.0f}  {s['candidate_median_rps']:>10.0f}  "
            f"{s['paired_median_pct']:>+7.2f}%  {ci:>18}  {s['wins']:>3}/{s['rounds']:<2}  {gate}"
        )
    print(f"\nGate is +{GATE * 100:.0f}% on the median. Quote the interval and round count with it.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-b", "--baseline", type=Path, required=True, help="path to the reference .so")
    p.add_argument("-c", "--candidate", type=Path, required=True, help="path to the .so under test")
    p.add_argument(
        "--sizes", default=",".join(str(s) for s in DEFAULT_SIZES), help="comma-separated payload sizes in bytes"
    )
    p.add_argument("--rounds", type=int, default=15, help="paired rounds per size (default 15)")
    p.add_argument("--duration", type=float, default=3.0, help="measured seconds per cell")
    p.add_argument("--transport", choices=sorted(SERVERS), default="plain")
    p.add_argument("--port", type=int, default=8890)
    p.add_argument("--json", type=Path, help="also write the summary as JSON")
    args = p.parse_args(argv)
    args.sizes = [int(s) for s in args.sizes.split(",")]
    for path in (args.baseline, args.candidate):
        if not path.exists():
            p.error(f"{path} does not exist")
    return args


# Below this, the bootstrap interval is wider than the effects worth merging, so
# the run cannot answer the question it was started to answer.
MIN_USEFUL_ROUNDS = 7


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.transport == "tls" and not (REPO / "tests/certs/cert.pem").exists():
        raise SystemExit("tests/certs/cert.pem missing. Run: make tls-certs")
    if args.rounds < MIN_USEFUL_ROUNDS:
        print(
            f"warning: {args.rounds} rounds resolves nothing near the +{GATE * 100:.0f}% gate — "
            f"a same-build run has landed at -5.8% with 3 rounds. Use at least "
            f"{MIN_USEFUL_ROUNDS}, and 15+ to report.",
            file=sys.stderr,
        )
    summary = run(args)
    report(summary, args.transport)
    if args.json:
        args.json.write_text(json.dumps({str(k): v for k, v in summary.items()}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
