# Makefile for WebSocket-RS development

.PHONY: help install dev test test-rustls bench bench-tls clean build build-rustls release tls-certs

# Default target
help:
	@echo "WebSocket-RS Development Commands"
	@echo "================================="
	@echo "make install   - Install dependencies using uv"
	@echo "make dev       - Build in development mode"
	@echo "make test      - Run all tests"
	@echo "make bench     - Run benchmarks"
	@echo "make bench-tls - Paired benchmark of the wss:// TLS backends"
	@echo "make clean     - Clean build artifacts"
	@echo "make build     - Build release version"
	@echo "make build-rustls - Build release with the experimental rustls TLS backend"
	@echo "make test-rustls  - Run the TLS backend tests against that build"
	@echo "make release   - Build wheels for distribution"
	@echo "make tls-certs - Generate self-signed cert for TLS benchmarks (localhost)"

# Install dependencies
install:
	@echo "📦 Installing dependencies with uv..."
	@command -v uv >/dev/null 2>&1 || (echo "❌ uv not found. Install from https://github.com/astral-sh/uv" && exit 1)
	uv venv
	. .venv/bin/activate && uv pip install -e ".[dev]"
	. .venv/bin/activate && uv pip install maturin

# Development build
dev:
	@echo "🔨 Building in development mode..."
	. .venv/bin/activate && maturin develop

# Release build
build:
	@echo "🚀 Building in release mode..."
	. .venv/bin/activate && maturin develop --release

# Run tests (matches CI and AGENTS.md: pytest over the whole suite)
test: build
	@echo "🧪 Running tests..."
	. .venv/bin/activate && pytest tests/

# Release build including the experimental same-thread rustls TLS transport
# (tls_backend="rustls"). Off in normal builds; see
# docs/performance-audit/RUSTLS-PROTOTYPE.md for why it is not the default.
build-rustls:
	@echo "🚀 Building in release mode with rustls-transport..."
	. .venv/bin/activate && maturin develop --release --features rustls-transport

# The rustls cells of the TLS backend suite skip unless the extension was built
# with the feature, so build it here rather than reusing whatever is installed.
test-rustls: build-rustls
	@echo "🧪 Running TLS backend tests against the rustls build..."
	. .venv/bin/activate && pytest tests/test_tls_backends.py

# Run the paired A/B benchmark harness
bench: build
	@echo "📊 Benchmark harness usage (run a scenario to actually benchmark):"
	. .venv/bin/activate && python tests/bench_ab.py --help

# Paired asyncio-vs-auto comparison of the wss:// path in the installed build.
# This is the measurement behind the aiofastnet default; see docs/TLS-BACKENDS.md.
bench-tls: build tls-certs
	@cargo build --release --bin ws_echo_server_tls
	. .venv/bin/activate && python tests/bench_tls_backends.py --rounds 15

# Build the echo servers the A/B harness drives
bench-servers:
	@echo "🔧 Building echo servers..."
	cargo build --release --features echo-server-bin --bin ws_echo_server
	cargo build --release --bin ws_echo_server_tls
	@echo "✅ Run: python tests/bench_ab.py --help"

# Clean build artifacts
clean:
	@echo "🧹 Cleaning build artifacts..."
	rm -rf target/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache/
	rm -rf __pycache__/
	rm -rf **/__pycache__/
	find ./websocket_rs -name "*.so" -delete 2>/dev/null || true
	find ./websocket_rs -name "*.pyd" -delete 2>/dev/null || true

# Build distribution wheels
release:
	@echo "📦 Building distribution wheels..."
	. .venv/bin/activate && maturin build --release

# Generate self-signed cert + key for TLS benchmarks (localhost only, 10y)
tls-certs:
	@mkdir -p tests/certs
	@openssl req -x509 -newkey rsa:2048 -days 3650 -nodes \
		-keyout tests/certs/key.pem -out tests/certs/cert.pem \
		-subj "/CN=127.0.0.1" \
		-addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
		-addext "basicConstraints=critical,CA:FALSE" \
		-addext "extendedKeyUsage=serverAuth" 2>/dev/null
	@chmod 600 tests/certs/key.pem
	@echo "✅ Wrote tests/certs/{cert,key}.pem (test-only, gitignored, end-entity cert)"

# Quick test (no server needed)
quick-test: build
	@echo "🧪 Running quick tests (no server required)..."
	. .venv/bin/activate && python tests/test_compatibility.py