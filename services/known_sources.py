"""
known_sources.py

For a handful of major, high-traffic packages, generic "look for a
CHANGELOG.md" logic isn't good enough - these projects publish detailed,
curated official release notes as source files in their own repo, which
is what actually powers their docs website. Reading that file directly is
more reliable than scraping the rendered docs site (which we also can't
easily parse generically) and more authoritative than falling back to
raw commit messages.

Each adapter below points at a real, verified file path pattern in the
project's GitHub repo. If a given version doesn't have a matching file
(happens sometimes for older patch releases), the caller falls back to
the normal GitHub Releases -> CHANGELOG -> commits chain automatically.

NOTE: these paths were verified against the live repos at the time this
was written. Projects occasionally restructure their docs; if an adapter
stops matching, the generic fallback chain still kicks in, so nothing
breaks - it just becomes slightly less precise for that one package.
"""

from services.source_service import (
    fetch_raw_file,
    extract_version_section,
)
import re

RAW_BASE = "https://raw.githubusercontent.com"


def _blob_url(repo, branch, path):
    return f"https://github.com/{repo}/blob/{branch}/{path}"


def _django_adapter(repo, version):
    # Each release gets its own full notes file - the whole file IS the notes.
    for branch in ["main", "master"]:
        path = f"docs/releases/{version}.txt"
        text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if text:
            return {"source": "Django Official Release Notes", "text": text,
                    "url": _blob_url(repo, branch, path)}
    return None


def _numpy_adapter(repo, version):
    for branch in ["main", "master"]:
        path = f"doc/source/release/{version}-notes.rst"
        text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if text:
            return {"source": "NumPy Official Release Notes", "text": text,
                     "url": _blob_url(repo, branch, path)}
    return None


def _pandas_adapter(repo, version):
    for branch in ["main", "master"]:
        path = f"doc/source/whatsnew/v{version}.rst"
        text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if text:
            return {"source": "pandas Official What's New", "text": text,
                     "url": _blob_url(repo, branch, path)}
    return None


def _seaborn_adapter(repo, version):
    for branch in ["main", "master"]:
        path = f"doc/whatsnew/v{version}.rst"
        text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if text:
            return {"source": "seaborn Official What's New", "text": text,
                     "url": _blob_url(repo, branch, path)}
    return None


def _flask_adapter(repo, version):
    for branch in ["main", "master"]:
        path = "CHANGES.rst"
        full_text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if full_text:
            section = extract_version_section(full_text, version)
            if section:
                return {"source": "Flask Official Changelog", "text": section,
                         "url": _blob_url(repo, branch, path)}
    return None


def _fastapi_adapter(repo, version):
    for branch in ["master", "main"]:
        path = "docs/en/docs/release-notes.md"
        full_text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if full_text:
            section = extract_version_section(full_text, version)
            if section:
                return {"source": "FastAPI Official Release Notes", "text": section,
                         "url": _blob_url(repo, branch, path) + f"#{version.replace('.', '')}"}
    return None


def _matplotlib_adapter(repo, version):
    # Matplotlib only publishes a dedicated "what's new" file for X.Y.0
    # releases, not for every patch - patch releases fall back to GitHub.
    parts = version.split(".")
    if len(parts) < 2:
        return None
    minor_version = f"{parts[0]}.{parts[1]}.0"
    for branch in ["main", "master"]:
        path = f"doc/users/prev_whats_new/whats_new_{minor_version}.rst"
        text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if text:
            return {"source": "Matplotlib Official What's New", "text": text,
                     "url": _blob_url(repo, branch, path)}
    return None


def _pydantic_adapter(repo, version):
    for branch in ["main", "master"]:
        path = "HISTORY.md"
        full_text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if full_text:
            section = extract_version_section(full_text, version)
            if section:
                return {"source": "pydantic Official Changelog", "text": section,
                         "url": _blob_url(repo, branch, path)}
    return None


def _scikit_learn_adapter(repo, version):
    parts = version.split(".")
    if len(parts) < 2:
        return None
    minor_file_version = f"{parts[0]}.{parts[1]}"
    for branch in ["main", "master"]:
        path = f"doc/whats_new/v{minor_file_version}.rst"
        full_text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if full_text:
            section = extract_version_section(full_text, version)
            if section:
                return {"source": "scikit-learn Official What's New", "text": section,
                         "url": _blob_url(repo, branch, path)}
    return None


_SQLALCHEMY_VERSION_BLOCK_RE = re.compile(
    r'\.\. changelog::\s*\n\s*:version:\s*([\d.]+)(.*?)(?=\n\.\. changelog::|\Z)',
    re.DOTALL,
)
_SQLALCHEMY_FIELD_LINE_RE = re.compile(r'^\s*:\w[\w-]*:.*$', re.MULTILINE)


def _sqlalchemy_adapter(repo, version):
    # SQLAlchemy's changelog uses a custom Sphinx directive
    # (".. changelog:: / :version: X.Y.Z / .. change:: ...") rather than
    # plain headings, so it needs its own small parser instead of the
    # generic heading-based extractor.
    parts = version.split(".")
    if len(parts) < 2:
        return None
    file_suffix = f"{parts[0]}{parts[1]}"  # e.g. "2.0.5" -> "20", "1.4.3" -> "14"
    for branch in ["main", "master"]:
        path = f"doc/build/changelog/changelog_{file_suffix}.rst"
        full_text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if not full_text:
            continue
        for match in _SQLALCHEMY_VERSION_BLOCK_RE.finditer(full_text):
            block_version, block_body = match.group(1), match.group(2)
            if block_version != version:
                continue
            # Strip Sphinx directive/field markers, keep the human-readable prose
            cleaned = _SQLALCHEMY_FIELD_LINE_RE.sub('', block_body)
            cleaned = re.sub(r'\.\. change::\s*', '', cleaned)
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
            if cleaned:
                return {"source": "SQLAlchemy Official Changelog", "text": cleaned,
                         "url": _blob_url(repo, branch, path)}
    return None


def _tensorflow_adapter(repo, version):
    for branch in ["master", "main"]:
        path = "RELEASE.md"
        full_text = fetch_raw_file(f"{RAW_BASE}/{repo}/{branch}/{path}")
        if full_text:
            section = extract_version_section(full_text, version)
            if section:
                return {"source": "TensorFlow Official Release Notes", "text": section,
                         "url": _blob_url(repo, branch, path)}
    return None


# Maps a normalized package name -> adapter function(repo, version) -> notes dict | None
KNOWN_ADAPTERS = {
    "django": _django_adapter,
    "numpy": _numpy_adapter,
    "pandas": _pandas_adapter,
    "seaborn": _seaborn_adapter,
    "flask": _flask_adapter,
    "fastapi": _fastapi_adapter,
    "matplotlib": _matplotlib_adapter,
    "pydantic": _pydantic_adapter,
    "scikit-learn": _scikit_learn_adapter,
    "sklearn": _scikit_learn_adapter,
    "sqlalchemy": _sqlalchemy_adapter,
    "tensorflow": _tensorflow_adapter,
}

# Packages with no dedicated adapter (below) rely on the generic chain -
# GitHub Releases -> CHANGELOG/HISTORY/CHANGES -> commits - which already
# works well for them because they publish plain CHANGELOG-style files or
# tag their GitHub Releases consistently. Listed here just for documentation:
# requests, langchain, pytorch, streamlit.


def get_known_adapter(package_name):
    return KNOWN_ADAPTERS.get(package_name.strip().lower())
