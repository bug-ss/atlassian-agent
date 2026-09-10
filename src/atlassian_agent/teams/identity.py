"""Who sent the message, and how a sign-in link is bound to them.

Two jobs, both security-critical:

* Derive a *stable* key for the sender, so one person's Atlassian grant is
  found again on their next message and never confused with anyone else's.
* Mint a sign-in link that only works for that person, once, and briefly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any


class InvalidState(Exception):
    """The sign-in link was tampered with, expired, or already used."""


@dataclass(frozen=True)
class TeamsUser:
    """The sender of a Teams activity, reduced to what we need."""

    key: str
    display_name: str
    channel_user_id: str
    tenant_id: str | None = None
    aad_object_id: str | None = None


def user_from_activity(activity: Any) -> TeamsUser:
    """Identify the sender.

    Prefer the Entra object ID: it is stable for a person across chats,
    channels and app reinstalls. The channel-scoped `from.id` is the fallback
    for contexts that do not carry one - it still identifies one user, but it
    can change, which shows up as being asked to reconnect.
    """
    sender = getattr(activity, "from_property", None)
    if sender is None or not getattr(sender, "id", None):
        raise ValueError("activity has no sender to identify")

    tenant_id = getattr(sender, "tenant_id", None) or _tenant_from_channel_data(activity)
    aad_object_id = getattr(sender, "aad_object_id", None)

    if aad_object_id:
        key = f"aad:{tenant_id or 'unknown'}:{aad_object_id}"
    else:
        key = f"channel:{getattr(activity, 'channel_id', 'unknown')}:{sender.id}"

    return TeamsUser(
        key=key,
        display_name=getattr(sender, "name", None) or "there",
        channel_user_id=sender.id,
        tenant_id=tenant_id,
        aad_object_id=aad_object_id,
    )


def _tenant_from_channel_data(activity: Any) -> str | None:
    channel_data = getattr(activity, "channel_data", None) or {}
    if isinstance(channel_data, dict):
        tenant = channel_data.get("tenant") or {}
        if isinstance(tenant, dict):
            return tenant.get("id")
    return None


# ---------------------------------------------------------------------------
# Sign-in state
# ---------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_sign_in_state(secret: str, user: TeamsUser, *, ttl_seconds: float) -> str:
    """Mint a signed, expiring, single-use token binding a login to one user.

    The token travels through the browser and comes back on the callback, so it
    is what stops someone completing a sign-in against another person's Teams
    identity. It carries a nonce the store marks as spent on first use.
    """
    payload = {
        "k": user.key,
        "n": secrets.token_urlsafe(16),
        "exp": time.time() + ttl_seconds,
        "name": user.display_name,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


def verify_sign_in_state(secret: str, token: str) -> dict[str, Any]:
    """Check signature and expiry. Single-use is enforced by the caller."""
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise InvalidState("Malformed sign-in token.") from exc

    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    try:
        provided = _b64decode(signature)
    except Exception as exc:
        raise InvalidState("Malformed sign-in token.") from exc
    if not hmac.compare_digest(expected, provided):
        raise InvalidState("Sign-in token failed its signature check.")

    try:
        payload = json.loads(_b64decode(body))
    except Exception as exc:
        raise InvalidState("Malformed sign-in token.") from exc

    if not isinstance(payload, dict) or "k" not in payload or "n" not in payload:
        raise InvalidState("Sign-in token is missing required fields.")
    if float(payload.get("exp", 0)) < time.time():
        raise InvalidState("This sign-in link has expired. Ask the bot for a new one.")
    return payload
