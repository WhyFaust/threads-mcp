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

## Tools

- `threads_latest_posts`: real `/me/threads` posts, bounded cursor pagination, explicit completeness.
- `threads_get_profile`: authenticated `/me` profile by default, or a numeric user id.
- `threads_get_post`: single-post retrieval by numeric media id.
- `threads_get_replies`, `threads_get_conversation`: bounded reply/conversation retrieval.
- `threads_profile_discovery`: username lookup or profile-post discovery.
- `threads_post_insights`: all documented media metrics: `views`, `likes`, `replies`, `reposts`, `quotes`, `shares`.
- `threads_account_insights`: all documented user metrics: `views`, `likes`, `replies`, `reposts`, `quotes`, `clicks`, `followers_count`, with coverage and time-window metadata.
- `threads_follower_demographics`: `country`, `city`, `age`, or `gender` breakdown. Meta requires one breakdown and at least 100 followers.
- `threads_publish_post`: official TEXT, IMAGE, VIDEO, and CAROUSEL (2–20 children) create → bounded status polling → publish workflow. `stage_only` returns a container id without publishing; `threads_publish_container` publishes that existing id later. Carousel `items` create IMAGE/VIDEO child containers with `is_carousel_item=true`; `children` accepts existing numeric container ids. Optional ids and `reply_control` are validated before any write. ERROR, EXPIRED, and timeout outcomes return the container id and never auto-retry POST.
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
