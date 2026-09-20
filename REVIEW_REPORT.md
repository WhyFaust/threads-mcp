# Threads MCP release review

Status: v2 implementation verified; publication and live writes were not performed.

## Verified

- `faust-threads-mcp` now resolves to `faust_threads_mcp.threads_mcp:main`.
- Clean wheel install succeeded from an unrelated working directory.
- Installed CLI imported successfully and reported `faust-threads-mcp`.
- Real installed-package MCP SDK handshake covers the v2 tool set.
- Coverage: **100.00%** (373 statements, 134 branches, 0 missing; `--cov-fail-under=100`). Core request, transport, pagination, protocol, CLI, retry, cache, media lifecycle, status polling, profile defaults, schema validation, and write paths are exercised; no broad function exclusions remain.
- Ruff check and format check: passed.
- mypy: passed.
- Bandit: passed. Bandit emitted only expected comment-token warnings for words in the URL safety comment.

## Security and correctness changes

- HTTP redirects are rejected before following them; bearer credentials are never sent to a redirected destination. Error responses remain body/token safe.
- Paging treats malformed/missing metadata and cursor cycles as unknown/error, deduplicates repeated post IDs, and reports the limit boundary explicitly.
- Account insights split lifetime-only `followers_count` from date-window metrics and report per-metric windows without claiming unsupported dates.
- Writes use only official API endpoints and have no artificial approval or quota gates. POST requests are never retried; partial create failures return the container id where available.
- GET retry is bounded to 429/5xx responses and honors `Retry-After`; optional TTL/LRU caching is scoped to exact query URLs and invalidated on writes.
- Removed all `from __future__ import annotations` imports.

## Data boundary

- `data/latest_posts.json` is a synthetic one-post fixture (`fixture: true`) and contains no author data or private snapshot.
- Generated caches and build artifacts were removed from the public package tree.

No commit, push, publication, account change, or live API write was performed.
