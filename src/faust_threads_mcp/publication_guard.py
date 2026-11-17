"""Verify an exact write approval and record its outcome before allowing another write."""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from typing import Any

Json = dict[str, Any]
Requester = Callable[..., Json]
_RECEIPT_FIELDS = {"approval_id", "intent_sha256", "expires_at", "signature"}
RECEIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(_RECEIPT_FIELDS),
    "properties": {
        "approval_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{16,128}$"},
        "intent_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "expires_at": {"type": "integer"},
        "signature": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    },
}
_KNOWN = {"created", "published", "hidden", "visible"}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _approval(name: str, args: Json, receipt: Any) -> tuple[str, str]:
    secret = os.environ.get("THREADS_APPROVAL_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("a trusted approval issuer must be configured")
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError("an exact approval receipt is required")
    identifier, signature, expires = (
        receipt["approval_id"],
        receipt["signature"],
        receipt["expires_at"],
    )
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", identifier):
        raise ValueError("invalid approval identifier")
    if type(expires) is not int or expires <= time.time():
        raise ValueError("approval expired or has an invalid expiry")
    digest = hashlib.sha256(_canonical([name, args])).hexdigest()
    if receipt["intent_sha256"] != digest:
        raise ValueError("approval does not match the exact action")
    expected = hmac.new(
        secret.encode("utf-8"),
        _canonical(["faust-threads-write-v1", identifier, digest, expires]),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature):
        raise ValueError("invalid approval signature")
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid approval signature")
    return identifier, digest


@contextmanager
def _journal() -> Iterator[sqlite3.Connection]:
    path = os.environ.get("THREADS_PUBLICATION_JOURNAL", "")
    if not path or path == ":memory:":
        raise RuntimeError("a persistent publication journal is required")
    try:
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS attempts (
                    approval_id TEXT PRIMARY KEY,
                    intent_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'known', 'held', 'rejected')),
                    outcome TEXT
                )"""
            )
            yield db
    except sqlite3.Error:
        raise RuntimeError("publication journal unavailable; reconcile the last action") from None


def _reserve(identifier: str, digest: str, deduplicate: bool) -> Json | None:
    with _journal() as db:
        previous = db.execute(
            "SELECT intent_sha256, state, outcome FROM attempts WHERE approval_id=?",
            (identifier,),
        ).fetchone()
        if previous is not None:
            if previous[0] != digest:
                raise RuntimeError("approval identifier already bound to another action")
            if previous[1] != "known":
                raise RuntimeError("prior attempt has no confirmed outcome; reconcile it")
            return {"status": "already_recorded", "outcome": json.loads(previous[2])}
        if db.execute(
            "SELECT 1 FROM attempts WHERE state IN ('pending', 'held') LIMIT 1"
        ).fetchone():
            raise RuntimeError("publication queue is held; reconcile the prior action")
        if (
            deduplicate
            and db.execute(
                "SELECT 1 FROM attempts WHERE intent_sha256=? AND state='known' LIMIT 1",
                (digest,),
            ).fetchone()
        ):
            raise RuntimeError("this exact publication is already recorded")
        db.execute(
            "INSERT INTO attempts(approval_id, intent_sha256, state) VALUES (?, ?, 'pending')",
            (identifier, digest),
        )
    return None


def _finish(identifier: str, state: str, outcome: Json) -> None:
    with _journal() as db:
        updated = db.execute(
            "UPDATE attempts SET state=?, outcome=? WHERE approval_id=? AND state='pending'",
            (state, json.dumps(outcome, sort_keys=True), identifier),
        )
        if updated.rowcount != 1:
            raise RuntimeError("publication journal changed; reconcile the last action")


def run_approved_write(
    name: str, args: Json, receipt: Any, handler: Callable[[Json, Requester], Json], send: Requester
) -> Json:
    if os.environ.get("THREADS_ENABLE_WRITES") != "1":
        raise RuntimeError("Threads writes are disabled")
    identifier, digest = _approval(name, args, receipt)
    previous = _reserve(identifier, digest, name != "threads_manage_reply")
    if previous is not None:
        return previous
    attempted = False

    def tracked(method: str, path: str, params: Json, body: Json | None = None) -> Json:
        nonlocal attempted
        if method != "GET":
            attempted = True
        return send(method, path, params, body)

    try:
        result = handler(args, tracked)
    except Exception:
        if attempted:
            _finish(identifier, "held", {"status": "unknown_outcome"})
            raise RuntimeError("write outcome unknown; reconcile the recorded action") from None
        _finish(identifier, "rejected", {"status": "preflight_failed"})
        raise
    status = result.get("status")
    state = "known" if status in _KNOWN else "held"
    # Save proof and identifiers only. Provider response text can contain private data.
    outcome = {"status": status}
    for key in ("container", "result"):
        value = result.get(key)
        item_id = value.get("id") if isinstance(value, dict) else None
        if isinstance(item_id, str) and re.fullmatch(r"[0-9]+", item_id):
            outcome[key] = {"id": item_id}
    _finish(identifier, state, outcome)
    if state == "held":
        return {**result, "queue": "held"}
    return result
