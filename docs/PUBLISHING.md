# Publishing WebSocket-RS to GitHub

This guide walks through the steps to publish the WebSocket-RS package to GitHub and enable the installation methods described in the README.

## Release Strategy

We recommend using Pull Requests for all releases:
1. Create feature branches for development
2. Submit PR for review
3. Merge to main after approval
4. Create release tags only from main branch

## Prerequisites

1. Create a GitHub repository (if not already created)
2. Set up Git locally and configure your GitHub credentials

## Step 1: Initialize Git Repository

```bash
cd /home/enor/websocket-rs
git init
git add .
git commit -m "Initial commit: High-performance WebSocket client library for Python using Rust"
```

## Step 2: Create GitHub Repository

1. Go to [GitHub](https://github.com) and create a new repository
2. Name it `websocket-rs`
3. Don't initialize with README, .gitignore, or license (we already have these)

## Step 3: Connect Local to Remote

Connect to GitHub repository:

```bash
git remote add origin https://github.com/nateyoder/websocket-rs.git
git branch -M main
git push -u origin main
```

## Step 4: Verify URLs

All URLs in README.md and pyproject.toml have been updated with the correct username `nateyoder`.

## Step 5: Create Release via Pull Request (Recommended)

### PR-based Release Workflow

1. **Create release branch**:
```bash
git checkout -b release/v0.2.0
```

2. **Update version numbers** in:
   - `Cargo.toml`
   - `pyproject.toml`
   - `websocket_rs/__init__.py`

3. **Commit and push**:
```bash
git add .
git commit -m "chore: prepare release v0.2.0"
git push origin release/v0.2.0
```

4. **Create Pull Request**:
   - Go to GitHub and create PR from `release/v0.2.0` to `main`
   - Wait for CI checks to pass
   - Get approval from reviewers

5. **After PR is merged**, create the release tag:
```bash
git checkout main
git pull origin main
git tag -a v0.2.0 -m "Release v0.2.0: Enhanced with monkeypatch support"
git push origin v0.2.0
```

## Step 5b: Direct Release (Alternative)

### Option A: Using GitHub Web Interface

1. Go to your repository on GitHub
2. Click on "Releases" → "Create a new release"
3. Tag version: `v0.2.0`
4. Release title: `v0.2.0 - Enhanced Release with Monkeypatch`
5. Description: Include performance metrics and features
6. Click "Publish release"

### Option B: Using GitHub CLI

```bash
# Install GitHub CLI if needed
# https://cli.github.com/

# Create and push tag
git tag -a v0.2.0 -m "Enhanced release with monkeypatch support and performance optimizations"
git push origin v0.2.0

# Create release
gh release create v0.2.0 \
  --title "v0.2.0 - Enhanced Release with Monkeypatch" \
  --notes "## Features
- 1.5-5x faster than native Python (single messages)
- 10-17x performance boost for batch operations
- 100% API compatible with Python websockets
- Zero-code-change acceleration via monkeypatch
- Multiple patching strategies (global, context, decorator, env)
- Thread-safe with concurrent operations
- Zero-copy optimizations

## Installation
\`\`\`bash
uv pip install git+https://github.com/nateyoder/websocket-rs.git@v0.2.0
\`\`\`
"
```

## Step 6: Verify GitHub Actions

After pushing the tag, GitHub Actions will automatically:

1. Build wheels for multiple platforms (Linux, Windows, macOS)
2. Build for Python 3.9, 3.10, 3.11, 3.12, 3.13
3. Upload wheels to the release

Check the Actions tab in your repository to monitor the build progress.

## Step 7: Test Installation Methods

Once the release is created and wheels are uploaded, test the installation:

```bash
# Test direct GitHub install
uv pip install git+https://github.com/nateyoder/websocket-rs.git

# Test specific version
uv pip install git+https://github.com/nateyoder/websocket-rs.git@v0.2.0

# Test wheel download (adjust filename for your platform)
uv pip install https://github.com/nateyoder/websocket-rs/releases/download/v0.7.10.post1/websocket_rs_nateyoder-0.7.10.post1-cp312-abi3-manylinux_2_34_x86_64.whl
```

## Step 8: Enable GitHub Pages for Documentation (Optional)

1. Go to Settings → Pages
2. Source: Deploy from a branch
3. Branch: main, folder: /docs (if you have documentation)

## Step 9: Add Badges to README (Optional)

Add these badges at the top of your README:

```markdown
[![Tests](https://github.com/nateyoder/websocket-rs/actions/workflows/test.yml/badge.svg)](https://github.com/nateyoder/websocket-rs/actions/workflows/test.yml)
[![Release](https://github.com/nateyoder/websocket-rs/actions/workflows/release.yml/badge.svg)](https://github.com/nateyoder/websocket-rs/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
```

## Future Releases

For subsequent releases:

1. Update version in `Cargo.toml` and `pyproject.toml`
2. Commit changes
3. Create and push a new tag:
   ```bash
   git tag -a v0.2.0 -m "Description of changes"
   git push origin v0.2.0
   ```
4. GitHub Actions will automatically build and upload wheels

## PyPI

This fork is **not** published to PyPI and is not intended to be: the name
`websocket-rs` there belongs to upstream, and the GitHub release assets are the
distribution channel. The release workflow's PyPI upload step was removed
deliberately -- do not restore it.

## Release checklist

Each release URL in the docs is pinned to a tag, so a new tag means updating
them. Miss this and the install commands keep pointing at the previous release.

1. Bump `version` in `pyproject.toml` to the next `<upstream base>.postN`. If
   the base changes, bump `Cargo.toml` to the new base too --
   `tests/test_version_metadata.py` enforces that they agree.
2. Move the `## [Unreleased]` heading in `CHANGELOG.md` to the new version and
   date it.
3. Update the pinned version and tag in every install command. As of 0.7.10.post1 they
   live in `README.md`, `README.zh-TW.md`, `README.pypi.md`, `docs/API.md` and
   `docs/TLS-BACKENDS.md`; confirm with:
   ```bash
   grep -rn "expanded_assets\|releases/download" README*.md docs/
   ```
4. Merge, then tag the merge commit and push:
   ```bash
   git tag -a v<version> -m "v<version>" && git push origin v<version>
   ```
5. The `Release` workflow builds wheels for all four targets plus an sdist and
   attaches them to a GitHub release. Verify the assets landed:
   ```bash
   gh release view v<version> --repo nateyoder/websocket-rs --json assets \
     -q '.assets[].name'
   ```
6. Verify a clean install actually resolves from the release:
   ```bash
   uv pip install "websocket-rs-nateyoder==<version>" \
     --find-links https://github.com/nateyoder/websocket-rs/releases/expanded_assets/v<version>
   ```

## Troubleshooting

### Actions Failing

- Check the Actions tab for error details
- Common issues:
  - Missing Rust toolchain for target platform
  - Python version compatibility
  - Dependency resolution

### Wheels Not Building

- Ensure `.github/workflows/release.yml` is properly configured
- Check that the tag matches the pattern `v*`
- Verify GitHub Actions permissions are enabled

### Installation Failing

- Check that the repository is public
- Verify the release and wheels are properly uploaded
- Test with different Python versions

## Support

For issues or questions, create an issue in the GitHub repository.