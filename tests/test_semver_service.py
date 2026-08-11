import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.semver_service import classify_bump, is_prerelease


def test_patch_bump():
    assert classify_bump("2.31.0", "2.31.1") == "patch"


def test_minor_bump():
    assert classify_bump("2.31.0", "2.32.0") == "minor"


def test_major_bump():
    assert classify_bump("2.31.0", "3.0.0") == "major"


def test_prerelease_detected_even_if_minor_shaped():
    assert classify_bump("2.31.0", "2.32.0b1") == "prerelease"


def test_patch_bump_across_versions_missing_patch_digit():
    # e.g. "5.0" -> "5.0.1" should still read as a patch bump
    assert classify_bump("5.0", "5.0.1") == "patch"


def test_is_prerelease():
    assert is_prerelease("1.0.0rc1") is True
    assert is_prerelease("1.0.0") is False
    assert is_prerelease("2.0.0a1") is True
