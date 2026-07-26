"""Webhook signature verification (X-Hub-Signature-256)."""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(secret: str, payload: bytes, signature_header: str | None) -> bool:
    """Validate GitHub's ``X-Hub-Signature-256`` header against the raw body.

    Uses a constant-time comparison to avoid timing attacks.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)
