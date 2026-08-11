"""
cache_service.py

A deliberately simple file-based cache, keyed by:

    ecosystem + package + version + source

e.g. "pypi:django:5.0.0:known_adapter", "npm:express:5.0.0:github_release".

Keying by source (not just by version) is what makes cross-checking
possible: we can cache "what did the known adapter say" separately from
"what did GitHub Releases say" for the same version, and compare them
without re-fetching either.

Released versions never change their changelog after the fact, so a
"found" result can be cached for a long time. A "not found" result is
cached for a much shorter time, in case it was a transient failure
(e.g. a GitHub rate limit) rather than a real absence of notes.

This is not meant to be a general-purpose caching library - just enough
to stop repeated runs from burning through GitHub's rate limit.
"""

import hashlib
import json
import os
import time

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache") #.../dependency_analyzer/cache

FOUND_TTL_SECONDS = 30 * 24 * 60 * 60   # TTL - Time To Live - 30 days
NOT_FOUND_TTL_SECONDS = 24 * 60 * 60    # 1 day

MISS = object()  # Sentinel meaning "no valid cache entry - go fetch it"


def _cache_path(ecosystem, package, version, source):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe_key = f"{ecosystem}:{package}:{version}:{source}"

    # Hash to keep filenames short and filesystem-safe regardless of package name
    digest = hashlib.sha256(safe_key.encode("utf-8")).hexdigest()[:24]   # 9f7a2c81b43e7f2d9a123456...
    return os.path.join(CACHE_DIR, f"{digest}.json")


def get(ecosystem, package, version, source="resolved"):
    """
    Returns:
      - MISS if there's no valid cache entry (caller should fetch live)
      - None if we previously looked and found nothing (still within TTL)
      - a notes dict {'source', 'text', 'url'} if we previously found something
    """
    path = _cache_path(ecosystem, package, version, source)
    if not os.path.exists(path):
        return MISS

    try:
        with open(path, "r", encoding="utf-8") as f:
            entry = json.load(f)
    except (json.JSONDecodeError, OSError):
        return MISS

    age = time.time() - entry.get("cached_at", 0)
    ttl = FOUND_TTL_SECONDS if entry.get("found") else NOT_FOUND_TTL_SECONDS
    if age > ttl:
        return MISS

    return entry.get("notes")  # None if this was a cached "not found"


def set(ecosystem, package, version, notes, source="resolved"): 
    """notes: a notes dict {'source', 'text', 'url'}, or None to cache a 'not found' result."""
    path = _cache_path(ecosystem, package, version, source)
    entry = {
        "found": notes is not None,
        "notes": notes,
        "cached_at": time.time(),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f)
    except OSError:
        pass  # Caching is a best-effort optimization, never fatal


def cache_key_string(ecosystem, package, version, source="resolved"):
    """Human-readable form of the key, e.g. 'pypi:django:5.0.0:known_adapter' - used in logs/tests."""
    return f"{ecosystem}:{package}:{version}:{source}"


        #          REQUEST
        #             │
        #             ▼
        # (ecosys:package:version:source)
        # pypi:django:5.0.0:known_adapter
        #             │
        #             ▼
        #      SHA-256 hash (takes an input of any size and converts it into a fixed 256-bit 
        #  9f7a2c81b43e7f2d9a123456    (32-byte) output, displayed as a 64-character hexadecimal string)
        #             │
        #             ▼
        #   cache/<hash>.json   cache/9f7a2c81b43e7f2d9a123456.json
        #             │
        #       Does it exist?
        #       /            \
        #     NO              YES
        #     │                │
        #     ▼                ▼
        #   MISS          Read JSON
        #     │                │
        #     │          Calculate age
        #     │                │
        # fetch from git ┌─────┴─────┐
        #     │          │           │
        #     │       expired      valid
        #     │          │           │
        #     │          ▼           ▼
        #     └──────── MISS       return
        #                │         cached
        #                ▼
        #           Fetch source
        #                │
        #                ▼
        #          Save to cache
        #                │
        #                ▼
        #          Return result
