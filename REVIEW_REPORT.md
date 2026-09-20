# Threads MCP release review

Status: release blockers fixed; publication and live writes were not performed.

## Verified

- `faust-threads-mcp` now resolves to `faust_threads_mcp.threads_mcp:main`.
- Clean wheel install succeeded from an unrelated working directory.
- Installed CLI imported successfully and reported `faust-threads-mcp`.
- Real installed-package MCP SDK handshake succeeded: initialize plus six-tool discovery.
- Pytest: **8 passed**.
- Coverage: **100.00%** (309 statements, 86 branches, 0 missing; `--cov-fail-under=100`). Core request, transport, pagination, protocol, CLI, and cross-process approval paths are exercised; no broad function exclusions remain.
- Ruff check and format check: passed.
- mypy: passed.
- Bandit: passed. Bandit emitted only expected comment-token warnings for words in the URL safety comment.

## Security and correctness changes

- HTTP redirects are rejected before following them; bearer credentials are never sent to a redirected destination. Error responses remain body/token safe.
- Paging treats malformed/missing metadata and cursor cycles as unknown/error, deduplicates repeated post IDs, and reports the limit boundary explicitly.
- Account insights split lifetime-only `followers_count` from date-window metrics and report per-metric windows without claiming unsupported dates.
- Writes require `THREADS_ENABLE_WRITES=1` and a signed, externally issued receipt bound to exact text hash, account, reply target, expiry, and nonce. The server exposes no approval issuer. Receipt IDs are durable, replay-protected, and subject to a daily per-account quota. An OS-level ledger lock serializes concurrent processes on Windows and POSIX; concurrency is covered by tests. Tests use only fake requesters; no live send occurred.
- Removed all `from __future__ import annotations` imports.

## Data boundary

- `data/latest_posts.json` is a synthetic one-post fixture (`fixture: true`) and contains no author data or private snapshot.
- Generated caches and build artifacts were removed from the public package tree.

No commit, push, publication, account change, or live API write was performed.
