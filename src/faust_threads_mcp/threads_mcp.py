"""Standalone MCP server for the official Threads API."""

import asyncio
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Tool, ToolAnnotations

from faust_threads_mcp.publication_guard import RECEIPT_SCHEMA, run_approved_write

API_BASE = "https://graph.threads.com/v1.0"
SERVER_NAME = "faust-threads-mcp"
SERVER_VERSION = "0.3.0"
POST_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
USER_METRICS = ("views", "likes", "replies", "reposts", "quotes", "clicks", "followers_count")
BREAKDOWNS = ("country", "city", "age", "gender")
MAX_PAGES = 100
MAX_ITEMS = 5000
MAX_BODY = 4 * 1024 * 1024
MAX_RETRIES = 2
STATUS_MAX_POLLS = 10
STATUS_POLL_DELAY = 1.0
MEDIA_TYPES = ("TEXT", "IMAGE", "VIDEO", "CAROUSEL")
REPLY_CONTROLS = ("everyone", "accounts_you_follow", "mentioned_only")
Json = dict[str, Any]
Requester = Callable[..., Json]


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


class _Cache:
    def __init__(
        self, ttl: float, capacity: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.values: OrderedDict[str, tuple[float, Json]] = OrderedDict()

    def get(self, key: str) -> Json | None:
        item = self.values.get(key)
        if item is None:
            return None
        if self.clock() - item[0] >= self.ttl:
            self.values.pop(key, None)
            return None
        self.values.move_to_end(key)
        return item[1]

    def put(self, key: str, value: Json) -> None:
        self.values[key] = (self.clock(), value)
        self.values.move_to_end(key)
        while len(self.values) > self.capacity:
            self.values.popitem(last=False)

    def invalidate(self) -> None:
        self.values.clear()


_CACHE = _Cache(
    float(os.environ.get("THREADS_CACHE_TTL", "0")),
    int(os.environ.get("THREADS_CACHE_SIZE", "128")),
)


def _retry_delay(headers: Any, attempt: int) -> float:
    try:
        retry_after = float(headers.get("Retry-After", ""))
        return min(max(retry_after, 0), 30)
    except (AttributeError, TypeError, ValueError):
        return float(min(2**attempt + random.random(), 30))  # nosec B311 - jitter only


def request(
    method: str,
    path: str,
    params: dict[str, Any],
    body: Json | None = None,
    *,
    use_cache: bool = True,
) -> Json:
    """Perform one API request. Only idempotent GET requests are retried."""
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
    key = url
    if method == "GET" and use_cache and _CACHE.ttl > 0:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.build_opener(_RejectRedirects).open(req, timeout=30) as response:  # nosec B310 - fixed API base
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise RuntimeError("Threads API response exceeded size limit")
            result = json.loads(raw)
            if not isinstance(result, dict) or "error" in result:
                raise RuntimeError("Threads API returned an error")
            if method == "GET" and use_cache and _CACHE.ttl > 0:
                _CACHE.put(key, result)
            return result
        except urllib.error.HTTPError as exc:
            if method == "GET" and exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(_retry_delay(exc.headers, attempt))
                continue
            raise RuntimeError(f"Threads API returned HTTP {exc.code}") from None
        except RuntimeError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if method == "GET" and attempt < MAX_RETRIES:
                time.sleep(_retry_delay({}, attempt))
                continue
            raise RuntimeError("Threads API connection failed") from None
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("Threads API returned non-JSON") from None
    raise RuntimeError(
        "Threads API connection failed"
    )  # pragma: no cover - loop always returns or raises


def _page(path: str, params: dict[str, Any], *, pages: int, limit: int, get: Requester) -> Json:
    if not 1 <= pages <= MAX_PAGES or not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"pages must be 1..{MAX_PAGES} and limit 1..{MAX_ITEMS}")
    rows: list[Any] = []
    seen_ids: set[str] = set()
    cursor: str | None = None
    used: set[str] = set()
    warnings: list[str] = []
    errors: list[str] = []
    complete: bool | None = None
    used_pages = 0
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
        cursors = paging.get("cursors") if isinstance(paging, dict) else None
        if not isinstance(cursors, dict):
            warnings.append("paging is missing or malformed; completeness is unknown")
            break
        candidate = _cursor(cursors.get("after"))
        if candidate is None:
            complete = True
            break
        if candidate in used or candidate == cursor:
            errors.append("pagination cursor cycle detected; completeness is unknown")
            break
        used.add(candidate)
        cursor = candidate
    else:
        warnings.append("page limit reached; completeness is unknown")
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


def _numeric(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        raise ValueError(f"{name} must be a numeric Threads id")
    return value


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


def profile(args: Json, get: Requester = request) -> Json:
    raw_user_id = args.get("user_id")
    user_id = "/me" if raw_user_id is None else "/" + _numeric(raw_user_id, "user_id")
    result = get(
        "GET",
        user_id,
        {"fields": "id,username,name,threads_profile_picture_url,threads_biography"},
    )
    return {"data": result, "source": f"Meta Threads API {user_id}"}


def post(args: Json, get: Requester = request) -> Json:
    post_id = _numeric(args.get("post_id"), "post_id")
    return {
        "data": get(
            "GET",
            f"/{post_id}",
            {
                "fields": "id,username,text,media_type,permalink,timestamp,has_replies,root_post,replied_to"
            },
        ),
        "source": f"Meta Threads API /{post_id}",
    }


def replies(args: Json, get: Requester = request) -> Json:
    post_id = _numeric(args.get("post_id"), "post_id")
    return _page(
        f"/{post_id}/replies",
        {"fields": "id,text,username,timestamp,has_replies"},
        pages=int(args.get("pages", 10)),
        limit=int(args.get("limit", 50)),
        get=get,
    )


def conversation(args: Json, get: Requester = request) -> Json:
    post_id = _numeric(args.get("post_id"), "post_id")
    return _page(
        f"/{post_id}/conversation",
        {"fields": "id,text,username,timestamp,has_replies"},
        pages=int(args.get("pages", 10)),
        limit=int(args.get("limit", 50)),
        get=get,
    )


def discover(args: Json, get: Requester = request) -> Json:
    username = args.get("username")
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", username):
        raise ValueError("username must contain only letters, digits, underscores, or dots")
    path = "/profile_posts" if args.get("posts", True) else "/profile_lookup"
    return {"data": get("GET", path, {"username": username}), "source": f"Meta Threads API {path}"}


def post_insights(args: Json, get: Requester = request) -> Json:
    post_id = _numeric(args.get("post_id"), "post_id")
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
    since, until = args.get("since"), args.get("until")
    windowed = [x for x in metrics if x != "followers_count"]
    lifetime = [x for x in metrics if x == "followers_count"]
    data: list[Any] = []
    windows: dict[str, Json] = {}
    if windowed:
        data.extend(
            get(
                "GET",
                "/me/threads_insights",
                {"metric": ",".join(windowed), "since": since, "until": until},
            ).get("data", [])
        )
        windows.update({x: {"since": since, "until": until} for x in windowed})
    if lifetime:
        data.extend(
            get("GET", "/me/threads_insights", {"metric": "followers_count"}).get("data", [])
        )
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
            "missing": [x for x in metrics if x not in returned],
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


def _required_id(args: Json, key: str) -> str:
    return _numeric(args.get(key), key)


def _optional_id(args: Json, key: str) -> str | None:
    value = args.get(key)
    return None if value is None else _numeric(value, key)


def _validate_text(text: Any) -> str:
    if not isinstance(text, str) or not 1 <= len(text) <= 500:
        raise ValueError("text must be 1..500 characters")
    return text


def _create(user_id: str, fields: Json, get: Requester) -> str:
    result = get("POST", f"/{user_id}/threads", fields, fields)
    creation_id = result.get("id")
    if not isinstance(creation_id, str) or not re.fullmatch(r"\d+", creation_id):
        raise RuntimeError("publish container response did not contain a valid id")
    return creation_id


def _fresh_status(creation_id: str, get: Requester) -> Json:
    params = {"fields": "id,status,error_message"}
    return (
        request("GET", f"/{creation_id}", params, use_cache=False)
        if get is request
        else get("GET", f"/{creation_id}", params)
    )


def _wait_for_container(user_id: str, creation_id: str, get: Requester) -> Json:
    del user_id
    last: Json = {}
    for attempt in range(STATUS_MAX_POLLS):
        last = _fresh_status(creation_id, get)
        state = last.get("status")
        if last.get("id") != creation_id:
            return {"status": "unverified_container", "container": {"id": creation_id}}
        if state == "FINISHED":
            return {"status": "finished", "container": {"id": creation_id}}
        if state == "PUBLISHED":
            return {"status": "published_unverified", "container": {"id": creation_id}}
        if state in ("ERROR", "EXPIRED"):
            return {
                "status": state.lower(),
                "container": {"id": creation_id},
                "error": last.get("error_message") or f"container status is {state}",
            }
        if attempt + 1 < STATUS_MAX_POLLS:
            time.sleep(STATUS_POLL_DELAY)
    return {
        "status": "timeout",
        "container": {"id": creation_id},
        "error": f"container did not reach FINISHED within {STATUS_MAX_POLLS} polls",
    }


def _validate_write_options(args: Json) -> None:
    for key in ("reply_to_id", "quote_post_id"):
        _optional_id(args, key)
    if args.get("reply_control") is not None and args["reply_control"] not in REPLY_CONTROLS:
        raise ValueError(f"reply_control must be one of {list(REPLY_CONTROLS)}")
    for key in ("topic_tag", "alt_text"):
        if key in args and args[key] is not None and not isinstance(args[key], str):
            raise ValueError(f"{key} must be a string")


def _carousel_children(args: Json, user_id: str, get: Requester) -> list[str]:
    children = args.get("children")
    items = args.get("items")
    if children is not None and items is not None:
        raise ValueError("provide children or items, not both")
    if isinstance(items, list):
        if not 2 <= len(items) <= 20 or any(not isinstance(item, dict) for item in items):
            raise ValueError("items must contain 2..20 media objects")
        fields = []
        for item in items:
            item_type = item.get("media_type")
            if item_type not in ("IMAGE", "VIDEO"):
                raise ValueError("carousel items must be IMAGE or VIDEO")
            key = "image_url" if item_type == "IMAGE" else "video_url"
            url = item.get(key)
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise ValueError("media URL must be an absolute HTTP(S) URL")
            fields.append({"media_type": item_type, key: url, "is_carousel_item": True})
        return [_create(user_id, item, get) for item in fields]
    if (
        not isinstance(children, list)
        or not 2 <= len(children) <= 20
        or any(not isinstance(x, str) or not re.fullmatch(r"\d+", x) for x in children)
    ):
        raise ValueError("children must contain 2..20 numeric container ids")
    return children


def _publish_existing(user_id: str, creation_id: str, get: Requester) -> Json:
    try:
        ready = _wait_for_container(user_id, creation_id, get)
    except RuntimeError:
        return {"status": "unverified_container", "container": {"id": creation_id}}
    if ready["status"] != "finished":
        return ready
    try:
        published = get(
            "POST",
            f"/{user_id}/threads_publish",
            {"creation_id": creation_id},
            {"creation_id": creation_id},
        )
    except RuntimeError:
        return {"status": "unknown_publish", "container": {"id": creation_id}}
    finally:
        _CACHE.invalidate()
    published_id = published.get("id")
    if not isinstance(published_id, str) or not re.fullmatch(r"[0-9]+", published_id):
        return {"status": "unknown_publish", "container": {"id": creation_id}}
    return {"status": "published", "container": {"id": creation_id}, "result": published}


def publish(args: Json, get: Requester = request) -> Json:
    user_id = _required_id(args, "user_id")
    media_type = args.get("media_type", "TEXT")
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"media_type must be one of {list(MEDIA_TYPES)}")
    _validate_write_options(args)
    fields: Json = {"media_type": media_type}
    if media_type == "TEXT":
        fields["text"] = _validate_text(args.get("text"))
    elif media_type in ("IMAGE", "VIDEO"):
        key = "image_url" if media_type == "IMAGE" else "video_url"
        url = args.get(key)
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ValueError("media URL must be an absolute HTTP(S) URL")
        fields[key] = url
        if "text" in args:
            fields["text"] = _validate_text(args["text"])
    else:
        if "text" in args:
            fields["text"] = _validate_text(args["text"])
        fields["children"] = ",".join(_carousel_children(args, user_id, get))
    for key in ("reply_to_id", "reply_control", "quote_post_id", "topic_tag", "alt_text"):
        if args.get(key) is not None:
            fields[key] = args[key]
    creation_id = _create(user_id, fields, get)
    _CACHE.invalidate()
    if args.get("stage_only"):
        return {"status": "created", "container": {"id": creation_id}}
    return _publish_existing(user_id, creation_id, get)


def publish_container(args: Json, get: Requester = request) -> Json:
    return _publish_existing(_required_id(args, "user_id"), _required_id(args, "creation_id"), get)


def manage_reply(args: Json, get: Requester = request) -> Json:
    reply_id = _required_id(args, "reply_id")
    hidden = args.get("hide")
    if not isinstance(hidden, bool):
        raise TypeError("hide must be boolean")
    result = get(
        "POST",
        f"/{reply_id}/manage_reply",
        {"hide": str(hidden).lower()},
        {"hide": str(hidden).lower()},
    )
    _CACHE.invalidate()
    return {"status": "hidden" if hidden else "visible", "result": result}


def _tool(
    name: str,
    description: str,
    properties: Json,
    required: list[str] | None = None,
    *,
    destructive: bool = False,
) -> Tool:
    if destructive:
        properties = {**properties, "approval_receipt": RECEIPT_SCHEMA}
        required = [*(required or []), "approval_receipt"]
    schema: Json = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    annotations = ToolAnnotations(
        readOnlyHint=not destructive,
        destructiveHint=destructive,
        idempotentHint=not destructive,
    )
    return Tool(name=name, description=description, inputSchema=schema, annotations=annotations)


TOOLS = [
    _tool(
        "threads_latest_posts",
        "Read the authenticated user's posts with bounded cursor pagination.",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ITEMS},
            "pages": {"type": "integer", "minimum": 1, "maximum": MAX_PAGES},
        },
    ),
    _tool(
        "threads_get_profile",
        "Read a Threads profile by numeric user id.",
        {"user_id": {"type": "string", "pattern": "^[0-9]+$"}},
    ),
    _tool(
        "threads_get_post",
        "Read one Threads post by numeric media id.",
        {"post_id": {"type": "string"}},
        ["post_id"],
    ),
    _tool(
        "threads_get_replies",
        "Read replies to a post with bounded pagination.",
        {"post_id": {"type": "string"}, "limit": {"type": "integer"}, "pages": {"type": "integer"}},
        ["post_id"],
    ),
    _tool(
        "threads_get_conversation",
        "Read a post conversation with bounded pagination.",
        {"post_id": {"type": "string"}, "limit": {"type": "integer"}, "pages": {"type": "integer"}},
        ["post_id"],
    ),
    _tool(
        "threads_profile_discovery",
        "Look up a public profile or retrieve its posts by username.",
        {"username": {"type": "string"}, "posts": {"type": "boolean"}},
        ["username"],
    ),
    _tool(
        "threads_post_insights",
        "Read all documented media insight metrics.",
        {"post_id": {"type": "string"}},
        ["post_id"],
    ),
    _tool(
        "threads_account_insights",
        "Read documented account insights and coverage windows.",
        {"metrics": {"type": "array"}, "since": {"type": "integer"}, "until": {"type": "integer"}},
    ),
    _tool(
        "threads_follower_demographics",
        "Read follower demographics for one documented breakdown.",
        {"breakdown": {"type": "string", "enum": list(BREAKDOWNS)}},
        ["breakdown"],
    ),
    _tool(
        "threads_publish_post",
        "Create and publish a text, media, or carousel post; use stage_only for a container-only command.",
        {
            "user_id": {"type": "string", "pattern": "^[0-9]+$"},
            "media_type": {"type": "string", "enum": list(MEDIA_TYPES)},
            "text": {"type": "string", "minLength": 1, "maxLength": 500},
            "image_url": {"type": "string", "format": "uri"},
            "video_url": {"type": "string", "format": "uri"},
            "children": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[0-9]+$"},
                "minItems": 2,
                "maxItems": 20,
            },
            "items": {"type": "array", "minItems": 2, "maxItems": 20, "items": {"type": "object"}},
            "reply_to_id": {"type": "string", "pattern": "^[0-9]+$"},
            "reply_control": {"type": "string", "enum": list(REPLY_CONTROLS)},
            "quote_post_id": {"type": "string", "pattern": "^[0-9]+$"},
            "topic_tag": {"type": "string"},
            "alt_text": {"type": "string"},
            "stage_only": {"type": "boolean"},
        },
        ["user_id"],
        destructive=True,
    ),
    _tool(
        "threads_publish_container",
        "Publish an existing media container returned by stage_only.",
        {
            "user_id": {"type": "string", "pattern": "^[0-9]+$"},
            "creation_id": {"type": "string", "pattern": "^[0-9]+$"},
        },
        ["user_id", "creation_id"],
        destructive=True,
    ),
    _tool(
        "threads_publish_reply",
        "Create and publish a reply using the official reply_to_id field.",
        {
            "user_id": {"type": "string"},
            "reply_to_id": {"type": "string"},
            "text": {"type": "string"},
        },
        ["user_id", "reply_to_id", "text"],
        destructive=True,
    ),
    _tool(
        "threads_manage_reply",
        "Hide or unhide a reply using the official manage_reply endpoint.",
        {"reply_id": {"type": "string"}, "hide": {"type": "boolean"}},
        ["reply_id", "hide"],
        destructive=True,
    ),
]

_HANDLERS: dict[str, Callable[[Json, Requester], Json]] = {
    "threads_latest_posts": latest_posts,
    "threads_get_profile": profile,
    "threads_get_post": post,
    "threads_get_replies": replies,
    "threads_get_conversation": conversation,
    "threads_profile_discovery": discover,
    "threads_post_insights": post_insights,
    "threads_account_insights": account_insights,
    "threads_follower_demographics": demographics,
    "threads_publish_post": publish,
    "threads_publish_container": publish_container,
    "threads_publish_reply": lambda args, get: publish(
        {**args, "reply_to_id": args.get("reply_to_id")}, get
    ),
    "threads_manage_reply": manage_reply,
}


def dispatch(name: str, args: Json, get: Requester = request) -> Json:
    if name not in _HANDLERS:
        raise KeyError(name)
    tool = next(tool for tool in TOOLS if tool.name == name)
    allowed = set(tool.inputSchema.get("properties", {}))
    unknown = set(args) - allowed
    if unknown:
        raise ValueError(f"unknown parameters: {sorted(unknown)}")
    required = set(tool.inputSchema.get("required", []))
    missing = required - set(args)
    if missing:
        raise ValueError(f"missing required parameters: {sorted(missing)}")
    handler = _HANDLERS[name]
    if tool.annotations and tool.annotations.destructiveHint:
        body = {key: value for key, value in args.items() if key != "approval_receipt"}

        def fresh(method: str, path: str, params: Json, data: Json | None) -> Json:
            return request(method, path, params, data, use_cache=False)

        return run_approved_write(
            name, body, args["approval_receipt"], handler, fresh if get is request else get
        )
    return handler(args, get)


def build_server() -> Server:
    server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()  # type: ignore[untyped-decorator, no-untyped-call]
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        try:
            result = dispatch(name, arguments)
            return CallToolResult(
                content=[], structuredContent=result, isError=result.get("queue") == "held"
            )
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
