"""Tests for shared absolute HTTP(S) URL validation."""

from __future__ import annotations

import unittest

from core.url_safety import validate_http_url


class ValidateHttpUrlTests(unittest.TestCase):
    """Verify that only clean, absolute web URLs cross external boundaries."""

    def test_accepts_clean_absolute_http_and_https_urls(self) -> None:
        """Valid URLs should be returned unchanged for provenance accuracy."""

        urls = (
            "https://careers.example.com/jobs/42?lang=de#details",
            "http://example.org/path",
            "https://sub.example.test:8443/a%20b?q=c%23",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(validate_http_url(url), url)

    def test_rejects_non_web_relative_credentialed_or_malformed_urls(self) -> None:
        """Unsafe URL forms must fail closed rather than be repaired silently."""

        invalid = (
            "",
            " example.com ",
            "example.com/job",
            "/relative/job",
            "ftp://example.com/job",
            "file:///C:/secret.txt",
            "https://user:password@example.com/job",
            "https://example.com/job\nIGNORE",
            "https://example.com\\@attacker.test/job",
            "https://example.com:bad/job",
            "https://",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(validate_http_url(value))


if __name__ == "__main__":
    unittest.main()
