"""
Golden regression tests for the normalized-change-model pipeline.

Every text sample here is the ACTUAL text from NumPy's real 2.5.0
release notes (verified live against
https://raw.githubusercontent.com/numpy/numpy/main/doc/source/release/2.5.0-notes.rst
while building this fix) - not synthetic approximations. These are the
exact cases that were previously misidentified, so this file is the
regression guard against that specific class of bug recurring.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from services.report_service import (
    _identify_change_subject,
    _find_project_usage,
    _find_attribute_assignment_usage,
    _compute_change_risk,
    _compute_overall_status,
    _usage_status_from_hits,
    NOT_SCANNED,
    SCANNED_NOT_FOUND,
    SCANNED_POTENTIAL,
    SCANNED_CONFIRMED,
)


# ============================================================
# GOLDEN CASE 1: in-place resizing
# Real text: "Resizing a Numpy array in place is deprecated ... As an
# alternative, you can create a resized array via np.resize."
# np.resize is the REPLACEMENT, not the deprecated subject.
# ============================================================

GOLDEN_RESIZE_TEXT = (
    "Resizing a Numpy array in place is deprecated since mutating an array is "
    "unsafe if an array is shared, especially by multiple threads.  As an "
    "alternative, you can create a resized array via np.resize."
)


def test_golden_resize_does_not_misidentify_replacement_as_subject():
    api, operation, replacement, confidence, attr_name = _identify_change_subject(GOLDEN_RESIZE_TEXT)
    assert api is None, "np.resize must NOT be reported as the deprecated API - it's the replacement"
    assert operation is not None
    assert "resiz" in operation.lower()
    assert "in place" in operation.lower() or "in-place" in operation.lower()
    assert replacement == "numpy.resize"


def test_golden_resize_np_resize_call_is_not_flagged_as_the_deprecated_api(tmp_path):
    # np.resize(...) is the RECOMMENDED replacement - calling it should
    # never be reported as usage of a deprecated/removed API.
    (tmp_path / "main.py").write_text(
        "import numpy as np\nresult = np.resize(arr, (3, 3))\n", encoding="utf-8"
    )
    # The golden case has no literal deprecated API name, so nothing
    # should be searched for under that name at all.
    api, operation, replacement, confidence, attr_name = _identify_change_subject(GOLDEN_RESIZE_TEXT)
    assert api is None


# ============================================================
# GOLDEN CASE 2: shape attribute assignment
# Real text: "Setting the shape attribute is deprecated ... As an
# alternative, you can create a new view via np.reshape or
# np.ndarray.reshape."
# np.reshape is the REPLACEMENT, not the deprecated subject.
# ============================================================

GOLDEN_SHAPE_TEXT = (
    "Setting the shape attribute is deprecated because mutating an array is "
    "unsafe if an array is shared, especially by multiple threads.  As an "
    "alternative, you can create a new view via np.reshape or np.ndarray.reshape."
)


def test_golden_shape_assignment_does_not_misidentify_reshape_as_subject():
    api, operation, replacement, confidence, attr_name = _identify_change_subject(GOLDEN_SHAPE_TEXT)
    assert api is None, "np.reshape must NOT be reported as deprecated - it's the replacement"
    assert operation == "shape attribute assignment"
    assert replacement == "numpy.reshape"
    assert attr_name == "shape", "attribute name must be extracted so the AST scanner can check for it"


def test_golden_reshape_call_is_not_confused_with_shape_assignment(tmp_path):
    (tmp_path / "main.py").write_text(
        "import numpy as np\nx = np.reshape(arr, (3, 5))\n", encoding="utf-8"
    )
    # np.reshape is a function call, not an attribute assignment - the
    # attribute-assignment scanner must not match it.
    hits = _find_attribute_assignment_usage("shape", str(tmp_path))
    assert hits == []


def test_golden_actual_shape_assignment_is_detected(tmp_path):
    (tmp_path / "main.py").write_text(
        "arr.shape = (3, 5)\n", encoding="utf-8"
    )
    hits = _find_attribute_assignment_usage("shape", str(tmp_path))
    assert len(hits) == 1
    assert hits[0]["kind"] == "potential"  # static analysis can't prove arr is ndarray
    assert hits[0]["confidence"] == "MEDIUM"


# ============================================================
# GOLDEN CASE 3: row_stack - a genuine removal with an explicit subject
# Real text: "numpy.row_stack has been removed in favor of numpy.vstack."
# Here the subject IS explicit, unlike cases 1 and 2 - the extractor
# must still get this right and not regress to the old naive behavior.
# ============================================================

GOLDEN_ROW_STACK_TEXT = "numpy.row_stack has been removed in favor of numpy.vstack. (deprecated since 2.0)"


def test_golden_row_stack_subject_correctly_identified():
    api, operation, replacement, confidence, attr_name = _identify_change_subject(GOLDEN_ROW_STACK_TEXT)
    assert api == "numpy.row_stack"
    assert replacement == "numpy.vstack"
    assert confidence == "HIGH"


def test_golden_row_stack_direct_call_detected(tmp_path):
    (tmp_path / "main.py").write_text(
        "import numpy as np\nresult = np.row_stack(data)\n", encoding="utf-8"
    )
    hits = _find_project_usage("numpy.row_stack", str(tmp_path))
    assert len(hits) == 1
    assert hits[0]["kind"] == "confirmed"
    assert hits[0]["confidence"] == "HIGH"
    assert "np.row_stack" in hits[0]["code"]


def test_golden_row_stack_from_import_detected(tmp_path):
    (tmp_path / "main.py").write_text(
        "from numpy import row_stack\nresult = row_stack(data)\n", encoding="utf-8"
    )
    hits = _find_project_usage("numpy.row_stack", str(tmp_path))
    assert len(hits) == 1
    assert hits[0]["kind"] == "confirmed"


def test_golden_row_stack_aliased_import_detected(tmp_path):
    (tmp_path / "main.py").write_text(
        "import numpy as n\nresult = n.row_stack(data)\n", encoding="utf-8"
    )
    hits = _find_project_usage("numpy.row_stack", str(tmp_path))
    assert len(hits) == 1
    assert hits[0]["kind"] == "confirmed"


def test_golden_row_stack_variable_reassignment_detected(tmp_path):
    # stack = np.row_stack; stack(data) - explicitly named in the spec
    (tmp_path / "main.py").write_text(
        "import numpy as np\nstack = np.row_stack\nresult = stack(data)\n", encoding="utf-8"
    )
    hits = _find_project_usage("numpy.row_stack", str(tmp_path))
    assert len(hits) == 1
    assert hits[0]["kind"] == "confirmed"


def test_golden_row_stack_in_comment_is_not_usage(tmp_path):
    (tmp_path / "main.py").write_text(
        "# np.row_stack is deprecated, don't use it\nresult = 1 + 1\n", encoding="utf-8"
    )
    hits = _find_project_usage("numpy.row_stack", str(tmp_path))
    assert hits == [], "a comment must never be reported as actual usage"


def test_golden_row_stack_in_string_literal_is_not_usage(tmp_path):
    (tmp_path / "main.py").write_text(
        'message = "np.row_stack is deprecated"\n', encoding="utf-8"
    )
    hits = _find_project_usage("numpy.row_stack", str(tmp_path))
    assert hits == [], "a string literal must never be reported as actual usage"


def test_golden_row_stack_wildcard_import_is_only_potential(tmp_path):
    (tmp_path / "main.py").write_text(
        "from numpy import *\nresult = row_stack(data)\n", encoding="utf-8"
    )
    hits = _find_project_usage("numpy.row_stack", str(tmp_path))
    assert len(hits) == 1
    assert hits[0]["kind"] == "potential"  # can't prove row_stack came from numpy


# ============================================================
# GOLDEN CASE 4: trailing citation clutter must not pollute the
# extracted operation/title text (real bug found against 2.5.0's
# actual bincount entry, which has no "is deprecated"/"has been
# removed" boundary phrase - just a trailing "(deprecated since X)").
# ============================================================

GOLDEN_BINCOUNT_TEXT = (
    "bincount now raises a TypeError for non-integer inputs. "
    "(deprecated since 2.1) (gh-30610)"
)


def test_golden_citation_suffix_stripped_from_operation():
    from services.report_service import _identify_change_subject
    api, operation, replacement, confidence, attr_name = _identify_change_subject(GOLDEN_BINCOUNT_TEXT)
    assert operation is not None
    assert "(gh-30610)" not in operation
    assert "(deprecated since 2.1)" not in operation
    assert operation.endswith("non-integer inputs.")


# ============================================================
# Scan status: four explicit states, never conflating "not scanned"
# with "not affected"
# ============================================================

def test_usage_status_not_scanned_when_no_scan_root():
    assert _usage_status_from_hits([], scan_root=None) == NOT_SCANNED


def test_usage_status_scanned_not_found():
    assert _usage_status_from_hits([], scan_root="/some/project") == SCANNED_NOT_FOUND


def test_usage_status_scanned_potential():
    hits = [{"kind": "potential"}]
    assert _usage_status_from_hits(hits, scan_root="/some/project") == SCANNED_POTENTIAL


def test_usage_status_scanned_confirmed():
    hits = [{"kind": "confirmed"}]
    assert _usage_status_from_hits(hits, scan_root="/some/project") == SCANNED_CONFIRMED


# ============================================================
# Risk engine: exact matrix from the spec
# ============================================================

def test_risk_removed_api_confirmed_usage_is_critical():
    assert _compute_change_risk("HIGH", SCANNED_CONFIRMED) == "CRITICAL"


def test_risk_removed_api_scanned_not_found_is_low():
    assert _compute_change_risk("HIGH", SCANNED_NOT_FOUND) == "LOW"


def test_risk_removed_api_not_scanned_is_unknown():
    assert _compute_change_risk("HIGH", NOT_SCANNED) == "UNKNOWN"


def test_risk_never_reports_unknown_as_low_or_safe():
    # UNKNOWN must be its own distinct value, not silently folded into
    # a "safe-looking" level.
    result = _compute_change_risk("HIGH", NOT_SCANNED)
    assert result not in ("LOW", "INFO")
    assert result == "UNKNOWN"


def test_overall_status_not_scanned_with_high_severity_changes_is_unknown():
    changes = [{"baseline_severity": "HIGH", "usage_status": NOT_SCANNED}]
    assert _compute_overall_status(changes, scan_root=None) == "UNKNOWN"


def test_overall_status_scanned_confirmed_is_critical():
    changes = [{"baseline_severity": "HIGH", "usage_status": SCANNED_CONFIRMED}]
    assert _compute_overall_status(changes, scan_root="/some/project") == "CRITICAL"


def test_overall_status_scanned_not_found_is_low():
    changes = [{"baseline_severity": "HIGH", "usage_status": SCANNED_NOT_FOUND}]
    assert _compute_overall_status(changes, scan_root="/some/project") == "LOW"


# ============================================================
# Full pipeline: verdict must never say "safe" when not scanned
# ============================================================

def test_full_report_never_says_safe_when_not_scanned(monkeypatch):
    import services.report_service as report_service_module
    monkeypatch.setattr(report_service_module, "PROJECT_SCAN_ROOT", None)

    notes = {"text": GOLDEN_ROW_STACK_TEXT, "url": "http://example.com"}
    report = report_service_module.build_upgrade_summary_report(
        package="numpy", ecosystem="pypi", current="2.4.6", target="2.5.0",
        path=["2.5.0"], version_notes=[("2.5.0", notes)], no_notes_versions=[],
    )
    assert "SAFE TO UPGRADE" not in report
    assert "Overall Risk: UNKNOWN" in report
    assert "CONDITIONAL" in report


def test_full_report_escalates_correctly_with_confirmed_removed_api_usage(tmp_path, monkeypatch):
    import services.report_service as report_service_module
    (tmp_path / "main.py").write_text(
        "import numpy as np\nresult = np.row_stack(data)\n", encoding="utf-8"
    )
    monkeypatch.setattr(report_service_module, "PROJECT_SCAN_ROOT", str(tmp_path))

    notes = {"text": GOLDEN_ROW_STACK_TEXT, "url": "http://example.com"}
    report = report_service_module.build_upgrade_summary_report(
        package="numpy", ecosystem="pypi", current="2.4.6", target="2.5.0",
        path=["2.5.0"], version_notes=[("2.5.0", notes)], no_notes_versions=[],
    )
    assert "Overall Risk: CRITICAL" in report
    assert "UPGRADE BLOCKED" in report
    assert "numpy.row_stack" in report
    assert "numpy.vstack" in report  # the replacement must be surfaced


def test_full_report_never_invents_api_for_resize_case(monkeypatch):
    import services.report_service as report_service_module
    monkeypatch.setattr(report_service_module, "PROJECT_SCAN_ROOT", None)

    notes = {"text": GOLDEN_RESIZE_TEXT, "url": "http://example.com"}
    report = report_service_module.build_upgrade_summary_report(
        package="numpy", ecosystem="pypi", current="2.4.6", target="2.5.0",
        path=["2.5.0"], version_notes=[("2.5.0", notes)], no_notes_versions=[],
    )
    assert "numpy.resize deprecat" not in report.lower()
    assert "resizing" in report.lower()


def test_full_report_never_invents_api_for_shape_case(monkeypatch):
    import services.report_service as report_service_module
    monkeypatch.setattr(report_service_module, "PROJECT_SCAN_ROOT", None)

    notes = {"text": GOLDEN_SHAPE_TEXT, "url": "http://example.com"}
    report = report_service_module.build_upgrade_summary_report(
        package="numpy", ecosystem="pypi", current="2.4.6", target="2.5.0",
        path=["2.5.0"], version_notes=[("2.5.0", notes)], no_notes_versions=[],
    )
    assert "numpy.reshape deprecat" not in report.lower()
    assert "shape attribute" in report.lower()


# ============================================================
# Noise filtering in the dump-style sections (New Features,
# Build/Toolchain): example error output and orphaned citation
# fragments must not appear as if they were real entries.
# ============================================================

def test_build_toolchain_filters_example_error_output():
    from services.report_service import _looks_like_noise_fragment
    assert _looks_like_noise_fragment(
        "/path/to/site-packages/numpy/__init__.pxd:11:13: Error in compile-time expression."
    ) is True
    assert _looks_like_noise_fragment(
        "NumPy now requires minimum MSVC 19.35 toolchain version on Windows."
    ) is False


def test_new_features_filters_note_that_asides():
    from services.report_service import _looks_like_noise_fragment
    assert _looks_like_noise_fragment(
        "Note that SIMD optimizations for sorting are not available for descending sorts."
    ) is True
    assert _looks_like_noise_fragment("Added support for descending sorts.") is False
