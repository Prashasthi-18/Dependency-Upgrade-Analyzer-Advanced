import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tempfile
import shutil

import pytest

from services import cache_service
from services.source_service import _detect_discrepancy
from services.security_advisory_service import _version_matches_range
from services.version_service import get_upgrade_path
from services import llm_classifier
from services.known_sources import get_known_adapter, KNOWN_ADAPTERS


@pytest.fixture(autouse=True)
def isolated_cache_dir(monkeypatch):
    """Each test gets its own throwaway cache directory so tests never
    interfere with each other or with a real cache/ folder on disk."""
    tmp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(cache_service, "CACHE_DIR", tmp_dir)
    yield
    shutil.rmtree(tmp_dir, ignore_errors=True)


# --- Cache keyed by ecosystem + package + version + source ---

def test_cache_key_includes_source():
    notes = {"source": "GitHub Release Notes", "text": "Fixed a bug", "url": "http://example.com"}
    cache_service.set("pypi", "django", "5.0.1", notes, source="known_adapter")

    # A different source for the same version should be a separate cache miss
    assert cache_service.get("pypi", "django", "5.0.1", source="github_release") is cache_service.MISS
    # The source it was actually stored under should hit
    assert cache_service.get("pypi", "django", "5.0.1", source="known_adapter") == notes


def test_cache_roundtrip_not_found():
    cache_service.set("pypi", "somepkg", "1.0.0", None, source="resolved")
    assert cache_service.get("pypi", "somepkg", "1.0.0", source="resolved") is None


def test_cache_miss_for_unseen_key():
    assert cache_service.get("pypi", "never-cached-pkg", "9.9.9", source="resolved") is cache_service.MISS


# --- Source discrepancy detection ---

def test_discrepancy_detected_for_unrelated_texts():
    a = {"text": "Fixed a crash in the connection pool when a socket times out unexpectedly during setup."}
    b = {"text": "Added a brand new plugin system for themes and custom widgets in the admin dashboard."}
    assert _detect_discrepancy(a, b) is True


def test_no_discrepancy_for_similar_texts():
    a = {"text": "Fixed a crash in the connection pool when a socket times out during setup."}
    b = {"text": "Fixed a crash in the connection pool that happened when a socket times out during setup."}
    assert _detect_discrepancy(a, b) is False


# --- Security advisory version-range matching ---

def test_advisory_range_matches_inside_bounds():
    assert _version_matches_range("1.1.0", ">= 1.0.0, < 1.2.0") is True


def test_advisory_range_excludes_outside_bounds():
    assert _version_matches_range("1.2.0", ">= 1.0.0, < 1.2.0") is False


def test_advisory_range_handles_exact_match():
    assert _version_matches_range("2.0.0", "= 2.0.0") is True


def test_advisory_range_unparseable_returns_false_not_error():
    # Should never raise, and should conservatively say "no match" rather
    # than risk a false positive security claim.
    assert _version_matches_range("1.0.0", "some nonsense string") is False


# --- Pre-release inclusion is deterministic ---

def test_prereleases_excluded_by_default():
    all_versions = ["1.0.0", "1.1.0a1", "1.1.0b1", "1.1.0", "1.2.0"]
    path = get_upgrade_path(all_versions, "1.0.0", "1.2.0", include_prereleases=False)
    assert path == ["1.1.0", "1.2.0"]


def test_prereleases_included_when_requested():
    all_versions = ["1.0.0", "1.1.0a1", "1.1.0b1", "1.1.0", "1.2.0"]
    path = get_upgrade_path(all_versions, "1.0.0", "1.2.0", include_prereleases=True)
    assert path == ["1.1.0a1", "1.1.0b1", "1.1.0", "1.2.0"]


def test_prerelease_toggle_is_deterministic_across_repeated_calls():
    all_versions = ["1.0.0", "1.1.0a1", "1.1.0"]
    result_1 = get_upgrade_path(all_versions, "1.0.0", "1.1.0", include_prereleases=False)
    result_2 = get_upgrade_path(all_versions, "1.0.0", "1.1.0", include_prereleases=False)
    assert result_1 == result_2 == ["1.1.0"]


# --- LLM classifier: disabled by default, never blocks the report ---

def test_llm_disabled_when_no_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm_classifier.is_enabled() is False


def test_llm_enabled_when_api_key_present(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    assert llm_classifier.is_enabled() is True


def test_llm_classification_error_on_bad_response(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")

    class FakeResp:
        status_code = 500
        text = "server error"

    def fake_post(*args, **kwargs):
        return FakeResp()

    monkeypatch.setattr("services.llm_classifier.requests.post", fake_post)
    with pytest.raises(llm_classifier.LLMClassificationError):
        llm_classifier.classify_notes_with_llm("1.0.0", "some release notes text")


def test_report_falls_back_to_keyword_when_llm_disabled(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from services.report_service import categorize_notes_smart
    notes = {"text": "- Fixed a crash on empty input", "url": ""}
    result = categorize_notes_smart("1.0.1", notes)
    assert len(result["Bug Fixes"]) == 1


# --- Known adapters registry covers the requested major packages ---

def test_known_adapters_cover_requested_packages():
    expected = ["django", "numpy", "pandas", "seaborn", "flask", "fastapi",
                "matplotlib", "pydantic", "scikit-learn", "sqlalchemy", "tensorflow"]
    for pkg in expected:
        assert get_known_adapter(pkg) is not None, f"missing adapter for {pkg}"


def test_unknown_package_has_no_known_adapter():
    assert get_known_adapter("some-totally-unknown-package-xyz") is None
