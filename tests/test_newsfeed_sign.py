"""The signing contract, section 8.3.

The three vectors below are the contract. Section 8.3 requires the same three in Provenance,
checked by `vitest` against `lib/newsfeed/verify.ts`. The expected signatures are written out as
literals on purpose: a test that recomputed them with the same code it is testing would pass
whatever that code did, including after a change that broke the far end.

If one of these fails, the question is never "what should the literal be now". It is "what changed
about the canonical string, and does Provenance know".
"""

from __future__ import annotations

import hashlib
import unittest

from newsfeed import sign

# Section 8.3, computed once and pinned. Recomputing these to make a test pass is changing the
# contract; the far end has the same numbers.
EXPECTED = {
    "signed GET": "v1=69ec99db348a3688296fe1059a98795d4f471952e55af1ef36bebe5a9be35cbb",
    "small POST": "v1=a22ea6637fe213495f9479091ca61b62951b4dd42ba0ff183c4271d760955722",
    "POST with non-ASCII in a headline":
        "v1=2f04f58bbbcd0221ef7ea3b689c3d7991e1e7866906d4e08de7c4fee83eac4e6",
}

EXPECTED_BODY_SHA = {
    "signed GET": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "small POST": "8b2ba860cb56204176174a02d8c835802cda8adcc6c8d9588b6549c6cbb07452",
    "POST with non-ASCII in a headline":
        "4922b91856703673c0f01f50f4a6c367dadc413bd5b182039e0598425b6f453a",
}


class VectorTests(unittest.TestCase):
    def vector(self, name: str) -> dict:
        for entry in sign.TEST_VECTORS:
            if entry["name"] == name:
                return entry
        raise AssertionError(f"no vector called {name}")

    def test_there_are_exactly_the_three_the_contract_names(self) -> None:
        names = [entry["name"] for entry in sign.TEST_VECTORS]
        self.assertEqual(names, list(EXPECTED))

    def test_every_signature_matches_the_pinned_value(self) -> None:
        for entry in sign.TEST_VECTORS:
            with self.subTest(vector=entry["name"]):
                produced = sign.sign(
                    sign.VECTOR_SECRET, entry["method"], entry["timestamp"], entry["body"]
                )
                self.assertEqual(produced, EXPECTED[entry["name"]])

    def test_every_body_hash_matches_the_pinned_value(self) -> None:
        for entry in sign.TEST_VECTORS:
            with self.subTest(vector=entry["name"]):
                self.assertEqual(sign.body_sha256(entry["body"]), EXPECTED_BODY_SHA[entry["name"]])

    def test_the_bytes_the_vector_states_are_the_bytes_body_bytes_produces(self) -> None:
        """The far end hashes what arrived. If our encoder disagrees with the vector, it lies."""
        for entry in sign.TEST_VECTORS:
            if entry["payload"] is None:
                continue
            with self.subTest(vector=entry["name"]):
                self.assertEqual(sign.body_bytes(entry["payload"]), entry["body"])

    def test_the_empty_body_hash_is_the_one_the_document_writes_out(self) -> None:
        self.assertEqual(sign.EMPTY_BODY_SHA256, hashlib.sha256(b"").hexdigest())
        self.assertEqual(self.vector("signed GET")["body_sha256"], sign.EMPTY_BODY_SHA256)

    def test_the_non_ascii_vector_really_is_non_ascii(self) -> None:
        """A vector that quietly became ASCII would still pass every test above and prove nothing."""
        body = self.vector("POST with non-ASCII in a headline")["body"]
        self.assertNotEqual(body.decode("utf-8"), body.decode("utf-8").encode("ascii", "replace").decode())
        self.assertIn("é".encode("utf-8"), body)


class CanonicalStringTests(unittest.TestCase):
    def test_the_shape_is_five_lines_with_no_trailing_newline(self) -> None:
        built = sign.canonical_string("POST", 1789464662, b"{}")
        self.assertFalse(built.endswith("\n"), "a trailing newline changes every signature")
        lines = built.split("\n")
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0], "v1")
        self.assertEqual(lines[1], "POST")
        self.assertEqual(lines[2], "/api/ingest/newsfeed")
        self.assertEqual(lines[3], "1789464662")
        self.assertEqual(lines[4], sign.body_sha256(b"{}"))

    def test_the_method_is_upper_cased(self) -> None:
        self.assertEqual(
            sign.canonical_string("post", 1, b""), sign.canonical_string("POST", 1, b"")
        )

    def test_the_path_is_signed_so_one_route_cannot_be_replayed_at_another(self) -> None:
        here = sign.sign(sign.VECTOR_SECRET, "POST", 1, b"{}")
        elsewhere = sign.sign(sign.VECTOR_SECRET, "POST", 1, b"{}", path="/api/other")
        self.assertNotEqual(here, elsewhere)

    def test_the_timestamp_is_signed_so_it_cannot_be_edited_in_the_header(self) -> None:
        self.assertNotEqual(
            sign.sign(sign.VECTOR_SECRET, "POST", 1, b"{}"),
            sign.sign(sign.VECTOR_SECRET, "POST", 2, b"{}"),
        )

    def test_one_changed_body_byte_changes_the_signature(self) -> None:
        self.assertNotEqual(
            sign.sign(sign.VECTOR_SECRET, "POST", 1, b'{"a":1}'),
            sign.sign(sign.VECTOR_SECRET, "POST", 1, b'{"a":2}'),
        )


class SecretTests(unittest.TestCase):
    def test_a_short_secret_is_refused_here_not_at_the_far_end(self) -> None:
        """Section 8.3 says each secret is at least 32 characters. A weak one must fail loudly."""
        with self.assertRaises(sign.SigningError) as raised:
            sign.sign("too-short", "POST", 1, b"{}")
        self.assertIn("32", str(raised.exception))

    def test_an_empty_secret_is_refused(self) -> None:
        with self.assertRaises(sign.SigningError):
            sign.sign("", "POST", 1, b"{}")

    def test_a_different_secret_gives_a_different_signature(self) -> None:
        other = "another-test-secret-0123456789abcdefghij"
        self.assertNotEqual(
            sign.sign(sign.VECTOR_SECRET, "POST", 1, b"{}"),
            sign.sign(other, "POST", 1, b"{}"),
        )


class HeaderTests(unittest.TestCase):
    def test_a_post_carries_the_three_headers(self) -> None:
        built = sign.headers(sign.VECTOR_SECRET, "POST", b"{}", timestamp=1789464662)
        self.assertEqual(built["X-Newsfeed-Timestamp"], "1789464662")
        self.assertEqual(built["Content-Type"], "application/json")
        self.assertTrue(built["X-Newsfeed-Signature"].startswith("v1="))

    def test_a_get_carries_no_content_type(self) -> None:
        built = sign.headers(sign.VECTOR_SECRET, "GET", b"", timestamp=1789464662)
        self.assertNotIn("Content-Type", built)
        self.assertEqual(built["X-Newsfeed-Signature"], EXPECTED["signed GET"])

    def test_the_timestamp_defaults_to_now(self) -> None:
        import time

        built = sign.headers(sign.VECTOR_SECRET, "POST", b"{}")
        self.assertLess(abs(int(built["X-Newsfeed-Timestamp"]) - int(time.time())), 5)


class VerifyTests(unittest.TestCase):
    """Both directions, so a canonical string that is wrong the same way twice cannot hide."""

    def test_a_signature_this_module_made_verifies(self) -> None:
        for entry in sign.TEST_VECTORS:
            with self.subTest(vector=entry["name"]):
                self.assertTrue(
                    sign.verify(
                        sign.VECTOR_SECRET, entry["method"], entry["timestamp"], entry["body"],
                        EXPECTED[entry["name"]], now=entry["timestamp"],
                    )
                )

    def test_a_stale_timestamp_is_refused(self) -> None:
        stamp = 1789464662
        signature = sign.sign(sign.VECTOR_SECRET, "POST", stamp, b"{}")
        edge = stamp + sign.TIMESTAMP_WINDOW_SECONDS
        self.assertTrue(sign.verify(sign.VECTOR_SECRET, "POST", stamp, b"{}", signature, now=edge))
        self.assertFalse(
            sign.verify(sign.VECTOR_SECRET, "POST", stamp, b"{}", signature, now=edge + 1)
        )

    def test_a_timestamp_from_the_future_is_refused_too(self) -> None:
        stamp = 1789464662
        signature = sign.sign(sign.VECTOR_SECRET, "POST", stamp, b"{}")
        self.assertFalse(
            sign.verify(
                sign.VECTOR_SECRET, "POST", stamp, b"{}", signature,
                now=stamp - sign.TIMESTAMP_WINDOW_SECONDS - 1,
            )
        )

    def test_a_tampered_body_is_refused(self) -> None:
        stamp = 1789464662
        signature = sign.sign(sign.VECTOR_SECRET, "POST", stamp, b'{"a":1}')
        self.assertFalse(
            sign.verify(sign.VECTOR_SECRET, "POST", stamp, b'{"a":2}', signature, now=stamp)
        )

    def test_a_missing_signature_is_refused_rather_than_crashing(self) -> None:
        for candidate in ("", None):
            with self.subTest(signature=candidate):
                self.assertFalse(
                    sign.verify(sign.VECTOR_SECRET, "POST", 1, b"{}", candidate, now=1)
                )


class PublicRepoTests(unittest.TestCase):
    def test_the_vector_secret_says_what_it_is(self) -> None:
        """The repo is public. A vector secret that looked real would be copied into production."""
        self.assertIn("test", sign.VECTOR_SECRET)
        self.assertIn("do-not-use", sign.VECTOR_SECRET)
        self.assertGreaterEqual(len(sign.VECTOR_SECRET), sign.MINIMUM_SECRET_LENGTH)


if __name__ == "__main__":
    unittest.main()
