import hashlib
import hmac
import json
import time

import pytest


@pytest.fixture
def signed_write(tmp_path, monkeypatch):
    secret = "synthetic-fixture-signing-key-for-tests"
    monkeypatch.setenv("THREADS_APPROVAL_SECRET", secret)
    monkeypatch.setenv("THREADS_ENABLE_WRITES", "1")
    monkeypatch.setenv("THREADS_PUBLICATION_JOURNAL", str(tmp_path / "queue.sqlite"))

    def encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    def sign(name, args, identifier="test_exact_approval_1", expiry=None):
        expiry = int(time.time()) + 60 if expiry is None else expiry
        intent = hashlib.sha256(encode([name, args])).hexdigest()
        signature = hmac.new(
            secret.encode(),
            encode(["faust-threads-write-v1", identifier, intent, expiry]),
            hashlib.sha256,
        ).hexdigest()
        return {
            **args,
            "approval_receipt": {
                "approval_id": identifier,
                "intent_sha256": intent,
                "expires_at": expiry,
                "signature": signature,
            },
        }

    return sign
