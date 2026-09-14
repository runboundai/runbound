"""Verifying a delivery from runbound's webhook adapter, receiver-side. Shown on: Alerts and callbacks."""

import hashlib
import hmac
import time

# docs: verify-webhook
from runbound import verify_webhook_signature

SECRET = "whsec_test"

def handle_delivery(headers: dict, body: bytes) -> bool:
    return verify_webhook_signature(
        SECRET,
        headers["X-Runbound-Timestamp"],
        body,
        headers["X-Runbound-Signature"],
    )
# /docs


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


body = b'{"event": "budget"}'
now = str(time.time())
good_headers = {"X-Runbound-Timestamp": now, "X-Runbound-Signature": _sign(SECRET, now, body)}
assert handle_delivery(good_headers, body) is True

touched_body = body + b"x"
assert handle_delivery(good_headers, touched_body) is False

old = str(time.time() - 400)  # more than the five-minute tolerance
old_headers = {"X-Runbound-Timestamp": old, "X-Runbound-Signature": _sign(SECRET, old, body)}
assert handle_delivery(old_headers, body) is False
