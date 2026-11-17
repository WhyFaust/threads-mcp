import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

import faust_threads_mcp.publication_guard as guard
import faust_threads_mcp.threads_mcp as m

PUBLISH = "threads_publish_post"
CONTAINER = "threads_publish_container"
POST = {"user_id": "1", "text": "Exact fixture"}


def unexpected_request(*args, **kwargs):
    raise AssertionError("unexpected request to provider")


@pytest.mark.parametrize(
    ("variable", "value", "error"),
    [
        ("THREADS_ENABLE_WRITES", "", "writes are disabled"),
        ("THREADS_APPROVAL_SECRET", "", "trusted approval issuer"),
        ("THREADS_PUBLICATION_JOURNAL", "", "persistent publication journal"),
        ("THREADS_PUBLICATION_JOURNAL", ":memory:", "persistent publication journal"),
    ],
)
def test_missing_safety_configuration_refuses_write(
    signed_write, monkeypatch, variable, value, error
):
    monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError, match=error):
        m.dispatch(PUBLISH, signed_write(PUBLISH, POST), unexpected_request)


@pytest.mark.parametrize(
    ("replacement", "error"),
    [
        (None, "exact approval receipt"),
        ({}, "exact approval receipt"),
        ({"approval_id": 1}, "invalid approval identifier"),
        ({"approval_id": "invalid!"}, "invalid approval identifier"),
        ({"expires_at": False}, "invalid expiry"),
        ({"expires_at": 1}, "approval expired"),
        ({"intent_sha256": "0" * 64}, "exact action"),
        ({"signature": 1}, "invalid approval signature"),
        ({"signature": "nochars"}, "invalid approval signature"),
        ({"signature": "0" * 64}, "invalid approval signature"),
    ],
)
def test_invalid_receipt_never_calls_provider(signed_write, replacement, error):
    args = signed_write(PUBLISH, POST)
    if replacement:
        args["approval_receipt"].update(replacement)
    else:
        args["approval_receipt"] = replacement
    with pytest.raises(ValueError, match=error):
        m.dispatch(PUBLISH, args, unexpected_request)


def test_confirmed_receipt_recorded_once_and_bound_to_full_action(signed_write, monkeypatch):
    calls = []

    def send(method, path, params, body=None, *, use_cache=True):
        calls.append((method, path, use_cache))
        if path.endswith("/threads"):
            return {"id": "91"}
        if method == "GET":
            return {"id": "91", "status": "FINISHED"}
        return {"id": "92", "private_field": "test response detail"}

    monkeypatch.setattr(m, "request", send)
    args = signed_write(PUBLISH, POST)
    assert m.dispatch(PUBLISH, args, send)["status"] == "published"
    assert calls == [
        ("POST", "/1/threads", False),
        ("GET", "/91", False),
        ("POST", "/1/threads_publish", False),
    ]
    assert m.dispatch(PUBLISH, args, unexpected_request) == {
        "status": "already_recorded",
        "outcome": {"status": "published", "container": {"id": "91"}, "result": {"id": "92"}},
    }
    with pytest.raises(RuntimeError, match="already recorded"):
        m.dispatch(PUBLISH, signed_write(PUBLISH, POST, "test_second_approval"), unexpected_request)
    with pytest.raises(RuntimeError, match="another action"):
        m.dispatch(
            PUBLISH, signed_write(PUBLISH, {**POST, "topic_tag": "changed"}), unexpected_request
        )
    with pytest.raises(ValueError, match="exact action"):
        m.dispatch(PUBLISH, {**args, "text": "changed"}, unexpected_request)


@pytest.mark.parametrize("remote_reply", ["lost_ack", {"ok": True}, {"id": "invalid"}])
def test_publish_outcome_requires_exact_id_and_freezes_further_writes(signed_write, remote_reply):
    calls = []
    existing = {"user_id": "1", "creation_id": "91"}
    args = signed_write(CONTAINER, existing)

    def send(method, path, params, body=None):
        calls.append(method)
        if method == "GET":
            return {"id": "91", "status": "FINISHED"}
        if remote_reply == "lost_ack":
            raise RuntimeError("synthetic lost response")
        return remote_reply

    assert m.dispatch(CONTAINER, args, send) == {
        "status": "unknown_publish",
        "container": {"id": "91"},
        "queue": "held",
    }
    assert calls == ["GET", "POST"]
    with pytest.raises(RuntimeError, match="no confirmed outcome"):
        m.dispatch(CONTAINER, args, unexpected_request)
    with pytest.raises(RuntimeError, match="queue is held"):
        m.dispatch(PUBLISH, signed_write(PUBLISH, POST, "test_second_approval"), unexpected_request)


@pytest.mark.parametrize("response", [{"id": "999", "status": "FINISHED"}, "read_failure"])
def test_mismatched_or_unavailable_container_refuses_publish(response):
    def send(method, path, params, body=None):
        assert method == "GET"
        if response == "read_failure":
            raise RuntimeError("synthetic read error")
        return response

    assert m.publish_container({"user_id": "1", "creation_id": "91"}, send) == {
        "status": "unverified_container",
        "container": {"id": "91"},
    }


def test_creator_ack_loss_falls_closed_and_masks_transport_error(signed_write):
    def lost(*args):
        raise RuntimeError("risk detail that must not be returned")

    with pytest.raises(RuntimeError, match="^write outcome unknown"):
        m.dispatch(PUBLISH, signed_write(PUBLISH, POST), lost)
    with pytest.raises(RuntimeError, match="queue is held"):
        m.dispatch(PUBLISH, signed_write(PUBLISH, POST, "test_second_approval"), unexpected_request)


def test_validating_all_carousel_items_prevents_partial_creation(signed_write):
    args = {
        "user_id": "1",
        "media_type": "CAROUSEL",
        "items": [
            {"media_type": "IMAGE", "image_url": "https://example.test/image"},
            {"media_type": "VIDEO", "video_url": "invalid"},
        ],
    }
    with pytest.raises(ValueError, match="absolute HTTP"):
        m.dispatch(PUBLISH, signed_write(PUBLISH, args), unexpected_request)
    args = signed_write(PUBLISH, {**POST, "stage_only": True}, "test_another_approval")
    result = m.dispatch(PUBLISH, args, lambda *a: {"id": "91"})
    assert result["status"] == "created"


def test_journal_rejects_a_second_attempt_while_a_write_is_in_flight(signed_write):
    entered = Event()
    release = Event()

    def send(method, path, params, body=None):
        entered.set()
        assert release.wait(5)
        return {"id": "91"}

    args = signed_write(PUBLISH, {**POST, "stage_only": True})
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(m.dispatch, PUBLISH, args, send)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match="queue is held"):
                m.dispatch(
                    PUBLISH,
                    signed_write(PUBLISH, {**POST, "text": "another"}, "test_second_approval"),
                    unexpected_request,
                )
        finally:
            release.set()
        assert first.result(timeout=5)["status"] == "created"


def test_unavailable_journal_and_missing_pending_record_fail_closed(
    signed_write, tmp_path, monkeypatch
):
    bad_file = tmp_path / "bad.sqlite"
    bad_file.write_bytes(b"not a sqlite journal")
    with monkeypatch.context() as env:
        env.setenv("THREADS_PUBLICATION_JOURNAL", str(bad_file))
        with pytest.raises(RuntimeError, match="journal unavailable"):
            m.dispatch(PUBLISH, signed_write(PUBLISH, POST), unexpected_request)
    with pytest.raises(RuntimeError, match="journal changed"):
        guard._finish("no_such_operation", "held", {"status": "unknown"})


def test_moderation_uses_approved_write_without_deduplicating_valid_new_receipts(signed_write):
    name = "threads_manage_reply"
    for approval in ("moderation_approval_1", "moderation_approval_2"):
        args = signed_write(name, {"reply_id": "1", "hide": False}, approval)
        assert m.dispatch(name, args, lambda *a: {"ok": True})["status"] == "visible"


def test_mcp_returns_error_for_held_publication(signed_write, monkeypatch):
    def send(method, path, params, body=None):
        if path == "/me":
            return {"id": "1"}
        if method == "GET":
            return {"id": "91", "status": "FINISHED"}
        return {}

    dispatch = m.dispatch
    monkeypatch.setattr(m, "dispatch", lambda name, args: dispatch(name, args, send))

    async def run():
        handler = m.build_server().request_handlers[CallToolRequest]
        profile = await handler(
            CallToolRequest(params=CallToolRequestParams(name="threads_get_profile", arguments={}))
        )
        assert not profile.root.isError
        assert profile.root.structuredContent["data"]["id"] == "1"
        args = signed_write(CONTAINER, {"user_id": "1", "creation_id": "91"})
        attempt = await handler(
            CallToolRequest(params=CallToolRequestParams(name=CONTAINER, arguments=args))
        )
        assert attempt.root.isError
        assert attempt.root.structuredContent["status"] == "unknown_publish"
        assert attempt.root.structuredContent["queue"] == "held"

    asyncio.run(run())
