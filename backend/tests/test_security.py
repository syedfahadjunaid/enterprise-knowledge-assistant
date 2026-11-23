"""Token verification and principals (no Docker needed)."""

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.security import AuthError, User
from tests.conftest import ALICE, BOB, DAVE, ERIN, TEST_VERIFIER, make_token


def test_valid_token_becomes_a_user_with_roles_and_groups() -> None:
    user = TEST_VERIFIER.verify(make_token(BOB))
    assert user.sub == "sub-bob"
    assert user.username == "bob"
    assert user.roles == frozenset({"kb-editor"})
    assert user.groups == ("/engineering/platform",)
    assert user.can_upload
    assert not user.is_admin


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"expires_in": -120},  # expired (beyond the 30 s leeway)
        {"audience": "some-other-api"},
        {"issuer": "http://evil.example/realms/eka"},
        {"key": rsa.generate_private_key(public_exponent=65537, key_size=2048)},  # wrong key
    ],
    ids=["expired", "wrong-audience", "wrong-issuer", "wrong-signing-key"],
)
def test_invalid_tokens_are_rejected(token_kwargs: dict) -> None:
    with pytest.raises(AuthError):
        TEST_VERIFIER.verify(make_token(BOB, **token_kwargs))


def test_unsigned_token_is_rejected() -> None:
    import jwt

    unsigned = jwt.encode({"sub": "x", "aud": "eka-api"}, key=None, algorithm="none")
    with pytest.raises(AuthError):
        TEST_VERIFIER.verify(unsigned)


def test_subgroup_members_get_every_parent_group() -> None:
    user = TEST_VERIFIER.verify(make_token(BOB))
    assert user.principals == (
        "user:sub-bob",
        "group:/engineering",
        "group:/engineering/platform",
        "everyone",
    )


def test_parent_group_members_do_not_get_subgroups() -> None:
    user = TEST_VERIFIER.verify(make_token(DAVE))
    assert "group:/engineering" in user.principals
    assert "group:/engineering/platform" not in user.principals


def test_user_without_groups_still_has_self_and_everyone() -> None:
    user = TEST_VERIFIER.verify(make_token(ERIN))
    assert user.principals == ("user:sub-erin", "everyone")
    assert not user.can_upload


def test_only_admins_may_share_with_everyone() -> None:
    admin = TEST_VERIFIER.verify(make_token(ALICE))
    editor = TEST_VERIFIER.verify(make_token(BOB))
    assert admin.share_targets == ("everyone", "group:/hr")
    assert editor.share_targets == ("group:/engineering", "group:/engineering/platform")


def test_malformed_group_claims_are_ignored() -> None:
    user = User("s", "u", "u", frozenset(), groups=("/ok", "/a//b/"))
    assert user.group_principals == ("group:/a", "group:/a/b", "group:/ok")
