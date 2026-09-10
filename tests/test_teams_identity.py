"""Sender identification and sign-in link binding."""

import time
from types import SimpleNamespace

import pytest

from atlassian_agent.teams.identity import (
    InvalidState,
    TeamsUser,
    issue_sign_in_state,
    user_from_activity,
    verify_sign_in_state,
)

SECRET = "a-test-signing-secret"


def activity(**kwargs):
    sender = SimpleNamespace(
        id=kwargs.pop("id", "29:abc"),
        name=kwargs.pop("name", "Alice Smith"),
        aad_object_id=kwargs.pop("aad_object_id", "oid-alice"),
        tenant_id=kwargs.pop("tenant_id", "tenant-1"),
    )
    return SimpleNamespace(
        from_property=sender,
        channel_id=kwargs.pop("channel_id", "msteams"),
        channel_data=kwargs.pop("channel_data", None),
    )


def test_entra_object_id_is_preferred_because_it_is_stable():
    user = user_from_activity(activity())
    assert user.key == "aad:tenant-1:oid-alice"
    assert user.display_name == "Alice Smith"


def test_same_person_in_a_different_chat_is_the_same_key():
    """A grant made in a 1:1 chat must be found again in a channel."""
    a = user_from_activity(activity(id="29:one", channel_id="msteams"))
    b = user_from_activity(activity(id="29:two", channel_id="msteams"))
    assert a.key == b.key


def test_different_people_never_collide():
    a = user_from_activity(activity(aad_object_id="oid-alice"))
    b = user_from_activity(activity(aad_object_id="oid-bob"))
    assert a.key != b.key


def test_same_object_id_in_another_tenant_is_a_different_user():
    a = user_from_activity(activity(tenant_id="tenant-1"))
    b = user_from_activity(activity(tenant_id="tenant-2"))
    assert a.key != b.key


def test_tenant_falls_back_to_channel_data():
    act = activity(tenant_id=None, channel_data={"tenant": {"id": "tenant-from-channel"}})
    assert user_from_activity(act).tenant_id == "tenant-from-channel"


def test_channel_scoped_id_is_the_fallback_without_an_object_id():
    user = user_from_activity(activity(aad_object_id=None))
    assert user.key == "channel:msteams:29:abc"


def test_a_sender_we_cannot_identify_is_an_error():
    with pytest.raises(ValueError):
        user_from_activity(SimpleNamespace(from_property=None))


# -- sign-in state ----------------------------------------------------------


@pytest.fixture
def alice():
    return TeamsUser(key="aad:t1:alice", display_name="Alice", channel_user_id="29:a")


def test_round_trip(alice):
    payload = verify_sign_in_state(SECRET, issue_sign_in_state(SECRET, alice, ttl_seconds=300))
    assert payload["k"] == alice.key
    assert payload["name"] == "Alice"
    assert payload["exp"] > time.time()


def test_each_link_has_its_own_nonce(alice):
    first = verify_sign_in_state(SECRET, issue_sign_in_state(SECRET, alice, ttl_seconds=300))
    second = verify_sign_in_state(SECRET, issue_sign_in_state(SECRET, alice, ttl_seconds=300))
    assert first["n"] != second["n"]


def test_a_token_signed_with_another_secret_is_rejected(alice):
    forged = issue_sign_in_state("attacker-secret", alice, ttl_seconds=300)
    with pytest.raises(InvalidState, match="signature"):
        verify_sign_in_state(SECRET, forged)


def test_tampering_with_the_identity_is_rejected(alice):
    """The whole point: you cannot repoint someone else's link at yourself."""
    import base64
    import json

    token = issue_sign_in_state(SECRET, alice, ttl_seconds=300)
    body, signature = token.split(".", 1)
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    payload["k"] = "aad:t1:mallory"
    swapped = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    with pytest.raises(InvalidState, match="signature"):
        verify_sign_in_state(SECRET, f"{swapped}.{signature}")


def test_expired_links_are_rejected(alice):
    with pytest.raises(InvalidState, match="expired"):
        verify_sign_in_state(SECRET, issue_sign_in_state(SECRET, alice, ttl_seconds=-1))


@pytest.mark.parametrize("token", ["", "nodot", "a.b.c", "!!!.???"])
def test_malformed_tokens_are_rejected(token):
    with pytest.raises(InvalidState):
        verify_sign_in_state(SECRET, token)
