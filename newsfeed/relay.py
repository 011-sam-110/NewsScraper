"""Signing and sending for the Provenance relay. One half of a contract.

The other half is `lib/relay/protocol.ts` in Provenance, which verifies with WebCrypto. The two are
checked against each other by the shared vectors at the bottom of this file, which Provenance's
`tests/unit/relay-protocol.test.ts` pins with the same literals. Change a vector and you have changed
the contract, not fixed a test.

The scheme is the news rail's (`newsfeed/rail.py`): HMAC-SHA256 over `prefix:timestamp_ms:sha256(body)`.
Only the prefix differs, `provenance-relay-v1`, so a signature minted for the rail can never verify at
the relay. The signature covers the UNCOMPRESSED JSON, because Cloudflare may re-encode a body.

The box-only `sources` package carries the same scheme for the war feeds. This copy lives in the
pipeline so that the Headline sources feed (newsfeed/headline_places.py) can ship from main. The
secret and the address are the box's relay settings: RELAY_SIGNING_SECRET and PROVENANCE_RELAY_URL,
in ~/.config/provenance-sources/sources.env (deploy/set-relay-secret.sh on the box branch writes it).
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any

SIGNATURE_PREFIX = "provenance-relay-v1"
TIMESTAMP_HEADER = "x-provenance-timestamp"
SIGNATURE_HEADER = "x-provenance-signature"
DIGEST_HEADER = "x-provenance-content-sha256"
ENVELOPE_VERSION = 1

# Refused here rather than at the far end, so a weak secret fails on the machine that chose it.
MINIMUM_SECRET_LENGTH = 32

# Cloudflare answers the default `Python-urllib/3.x` agent with 403 (error 1010) before the route
# sees the request. newsfeed/rail.py records the measurement.
USER_AGENT = "NewsScraper-relay/1 (+https://github.com/011-sam-110/NewsScraper)"


class SigningError(ValueError):
    """The request cannot be signed as asked. Never raised because of what the far end answered."""


class RelayRefused(Exception):
    """The relay answered with a status that is not a success.

    404 the feed id is not on Provenance's list or its secret is unset, 401 the secret is wrong or the
    clock has drifted, 413 the body is too big, 422 the body failed the feed's schema.
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def body_bytes(payload: Any) -> bytes:
    """The exact bytes to send AND to sign."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def body_digest_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signing_string(timestamp_ms: int, digest_hex: str) -> str:
    """Exactly what both sides sign. `relaySigningString` in lib/relay/protocol.ts builds the same."""
    return f"{SIGNATURE_PREFIX}:{timestamp_ms}:{digest_hex}"


def sign(secret: str, timestamp_ms: int, body: bytes) -> str:
    """The signature header value: `sha256=` and the hex HMAC."""
    if not secret or len(secret) < MINIMUM_SECRET_LENGTH:
        raise SigningError(
            f"RELAY_SIGNING_SECRET must be at least {MINIMUM_SECRET_LENGTH} characters; "
            f"this one is {len(secret or '')}"
        )
    message = signing_string(timestamp_ms, body_digest_hex(body)).encode("utf-8")
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def headers(secret: str, body: bytes, timestamp_ms: int | None = None) -> dict[str, str]:
    stamp = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    return {
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        TIMESTAMP_HEADER: str(stamp),
        SIGNATURE_HEADER: sign(secret, stamp, body),
        DIGEST_HEADER: body_digest_hex(body),
    }


def snapshot_envelope(feed: str, fetched_at: str, data: Any) -> dict[str, Any]:
    """One snapshot push. `fetched_at` is when this machine built the snapshot, RFC3339 Z."""
    return {"version": ENVELOPE_VERSION, "feed": feed, "fetchedAt": fetched_at, "data": data}


def feed_url(base: str, feed: str) -> str:
    """`https://provenance-online.com` -> `https://provenance-online.com/api/relay/<feed>`."""
    base = (base or "").strip().rstrip("/")
    if not base.startswith("https://"):
        raise SigningError("PROVENANCE_RELAY_URL must be an https:// address")
    if base.endswith("/api/relay"):
        return f"{base}/{feed}"
    return f"{base}/api/relay/{feed}"


def post(url: str, secret: str, body: bytes, *, timeout: float = 60.0) -> dict[str, Any]:
    """One signed POST. Compression is applied AFTER signing, because the signature covers the content."""
    sent = headers(secret, body)
    payload = body
    if len(body) > 1024:
        payload = gzip.compress(body)
        sent["content-encoding"] = "gzip"
    request = urllib.request.Request(url, data=payload, headers=sent, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RelayRefused(error.code, detail) from None


# --- The shared vectors -------------------------------------------------------------------------
#
# The same literals as Provenance's tests/unit/relay-protocol.test.ts and the box branch's
# sources/sign.py. The secrets are fixed test strings and NOT real secrets. This repo is public.

TEST_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "name": "probe",
        "secret": "provenance-relay-test-secret-00000001",
        "timestamp_ms": 1792180000000,
        "body": b'{"version":1,"feed":"celestrak","probe":true}',
        "digest": "6fa575fc7259ec4870a16a84ec041b6b4169faf725639e759ae3cdeb952e97ee",
        "signature": "83e31984f2d782f89fb00963bd1d59cc74e71d3280c3ba3a0f9f096ea9fbf51b",
    },
    {
        "name": "unicode snapshot",
        "secret": "provenance-relay-test-secret-00000002",
        "timestamp_ms": 1792180060000,
        # Three-byte UTF-8 characters are where a byte-counting bug shows.
        "body": '{"version":1,"feed":"jma-volcam","fetchedAt":"2026-10-16T19:00:00Z","data":'
                '{"site":"北東山麓"}}'.encode("utf-8"),
        "digest": "55a1973314edbd3284a932d063214d144b9d7e88996c15e031edb757bd7e04d6",
        "signature": "eed2ee898884ccc3cbbdce9f5319ce70608076f0a41242b00cb9955e67495b59",
    },
)
