"""Tests for deterministic German and English language detection."""

from __future__ import annotations

import unittest

from core.language import detect_language


class DetectLanguageTests(unittest.TestCase):
    """Verify stopword scoring, tokenization, and German fallback behavior."""

    def test_detects_german_job_description(self) -> None:
        """A posting with more German stopwords is classified as German."""

        text = (
            "Wir sind ein Unternehmen für industrielle Lösungen und suchen "
            "eine Person, die mit uns auf neuen Systemen arbeiten wird."
        )

        self.assertEqual(detect_language(text), "de")

    def test_detects_english_job_description(self) -> None:
        """A posting with more English stopwords is classified as English."""

        text = (
            "The role is in our engineering team, and you will work with "
            "colleagues from across the company."
        )

        self.assertEqual(detect_language(text), "en")

    def test_repeated_stopwords_are_counted_individually(self) -> None:
        """Repeated occurrences contribute separately to the language score."""

        self.assertEqual(detect_language("the the the und"), "en")

    def test_equal_scores_default_to_german(self) -> None:
        """A stopword-score tie uses the required German default."""

        self.assertEqual(detect_language("und and mit with"), "de")

    def test_empty_text_defaults_to_german(self) -> None:
        """Empty input has no hits and therefore defaults to German."""

        self.assertEqual(detect_language(""), "de")

    def test_whitespace_and_punctuation_default_to_german(self) -> None:
        """Non-word input has no hits and therefore defaults to German."""

        self.assertEqual(detect_language(" \n\t— / 123 !!!"), "de")

    def test_technical_text_without_stopwords_defaults_to_german(self) -> None:
        """Technical vocabulary without stopwords uses the German default."""

        self.assertEqual(
            detect_language("Python Kubernetes SAP S/4HANA AutoCAD"),
            "de",
        )

    def test_matching_is_case_insensitive(self) -> None:
        """Uppercase English stopwords are normalized before scoring."""

        self.assertEqual(detect_language("THE AND WITH FOR"), "en")

    def test_punctuation_separates_complete_tokens(self) -> None:
        """Adjacent punctuation does not prevent complete stopword matches."""

        self.assertEqual(detect_language("the,and.with;for"), "en")

    def test_substrings_do_not_count_as_stopwords(self) -> None:
        """Stopword-looking substrings inside larger words are not counted."""

        text = "theater android within format young area"

        self.assertEqual(detect_language(text), "de")

    def test_german_umlauts_are_part_of_tokens(self) -> None:
        """Unicode German stopwords containing umlauts are recognized."""

        self.assertEqual(detect_language("FÜR the ÜBER and"), "de")


if __name__ == "__main__":
    unittest.main()
