"""
version_service.py

Responsible for:
- Talking to PyPI (Python) or the npm registry (JavaScript) to get package metadata
- Listing all released versions of a package
- Validating that the given versions actually exist
- Computing the exact list of intermediate versions between two versions
- Figuring out the package's GitHub repo (used later to fetch changelogs)
"""

import requests
from packaging import version as pkgversion

PYPI_API = "https://pypi.org/pypi/{package}/json"
NPM_API = "https://registry.npmjs.org/{package}"


class PackageNotFoundError(Exception):
    """Raised when the package does not exist in the given ecosystem's registry."""
    pass


class VersionNotFoundError(Exception):
    """Raised when a given version does not exist for the package."""
    pass


def fetch_package_info(ecosystem, package_name):
    """
    Fetch and normalize package metadata for either ecosystem.
    Returns {"all_versions": [...], "repo": "owner/repo" | None}
    """
    if ecosystem == "npm":
        return _fetch_npm_info(package_name)
    return _fetch_pypi_info(package_name)


def _fetch_pypi_info(package_name):
    url = PYPI_API.format(package=package_name)
    try:
        resp = requests.get(url, timeout=10)
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Network error while contacting PyPI: {e}")

    if resp.status_code == 404:
        raise PackageNotFoundError(f"Package '{package_name}' was not found on PyPI.")
    if resp.status_code != 200:
        raise ConnectionError(f"PyPI returned an unexpected status code: {resp.status_code}")

    data = resp.json()

    all_versions = []
    for v, files in data.get("releases", {}).items():
        if not files:
            continue  # Skip versions with no uploaded files (often yanked/empty)
        try:
            pkgversion.parse(v)
            all_versions.append(v)
        except pkgversion.InvalidVersion:
            continue
    all_versions.sort(key=lambda v: pkgversion.parse(v))

    repo = _extract_pypi_repo(data)
    return {"all_versions": all_versions, "repo": repo}


def _extract_pypi_repo(package_info):
    info = package_info.get("info", {})
    project_urls = info.get("project_urls") or {}

    candidates = []
    for key, url in project_urls.items():
        if url and "github.com" in url:
            candidates.append((key, url))

    home = info.get("home_page") or ""
    if "github.com" in home:
        candidates.append(("home_page", home))

    priority_keys = ["Source", "Source Code", "Repository", "Code", "GitHub", "Homepage", "Home"]
    for pk in priority_keys:
        for key, url in candidates:
            if pk.lower() in key.lower():
                normalized = normalize_github_url(url)
                if normalized:
                    return normalized

    for _, url in candidates:
        normalized = normalize_github_url(url)
        if normalized:
            return normalized

    return None


def _fetch_npm_info(package_name):
    url = NPM_API.format(package=package_name)
    try:
        resp = requests.get(url, timeout=10)
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Network error while contacting the npm registry: {e}")

    if resp.status_code == 404:
        raise PackageNotFoundError(f"Package '{package_name}' was not found on npm.")
    if resp.status_code != 200:
        raise ConnectionError(f"npm registry returned an unexpected status code: {resp.status_code}")

    data = resp.json()

    all_versions = []
    for v in data.get("versions", {}).keys():
        try:
            pkgversion.parse(v)
            all_versions.append(v)
        except pkgversion.InvalidVersion:
            continue
    all_versions.sort(key=lambda v: pkgversion.parse(v))

    repo = None
    repo_field = data.get("repository")
    if isinstance(repo_field, dict):
        repo_url = repo_field.get("url", "")
    elif isinstance(repo_field, str):
        repo_url = repo_field
    else:
        repo_url = ""
    if "github.com" in repo_url:
        repo = normalize_github_url(repo_url)

    return {"all_versions": all_versions, "repo": repo}


def normalize_github_url(url):
    """Turn a GitHub URL (including git+https:// and .git forms) into 'owner/repo', or None."""
    try:
        url = url.strip().rstrip("/")
        url = url.replace("git+", "").replace(".git", "")
        parts = url.split("github.com/")[-1].split("/")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1]}"
    except Exception:
        pass
    return None


def get_upgrade_path(all_versions, current, target, include_prereleases=False):
    """
    Return every version v such that current < v <= target, sorted oldest -> newest.
    Prerelease versions (alpha/beta/rc) are excluded unless include_prereleases=True.
    Raises if current/target don't exist or if target isn't newer than current.
    """
    if current not in all_versions:
        raise VersionNotFoundError(f"Current version '{current}' was not found for this package.")
    if target not in all_versions:
        raise VersionNotFoundError(f"Target version '{target}' was not found for this package.")

    cur_v = pkgversion.parse(current)
    tgt_v = pkgversion.parse(target)

    if cur_v >= tgt_v:
        raise ValueError("Target version must be greater than the current version.")

    path = [v for v in all_versions if cur_v < pkgversion.parse(v) <= tgt_v]

    if not include_prereleases:
        path = [v for v in path if not pkgversion.parse(v).is_prerelease]

    return path
