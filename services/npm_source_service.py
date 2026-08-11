"""
npm_source_service.py

A separate source-resolution branch for the npm ecosystem, kept distinct
from PyPI's flow because npm packages carry a first-party signal Python
packages don't expose the same way: the registry itself lets a maintainer
mark a specific published version as deprecated, with a message, right in
the package metadata. That's official and structured - worth checking
before falling back to changelog scraping.

The general changelog/release-notes chain (GitHub Releases, CHANGELOG.md,
commits) is ecosystem-agnostic and lives in source_service.py; this module
only adds the npm-specific piece and then still lets the generic chain run
for everything else (breaking changes, features, bug fixes, etc.).
"""

import requests

from services.source_service import _with_retries

NPM_REGISTRY = "https://registry.npmjs.org"


def get_npm_deprecation_notice(package, version):
    """
    Returns a notes-shaped dict if npm's registry has this exact version
    marked deprecated, else None. This is a first-party npm signal, not
    something scraped from a changelog.
    """
    url = f"{NPM_REGISTRY}/{package}/{version}"
    try:
        resp = _with_retries(requests.get, url, timeout=10)
    except Exception:
        return None

    if resp.status_code != 200:
        return None

    data = resp.json()
    message = data.get("deprecated")
    if not message:
        return None

    return {
        "source": "npm Registry Deprecation Notice",
        "text": f"This version is marked deprecated by the maintainer: {message}",
        "url": f"https://www.npmjs.com/package/{package}/v/{version}",
    }
