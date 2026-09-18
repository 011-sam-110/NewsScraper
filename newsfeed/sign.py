"""Signing for the snapshot push. Section 8.3.

This module is one half of a contract. The other half is `lib/newsfeed/verify.ts` in Provenance,
which verifies with `crypto.subtle.verify`, and the two are only ever checked against each other by
the shared test vectors at the bottom of this file. Section 8.3 requires the same three vectors in
both repos, checked by `unittest` here and by `vitest` there. Change a vector and you have changed
the contract, not fixed a test.

WHAT IS SIGNED, AND WHY IT IS NOT THE BODY ITSELF. The canonical string names the method, the path
and the timestamp as well as a hash of the body. Signing the body alone would let a captured
request be replayed against a different route, or replayed forever. The timestamp is inside the
signed string rather than only in a header, so it cannot be edited without breaking the signature.

WHY THE HASH OF THE BODY RATHER THAN THE BODY. The verifier has to hash the bytes it received
anyway to know they are the bytes that were signed, and a hash is a fixed 64 characters whatever
the snapshot weighs. Section 9.0 records the reason this matters in production: Cloudflare rewrites
wire bytes, so the two ends must agree on WHICH bytes are signed. Here it is the uncompressed body
exactly as `body_bytes()` returns it, before any transport touches it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

# Section 8.3. The path is part of the signed string, so a signature for one route cannot be
# replayed against another. It is a constant rather than a parameter for that reason: a caller that
# could pass any path could sign for any route.
INGEST_PATH = "/api/ingest/newsfeed"

SIGNATURE_VERSION = "v1"

TIMESTAMP_HEADER = "X-Newsfeed-Timestamp"
SIGNATURE_HEADER = "X-Newsfeed-Signature"

# Section 8.3, point 2. The box rejects anything further from its clock than this.
TIMESTAMP_WINDOW_SECONDS = 300

# Section 8.3, point 3. Shorter secrets are refused here rather than at the far end, so a weak
# secret fails on the machine that chose it instead of silently pushing nothing for a day.
MINIMUM_SECRET_LENGTH = 32

# The SHA-256 of nothing. A signed GET uses it, and section 8.3 writes it out, so it is written out
# here too: a reader comparing the two documents should not have to run anything to check.
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class SigningError(ValueError):
    """The request cannot be signed as asked. Never raised because of what the far end answered."""


def body_bytes(payload: Any) -> bytes:
    """The exact bytes to send AND to sign. One function, so the two can never differ.

    `separators` removes the spaces Python would otherwise put after `:` and `,`, and
    `ensure_ascii=False` keeps a headline in its own alphabet rather than expanding it to escapes.
    Both are about the bytes, not about taste: whatever this returns is what gets hashed, so it has
    to be produced once and used for both purposes.

    Keys are NOT sorted. The snapshot is built in the order section 8.2 lists, and re-ordering it
    here would make the file that documents the contract disagree with the bytes on the wire.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def body_sha256(body: bytes) -> str:
    """Lowercase hex, as the canonical string requires."""
    return hashlib.sha256(body).hexdigest()


def canonical_string(method: str, timestamp: int, body: bytes, path: str = INGEST_PATH) -> str:
    """The exact string the signature is taken over. Section 8.3.

    Five lines joined with a newline and NO trailing newline. The trailing newline matters: adding
    one changes every signature, and the far end would reject every request with no way to see why
    from the outside.
    """
    return "\n".join(
        [SIGNATURE_VERSION, method.upper(), path, str(timestamp), body_sha256(body)]
    )


def sign(secret: str, method: str, timestamp: int, body: bytes, path: str = INGEST_PATH) -> str:
    """The value of the signature header, including the `v1=` prefix."""
    if not secret or len(secret) < MINIMUM_SECRET_LENGTH:
        raise SigningError(
            f"the ingest secret must be at least {MINIMUM_SECRET_LENGTH} characters; "
            f"this one is {len(secret or '')}"
        )
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_string(method, timestamp, body, path).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def headers(
    secret: str,
    method: str,
    body: bytes,
    timestamp: int | None = None,
    path: str = INGEST_PATH,
) -> dict[str, str]:
    """Everything the request needs beyond the body itself.

    `Content-Type` is included for a body and left off for a GET, because a GET has no body to
    describe and section 8.1 fixes the type only for the snapshot.
    """
    when = int(time.time()) if timestamp is None else int(timestamp)
    built = {
        TIMESTAMP_HEADER: str(when),
        SIGNATURE_HEADER: sign(secret, method, when, body, path),
    }
    if body:
        built["Content-Type"] = "application/json"
    return built


def verify(
    secret: str, method: str, timestamp: int, body: bytes, signature: str,
    path: str = INGEST_PATH, now: int | None = None,
) -> bool:
    """The far end's check, in Python. Used by the test vectors and by nothing in the pipeline.

    It exists so that a vector proves BOTH directions on this side before the TypeScript is
    written. A vector that only proves signing would still pass if the canonical string were wrong
    in the same way in both repos.

    `compare_digest`, not `==`: a plain comparison returns early on the first differing byte, and
    the time it takes leaks how much of a guess was right.
    """
    moment = int(time.time()) if now is None else int(now)
    if abs(moment - int(timestamp)) > TIMESTAMP_WINDOW_SECONDS:
        return False
    try:
        expected = sign(secret, method, timestamp, body, path)
    except SigningError:
        return False
    return hmac.compare_digest(expected, signature or "")


# --- The shared test vectors ------------------------------------------------------------
#
# Section 8.3 requires these three, byte for byte, in both repos: a GET, a small POST, and a POST
# with non-ASCII characters in a headline. They live in the module rather than in the test file so
# that the TypeScript side can be generated from one source, and so that a reader of this module
# can see what the contract actually resolves to.
#
# The secret is a fixed test string and is NOT a real secret. The repo is public: nothing here may
# ever be the value in NEWSFEED_INGEST_SECRET.

VECTOR_SECRET = "test-secret-do-not-use-in-production-0123456789"

TEST_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "name": "signed GET",
        "method": "GET",
        "timestamp": 1789464662,
        "payload": None,
        "body": b"",
        "body_sha256": EMPTY_BODY_SHA256,
    },
    {
        "name": "small POST",
        "method": "POST",
        "timestamp": 1789464662,
        "payload": {"schema": "provenance.newsfeed/1", "items": []},
        "body": b'{"schema":"provenance.newsfeed/1","items":[]}',
        "body_sha256": None,
    },
    {
        "name": "POST with non-ASCII in a headline",
        "method": "POST",
        "timestamp": 1789464662,
        # A real headline shape: the accents and the dash are exactly what a naive encoder breaks.
        "payload": {"items": [{"headline": "Céline Dion returns to the stage in Paris"}]},
        "body": '{"items":[{"headline":"Céline Dion returns to the stage in Paris"}]}'.encode(
            "utf-8"
        ),
        "body_sha256": None,
    },
)
