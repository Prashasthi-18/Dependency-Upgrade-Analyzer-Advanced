"""
source_service.py

Retrieves the raw change notes for a single version, following this
strict priority order:

    0. Known official adapter, if this package has one (see known_sources.py)
    1. GitHub Releases (official release notes attached to a tag)
    2. CHANGELOG.md / HISTORY.md / CHANGES.md in the repo (official changelog)
    3. GitHub commit messages between the previous tag and this tag
       (last resort, only used if nothing official is found)

Every result includes a direct source URL so the report can show exactly
where each line came from, instead of asking the reader to trust it blindly.
Results are cached on disk (see cache_service.py) so re-running the same
query doesn't re-hit GitHub every time, and network calls retry with
backoff instead of failing on the first hiccup.
"""

import os # used to access the environment variables
import re # regex - regular expression
import time  # Used for retry delays
import requests

from services import cache_service

GITHUB_API = "https://api.github.com"

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5


class SourceFetchError(Exception):
    """Raised for network/rate-limit problems talking to GitHub."""
    pass


def _with_retries(func, *args, **kwargs):
    """Retry a network call with exponential backoff on transient failures."""
    last_error = None  # Stores the most recent exception

    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))  # 0.5 × 2⁰ = 0.5 sec  
    raise SourceFetchError(f"Network error after {MAX_RETRIES} attempts: {last_error}") #exponential backoff.


def _github_get(url, params=None):
    headers = {"Accept": "application/vnd.github+json"}
    # Unauthenticated requests are capped at 60/hour, which is easy to hit.
    # Setting a GITHUB_TOKEN env var (no special scopes needed) raises this to 5000/hour.
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = _with_retries(requests.get, url, headers=headers, params=params, timeout=10) # GitHub requests automatically receive retry behavior.

    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        if token:
            raise SourceFetchError("GitHub API rate limit exceeded even with a token. Please try again later.")
        raise SourceFetchError(
            "GitHub API rate limit exceeded (60 requests/hour without authentication). "
            "Set a GITHUB_TOKEN environment variable to raise this to 5000 requests/hour."
        )

    return resp


def fetch_raw_file(url):
    """Fetch a raw file (e.g. from raw.githubusercontent.com). Returns text, or None if not found."""
    try:
        resp = _with_retries(requests.get, url, timeout=10)
    except SourceFetchError:
        return None
    if resp.status_code == 200 and resp.text.strip():
        return resp.text
    return None


def get_release_notes_for_version(repo, package_name, version):
    """Source priority #1: GitHub Releases API, trying common tag naming styles."""
    repo_name = repo.split("/")[-1]
    tag_candidates = [  # 2.32.0 , v2.32.0, v2.32.0, requests==2.32.0, release-2.32.0
        version,
        f"v{version}",
        f"V{version}",
        f"release-{version}",
        f"{repo_name}-{version}",
        f"{package_name}=={version}",  # common in monorepos (e.g. langchain)
    ]
    for tag in tag_candidates:
        resp = _github_get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}")
        if resp.status_code == 200:
            data = resp.json()
            body = (data.get("body") or "").strip()
            if body:
                return {
                    "source": "GitHub Release Notes",
                    "text": body,
                    "url": data.get("html_url", f"https://github.com/{repo}/releases/tag/{tag}"),
                }
    return None


_UNDERLINE_RE = re.compile(r'^[-=~^]{3,}\s*$')
_VERSION_LIKE_RE = re.compile(r'\d+(?:\.\d+){1,3}')


def _find_heading_lines(lines):
    """
    Return the line index of every heading in the changelog, supporting:
      - Markdown ATX headings: '## 2.32.0 (2024-05-20)'
      - RST/Setext headings: a version line followed by a line of ---/===/~~~
    Only headings that actually mention a version-like number are returned,
    since changelogs also have non-version headings (e.g. 'Improvements').
    """
    headings = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if not _VERSION_LIKE_RE.search(stripped):
            continue
        if stripped.startswith("#"):
            headings.append(i)
        elif i + 1 < len(lines) and _UNDERLINE_RE.match(lines[i + 1].strip()):
            headings.append(i)
    return headings


def extract_version_section(text, version):
    """Find the heading that mentions this exact version, and grab everything until the next heading."""
    lines = text.splitlines()
    heading_indices = _find_heading_lines(lines)

    version_pattern = re.compile(r'(?<!\d)' + re.escape(version) + r'(?!\d)')
    match_idx = None
    for idx in heading_indices:
        if version_pattern.search(lines[idx]):
            match_idx = idx
            break

    if match_idx is None:
        return None

    later_headings = [h for h in heading_indices if h > match_idx]
    end_idx = later_headings[0] if later_headings else len(lines)

    # Skip the heading line itself, and its underline (if RST-style), since
    # it's just a label ("2.32.0 -------") and not an actual change.
    body_start = match_idx + 1
    if body_start < len(lines) and _UNDERLINE_RE.match(lines[body_start].strip()):
        body_start += 1

    section = "\n".join(lines[body_start:end_idx]).strip()
    return section if section else None


def get_changelog_section(repo, version):
    """Source priority #2: CHANGELOG.md (or similar) in the repo, matched to this version."""
    for branch in ["main", "master"]:
        for filename in ["CHANGELOG.md", "CHANGELOG.rst", "CHANGES.md", "CHANGES.rst",
                          "HISTORY.md", "HISTORY.rst", "History.md", "Changelog.md",
                          "changelog.md", "CHANGES.txt"]:
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/{filename}"
            text = fetch_raw_file(url)
            if text:
                section = extract_version_section(text, version)
                if section:
                    return {
                        "source": f"CHANGELOG ({filename})",
                        "text": section,
                        "url": f"https://github.com/{repo}/blob/{branch}/{filename}",
                    }
    return None


def get_commits_for_version(repo, version, prev_version):
    """Source priority #3 (last resort): commit messages between the previous tag and this tag."""
    if not prev_version:
        return None

    tag_new_candidates = [f"v{version}", version]
    tag_old_candidates = [f"v{prev_version}", prev_version]

    for tag_old in tag_old_candidates:
        for tag_new in tag_new_candidates:
            url = f"{GITHUB_API}/repos/{repo}/compare/{tag_old}...{tag_new}"
            resp = _github_get(url)
            if resp.status_code == 200:
                data = resp.json()
                commits = data.get("commits", [])
                messages = [c["commit"]["message"].split("\n")[0] for c in commits]
                messages = [m for m in messages if m.strip()]
                if messages:
                    return {
                        "source": "GitHub Commits",
                        "text": "\n".join(f"- {m}" for m in messages),
                        "url": f"https://github.com/{repo}/compare/{tag_old}...{tag_new}",
                    }
    return None


def _word_set(text):
    return set(re.findall(r'\w+', text.lower()))


def _detect_discrepancy(notes_a, notes_b):
    """
    Crude but effective: if two 'official' sources for the same version
    share almost no vocabulary, they're very likely describing different
    things and should be flagged rather than silently picked between.
    """
    words_a = _word_set(notes_a["text"])
    words_b = _word_set(notes_b["text"])
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b) / len(words_a | words_b)
    return overlap < 0.15


def fetch_notes_for_version(package_name, repo, version, prev_version=None, ecosystem="pypi"):
    """
    Try each source in priority order and return a result dict:
        {
          "notes": <primary notes dict, or None if nothing found>,
          "cross_check": <secondary notes dict, or None>,
          "discrepancy": bool,
          "unavailable": bool,   # True if a network/rate-limit error is why
                                  # nothing was found, as opposed to there
                                  # genuinely being no notes for this version
          "npm_deprecation": <notes dict, or None>,  # npm-only signal
        }

    Individual source failures (e.g. GitHub rate limit) are caught and
    logged internally rather than aborting the whole lookup - we move on
    to the next source in the chain. Results are cached per-source so
    repeated runs, and future cross-checks, don't re-hit the network.
    """
    result = {"notes": None, "cross_check": None, "discrepancy": False,
              "unavailable": False, "npm_deprecation": None}

    cached = cache_service.get(ecosystem, package_name, version, source="resolved")
    if cached is not cache_service.MISS:
        result["notes"] = cached
        return result

    if not repo:
        return result

    if ecosystem == "npm":
        from services.npm_source_service import get_npm_deprecation_notice
        result["npm_deprecation"] = get_npm_deprecation_notice(package_name, version)

    had_error = False
    primary = None
    primary_kind = None

    from services.known_sources import get_known_adapter
    adapter = get_known_adapter(package_name)
    if adapter:
        primary = adapter(repo, version)
        if primary:
            primary_kind = "known_adapter"

    if not primary:
        try:
            primary = get_release_notes_for_version(repo, package_name, version)
            if primary:
                primary_kind = "github_release"
        except SourceFetchError:
            had_error = True

    if not primary:
        primary = get_changelog_section(repo, version)  # uses fetch_raw_file, never raises
        if primary:
            primary_kind = "changelog"

    if not primary:
        try:
            primary = get_commits_for_version(repo, version, prev_version)
            if primary:
                primary_kind = "commits"
        except SourceFetchError:
            had_error = True

    # Cross-check: when the primary result came from a curated source
    # (known adapter or CHANGELOG), also check GitHub Releases as an
    # independent confirmation, and flag it if they meaningfully disagree.
    if primary and primary_kind in ("known_adapter", "changelog"):
        try:
            secondary = get_release_notes_for_version(repo, package_name, version)
        except SourceFetchError:
            secondary = None
        if secondary:
            result["cross_check"] = secondary
            result["discrepancy"] = _detect_discrepancy(primary, secondary)

    result["notes"] = primary
    result["unavailable"] = primary is None and had_error

    # Only cache a definitive "not found" (not one caused by a transient error)
    if not (result["unavailable"]):
        cache_service.set(ecosystem, package_name, version, primary, source="resolved")

    return result
