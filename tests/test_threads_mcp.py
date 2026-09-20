import asyncio
import base64
import hashlib
import hmac
import json
import multiprocessing
import os
import runpy
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

import faust_threads_mcp.threads_mcp as mod
from faust_threads_mcp.threads_mcp import (
    _approval_payload,
    _cursor,
    _page,
    _path,
    _token,
    account_insights,
    demographics,
    dispatch,
    latest_posts,
    post_insights,
    publish,
    reply,
    request,
)


def fake(method: str, path: str, params: dict, body: dict | None = None) -> dict:
    assert path.startswith("/")
    if path == "/me/threads":
        after = params.get("after")
        return {
            "data": [{"id": "1", "text": "hello"}] if not after else [{"id": "1"}, {"id": "2"}],
            "paging": {"cursors": {"after": "next"}} if not after else {"cursors": {}},
        }
    if path.endswith("/insights") and path != "/me/threads_insights":
        return {
            "data": [
                {"name": name, "period": "lifetime", "values": [{"value": 1}]}
                for name in ("views", "likes", "replies", "reposts", "quotes", "shares")
            ]
        }
    if path == "/me/threads_insights":
        return {
            "data": [
                {"name": name, "total_value": {"value": 2}} for name in params["metric"].split(",")
            ]
        }
    if method == "POST":
        return {"id": "99"}
    raise AssertionError(path)


def receipt(
    text: str,
    account: str = "1",
    reply_to: str = "",
    approval_id: str = "a1",
    expires_at: str | None = None,
) -> dict:
    value = {
        "approval_id": approval_id,
        "account_id": account,
        "reply_to_id": reply_to,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "expires_at": expires_at or (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "nonce": "n1",
    }
    value["signature"] = (
        base64.urlsafe_b64encode(
            hmac.new(b"secret", _approval_payload(value).encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )
    return value


def concurrent_publish_worker(args: tuple[dict, str]) -> str:
    value, ledger = args
    os.environ["THREADS_ENABLE_WRITES"] = "1"
    os.environ["THREADS_APPROVAL_SECRET"] = "secret"
    os.environ["THREADS_APPROVAL_LEDGER"] = ledger
    try:
        publish(value, fake)
    except RuntimeError as exc:
        return str(exc)
    return "published"


def test_reads_and_coverage() -> None:
    result = latest_posts({"limit": 3, "pages": 2}, fake)
    assert result["coverage"]["pages"] == 2
    assert result["data"] == [{"id": "1", "text": "hello"}, {"id": "2"}]
    assert len(post_insights({"post_id": "123"}, fake)["data"]) == 6
    result = account_insights(
        {"metrics": ["likes", "followers_count"], "since": 1, "until": 2}, fake
    )
    assert result["coverage"]["windows"]["likes"] == {"since": 1, "until": 2}
    assert result["coverage"]["windows"]["followers_count"]["since"] is None
    assert demographics({"breakdown": "country"}, fake)["coverage"]["breakdown"] == "country"


def test_writes_are_gated_and_receipts_are_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("THREADS_ENABLE_WRITES", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        publish({"user_id": "1", "text": "x", "approval_receipt": {"author_approved": True}}, fake)
    monkeypatch.setenv("THREADS_ENABLE_WRITES", "1")
    monkeypatch.setenv("THREADS_APPROVAL_SECRET", "secret")
    monkeypatch.setenv("THREADS_APPROVAL_LEDGER", str(tmp_path / "ledger.json"))
    with pytest.raises(RuntimeError, match="trusted"):
        publish({"user_id": "1", "text": "x", "approval_receipt": {"author_approved": True}}, fake)
    approved = {"user_id": "1", "text": "x", "approval_receipt": receipt("x")}
    assert publish(approved, fake)["status"] == "published"
    with pytest.raises(RuntimeError, match="already"):
        publish(approved, fake)
    with pytest.raises(RuntimeError, match="different text"):
        publish({**approved, "text": "y", "approval_receipt": receipt("x", approval_id="a2")}, fake)
    reply_args = {
        "user_id": "1",
        "reply_to_id": "2",
        "text": "reply",
        "approval_receipt": receipt("reply", reply_to="2", approval_id="a3"),
    }
    assert reply(reply_args, fake)["status"] == "published"
    assert json.loads((tmp_path / "ledger.json").read_text())[0]["approval_id"] == "a1"
    with pytest.raises(RuntimeError, match="different account"):
        publish(
            {"user_id": "2", "text": "x", "approval_receipt": receipt("x", approval_id="a4")}, fake
        )
    with pytest.raises(RuntimeError, match="expired"):
        publish(
            {
                "user_id": "1",
                "text": "old",
                "approval_receipt": receipt(
                    "old",
                    approval_id="a5",
                    expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                ),
            },
            fake,
        )
    with pytest.raises(RuntimeError, match="expiry is invalid"):
        publish(
            {
                "user_id": "1",
                "text": "bad-time",
                "approval_receipt": receipt("bad-time", approval_id="a5b", expires_at="not-a-time"),
            },
            fake,
        )
    bad_signature = receipt("bad", approval_id="a6")
    bad_signature["signature"] = "invalid"
    with pytest.raises(RuntimeError, match="signature"):
        publish({"user_id": "1", "text": "bad", "approval_receipt": bad_signature}, fake)
    invalid_encoding = receipt("encoding", approval_id="a6b")
    invalid_encoding["signature"] = "a"
    with pytest.raises(RuntimeError, match="signature"):
        publish({"user_id": "1", "text": "encoding", "approval_receipt": invalid_encoding}, fake)
    with pytest.raises(ValueError, match="1..500"):
        publish(
            {"user_id": "1", "text": "", "approval_receipt": receipt("", approval_id="a7")},
            fake,
            _guarded=True,
        )
    with pytest.raises(RuntimeError, match="valid id"):
        publish(
            {"user_id": "1", "text": "x", "approval_receipt": receipt("x", approval_id="a8")},
            lambda *_args: {},
        )
    (tmp_path / "ledger.json").write_text("{}")
    with pytest.raises(RuntimeError, match="ledger is invalid"):
        publish(
            {"user_id": "1", "text": "z", "approval_receipt": receipt("z", approval_id="a9")}, fake
        )
    today = datetime.now(UTC).isoformat()
    (tmp_path / "ledger.json").write_text(
        json.dumps(
            [{"approval_id": str(i), "account_id": "1", "used_at": today} for i in range(20)]
        )
    )
    with pytest.raises(RuntimeError, match="quota"):
        publish(
            {
                "user_id": "1",
                "text": "quota",
                "approval_receipt": receipt("quota", approval_id="a10"),
            },
            fake,
        )
    original_replace = os.replace
    os.replace = lambda *_args: (_ for _ in ()).throw(OSError("replace failed"))
    try:
        with pytest.raises(OSError):
            mod._write_ledger([])
    finally:
        os.replace = original_replace


def test_same_receipt_is_idempotent_across_processes(tmp_path: Path) -> None:
    ledger = str(tmp_path / "ledger.json")
    value = {"user_id": "1", "text": "once", "approval_receipt": receipt("once")}
    context = multiprocessing.get_context("spawn")
    with context.Pool(2) as pool:
        results = pool.map(concurrent_publish_worker, [(value, ledger)] * 2)
    assert sorted(results) == ["approval receipt has already been used", "published"]
    assert len(json.loads(Path(ledger).read_text())) == 1


def test_posix_ledger_lock_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(fd: int, operation: int) -> None:
            calls.append((fd, operation))

    monkeypatch.setitem(sys.modules, "fcntl", FakeFcntl)
    monkeypatch.setattr(mod.sys, "platform", "linux")
    monkeypatch.setenv("THREADS_APPROVAL_LEDGER", str(tmp_path / "ledger.json"))
    with mod._ledger_file_lock():
        pass
    assert [operation for _, operation in calls] == [FakeFcntl.LOCK_EX, FakeFcntl.LOCK_UN]


def test_internal_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        _token()
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "token")
    assert _token() == "token"
    assert _path("/me/threads") == "/me/threads"
    with pytest.raises(ValueError):
        _path("https://bad")
    with pytest.raises(ValueError):
        _path("/../bad")
    assert _cursor(None) is None
    assert _cursor("after") == "after"
    with pytest.raises(ValueError):
        _cursor(1)
    with pytest.raises(ValueError):
        _cursor("x" * 513)
    with pytest.raises(ValueError):
        _cursor("access_token=bad")
    with pytest.raises(ValueError):
        _page("/x", {}, pages=0, limit=1, get=fake)
    assert (
        _page("/x", {}, pages=1, limit=2, get=lambda *_: {"data": "bad"})["coverage"]["complete"]
        is None
    )
    malformed = _page("/x", {}, pages=1, limit=2, get=lambda *_: {"data": [{"id": "1"}]})
    assert malformed["coverage"]["complete"] is None
    cycled = _page(
        "/x",
        {},
        pages=2,
        limit=5,
        get=lambda *_: {"data": [{"id": "1"}], "paging": {"cursors": {"after": "a"}}},
    )
    assert cycled["coverage"]["complete"] is None
    assert cycled["coverage"]["errors"]
    assert (
        _page(
            "/x",
            {},
            pages=2,
            limit=5,
            get=lambda *_: {"data": [], "paging": {"cursors": {"after": None}}},
        )["coverage"]["complete"]
        is True
    )
    assert _page(
        "/x", {}, pages=1, limit=5, get=lambda *_: {"data": [], "paging": {"cursors": "bad"}}
    )["coverage"]["warnings"]
    assert (
        _page("/x", {}, pages=1, limit=1, get=lambda *_: {"data": [{"id": "1"}, {"id": "2"}]})[
            "coverage"
        ]["more_available"]
        is True
    )
    assert (
        _page(
            "/x",
            {},
            pages=1,
            limit=5,
            get=lambda *_: {"data": [], "paging": {"cursors": {"after": "a"}}},
        )["coverage"]["complete"]
        is None
    )
    assert (
        _page(
            "/x",
            {},
            pages=1,
            limit=5,
            get=lambda *_: {"data": [{"text": "no id"}], "paging": {"cursors": {"after": None}}},
        )["coverage"]["rows"]
        == 1
    )
    with pytest.raises(ValueError):
        _page(
            "/x",
            {},
            pages=1,
            limit=5,
            get=lambda *_: {"data": [], "paging": {"cursors": {"after": "bad://cursor"}}},
        )
    monkeypatch.setenv("THREADS_ENABLE_WRITES", "1")
    monkeypatch.setenv("THREADS_APPROVAL_SECRET", "secret")
    monkeypatch.setenv("THREADS_APPROVAL_LEDGER", str(Path.cwd() / "unused-ledger.json"))
    with pytest.raises(ValueError):
        publish({"user_id": "bad", "text": "x", "approval_receipt": {}}, fake)
    with pytest.raises(ValueError):
        reply({"user_id": "1", "reply_to_id": "bad", "text": "x", "approval_receipt": {}}, fake)
    with pytest.raises(KeyError):
        dispatch("unknown", {}, fake)
    with pytest.raises(ValueError):
        latest_posts({"pages": 0}, fake)
    with pytest.raises(ValueError):
        post_insights({"post_id": "bad"}, fake)
    with pytest.raises(ValueError):
        demographics({"breakdown": "bad"}, fake)
    with pytest.raises(ValueError):
        post_insights({"post_id": "bad"}, fake)
    with pytest.raises(ValueError):
        account_insights({"metrics": ["bad"]}, fake)
    assert (
        account_insights({"metrics": ["followers_count"]}, fake)["coverage"]["windows"][
            "followers_count"
        ]["until"]
        is None
    )
    assert (
        account_insights({"metrics": ["likes"]}, fake)["coverage"]["windows"]["likes"]["since"]
        is None
    )


def test_request_rejects_redirect_and_errors_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "secret-token")

    class RedirectOpener:
        def open(self, *_args, **_kwargs):
            raise RuntimeError("Threads API redirect rejected")

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: RedirectOpener())
    with pytest.raises(RuntimeError, match="redirect rejected"):
        request("GET", "/me/threads", {})
    with pytest.raises(RuntimeError, match="redirect rejected"):
        mod._RejectRedirects().redirect_request(
            urllib.request.Request("https://example.invalid"),
            None,
            302,
            "",
            {},
            "https://other.invalid",
        )

    class Response:
        def __init__(self, raw: bytes):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.raw

    class Opener:
        def __init__(self, value):
            self.value = value

        def open(self, *_args, **_kwargs):
            if isinstance(self.value, BaseException):
                raise self.value
            return Response(self.value)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener(b'{"ok": true}'))
    assert request("GET", "/me/threads", {"x": 1}) == {"ok": True}
    for error, message in (
        (urllib.error.HTTPError("u", 500, "", {}, None), "HTTP 500"),
        (urllib.error.URLError("x"), "connection failed"),
        (TimeoutError(), "connection failed"),
    ):
        monkeypatch.setattr(
            "urllib.request.build_opener", lambda *_args, error=error: Opener(error)
        )
        with pytest.raises(RuntimeError, match=message):
            request("GET", "/me/threads", {})
    for raw, message in ((b"x", "non-JSON"), (b'{"error": {}}', "returned an error")):
        monkeypatch.setattr("urllib.request.build_opener", lambda *_args, raw=raw: Opener(raw))
        with pytest.raises(RuntimeError, match=message):
            request("GET", "/me/threads", {})
    monkeypatch.setattr(
        "urllib.request.build_opener", lambda *_args: Opener(b"x" * (mod.MAX_BODY + 1))
    )
    with pytest.raises(RuntimeError, match="size limit"):
        request("GET", "/me/threads", {})


def test_transport_functions() -> None:
    server = mod.build_server()
    assert server is not None

    async def exercise() -> None:
        class FakeServer:
            def get_capabilities(self, **_kwargs):
                return {}

            async def run(self, *_args):
                return None

        class Context:
            async def __aenter__(self):
                return "read", "write"

            async def __aexit__(self, *_args):
                return False

        original_build = mod.build_server
        original_stdio = mod.stdio_server
        mod.build_server = lambda: FakeServer()
        mod.stdio_server = lambda: Context()
        try:
            await mod.run()
        finally:
            mod.build_server = original_build
            mod.stdio_server = original_stdio

    asyncio.run(exercise())

    async def handlers() -> None:
        listed = await server.request_handlers[ListToolsRequest](ListToolsRequest())
        assert len(listed.root.tools) == 6
        called = await server.request_handlers[CallToolRequest](
            CallToolRequest(params=CallToolRequestParams(name="unknown", arguments={}))
        )
        assert called.root.isError is True
        original_dispatch = mod.dispatch
        mod.dispatch = lambda _name, _arguments: {"ok": True}
        try:
            successful = await server.request_handlers[CallToolRequest](
                CallToolRequest(params=CallToolRequestParams(name="known", arguments={}))
            )
            assert successful.root.isError is False
        finally:
            mod.dispatch = original_dispatch

    asyncio.run(handlers())
    original_asyncio_run = mod.asyncio.run
    called = []
    mod.asyncio.run = lambda coro: (called.append(True), coro.close())[0]
    try:
        mod.main()
    finally:
        mod.asyncio.run = original_asyncio_run
    assert called
    mod.asyncio.run = lambda coro: coro.close()
    try:
        runpy.run_path(str(Path(mod.__file__)), run_name="__main__")
    finally:
        mod.asyncio.run = original_asyncio_run


def test_sdk_handshake_and_call() -> None:
    async def run() -> None:
        server = Path(__file__).parents[1] / "src" / "faust_threads_mcp" / "threads_mcp.py"
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(server)],
            env={**os.environ, "THREADS_ACCESS_TOKEN": "placeholder"},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "faust-threads-mcp"
            tools = await session.list_tools()
            assert len(tools.tools) == 6
            result = await session.call_tool("threads_account_insights", {"metrics": ["likes"]})
            assert result.isError is True

    asyncio.run(run())
