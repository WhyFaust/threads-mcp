import asyncio
import os
import sys
import urllib.error
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import faust_threads_mcp.threads_mcp as m


def fake(method, path, params, body=None):
    if path in ("/me/threads", "/1/replies", "/1/conversation"):
        after = params.get("after")
        return {
            "data": [{"id": "1"}, {"id": "2"}] if not after else [{"id": "2"}, {"id": "3"}],
            "paging": {"cursors": {"after": None if after else "next"}},
        }
    if path == "/profile_posts":
        return {"data": [{"id": "1"}]}
    if path == "/profile_lookup":
        return {"id": "7", "username": params["username"]}
    if path.endswith("/insights"):
        return {"data": [{"name": "views"}]}
    if path == "/me/threads_insights":
        return {"data": [{"name": x} for x in params["metric"].split(",")]}
    if method == "POST" and path.endswith("/threads"):
        return {"id": "99"}
    if method == "GET" and path == "/99":
        return {"id": "99", "status": "FINISHED"}
    if method == "POST":
        return {"id": "100"}
    return {"id": path.strip("/")}


def test_reads_and_paging():
    assert len(m.latest_posts({"limit": 5, "pages": 2}, fake)["data"]) == 3
    assert m.profile({"user_id": "1"}, fake)["data"]["id"] == "1"
    assert m.post({"post_id": "1"}, fake)["data"]["id"] == "1"
    assert m.replies({"post_id": "1", "limit": 3, "pages": 2}, fake)["coverage"]["pages"] == 2
    assert m.conversation({"post_id": "1"}, fake)["data"]
    assert m.discover({"username": "alice", "posts": False}, fake)["data"]["id"] == "7"
    assert m.discover({"username": "alice"}, fake)["data"]
    assert m.post_insights({"post_id": "1"}, fake)["coverage"]["requested"]
    assert m.account_insights(
        {"metrics": ["likes", "followers_count"], "since": 1, "until": 2}, fake
    )["data"]
    assert m.demographics({"breakdown": "country"}, fake)["coverage"]["breakdown"] == "country"


def test_publish_media_reply_and_manage():
    assert (
        m.publish({"user_id": "1", "text": "hello", "stage_only": True}, fake)["status"]
        == "created"
    )
    assert (
        m.publish(
            {"user_id": "1", "media_type": "IMAGE", "image_url": "https://x/a.jpg", "text": "x"},
            fake,
        )["status"]
        == "published"
    )
    assert (
        m.publish({"user_id": "1", "media_type": "VIDEO", "video_url": "https://x/a.mp4"}, fake)[
            "status"
        ]
        == "published"
    )
    assert (
        m.publish({"user_id": "1", "media_type": "CAROUSEL", "children": ["1", "2"]}, fake)[
            "status"
        ]
        == "published"
    )
    assert (
        m.dispatch(
            "threads_publish_reply", {"user_id": "1", "reply_to_id": "2", "text": "r"}, fake
        )["status"]
        == "published"
    )
    assert m.manage_reply({"reply_id": "2", "hide": True}, fake)["status"] == "hidden"
    assert m.manage_reply({"reply_id": "2", "hide": False}, fake)["status"] == "visible"


def test_validation_and_paging_branches():
    for fn, args in (
        (m.profile, {"user_id": "x"}),
        (m.post, {"post_id": "x"}),
        (m.replies, {"post_id": "x"}),
        (m.conversation, {"post_id": "x"}),
    ):
        with pytest.raises(ValueError):
            fn(args, fake)
    with pytest.raises(ValueError):
        m.discover({"username": "bad name"}, fake)
    with pytest.raises(ValueError):
        m.demographics({"breakdown": "x"}, fake)
    with pytest.raises(ValueError):
        m.publish({"user_id": "1", "media_type": "A"}, fake)
    with pytest.raises(ValueError):
        m.publish({"user_id": "1", "media_type": "IMAGE", "image_url": "bad"}, fake)
    with pytest.raises(ValueError):
        m.publish({"user_id": "1", "media_type": "CAROUSEL", "children": ["1"]}, fake)
    with pytest.raises(TypeError):
        m.manage_reply({"reply_id": "1", "hide": "yes"}, fake)
    with pytest.raises(ValueError):
        m._page("/x", {}, pages=0, limit=1, get=fake)
    assert m._page("/x", {}, pages=1, limit=2, get=lambda *_: {"data": "x"})["coverage"]["errors"]
    assert m._page("/x", {}, pages=1, limit=2, get=lambda *_: {"data": [], "paging": {}})[
        "coverage"
    ]["warnings"]
    cycle = m._page(
        "/x",
        {},
        pages=2,
        limit=5,
        get=lambda *_: {"data": [], "paging": {"cursors": {"after": "a"}}},
    )
    assert cycle["coverage"]["errors"]
    with pytest.raises(ValueError):
        m._cursor("access_token=x")
    with pytest.raises(ValueError):
        m._path("https://bad")


def test_request_safety_and_retry(monkeypatch):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "token")

    class Response:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, *_):
            return self.raw

    class Opener:
        def __init__(self):
            self.n = 0

        def open(self, *_args, **_kwargs):
            self.n += 1
            if self.n == 1:
                raise urllib.error.HTTPError("u", 503, "", {}, None)
            return Response(b'{"ok": true}')

    opener = Opener()
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_: opener)
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    assert m.request("GET", "/me/threads", {}) == {"ok": True}
    assert opener.n == 2
    with pytest.raises(ValueError):
        m.request("GET", "https://bad", {})

    class Bad:
        def open(self, *_args, **_kwargs):
            raise RuntimeError("Threads API redirect rejected")

    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_: Bad())
    with pytest.raises(RuntimeError, match="redirect"):
        m.request("POST", "/1/threads", {})


def test_cache_and_sdk_smoke(tmp_path: Path, monkeypatch):
    clock = [0.0]
    cache = m._Cache(5, 1, lambda: clock[0])
    cache.put("a", {"x": 1})
    assert cache.get("a") == {"x": 1}
    cache.put("b", {})
    assert cache.get("a") is None
    clock[0] = 10
    assert cache.get("b") is None
    server = Path(m.__file__)

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(server)],
            env={**os.environ, "THREADS_ACCESS_TOKEN": "x"},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            info = await session.initialize()
            assert info.serverInfo.name == "faust-threads-mcp"
            tools = await session.list_tools()
            assert len(tools.tools) == 13

    asyncio.run(run())


def test_server_dispatch_error():
    with pytest.raises(KeyError):
        m.dispatch("missing", {}, fake)

    async def run():
        server = m.build_server()
        result = await server.request_handlers[
            __import__("mcp.types", fromlist=["CallToolRequest"]).CallToolRequest
        ](
            __import__("mcp.types", fromlist=["CallToolRequest"]).CallToolRequest(
                params=__import__(
                    "mcp.types", fromlist=["CallToolRequestParams"]
                ).CallToolRequestParams(name="missing", arguments={})
            )
        )
        assert result.root.isError

    asyncio.run(run())


def test_remaining_boundaries(monkeypatch):
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        m._token()
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "bad\n")
    with pytest.raises(RuntimeError):
        m._token()
    assert m._cursor(None) is None
    with pytest.raises(ValueError):
        m._cursor(1)
    with pytest.raises(ValueError):
        m._cursor("x" * 513)
    with pytest.raises(ValueError):
        m._cursor("bad\n")
    with pytest.raises(ValueError):
        m._path("/../x")
    assert m._retry_delay({}, 0) >= 1
    assert m._retry_delay({"Retry-After": "2"}, 0) == 2
    assert m._retry_delay({"Retry-After": "bad"}, 0) >= 1
    with pytest.raises(RuntimeError):
        m._RejectRedirects().redirect_request(None, None, 302, "", {}, "x")
    assert m._page("/x", {}, pages=1, limit=1, get=lambda *_: {"data": [{"id": "1"}, {"id": "2"}]})[
        "coverage"
    ]["more_available"]
    assert m._page(
        "/x",
        {},
        pages=1,
        limit=1,
        get=lambda *_: {"data": [], "paging": {"cursors": {"after": None}}},
    )["coverage"]["complete"]
    with pytest.raises(ValueError):
        m._page(
            "/x",
            {},
            pages=1,
            limit=1,
            get=lambda *_: {"data": [], "paging": {"cursors": {"after": "bad://x"}}},
        )
    with pytest.raises(ValueError):
        m.account_insights({"metrics": ["bad"]}, fake)
    m.account_insights({"metrics": ["likes"]}, fake)
    with pytest.raises(ValueError):
        m.publish({"user_id": "1", "text": ""}, fake)
    with pytest.raises(RuntimeError):
        m.publish({"user_id": "1", "text": "x"}, lambda *args: {})
    assert (
        m.publish(
            {"user_id": "1", "text": "x"},
            lambda method, path, params, body=None: (
                {"id": "99", "status": "IN_PROGRESS"}
                if method == "POST"
                else {"status": "IN_PROGRESS"}
            ),
        )["status"]
        == "timeout"
    )


def test_request_failures_and_cache(monkeypatch):
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "x")

    class R:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, *_):
            return self.raw

    class O:
        def __init__(self, error=None, raw=b"{}"):
            self.error, self.raw = error, raw

        def open(self, *_args, **_kwargs):
            if self.error is not None:
                raise self.error
            return R(self.raw)

    for error in (
        urllib.error.HTTPError("u", 400, "", {}, None),
        urllib.error.URLError("x"),
        TimeoutError(),
    ):
        o = O(error)
        monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_, opener=o: opener)
        with pytest.raises(RuntimeError):
            m.request("GET", "/x", {})
    for raw in (b"x", b'{"error": {}}', b"x" * (m.MAX_BODY + 1)):
        o = O(raw=raw)
        monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_, opener=o: opener)
        with pytest.raises(RuntimeError):
            m.request("GET", "/x", {})
    old = m._CACHE
    m._CACHE = m._Cache(10, 2, lambda: 0)
    o = O(raw=b'{"ok": true}')
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_: o)
    assert m.request("GET", "/x", {"a": 1}) == {"ok": True}
    assert m.request("GET", "/x", {"a": 1}) == {"ok": True}
    m._CACHE = old


def test_final_branches(monkeypatch):
    import runpy

    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "x")
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)

    class Bad:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError("x")

    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_: Bad())
    with pytest.raises(RuntimeError, match="connection failed"):
        m.request("GET", "/x", {})
    dup = m._page(
        "/x",
        {},
        pages=1,
        limit=5,
        get=lambda *_: {"data": [{"id": "1"}, {"id": "1"}], "paging": {"cursors": {"after": "x"}}},
    )
    assert dup["coverage"]["warnings"]
    assert m._page(
        "/x",
        {},
        pages=1,
        limit=5,
        get=lambda *_: {"data": [None], "paging": {"cursors": {"after": None}}},
    )["data"] == [None]
    assert m._page(
        "/x",
        {},
        pages=1,
        limit=5,
        get=lambda *_: {"data": [], "paging": {"cursors": {"after": "x"}}},
    )["coverage"]["warnings"] == ["page limit reached; completeness is unknown"]
    assert m.account_insights({"metrics": ["followers_count"]}, fake)["data"]
    assert (
        m.publish(
            {"user_id": "1", "media_type": "CAROUSEL", "children": ["1", "2"], "text": "caption"},
            fake,
        )["status"]
        == "published"
    )

    class Context:
        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *_):
            return False

    class Server:
        def get_capabilities(self, **_):
            return {}

        async def run(self, *_):
            return None

    old_build, old_stdio = m.build_server, m.stdio_server
    m.build_server, m.stdio_server = lambda: Server(), lambda: Context()
    try:
        asyncio.run(m.run())
    finally:
        m.build_server, m.stdio_server = old_build, old_stdio
    old_run = m.asyncio.run
    m.asyncio.run = lambda coro: coro.close()
    try:
        runpy.run_path(str(Path(m.__file__)), run_name="__main__")
    finally:
        m.asyncio.run = old_run


def test_release_blockers_are_explicit(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    calls = []

    def statuses(method, path, params, body=None):
        calls.append((method, path, params, body))
        if method == "POST" and path == "/1/threads":
            return {"id": "77"}
        if method == "GET" and path == "/77":
            return {"id": "77", "status": "ERROR", "error_message": "bad media"}
        return {"ok": True}

    failed = m.publish({"user_id": "1", "text": "x"}, statuses)
    assert failed == {"status": "error", "container": {"id": "77"}, "error": "bad media"}
    assert not any(item[0] == "POST" and item[1].endswith("threads_publish") for item in calls)
    assert (
        m.publish_container({"user_id": "1", "creation_id": "77"}, statuses)["container"]["id"]
        == "77"
    )
    published = m.publish_container(
        {"user_id": "1", "creation_id": "77"},
        lambda method, path, params, body=None: {"status": "PUBLISHED"},
    )
    assert published["status"] == "published"
    assert (
        m.profile({}, lambda method, path, params, body=None: {"id": path})["data"]["id"] == "/me"
    )
    with pytest.raises(ValueError, match="unknown parameters"):
        m.dispatch("threads_publish_post", {"user_id": "1", "text": "x", "skip_status": True}, fake)
    with pytest.raises(ValueError):
        m.publish({"user_id": "1", "text": "x", "reply_to_id": "bad"}, fake)
    with pytest.raises(ValueError):
        m.publish({"user_id": "1", "text": "x", "reply_control": "bad"}, fake)


def test_carousel_items_use_official_flag(monkeypatch):
    seen = []

    def capture(method, path, params, body=None):
        seen.append((method, path, params))
        if method == "POST" and path == "/1/threads":
            return {"id": str(10 + len([x for x in seen if x[0] == "POST"]))}
        if method == "GET":
            return {"status": "FINISHED"}
        return {"ok": True}

    result = m.publish(
        {
            "user_id": "1",
            "media_type": "CAROUSEL",
            "items": [
                {"media_type": "IMAGE", "image_url": "https://x/a"},
                {"media_type": "VIDEO", "video_url": "https://x/b"},
            ],
        },
        capture,
    )
    assert result["status"] == "published"
    assert all(item[2].get("is_carousel_item") is True for item in seen[:2])
    with pytest.raises(ValueError):
        m.publish(
            {
                "user_id": "1",
                "media_type": "CAROUSEL",
                "children": ["1", "2"],
                "items": [
                    {"media_type": "IMAGE", "image_url": "https://x/a"},
                    {"media_type": "IMAGE", "image_url": "https://x/b"},
                ],
            },
            capture,
        )
    with pytest.raises(ValueError):
        m.publish(
            {"user_id": "1", "media_type": "CAROUSEL", "items": [{"media_type": "IMAGE"}]}, capture
        )
    with pytest.raises(ValueError):
        m.publish(
            {
                "user_id": "1",
                "media_type": "CAROUSEL",
                "items": [{"media_type": "TEXT"}, {"media_type": "IMAGE"}],
            },
            capture,
        )
    with pytest.raises(ValueError):
        m.publish(
            {
                "user_id": "1",
                "media_type": "CAROUSEL",
                "items": [
                    {"media_type": "IMAGE", "image_url": "bad"},
                    {"media_type": "IMAGE", "image_url": "https://x/b"},
                ],
            },
            capture,
        )
    with pytest.raises(ValueError):
        m.publish({"user_id": "1", "text": "x", "alt_text": 1}, capture)
    with pytest.raises(ValueError, match="missing required"):
        m.dispatch("threads_publish_container", {"user_id": "1"}, capture)
