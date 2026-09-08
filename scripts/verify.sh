#!/bin/bash
# Local evidence: use the repository's test setup before Python-linked Rust tests.
set -euo pipefail
bash scripts/test.sh "$@"
cargo fmt -- --check
cargo clippy -- -D warnings
export PYO3_PYTHON="$PWD/.venv/bin/python"
export LD_LIBRARY_PATH="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export DYLD_FALLBACK_LIBRARY_PATH="$LD_LIBRARY_PATH"
cargo test --lib --no-default-features
