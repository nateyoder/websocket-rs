# Migrating to `websocket_rs.connect`

The canonical high-performance entry point is now `websocket_rs.connect`,
backed by the new asyncio.Protocol-native client: runs on the asyncio loop
thread, AVX2-vectorised masking, zero-copy recv via a buffer-protocol
`WSMessage`. Both throughput and tail latency improve substantially over
the legacy tokio-backed path.

The old `websocket_rs.async_client.connect` still works but is deprecated
and will be removed in 2.0.

## Side-by-side

```python
# Old (websockets-compatible, deprecated)
import websocket_rs.async_client
async def handler():
    ws = await websocket_rs.async_client.connect("wss://api.example.com")
    await ws.send(b"hello")
    msg = await ws.recv()        # returns bytes
    if isinstance(msg, bytes):
        ...

# New (canonical, fastest)
from websocket_rs import connect
async def handler():
    async with await connect("wss://api.example.com") as ws:
        ws.send(b"hello")        # sync — no await
        msg = await ws.recv()    # returns WSMessage (zero-copy)
        # Use memoryview/struct.unpack_from/bytes() as needed:
        mv = memoryview(msg)
        if len(msg) >= 4:
            ...
```

## Breaking changes

| Old API                            | New API                              | Why |
|------------------------------------|--------------------------------------|-----|
| `await ws.send(msg)`               | `ws.send(msg)` (sync)                | `transport.write` is already non-blocking; the `await` was pure overhead. |
| `msg = await ws.recv()` → `bytes`  | `msg = await ws.recv()` → `WSMessage`| `WSMessage` wraps a shared `Bytes` slice — zero memcpy on the hot path. Supports the buffer protocol. |
| `isinstance(msg, bytes)` → `True`  | Use `isinstance(msg, (bytes, WSMessage))` or just `len(msg)` | `WSMessage` does not subclass `bytes` — any `bytes` subclass would require a forced copy. |
| `msg[i:j]` → `bytes`               | `msg[i:j]` → `bytes`                 | Slicing still returns a real `bytes` (a short copy).  `memoryview(msg)[i:j]` stays zero-copy. |

## What works unchanged

- `memoryview(msg)` — zero-copy view
- `struct.unpack_from("=I", msg, 0)` — accepts any buffer protocol
- `msg[:n]` slicing returns `bytes` as before
- `len(msg)`
- `msg == some_bytes`
- `bytes(msg)` forces a real copy (explicit opt-in)
- `async for msg in ws:` — works on both clients
- `async with ... as ws:` — works on both clients
- `ws.ping()`, `ws.close()`, `ws.subprotocol`, `ws.close_code`, `ws.close_reason`

## When to stay on the old client (for now)

- You rely on `await ws.send(...)` semantics in code you can't change.
- You're testing a regression and want to compare.

Everyone else should migrate.

### Native connection diagnostics and teardown

Native clients validate HTTP/1.1 status 101, the Upgrade and Connection headers,
and an exact, single Sec-WebSocket-Accept header. Rejected HTTP upgrades raise
`ConnectionError` with a `status_code` attribute (an integer when the status line
can be parsed, otherwise `None`). Failed, timed-out, and cancelled connection
attempts close their transport.

`close_received_code` and `close_received_reason` describe an actual received
Close frame. `close_sent_code` and `close_sent_reason` describe a Close frame
successfully submitted to the transport; a local protocol error currently sends
the code with an empty reason. `None` means no code/reason is known, including
bare EOF and the empty local Close frame. These fields do not synthesize 1006 or
claim a Close was received when only a local failure occurred. Existing
`close_code` / `close_reason` retain their compatibility behavior.

Final data callbacks run before peer-close teardown, including data and Close
frames arriving in one read. All terminal paths release cached transport, loop,
and callback references. Native clients remain thread-affine: close and dispose
them on their owning event-loop thread, and do not retain them in exception
tracebacks that will be collected on another thread.
