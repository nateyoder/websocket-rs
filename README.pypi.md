# WebSocket-RS 🚀

> **This is a fork** of [coseto6125/websocket-rs](https://github.com/coseto6125/websocket-rs),
> distributed as `websocket-rs-nateyoder` via [GitHub releases](https://github.com/nateyoder/websocket-rs/releases)
> -- not PyPI -- and importing as `websocket_rs`. It is a drop-in replacement
> carrying changes not yet released upstream. Depend on
> exactly one of the two: installing both puts two distributions in the same
> import name and you get whichever landed last.

High-performance WebSocket client implementation in Rust with Python bindings. Provides both sync and async APIs with significant performance improvements over pure Python implementations.

## 🎯 Performance Highlights

- **Sync Client**: 1.85x faster than websocket-client, 6.2x faster than websockets
- **Async Client (Pipelined)**: 12x faster than picows, 21x faster than websockets
- Pure Rust implementation with zero-copy optimizations
- Thread-safe with concurrent operations support

## 📦 Installation

```bash
uv pip install "websocket-rs-nateyoder==0.7.10.post1" --find-links https://github.com/nateyoder/websocket-rs/releases/expanded_assets/v0.7.10.post1
```

See the [repository README](https://github.com/nateyoder/websocket-rs#installation) for pinning it as a `uv` project dependency.

## 🚀 Quick Start

### Synchronous Client

```python
from websocket_rs.sync.client import connect

# Simple usage
with connect("ws://localhost:8765") as ws:
    ws.send("Hello, WebSocket!")
    response = ws.recv()
    print(response)
```

### Asynchronous Client

```python
import asyncio
from websocket_rs.async_client import connect

async def main():
    async with connect("ws://localhost:8765") as ws:
        await ws.send("Hello, Async!")
        response = await ws.recv()
        print(response)

asyncio.run(main())
```

## 📚 Full Documentation

Visit our GitHub repository for comprehensive documentation:

- **📖 Full README**: https://github.com/coseto6125/websocket-rs#readme
- **📊 Performance Benchmarks**: https://github.com/coseto6125/websocket-rs/blob/main/docs/BENCHMARKS.md
- **🔧 API Reference**: https://github.com/coseto6125/websocket-rs/blob/main/docs/API.md
- **🤝 Contributing Guide**: https://github.com/coseto6125/websocket-rs/blob/main/docs/CONTRIBUTING.md

## 🌟 Key Features

- **🚄 High Performance**: Rust-powered implementation
- **🔄 Dual APIs**: Both sync and async support
- **✅ Drop-in Replacement**: Compatible with Python websockets library
- **🔒 Thread-Safe**: Safe concurrent operations
- **⚡ Zero-Copy**: Optimized memory usage
- **🐍 Python 3.12+**: Modern Python support

## 📝 Requirements

- Python 3.12 or higher
- Supported platforms: Linux, Windows, macOS (x86_64, ARM64)

## 🔗 Links

- **Repository**: https://github.com/coseto6125/websocket-rs
- **Issues**: https://github.com/coseto6125/websocket-rs/issues
- **Changelog**: https://github.com/coseto6125/websocket-rs/releases

## 📄 License

MIT License - see [LICENSE](https://github.com/coseto6125/websocket-rs/blob/main/LICENSE) for details.

---

**Made with ❤️ using Rust and PyO3**
