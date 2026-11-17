# Faust Threads MCP

Standalone Python MCP server for the official Meta Threads API. It is portable: it does not import the Faust blog, read local author files, or require a repository path.

## Install and run

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: . .venv/bin/activate
python -m pip install -e ".[test,quality]"
export THREADS_ACCESS_TOKEN="replace-with-your-token"  # PowerShell: $env:THREADS_ACCESS_TOKEN="..."
faust-threads-mcp
```

Configure an MCP client with stdio:

```json
{
  "mcpServers": {
    "threads": {"command": "faust-threads-mcp", "env": {"THREADS_ACCESS_TOKEN": "REPLACE_ME"}}
  }
}
```

The package never prints tokens. API errors are redacted and provider `paging.next` URLs are never fetched or persisted; only validated `paging.cursors.after` values are followed. GET requests have bounded 429/5xx retry with `Retry-After`; POST requests are never retried. Set `THREADS_CACHE_TTL` and `THREADS_CACHE_SIZE` to enable an in-process TTL/LRU cache; writes invalidate it.

## Publication approval and recovery

MCP writes default to disabled. An authorized installation needs a trusted
external approval issuer, an explicit `THREADS_ENABLE_WRITES=1` setting and a
persistent SQLite file configured as `THREADS_PUBLICATION_JOURNAL`. Keep the
same journal across server restarts. The signing key
`THREADS_APPROVAL_SECRET` belongs to the trusted issuer and the server.

Each write takes an `approval_receipt`. The issuer binds the receipt to the
complete tool call, including media, reply target and options. Take the
SHA-256 of the UTF-8 JSON array `[tool_name, arguments_without_receipt]` with
sorted keys, no insignificant whitespace and unescaped Unicode. The receipt
contains that lowercase hex value as `intent_sha256`, a unique
`approval_id`, an integer Unix-second `expires_at`, and the lowercase
HMAC-SHA-256 hex signature of the equally encoded array
`["faust-threads-write-v1", approval_id, intent_sha256, expires_at]`.
Earlier text-only receipts require re-approval for this format.

The journal durably reserves a receipt before provider access. Repeating a
confirmed receipt returns its saved identifiers. A lost response or
unverified outcome holds subsequent writes across restart. To investigate a
hold, verify the exact operation and any container or published ID using
read-only provider evidence. Keep the journal while investigating external
review denials and other holds. External approval remains the responsibility
of the issuer and the host that dispatches the tool call.

## Tools

- `threads_latest_posts`: real `/me/threads` posts, bounded cursor pagination, explicit completeness.
- `threads_get_profile`: authenticated `/me` profile by default, or a numeric user id.
- `threads_get_post`: single-post retrieval by numeric media id.
- `threads_get_replies`, `threads_get_conversation`: bounded reply/conversation retrieval.
- `threads_profile_discovery`: username lookup or profile-post discovery.
- `threads_post_insights`: all documented media metrics: `views`, `likes`, `replies`, `reposts`, `quotes`, `shares`.
- `threads_account_insights`: all documented user metrics: `views`, `likes`, `replies`, `reposts`, `quotes`, `clicks`, `followers_count`, with coverage and time-window metadata.
- `threads_follower_demographics`: `country`, `city`, `age`, or `gender` breakdown. Meta requires one breakdown and at least 100 followers.
- `threads_publish_post`: TEXT, IMAGE, VIDEO, and CAROUSEL (2–20 children) creation, readiness checks and publication. `stage_only` returns a container ID for a later approved `threads_publish_container` call. Every carousel item is validated before creation. The server checks the exact container and requires a published ID for confirmed success.
- `threads_publish_reply`: official `reply_to_id` publishing flow.
- `threads_manage_reply`: official hide/unhide endpoint with a boolean `hide` field.

Published-post editing is not exposed: the official API capability is not claimed and delete/repost is not substituted.

## Included latest-post snapshot

`data/latest_posts.json` is a synthetic fixture and contains no author data. The live MCP tool is the source of truth for current latest posts and reports pagination coverage. No private API snapshot is part of this package.

## Official API scope

The implementation follows Meta's official documentation:

- [Threads API](https://developers.facebook.com/docs/threads/)
- [Get started and permissions](https://developers.facebook.com/docs/threads/get-started/)
- [Insights API](https://developers.facebook.com/docs/threads/insights/)
- [Publishing reference](https://developers.facebook.com/docs/threads/reference/publishing)
- [Create replies](https://developers.facebook.com/documentation/threads/retrieve-and-manage-replies/create-replies)

Required permissions depend on the operation: `threads_basic`; `threads_manage_insights` for insights; `threads_content_publish` for posts; and `threads_manage_replies` for replies. UI-only Insights observations are not represented as API fields.

## Development and verification

```bash
python -m pytest --cov=faust_threads_mcp --cov-report=term-missing --cov-fail-under=100
ruff check src tests
ruff format --check src tests
mypy src
bandit -q -r src
```

Tests use a mocked transport for all API operations and validate an actual MCP SDK stdio client handshake/list/call. No test sends a network write.

## Licensing

This project is licensed under the MIT License; see `LICENSE`.
