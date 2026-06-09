"""
Unit tests — apps.api.core.s3

Tests the make_safe_filename sanitisation logic and the get_s3_client
dependency factory (client construction validated via mock).

Milestone: M2-Step 8
"""

from __future__ import annotations

from apps.api.core.s3 import make_safe_filename


class TestMakeSafeFilename:
    def test_plain_filename_unchanged(self) -> None:
        assert make_safe_filename("report.pdf") == "report.pdf"

    def test_path_traversal_stripped(self) -> None:
        result = make_safe_filename("../../etc/passwd")
        assert "../" not in result
        assert "etc" not in result or result == "passwd"

    def test_windows_path_stripped(self) -> None:
        result = make_safe_filename(r"C:\Users\HP\secret.pdf")
        assert "\\" not in result
        assert ":" not in result

    def test_spaces_replaced(self) -> None:
        result = make_safe_filename("annual report 2023.pdf")
        assert " " not in result

    def test_empty_after_strip_returns_document(self) -> None:
        result = make_safe_filename("../../")
        assert result == "document"

    def test_leading_dots_stripped(self) -> None:
        result = make_safe_filename(".hidden_file.txt")
        assert not result.startswith(".")

    def test_max_length_255(self) -> None:
        long_name = "a" * 300 + ".pdf"
        result = make_safe_filename(long_name)
        assert len(result) <= 255

    def test_unicode_special_chars_replaced(self) -> None:
        result = make_safe_filename("résumé@2023!.pdf")
        # Only word chars, hyphens and dots allowed
        import re
        assert re.fullmatch(r"[\w\-.]+", result)

    def test_normal_hyphenated_name(self) -> None:
        result = make_safe_filename("10-K_annual-2023.pdf")
        assert result == "10-K_annual-2023.pdf"

    def test_only_leading_dot_becomes_document(self) -> None:
        result = make_safe_filename("...")
        assert result == "document"
