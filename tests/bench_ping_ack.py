#!/usr/bin/env python3
"""Isolated protocol CPU/allocation benchmark; no kernel/network latency claims.

Load one release extension by path, use a deterministic transport, and emit JSON.
Run builds in alternating subprocess order to avoid module cache contamination.
Python tracemalloc excludes Rust allocations; RSS is peak process memory only.
"""
import argparse
import asyncio
import base64
import gc
import hashlib
import importlib.util
import json
import resource
import statistics
import time
import tracemalloc


async def benchmark(native):
    loop = asyncio.get_running_loop()
    original = loop.create_connection

    class Transport:
        def __init__(self, protocol):
            self.protocol = protocol

        def get_extra_info(self, _name):
            return None

        def get_write_buffer_size(self):
            return 0

        def write(self, data):
            if data.startswith(b"GET "):
                key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                self.protocol.data_received(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")

        def close(self):
            pass

    async def create(factory, *_args, **_kwargs):
        protocol = factory()
        transport = Transport(protocol)
        protocol.connection_made(transport)
        return transport, protocol

    loop.create_connection = create
    try:
        ws = await native.connect("ws://localhost:1")
    finally:
        loop.create_connection = original
    results = {}
    # Establish steady allocator/bytecode state; ordinary paths exist in baseline.
    data = b"\x82\x10" + b"x" * 16
    for _ in range(2000):
        ws.data_received(data)
        await ws.recv()
        ws.send(b"x" * 16)
    count = 30000
    start = time.process_time_ns()
    for _ in range(count):
        ws.data_received(data)
        await ws.recv()
        ws.send(b"x" * 16)
    results["application_cpu_ns_per_exchange"] = (time.process_time_ns() - start) / count
    if hasattr(ws, "ping_waiter"):
        pong = b"\x8a\x08abcdefgh"
        for _ in range(1000):
            wait = ws.ping_waiter(b"abcdefgh")
            ws.data_received(pong)
            await wait
        start = time.process_time_ns()
        for _ in range(count):
            wait = ws.ping_waiter(b"abcdefgh")
            ws.data_received(pong)
            await wait
        await asyncio.sleep(0)
        results["probe_cpu_ns_per_ack"] = (time.process_time_ns() - start) / count
        # Isolate registration CPU scaling with distinct outstanding payloads.
        for size in (32, 256, 1024):
            keys = [i.to_bytes(8, "big") for i in range(size)]
            batches = []
            for _ in range(9):
                start = time.process_time_ns()
                waits = [ws.ping_waiter(key) for key in keys]
                batches.append((time.process_time_ns() - start) / size)
                for wait in waits:
                    wait.cancel()
                del waits, wait
                await asyncio.sleep(0)
            results[f"registration_ns_per_probe_{size}"] = statistics.median(batches[2:])
        # Outstanding Futures, then cancellation with no subsequent wire traffic.
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        waits = [ws.ping_waiter(i.to_bytes(8, "big")) for i in range(4096)]
        results["python_pending_bytes_per_probe"] = (tracemalloc.get_traced_memory()[0] - before) / len(waits)
        for wait in waits:
            wait.cancel()
        del waits, wait
        await asyncio.sleep(0)
        gc.collect()
        results["python_retained_bytes_after_cancel_4096"] = tracemalloc.get_traced_memory()[0] - before
        tracemalloc.stop()
    ws.close()
    await asyncio.sleep(0)
    results["peak_rss_platform_units"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return results


async def network_benchmark(native, uri):
    import ssl

    options = {}
    if uri.startswith("wss:"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # controlled loopback benchmark only
        options["ssl_context"] = context
    ws = await native.connect(uri, **options)
    samples = []
    for i in range(500):
        await ws.ping_waiter(i.to_bytes(8, "big"))
    cpu_start = time.process_time_ns()
    for i in range(5000):
        start = time.perf_counter_ns()
        await ws.ping_waiter(i.to_bytes(8, "big"))
        samples.append(time.perf_counter_ns() - start)
    cpu = (time.process_time_ns() - cpu_start) / len(samples)
    ws.close()
    samples.sort()
    return {"p50_ns": statistics.median(samples), "p95_ns": samples[int(len(samples) * .95)],
            "p99_ns": samples[int(len(samples) * .99)], "client_cpu_ns_per_ack": cpu}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extension")
    parser.add_argument("--uri", help="measure RTT against a controlled echo peer instead of isolated CPU")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("websocket_rs", args.extension)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    operation = network_benchmark(module.native_client, args.uri) if args.uri else benchmark(module.native_client)
    print(json.dumps(asyncio.run(operation)))
