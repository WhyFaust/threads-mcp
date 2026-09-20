"""Portable official Threads API MCP server.

Only graph.threads.com/v1.0 is reachable. Reads follow cursors, never provider ``next`` URLs.
Writes are disabled unless explicitly enabled and carry an author approval receipt.
"""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Tool

API_BASE = "https://graph.threads.com/v1.0"
SERVER_NAME = "faust-threads-mcp"
SERVER_VERSION = "0.1.0"
POST_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
USER_METRICS = ("views", "likes", "replies", "reposts", "quotes", "clicks", "followers_count")
BREAKDOWNS = ("country", "city", "age", "gender")
MAX_PAGES = 100
MAX_ITEMS = 5000
MAX_BODY = 4 * 1024 * 1024
MAX_APPROVALS_PER_ACCOUNT_DAY = 20

Json = dict[str, Any]
Requester = Callable[..., Json]
_LEDGER_LOCK = threading.Lock()


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise RuntimeError("Threads API redirect rejected")


def _token() -> str:
    value = os.environ.get("THREADS_ACCESS_TOKEN", "")
    if not value or "\r" in value or "\n" in value:
        raise RuntimeError("THREADS_ACCESS_TOKEN is required")
    return value


def _path(path: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path) or ".." in path:
        raise ValueError("path is not an allowlisted relative API path")
    return path


def _cursor(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("invalid pagination cursor")
    if any(ord(c) < 32 for c in value) or "://" in value or "access_token" in value.lower():
        raise ValueError("unsafe pagination cursor")
    return value


def request(method: str, path: str, params: dict[str, Any], body: Json | None = None) -> Json:
    """Perform one official API request; errors never include tokens or response bodies."""
    url = API_BASE + _path(path)
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    if query:
        url += "?" + query
    payload = urllib.parse.urlencode(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}",
        },
    )
    try:
        opener = urllib.request.build_opener(_RejectRedirects)
        with opener.open(req, timeout=30) as response:  # nosec B310 - URL is fixed to Meta API
            raw = response.read(MAX_BODY + 1)
    except RuntimeError:
        raise
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Threads API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("Threads API connection failed") from None
    if len(raw) > MAX_BODY:
        raise RuntimeError("Threads API response exceeded size limit")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("Threads API returned non-JSON") from None
    if not isinstance(result, dict) or "error" in result:
        raise RuntimeError("Threads API returned an error")
    return result


def _page(path: str, params: dict[str, Any], *, pages: int, limit: int, get: Requester) -> Json:
    if not 1 <= pages <= MAX_PAGES or not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"pages must be 1..{MAX_PAGES} and limit 1..{MAX_ITEMS}")
    rows: list[Any] = []
    seen_ids: set[str] = set()
    cursor: str | None = None
    used_cursors: set[str] = set()
    complete: bool | None = None
    used_pages = 0
    warnings: list[str] = []
    errors: list[str] = []
    for _ in range(pages):
        used_pages += 1
        query = dict(params)
        query["limit"] = min(int(query.get("limit", 50)), 100)
        if cursor:
            query["after"] = cursor
        payload = get("GET", path, query)
        data = payload.get("data")
        if not isinstance(data, list):
            errors.append("response data is not a list")
            complete = None
            break
        for row in data:
            row_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(row_id, str):
                if row_id in seen_ids:
                    continue
                seen_ids.add(row_id)
            rows.append(row)
        if len(rows) >= limit:
            rows = rows[:limit]
            complete = False
            break
        paging = payload.get("paging")
        if not isinstance(paging, dict):
            warnings.append("paging is missing or malformed; completeness is unknown")
            complete = None
            break
        cursors = paging.get("cursors")
        if not isinstance(cursors, dict):
            warnings.append("paging.cursors is missing or malformed; completeness is unknown")
            complete = None
            break
        candidate = _cursor(cursors.get("after"))
        if candidate is None:
            complete = True
            break
        if candidate in used_cursors or candidate == cursor:
            errors.append("pagination cursor cycle detected; completeness is unknown")
            complete = None
            break
        used_cursors.add(candidate)
        cursor = candidate
    else:
        warnings.append("page limit reached; completeness is unknown")
        complete = None
    return {
        "data": rows[:limit],
        "coverage": {
            "rows": len(rows[:limit]),
            "pages": used_pages,
            "max_pages": pages,
            "complete": complete,
            "more_available": True if complete is False else (False if complete is True else None),
            "warnings": warnings,
            "errors": errors,
        },
    }


def latest_posts(args: Json, get: Requester = request) -> Json:
    result = _page(
        "/me/threads",
        {"fields": "id,text,permalink,timestamp,has_replies"},
        pages=int(args.get("pages", 10)),
        limit=int(args.get("limit", 50)),
        get=get,
    )
    result["source"] = "Meta Threads API /me/threads"
    return result


def post_insights(args: Json, get: Requester = request) -> Json:
    post_id = args.get("post_id")
    if not isinstance(post_id, str) or not re.fullmatch(r"\d+", post_id):
        raise ValueError("post_id must be a numeric Threads media id")
    result = get("GET", f"/{post_id}/insights", {"metric": ",".join(POST_METRICS)})
    return {
        "data": result.get("data", []),
        "source": f"Meta Threads API /{post_id}/insights",
        "coverage": {
            "requested": list(POST_METRICS),
            "returned": [x.get("name") for x in result.get("data", []) if isinstance(x, dict)],
        },
    }


def account_insights(args: Json, get: Requester = request) -> Json:
    metrics = args.get("metrics", list(USER_METRICS))
    if not isinstance(metrics, list) or not metrics or any(x not in USER_METRICS for x in metrics):
        raise ValueError(f"metrics must contain only {list(USER_METRICS)}")
    since = args.get("since")
    until = args.get("until")
    windowed = [metric for metric in metrics if metric != "followers_count"]
    lifetime = [metric for metric in metrics if metric == "followers_count"]
    data: list[Any] = []
    windows: dict[str, Json] = {}
    if windowed:
        result = get(
            "GET",
            "/me/threads_insights",
            {"metric": ",".join(windowed), "since": since, "until": until},
        )
        data.extend(result.get("data", []))
        for metric in windowed:
            windows[metric] = {"since": since, "until": until}
    if lifetime:
        result = get("GET", "/me/threads_insights", {"metric": "followers_count"})
        data.extend(result.get("data", []))
        windows["followers_count"] = {
            "since": None,
            "until": None,
            "reason": "lifetime-only metric",
        }
    returned = [x.get("name") for x in data if isinstance(x, dict)]
    return {
        "data": data,
        "source": "Meta Threads API /me/threads_insights",
        "coverage": {
            "requested": metrics,
            "returned": returned,
            "missing": [m for m in metrics if m not in returned],
            "windows": windows,
        },
    }


def demographics(args: Json, get: Requester = request) -> Json:
    breakdown = args.get("breakdown")
    if breakdown not in BREAKDOWNS:
        raise ValueError(f"breakdown must be one of {list(BREAKDOWNS)}")
    result = get(
        "GET", "/me/threads_insights", {"metric": "follower_demographics", "breakdown": breakdown}
    )
    return {
        "data": result.get("data", []),
        "source": "Meta Threads API /me/threads_insights",
        "coverage": {
            "metric": "follower_demographics",
            "breakdown": breakdown,
            "note": "Meta requires one breakdown and at least 100 followers.",
        },
    }


def _approval_payload(receipt: Json) -> str:
    fields = {
        "approval_id": receipt.get("approval_id"),
        "account_id": receipt.get("account_id"),
        "reply_to_id": receipt.get("reply_to_id"),
        "text_sha256": receipt.get("text_sha256"),
        "expires_at": receipt.get("expires_at"),
        "nonce": receipt.get("nonce"),
    }
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))


def _ledger_path() -> Path:
    return Path(
        os.environ.get(
            "THREADS_APPROVAL_LEDGER",
            Path(tempfile.gettempdir()) / "faust-threads-approval-ledger.json",
        )
    )


def _write_ledger(ledger: list[Json]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".approval-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(ledger, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


@contextlib.contextmanager
def _ledger_file_lock() -> Any:
    """Serialize ledger read/modify/write across processes on Windows and POSIX."""
    lock_path = Path(f"{_ledger_path()}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_guard(args: Json) -> None:
    if os.environ.get("THREADS_ENABLE_WRITES") != "1":
        raise RuntimeError("writes are disabled; set THREADS_ENABLE_WRITES=1 explicitly")
    text = args.get("text")
    account_id = args.get("user_id")
    reply_to_id = args.get("reply_to_id")
    if (
        not isinstance(text, str)
        or not isinstance(account_id, str)
        or not re.fullmatch(r"\d+", account_id)
    ):
        raise ValueError("approval binding requires a numeric user_id and text")
    if reply_to_id is not None and (
        not isinstance(reply_to_id, str) or not re.fullmatch(r"\d+", reply_to_id)
    ):
        raise ValueError("approval binding has an invalid reply target")
    receipt = args.get("approval_receipt")
    secret = os.environ.get("THREADS_APPROVAL_SECRET", "")
    required = (
        "approval_id",
        "account_id",
        "reply_to_id",
        "text_sha256",
        "expires_at",
        "nonce",
        "signature",
    )
    if (
        not secret
        or not isinstance(receipt, dict)
        or any(not isinstance(receipt.get(key), str) for key in required)
    ):
        raise RuntimeError("trusted external approval receipt is required")
    if receipt["account_id"] != account_id or receipt["reply_to_id"] != (reply_to_id or ""):
        raise RuntimeError("approval receipt is bound to a different account or reply target")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(receipt["text_sha256"], digest):
        raise RuntimeError("approval receipt is bound to different text")
    try:
        expires = datetime.fromisoformat(receipt["expires_at"])
    except ValueError:
        raise RuntimeError("approval receipt expiry is invalid") from None
    if expires.tzinfo is None or expires <= datetime.now(UTC):
        raise RuntimeError("approval receipt is expired")
    expected = hmac.new(
        secret.encode("utf-8"), _approval_payload(receipt).encode(), hashlib.sha256
    ).digest()
    try:
        supplied = base64.urlsafe_b64decode(receipt["signature"] + "===")
    except (ValueError, binascii.Error):
        raise RuntimeError("approval receipt signature is invalid") from None
    if not hmac.compare_digest(expected, supplied):
        raise RuntimeError("approval receipt signature is invalid")
    now = datetime.now(UTC).isoformat()
    with _LEDGER_LOCK, _ledger_file_lock():
        try:
            ledger = json.loads(_ledger_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            ledger = []
        if not isinstance(ledger, list) or any(not isinstance(item, dict) for item in ledger):
            raise RuntimeError("approval ledger is invalid")
        if any(item.get("approval_id") == receipt["approval_id"] for item in ledger):
            raise RuntimeError("approval receipt has already been used")
        day = now[:10]
        used_today = sum(
            1
            for item in ledger
            if item.get("account_id") == account_id and str(item.get("used_at", "")).startswith(day)
        )
        if used_today >= MAX_APPROVALS_PER_ACCOUNT_DAY:
            raise RuntimeError("daily write quota exceeded")
        ledger.append(
            {"approval_id": receipt["approval_id"], "account_id": account_id, "used_at": now}
        )
        _write_ledger(ledger)


def publish(args: Json, get: Requester = request, *, _guarded: bool = False) -> Json:
    if not _guarded:
        _write_guard(args)
    text = args.get("text")
    user_id = args.get("user_id")
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 500
        or not isinstance(user_id, str)
        or not re.fullmatch(r"\d+", user_id)
    ):
        raise ValueError("user_id must be numeric and text must be 1..500 characters")
    container = get(
        "POST",
        f"/{user_id}/threads",
        {"media_type": "TEXT", "text": text},
        {"media_type": "TEXT", "text": text},
    )
    creation_id = container.get("id")
    if not isinstance(creation_id, str) or not re.fullmatch(r"\d+", creation_id):
        raise RuntimeError("publish container response did not contain a valid id")
    published = get(
        "POST",
        f"/{user_id}/threads_publish",
        {"creation_id": creation_id},
        {"creation_id": creation_id},
    )
    return {"status": "published", "container": {"id": creation_id}, "result": published}


def reply(args: Json, get: Requester = request) -> Json:
    _write_guard(args)
    args = dict(args)
    args["text"] = args.get("text")
    parent = args.get("reply_to_id")
    return publish({**args, "reply_to_id": parent}, get, _guarded=True)


TOOLS = [
    Tool(
        name="threads_latest_posts",
        description="Fetch real latest posts with cursor pagination.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000},
                "pages": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    ),
    Tool(
        name="threads_post_insights",
        description="Fetch all documented media insight metrics.",
        inputSchema={
            "type": "object",
            "properties": {"post_id": {"type": "string"}},
            "required": ["post_id"],
        },
    ),
    Tool(
        name="threads_account_insights",
        description="Fetch documented user insights with explicit coverage and window.",
        inputSchema={
            "type": "object",
            "properties": {
                "metrics": {"type": "array", "items": {"type": "string"}},
                "since": {"type": "integer"},
                "until": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="threads_follower_demographics",
        description="Fetch follower demographics for one documented breakdown.",
        inputSchema={
            "type": "object",
            "properties": {"breakdown": {"type": "string", "enum": list(BREAKDOWNS)}},
            "required": ["breakdown"],
        },
    ),
    Tool(
        name="threads_publish_post",
        description="Publish only with explicit environment enablement and author approval receipt.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "text": {"type": "string"},
                "approval_receipt": {"type": "object"},
            },
            "required": ["user_id", "text", "approval_receipt"],
        },
    ),
    Tool(
        name="threads_publish_reply",
        description="Publish a reply only with explicit environment enablement and author approval receipt.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "reply_to_id": {"type": "string"},
                "text": {"type": "string"},
                "approval_receipt": {"type": "object"},
            },
            "required": ["user_id", "reply_to_id", "text", "approval_receipt"],
        },
    ),
]


def dispatch(name: str, args: Json, get: Requester = request) -> Json:
    return {
        "threads_latest_posts": latest_posts,
        "threads_post_insights": post_insights,
        "threads_account_insights": account_insights,
        "threads_follower_demographics": demographics,
        "threads_publish_post": publish,
        "threads_publish_reply": reply,
    }[name](args, get)


def build_server() -> Server:
    server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()  # type: ignore[untyped-decorator, no-untyped-call]
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            value = dispatch(name, arguments)
            return CallToolResult(content=[], structuredContent=value, isError=False)
        except (RuntimeError, ValueError, TypeError, KeyError) as exc:
            return CallToolResult(content=[], isError=True, structuredContent={"error": str(exc)})

    return server


async def run() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(), experimental_capabilities={}
                ),
            ),
        )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
