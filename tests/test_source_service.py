import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.source_service import extract_version_section

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def test_extract_markdown_section():
    text = _load("sample_changelog.md")
    section = extract_version_section(text, "2.4.0")
    assert section is not None
    assert "Removed support for Python 3.7" in section
    assert "async context managers" in section
    # Must not bleed into the next version's section
    assert "old_connect" not in section


def test_extract_markdown_section_does_not_include_heading_itself():
    text = _load("sample_changelog.md")
    section = extract_version_section(text, "2.4.1")
    assert section is not None
    assert "2.4.1 (2024-03-10)" not in section


def test_extract_rst_setext_section():
    text = _load("sample_changelog.rst")
    section = extract_version_section(text, "1.9.0")
    assert section is not None
    assert "timeout" in section
    assert "Session.stream()" in section
    # Must not bleed into 1.9.1's section
    assert "Reduced memory usage" not in section


def test_extract_rst_section_excludes_underline():
    text = _load("sample_changelog.rst")
    section = extract_version_section(text, "1.9.1")
    assert section is not None
    assert "------------------" not in section


def test_extract_missing_version_returns_none():
    text = _load("sample_changelog.md")
    assert extract_version_section(text, "9.9.9") is None


def test_version_number_not_confused_with_similar_prefix():
    # "1.9.0" must not match inside "1.9.02" or similar - exact word-boundary match
    text = "1.9.0\n-----\nSome notes.\n\n1.9.02\n------\nOther notes about a different, unrelated version.\n"
    section = extract_version_section(text, "1.9.0")
    assert "Some notes" in section
    assert "Other notes" not in section
