import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.report_service import categorize_notes, merge_categories, build_report, _find_project_usage, _is_noise


def test_breaking_change_is_categorized():
    notes = {"text": "- This is a breaking change: removed the `foo` argument.", "url": "http://example.com"}
    result = categorize_notes("2.0.0", notes)
    assert len(result["Breaking Changes"]) == 1


def test_security_line_is_categorized():
    notes = {"text": "- Fixed a security vulnerability in token handling (CVE-2024-1234)", "url": ""}
    result = categorize_notes("1.2.3", notes)
    assert len(result["Security Updates"]) == 1


def test_bug_fix_is_categorized():
    notes = {"text": "- Fixed a crash when the input list is empty", "url": ""}
    result = categorize_notes("1.2.3", notes)
    assert len(result["Bug Fixes"]) == 1


def test_docs_only_line_is_filtered_out():
    notes = {"text": "- Updated the README with new install instructions", "url": ""}
    result = categorize_notes("1.2.3", notes)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_test_suite_line_is_filtered_out():
    notes = {"text": "- Added more unit tests for the parser", "url": ""}
    result = categorize_notes("1.2.3", notes)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_thanks_line_is_filtered_out():
    notes = {"text": "- Thanks to @someuser for the contribution!", "url": ""}
    result = categorize_notes("1.2.3", notes)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_bare_reference_line_is_filtered_out():
    notes = {"text": "- (#1234)", "url": ""}
    result = categorize_notes("1.2.3", notes)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_bare_subheader_is_filtered_out():
    notes = {"text": "**Security**\n- Fixed a real security issue with token leakage", "url": ""}
    result = categorize_notes("1.2.3", notes)
    total = sum(len(v) for v in result.values())
    assert total == 1  # only the actual bullet, not the bare "**Security**" header


def test_dedup_across_versions():
    notes_a = {"text": "- Fixed a crash on empty input", "url": ""}
    notes_b = {"text": "- Fixed a crash on empty input", "url": ""}
    cat_a = categorize_notes("1.0.1", notes_a)
    cat_b = categorize_notes("1.0.2", notes_b)
    merged = merge_categories([cat_a, cat_b])
    assert len(merged["Bug Fixes"]) == 1


def test_breaking_change_is_reported_without_semver_claim():
    notes = {"text": "- Breaking change: removed the legacy `connect()` signature.", "url": "http://example.com/1.0.1"}
    report = build_report(
        package="testpkg", ecosystem="pypi", current="1.0.0", target="1.0.1",
        path=["1.0.1"], version_notes=[("1.0.1", notes)], no_notes_versions=[],
    )
    assert "SEMVER VIOLATION" not in report
    assert "[1.0.1 · PATCH]" in report


def test_removed_api_is_classified_as_expired_deprecation():
    notes = {"text": "- numpy.row_stack has been removed; it was deprecated since NumPy 2.0.", "url": "http://example.com"}
    result = categorize_notes("2.5.0", notes)
    assert len(result["Expired Deprecations"]) == 1


def test_compatibility_change_is_classified():
    notes = {"text": "- numpy.where now raises OverflowError for out-of-range Python integers.", "url": "http://example.com"}
    result = categorize_notes("2.5.0", notes)
    assert len(result["Compatibility Changes"]) == 1


def test_orphaned_issue_reference_is_filtered_out():
    notes = {"text": "- (gh-30605)", "url": "http://example.com"}
    result = categorize_notes("2.5.0", notes)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_report_preserves_release_note_section():
    notes = {
        "text": "Expired deprecations\n- numpy.row_stack has been removed; it was deprecated since NumPy 2.0.",
        "url": "http://example.com",
    }
    report = build_report(
        package="testpkg", ecosystem="pypi", current="2.4.6", target="2.5.0",
        path=["2.5.0"], version_notes=[("2.5.0", notes)], no_notes_versions=[],
    )
    assert "Section: Expired deprecations" in report


def test_directives_and_navigation_text_are_filtered_out():
    notes = {
        "text": ".. currentmodule:: numpy\nSee New Features below for other additions.\narr = np.array([1, 2, 3], dtype='m8[s]')",
        "url": "http://example.com",
    }
    result = categorize_notes("2.5.0", notes)
    total = sum(len(v) for v in result.values())
    assert total == 0


def test_descending_sorting_is_classified_as_new_feature():
    notes = {
        "text": "- numpy.sort and numpy.argsort now support descending=True.",
        "url": "http://example.com",
    }
    result = categorize_notes("2.5.0", notes)
    assert len(result["New Features"]) == 1


def test_numpy_cross_is_classified_as_expired_deprecation():
    notes = {"text": "- numpy.cross no longer supports 2-D vectors. Deprecated since 2.0.", "url": "http://example.com"}
    result = categorize_notes("2.5.0", notes)
    assert len(result["Expired Deprecations"]) == 1


def test_numpy_fix_is_classified_as_new_deprecation():
    notes = {"text": "- numpy.fix is deprecated; use numpy.trunc instead.", "url": "http://example.com"}
    result = categorize_notes("2.5.0", notes)
    assert len(result["New Deprecations"]) == 1


def test_numpy_searchsorted_is_classified_as_performance_improvement():
    notes = {"text": "- numpy.searchsorted now uses a binary-search implementation with better cache locality.", "url": "http://example.com"}
    result = categorize_notes("2.5.0", notes)
    assert len(result["Performance Improvements"]) == 1


def test_expired_deprecations_section_is_respected():
    notes = {
        "text": "Expired deprecations\n- Resizing a NumPy array in place is deprecated since mutating an array is unsafe if an array is shared.",
        "url": "http://example.com",
    }
    result = categorize_notes("2.5.0", notes)
    assert len(result["Expired Deprecations"]) == 1
    assert len(result["New Deprecations"]) == 0


def test_deprecations_section_is_classified_as_new_deprecation():
    notes = {
        "text": "Deprecations\n- numpy.fix is deprecated; use numpy.trunc instead.",
        "url": "http://example.com",
    }
    result = categorize_notes("2.5.0", notes)
    assert len(result["New Deprecations"]) == 1
    assert len(result["Expired Deprecations"]) == 0


def test_supporting_paragraphs_stay_with_parent_change():
    notes = {
        "text": "Build / Toolchain Changes\nNumPy Cython headers require Cython 3.0.0 or newer to build.\n\n/path/to/site-packages/numpy/__init__.pxd:11:13: Error in compile-time expression.\n\nNote that the invalid integer is not a bug in NumPy.",
        "url": "http://example.com",
    }
    result = categorize_notes("2.5.0", notes)
    total = sum(len(v) for v in result.values())
    assert total == 1


def test_project_impact_is_reported_for_detected_usage(tmp_path, monkeypatch):
    # The project-usage scan is opt-in (PROJECT_SCAN_ROOT), specifically so
    # it never silently scans this tool's own source tree by default - see
    # report_service.PROJECT_SCAN_ROOT. Point it at the fake project here.
    import services.report_service as report_service_module
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "data.py").write_text("import numpy as np\nnp.row_stack([1, 2])\n", encoding="utf-8")
    monkeypatch.setattr(report_service_module, "PROJECT_SCAN_ROOT", str(tmp_path))

    notes = {"text": "- numpy.row_stack has been removed.", "url": "http://example.com"}
    report = build_report(
        package="numpy", ecosystem="pypi", current="2.4.6", target="2.5.0",
        path=["2.5.0"], version_notes=[("2.5.0", notes)], no_notes_versions=[],
    )
    assert "Confirmed Usage" in report
    assert "numpy.row_stack" in report


def test_unverified_version_is_marked_in_report():
    report = build_report(
        package="testpkg", ecosystem="pypi", current="1.0.0", target="1.0.2",
        path=["1.0.1", "1.0.2"],
        version_notes=[("1.0.2", {"text": "- Fixed a bug in the parser", "url": "http://example.com"})],
        no_notes_versions=["1.0.1"],
    )
    assert "1.0.1" in report
    assert "Unverified" in report


def test_source_url_appears_in_report():
    notes = {"text": "- Fixed a bug causing incorrect totals", "url": "https://example.com/changelog#1.0.1"}
    report = build_report(
        package="testpkg", ecosystem="pypi", current="1.0.0", target="1.0.1",
        path=["1.0.1"], version_notes=[("1.0.1", notes)], no_notes_versions=[],
    )
    assert "https://example.com/changelog#1.0.1" in report


def test_is_noise_short_line():
    assert _is_noise("ok") is True


def test_is_noise_real_content():
    assert _is_noise("Fixed a crash when passing invalid encoding") is False


def test_virtualenv_directories_are_skipped(tmp_path):
    venv_dir = tmp_path / ".venv" / "Lib" / "site-packages"
    venv_dir.mkdir(parents=True)
    (venv_dir / "fake_dep.py").write_text("import numpy as np\nnp.row_stack([1, 2])\n", encoding="utf-8")

    hits = _find_project_usage("numpy.row_stack", str(tmp_path))

    assert hits == []


def test_wildcard_import_is_only_potential_usage(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "data.py").write_text("from numpy import *\nrow_stack([1, 2])\n", encoding="utf-8")

    hits = _find_project_usage("numpy.row_stack", str(tmp_path))

    assert len(hits) == 1
    assert hits[0]["confidence"] == "LOW"
    assert hits[0]["kind"] == "potential"


def test_unrelated_subsection_does_not_bleed_into_previous_entry():
    # Regression test for a real bug found against NumPy's actual 2.5.0
    # release notes: an RST subsection heading not in _SECTION_NAMES
    # (e.g. "Compatibility notes" followed by its own sub-heading) let
    # unrelated prose merge into the previous, unrelated deprecation
    # entry, because "current_section" only reset on *known* headings.
    text = (
        "Expired Deprecations\n"
        "=====================\n\n"
        "* ``_add_newdoc_ufunc(ufunc, newdoc)`` has been removed in favor of\n"
        "  ``ufunc.__doc__ = newdoc``.\n"
        "  (deprecated since 2.2)\n\n"
        "Compatibility notes\n"
        "===================\n\n"
        "``linalg.eig`` now always returns complex arrays\n"
        "--------------------------------------------------\n"
        "Previously, the return values depended on whether the eigenvalues\n"
        "happen to lie on the real line, which is not guaranteed.\n"
    )
    notes = {"text": text, "url": "http://example.com"}
    result = categorize_notes("2.5.0", notes)
    all_lines = [line for entries in result.values() for _, line, _ in entries]

    deprecation_line = next((l for l in all_lines if "_add_newdoc_ufunc" in l), None)
    assert deprecation_line is not None
    # The unrelated "eigenvalues" content must NOT have bled into this entry
    assert "eigenvalues" not in deprecation_line
    assert "real line" not in deprecation_line
