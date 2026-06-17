#!/usr/bin/env bash
set -euo pipefail

UV_CACHE_DIR=.uv-cache uv run --extra dev ruff format .
UV_CACHE_DIR=.uv-cache uv run --extra dev ruff check .
UV_CACHE_DIR=.uv-cache uv run --extra dev mypy src
UV_CACHE_DIR=.uv-cache uv run --extra dev pytest
