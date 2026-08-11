"""
security_advisory_service.py

Pulls structured vulnerability data from GitHub's Security Advisory
database (the same data that powers Dependabot/OSV), keyed by ecosystem
+ package. This is a higher-confidence source for the Security Updates
category than keyword-matching changelog text, because it's a curated,
structured record of "this package, this version range, this CVE" rather
than free text that might mention the word "security" for unrelated reasons.

This is an additive source: it runs alongside (not instead of) the normal
changelog/release-notes chain, and never blocks the rest of the report if
it fails or the API is rate-limited - a missing advisory check just means
one extra confidence signal is absent, not that the whole run fails.
"""

import os
import re

import requests

from services import cache_service
from packaging import version as pkgversion

GITHUB_API = "https://api.github.com"

# GitHub's Advisory Database uses its own ecosystem identifiers
_ECOSYSTEM_MAP = {"pypi": "pip", "npm": "npm"}


class AdvisoryFetchError(Exception):
    pass


def _github_get(url, params=None):
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
    except requests.exceptions.RequestException as e:
        raise AdvisoryFetchError(str(e))
    return resp


_RANGE_CONDITION_RE = re.compile(r'(>=|<=|>|<|=)\s*([\w.\-]+)')


def _version_matches_range(version_str, range_str):
    """
    Evaluate simple comma-separated range expressions like
    '>= 1.0.0, < 1.2.0' against a specific version. Best-effort: if the
    range can't be parsed, we conservatively return False rather than
    risk a false positive security claim.
    """
    if not range_str:
        return False
    try:
        v = pkgversion.parse(version_str)
    except pkgversion.InvalidVersion:
        return False

    conditions = _RANGE_CONDITION_RE.findall(range_str)
    if not conditions:
        return False

    for op, bound_str in conditions:
        try:
            bound = pkgversion.parse(bound_str)
        except pkgversion.InvalidVersion:
            return False
        if op == ">=" and not (v >= bound):
            return False
        if op == "<=" and not (v <= bound):
            return False
        if op == ">" and not (v > bound):
            return False
        if op == "<" and not (v < bound):
            return False
        if op == "=" and not (v == bound):
            return False
    return True


def _fetch_advisories_for_package(ecosystem, package):
    """Fetch the full advisory list for a package (cached per-package, not per-version)."""
    cached = cache_service.get(ecosystem, package, "__all__", source="ghsa")
    if cached is not cache_service.MISS:
        return cached or []

    gh_ecosystem = _ECOSYSTEM_MAP.get(ecosystem)
    if not gh_ecosystem:
        return []

    try:
        resp = _github_get(f"{GITHUB_API}/advisories",
                            params={"ecosystem": gh_ecosystem, "affects": package, "per_page": 100})
    except AdvisoryFetchError:
        return []  # Never block the report over an optional signal

    if resp.status_code != 200:
        cache_service.set(ecosystem, package, "__all__", None, source="ghsa")
        return []

    advisories = resp.json()
    cache_service.set(ecosystem, package, "__all__", advisories, source="ghsa")
    return advisories


def get_advisories_for_version(ecosystem, package, version):
    """
    Returns a list of {"summary", "severity", "url", "ghsa_id"} dicts for
    advisories that specifically affect this version, or [] if none/unavailable.
    """
    advisories = _fetch_advisories_for_package(ecosystem, package)
    gh_ecosystem = _ECOSYSTEM_MAP.get(ecosystem)
    matches = []

    for advisory in advisories:
        for vuln in advisory.get("vulnerabilities", []) or []:
            pkg_info = vuln.get("package") or {}
            if pkg_info.get("ecosystem") != gh_ecosystem:
                continue
            if pkg_info.get("name", "").lower() != package.lower():
                continue
            if _version_matches_range(version, vuln.get("vulnerable_version_range", "")):
                matches.append({
                    "summary": advisory.get("summary", "").strip(),
                    "severity": advisory.get("severity", "unknown"),
                    "url": advisory.get("html_url", ""),
                    "ghsa_id": advisory.get("ghsa_id", ""),
                })
                break  # Don't double count the same advisory for multiple vuln entries

    return matches
