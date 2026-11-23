"""Who is calling: JWT verification, the ``User`` model and the principals used for access control.

Every request carries a Keycloak access token. We verify it ourselves (signature, expiry,
issuer, audience) and build a ``User`` from its claims. The user's *principals* are the keys
that document grants are matched against, inside the database (see ``app.db``):

    user:<sub>                  the user themselves (document owners get this grant)
    group:/engineering          each Keycloak group the user is in ...
    group:/engineering/platform ... including every parent group of a subgroup
    everyone                    every signed-in user

Group membership comes only from the signed token, never from the request.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger(__name__)

KeyResolver = Callable[[str], Any]

ROLE_ADMIN = "kb-admin"
ROLE_EDITOR = "kb-editor"
EVERYONE = "everyone"


@dataclass(frozen=True)
class User:
    """The verified caller."""

    sub: str
    username: str
    name: str
    roles: frozenset[str]
    groups: tuple[str, ...]  # full Keycloak group paths, e.g. "/engineering/platform"

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def can_upload(self) -> bool:
        return bool(self.roles & {ROLE_ADMIN, ROLE_EDITOR})

    @property
    def group_principals(self) -> tuple[str, ...]:
        """``group:`` principals for every group the user is in, plus all their parent groups.

        A document shared with /engineering is meant for everyone in engineering, including the
        members of /engineering/platform. Sharing never flows the other way: a document shared
        with /engineering/platform is not visible to someone who is only in /engineering.
        """
        paths: set[str] = set()
        for group in self.groups:
            parts = [p for p in group.split("/") if p]
            for depth in range(1, len(parts) + 1):
                paths.add("/" + "/".join(parts[:depth]))
        return tuple(f"group:{p}" for p in sorted(paths))

    @property
    def principals(self) -> tuple[str, ...]:
        return (f"user:{self.sub}", *self.group_principals, EVERYONE)

    @property
    def share_targets(self) -> tuple[str, ...]:
        """Principals this user may share a document with (besides themselves)."""
        targets = self.group_principals
        return (EVERYONE, *targets) if self.is_admin else targets


class AuthError(Exception):
    pass


class AuthUnavailableError(Exception):
    """The identity provider's keys could not be fetched (an outage, not a bad token)."""


class TokenVerifier:
    def __init__(self, issuer: str, audience: str, key_resolver: KeyResolver) -> None:
        self._issuer = issuer
        self._audience = audience
        self._key_resolver = key_resolver

    def verify(self, token: str) -> User:
        try:
            key = self._key_resolver(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],  # never accept "none" or a symmetric algorithm
                audience=self._audience,
                issuer=self._issuer,
                leeway=30,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWKClientConnectionError as e:
            raise AuthUnavailableError(str(e)) from e
        except jwt.PyJWTError as e:
            raise AuthError(str(e)) from e

        sub = claims["sub"]
        if not isinstance(sub, str) or not sub.strip():
            raise AuthError("token has an empty subject")

        realm_access = claims.get("realm_access")
        raw_roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
        raw_groups = claims.get("groups", [])
        groups = (
            tuple(g for g in raw_groups if isinstance(g, str) and g.startswith("/"))
            if isinstance(raw_groups, list)
            else ()
        )
        username = claims.get("preferred_username") or sub
        return User(
            sub=sub,
            username=username,
            name=claims.get("name") or username,
            roles=frozenset(r for r in raw_roles if isinstance(r, str)),
            groups=groups,
        )


def jwks_key_resolver(jwks_url: str) -> KeyResolver:
    """Fetches Keycloak's public keys and caches them (refetched when a new key id appears)."""
    client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300, timeout=5)
    return lambda token: client.get_signing_key_from_jwt(token).key


_bearer = HTTPBearer(auto_error=False)


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> User:
    """FastAPI dependency: the verified user, or 401."""
    if credentials is None:
        raise _unauthorized("missing bearer token")
    verifier: TokenVerifier = request.app.state.verifier
    try:
        return verifier.verify(credentials.credentials)
    except AuthUnavailableError as e:
        log.error("JWKS unavailable: %s", e)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication is unavailable"
        ) from e
    except AuthError as e:
        log.info("Rejected token: %s", e)
        raise _unauthorized("invalid token") from e


def require_role(*roles: str) -> Callable[[User], User]:
    """FastAPI dependency factory: the user must have at least one of ``roles`` (else 403)."""

    def checker(user: User = Depends(current_user)) -> User:  # noqa: B008
        if not any(user.has_role(r) for r in roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have permission to do that")
        return user

    return checker


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )
