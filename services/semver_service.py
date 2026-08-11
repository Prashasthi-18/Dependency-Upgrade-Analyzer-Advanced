"""
semver_service.py

Classifies the "size" of a version bump (major / minor / patch / prerelease).
This is what lets the report say "this was supposed to be a patch release
but it contains a breaking change" - the single most useful reliability
signal we can surface.
"""

from packaging import version as pkgversion


def classify_bump(prev_version, current_version):
    """
    Return 'major', 'minor', 'patch', or 'prerelease' describing the step
    from prev_version to current_version.
    """
    v = pkgversion.parse(current_version)

    if v.is_prerelease or v.is_devrelease:
        return "prerelease"

    p = pkgversion.parse(prev_version)

    def _padded_release(parsed):
        parts = list(parsed.release) + [0, 0, 0]
        return parts[:3]

    v_parts = _padded_release(v)
    p_parts = _padded_release(p)

    if v_parts[0] != p_parts[0]:
        return "major"
    if v_parts[1] != p_parts[1]:
        return "minor"
    return "patch"


def is_prerelease(version_str):
    return pkgversion.parse(version_str).is_prerelease or pkgversion.parse(version_str).is_devrelease


BUMP_LABELS = {
    "major": "MAJOR",
    "minor": "MINOR",
    "patch": "PATCH",
    "prerelease": "PRE-RELEASE",
}
