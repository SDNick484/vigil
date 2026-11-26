"""Cloudflare Access assertions (`vigil.web.access`): signature, claims, identities, and how the
team's keys are fetched and cached."""

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Callable

import httpx
import pytest
from cryptography.hazmat.primitives import serialization

from tests.conftest import (
    ACCESS_TEAM,
    CERTS_URL,
    AccessKeys,
    CertsServer,
    generate_access_keys,
    make_assertion,
)
from vigil.config import AccessConfig, ServiceTokenConfig
from vigil.web.access import (
    KEYS_TTL,
    KID_REFETCH_AFTER,
    AccessError,
    AccessVerifier,
)
from vigil.web.auth import Grant

CONFIG = AccessConfig(
    team_domain=ACCESS_TEAM,
    aud=("aud-placeholder",),
    emails=frozenset({"author@example.com"}),
    service_tokens=(
        ServiceTokenConfig("0123abcd.access", "push", "laptop"),
        ServiceTokenConfig("4567efab.access", "read"),
    ),
)


class FakeMonotonic:
    """A monotonic clock that only moves when told to."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def certs(access_keys: AccessKeys) -> CertsServer:
    return CertsServer(access_keys)


@pytest.fixture
def monotonic() -> FakeMonotonic:
    return FakeMonotonic()


@pytest.fixture
def verifier(certs: CertsServer, monotonic: FakeMonotonic) -> AccessVerifier:
    return AccessVerifier(CONFIG, transport=certs, monotonic=monotonic)


def verify(verifier: AccessVerifier, assertion: str | None) -> Grant | str:
    return asyncio.run(verifier.verify(assertion))


def refused(result: Grant | str, *words: str) -> None:
    assert isinstance(result, str), result
    for word in words:
        assert word in result, result


def test_urls() -> None:
    verifier = AccessVerifier(CONFIG)
    assert verifier.certs_url == CERTS_URL
    assert verifier.issuer == f"https://{ACCESS_TEAM}"


def test_valid_user(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")


def test_email_case_insensitive(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    assert verify(verifier, make_assertion(access_keys, email="AUTHOR@example.com")) == Grant(
        "full"
    )


def test_valid_service_token(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    assertion = make_assertion(access_keys, email=None, common_name="0123abcd.access")
    assert verify(verifier, assertion) == Grant("push", "laptop")
    assertion = make_assertion(access_keys, email=None, common_name="4567efab.access")
    assert verify(verifier, assertion) == Grant("read")


def test_aud_list_or_string(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    assert verify(verifier, make_assertion(access_keys, aud="aud-placeholder")) == Grant("full")
    assertion = make_assertion(access_keys, aud=["other-app", "aud-placeholder"])
    assert verify(verifier, assertion) == Grant("full")


def test_wrong_aud(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    refused(verify(verifier, make_assertion(access_keys, aud=["other-app"])), "aud")
    refused(verify(verifier, make_assertion(access_keys, aud=None)), "aud")


def test_wrong_iss(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    assertion = make_assertion(access_keys, iss="https://other-team.cloudflareaccess.com")
    refused(verify(verifier, assertion), "iss")
    refused(verify(verifier, make_assertion(access_keys, iss=None)), "iss")


def test_expired(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    now = int(time.time())
    refused(verify(verifier, make_assertion(access_keys, iat=now - 600, exp=now - 300)), "exp")
    refused(verify(verifier, make_assertion(access_keys, exp=None)), "exp")


def test_future_nbf(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    refused(verify(verifier, make_assertion(access_keys, nbf=int(time.time()) + 300)), "nbf")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hand_made(header: dict[str, object], claims: dict[str, object], secret: bytes | None) -> str:
    """A JWT put together by hand, as an attacker would (PyJWT refuses to sign these)."""
    signing_input = _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(claims).encode())
    signature = b""
    if secret is not None:
        signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64(signature)


def test_not_rs256(verifier: AccessVerifier, access_keys: AccessKeys, certs: CertsServer) -> None:
    """The classic confusion: HS256 signed with the public key as the shared secret; and `none`."""
    public = access_keys.private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": f"https://{ACCESS_TEAM}",
        "aud": ["aud-placeholder"],
        "iat": now,
        "exp": now + 300,
        "email": "author@example.com",
    }
    header: dict[str, object] = {"alg": "HS256", "typ": "JWT", "kid": access_keys.kid}
    refused(verify(verifier, _hand_made(header, claims, public)), "RS256")
    header = {"alg": "none", "typ": "JWT", "kid": access_keys.kid}
    refused(verify(verifier, _hand_made(header, claims, None)), "RS256")
    # Even with the key's own `alg` claim in the JWK, only RS256 is ever accepted.
    header = {"alg": "RS512", "typ": "JWT", "kid": access_keys.kid}
    refused(verify(verifier, _hand_made(header, claims, public)), "RS256")


def test_bad_signature(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    """Signed by another key that claims the served key's `kid`."""
    forger = generate_access_keys(access_keys.kid)
    refused(verify(verifier, make_assertion(forger)), "signature")


@pytest.mark.parametrize("assertion", [None, "", "not-a-jwt", "a.b.c", "x" * 20000])
def test_missing_or_malformed(verifier: AccessVerifier, assertion: str | None) -> None:
    refused(verify(verifier, assertion))


@pytest.mark.parametrize(
    "claims",
    [
        {"iat": "now"},
        {"exp": True},
        {"nbf": "later"},
        {"iat": None},
        {"exp": float("nan")},
    ],
)
def test_bad_time_claims(
    verifier: AccessVerifier, access_keys: AccessKeys, claims: dict[str, object]
) -> None:
    refused(verify(verifier, make_assertion(access_keys, **claims)))


def test_clock_skew_within_leeway(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    now = int(time.time())
    assertion = make_assertion(access_keys, iat=now + 20, nbf=now + 20)
    assert verify(verifier, assertion) == Grant("full")
    assertion = make_assertion(access_keys, iat=now - 300, exp=now - 20)
    assert verify(verifier, assertion) == Grant("full")


def test_clock_skew_beyond_leeway(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    now = int(time.time())
    refused(verify(verifier, make_assertion(access_keys, iat=now + 45)), "iat")
    refused(verify(verifier, make_assertion(access_keys, nbf=now + 45)), "nbf")
    refused(verify(verifier, make_assertion(access_keys, iat=now - 300, exp=now - 45)), "exp")


def test_injected_clock(certs: CertsServer, access_keys: AccessKeys) -> None:
    """Time claims are checked against the verifier's clock, not the system's."""
    later = time.time() + 3600
    verifier = AccessVerifier(CONFIG, transport=certs, clock=lambda: later)
    refused(verify(verifier, make_assertion(access_keys)), "exp")


def test_unknown_kid_refetches_once(
    verifier: AccessVerifier,
    access_keys: AccessKeys,
    certs: CertsServer,
    monotonic: FakeMonotonic,
) -> None:
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    assert certs.fetches == 1
    stranger = generate_access_keys("kid-unknown")
    monotonic.now += KID_REFETCH_AFTER
    refused(verify(verifier, make_assertion(stranger)), "kid-unknown")
    assert certs.fetches == 2
    monotonic.now += 10
    refused(verify(verifier, make_assertion(stranger)), "kid-unknown")
    assert certs.fetches == 2
    # The known key still works meanwhile.
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    assert certs.fetches == 2


def test_unknown_kid_right_after_a_fetch_waits(
    verifier: AccessVerifier, access_keys: AccessKeys, certs: CertsServer
) -> None:
    """A stream of made-up `kid`s cannot make vigil fetch more than once a minute."""
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    for number in range(5):
        refused(verify(verifier, make_assertion(generate_access_keys(f"kid-made-up-{number}"))))
    assert certs.fetches == 1


def test_rotated_key_found_after_refetch(
    verifier: AccessVerifier,
    access_keys: AccessKeys,
    certs: CertsServer,
    monotonic: FakeMonotonic,
) -> None:
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    rotated = generate_access_keys("kid-placeholder-2")
    certs.jwks.append(rotated.jwks)
    monotonic.now += KID_REFETCH_AFTER
    assert verify(verifier, make_assertion(rotated)) == Grant("full")
    assert certs.fetches == 2


def test_unreachable_certs_fail_closed(
    verifier: AccessVerifier,
    access_keys: AccessKeys,
    certs: CertsServer,
    monotonic: FakeMonotonic,
) -> None:
    certs.fail = True
    refused(verify(verifier, make_assertion(access_keys)), "unreachable")
    assert certs.fetches == 1
    monotonic.now += KID_REFETCH_AFTER - 1
    refused(verify(verifier, make_assertion(access_keys)))
    assert certs.fetches == 1
    # Retried a minute after the failed attempt; once it answers, assertions pass.
    certs.fail = False
    monotonic.now += 1
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    assert certs.fetches == 2


def test_keys_cached_for_an_hour(
    verifier: AccessVerifier,
    access_keys: AccessKeys,
    certs: CertsServer,
    monotonic: FakeMonotonic,
) -> None:
    start = monotonic.now
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    assert certs.fetches == 1
    monotonic.now = start + KEYS_TTL - 1
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    assert certs.fetches == 1
    monotonic.now = start + KEYS_TTL + 1
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    assert certs.fetches == 2


def test_expired_keys_not_used_when_refetch_fails(
    verifier: AccessVerifier,
    access_keys: AccessKeys,
    certs: CertsServer,
    monotonic: FakeMonotonic,
) -> None:
    """Keys older than an hour are dropped, even when they cannot be replaced: fail closed."""
    assert verify(verifier, make_assertion(access_keys)) == Grant("full")
    certs.fail = True
    monotonic.now += KEYS_TTL + 1
    refused(verify(verifier, make_assertion(access_keys)), "unreachable")
    refused(verify(verifier, make_assertion(access_keys)))
    assert certs.fetches == 2


def test_concurrent_verifications_fetch_once(
    verifier: AccessVerifier, access_keys: AccessKeys, certs: CertsServer
) -> None:
    certs.block = threading.Event()
    block = certs.block
    assertion = make_assertion(access_keys)

    async def release_when_fetching() -> None:
        # The loop stays free while the fetch waits in its thread.
        for _ in range(500):
            if certs.fetches:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        block.set()

    async def run() -> list[Grant | str | None]:
        results = await asyncio.gather(
            *(verifier.verify(assertion) for _ in range(5)), release_when_fetching()
        )
        return list(results)

    results = asyncio.run(run())
    assert results[:5] == [Grant("full")] * 5
    assert certs.fetches == 1


def test_unlisted_email(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    result = verify(verifier, make_assertion(access_keys, email="someone@example.com"))
    refused(result, "identity not allowed", "someone@example.com")


def test_unlisted_client_id(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    assertion = make_assertion(access_keys, email=None, common_name="89abcdef.access")
    refused(verify(verifier, assertion), "identity not allowed", "89abcdef.access")


def test_no_identity(verifier: AccessVerifier, access_keys: AccessKeys) -> None:
    refused(verify(verifier, make_assertion(access_keys, email=None)), "identity")
    refused(verify(verifier, make_assertion(access_keys, email="")), "identity")


def test_no_redirect_followed(
    verifier: AccessVerifier, access_keys: AccessKeys, certs: CertsServer
) -> None:
    certs.redirect = "https://elsewhere.example.com/certs"
    refused(verify(verifier, make_assertion(access_keys)), "302")
    assert certs.fetches == 1
    assert [str(request.url) for request in certs.requests] == [CERTS_URL]


def answering(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AccessVerifier:
    return AccessVerifier(CONFIG, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("response", "problem"),
    [
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(200, text="not json"), "JSON"),
        (httpx.Response(200, json=[]), "keys"),
        (httpx.Response(200, json={"keys": "x"}), "keys"),
        (httpx.Response(200, json={"keys": []}), "no usable"),
        (httpx.Response(200, json={"keys": [{"kty": "oct", "kid": "a", "k": "c2VjcmV0"}]}), "no"),
        (httpx.Response(200, content=b"{" + b" " * (1024 * 1024 + 1) + b"}"), "larger"),
    ],
)
def test_fetch_keys_refuses(response: httpx.Response, problem: str) -> None:
    with pytest.raises(AccessError, match=problem):
        answering(lambda request: response).fetch_keys()


def test_fetch_keys_skips_unusable_keys(access_keys: AccessKeys) -> None:
    small = generate_access_keys("kid-small", key_size=1024)
    entries = [
        {"kty": "RSA", "kid": "kid-broken", "n": "AQAB", "e": "!!"},
        {**access_keys.jwks, "kid": "kid-enc", "use": "enc"},
        {**access_keys.jwks, "kid": "kid-ps", "alg": "PS256"},
        {key: value for key, value in access_keys.jwks.items() if key != "kid"},
        "not a key",
        access_keys.jwks,
        small.jwks,
    ]
    keys = answering(lambda request: httpx.Response(200, json={"keys": entries})).fetch_keys()
    assert set(keys) == {access_keys.kid}


def test_fetch_keys_sends_no_credentials(certs: CertsServer) -> None:
    AccessVerifier(CONFIG, transport=certs).fetch_keys()
    (request,) = certs.requests
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
