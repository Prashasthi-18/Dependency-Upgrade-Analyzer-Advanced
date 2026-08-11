"""
llm_classifier.py

Optional LLM-based classification, using Groq, as a smarter alternative
to the keyword-based categorizer in report_service.py. This solves the
false-positive problem keyword matching has (e.g. "Improved test coverage"
matching on the word "test") by giving the model the actual context of
each line instead of pattern-matching in isolation.

This is opt-in: it only activates if a GROQ_API_KEY environment
variable is set. If it's not set, or the API call fails for any reason,
callers should fall back to report_service.categorize_notes() (the
keyword-based path). Nothing in this file is allowed to invent content -
the prompt explicitly instructs the model to only extract and rephrase
what's in the given text, never add information.
"""

import json
import os
import re

import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

VALID_CATEGORIES = [
    "Breaking Changes", "Security Updates", "Deprecations", "New Features",
    "Bug Fixes", "Performance Improvements", "Database / ORM Changes",
    "API Changes", "Other User-Impacting Changes",
]

SYSTEM_PROMPT = """You are analyzing one version's official changelog/release notes entry \
for a software package, to help a developer understand what changed before upgrading.

Extract ONLY user-impacting changes: breaking changes, new features, security fixes, \
bug fixes, performance improvements, deprecations, API changes, configuration changes, \
database/ORM/migration changes, validation/serialization changes, and compatibility changes.

Explicitly EXCLUDE: documentation updates, README changes, examples, tutorials, CI/CD, \
GitHub Actions, test suite changes (e.g. "improved test coverage" is NOT a feature or \
bug fix - exclude it), formatting/linting, typo fixes, contributor thanks, internal \
refactoring with no external effect, build tooling, and version bumps.

CRITICAL RULES:
- Only use information present in the given text. Never invent, infer, or add a change \
that isn't explicitly stated.
- Rephrase for clarity, but stay strictly faithful to what the source says - do not \
change the meaning, scope, or severity of a change.
- If a line is ambiguous about whether it's user-impacting, exclude it rather than guess.
- If nothing in the text is user-impacting, return an empty array.

Respond with ONLY a JSON array (no markdown fences, no commentary), where each item is:
{"category": "<one of: Breaking Changes, Security Updates, Deprecations, New Features, \
Bug Fixes, Performance Improvements, Database / ORM Changes, API Changes, \
Other User-Impacting Changes>", "summary": "<one faithful, clear sentence>"}
"""


class LLMClassificationError(Exception):
    pass


def is_enabled():
    return bool(os.environ.get("GROQ_API_KEY"))


def classify_notes_with_llm(version, notes_text, timeout=30):
    """
    Returns a list of (category, summary) tuples, or raises LLMClassificationError
    if the call fails for any reason. Callers should catch this and fall back to
    keyword-based categorization - this function is never allowed to be the only
    path, since network/API issues must not silently lose the whole report.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise LLMClassificationError("GROQ_API_KEY is not set.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEFAULT_MODEL,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Version: {version}\n\nRelease notes text:\n\n{notes_text}"},
        ],
    }

    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise LLMClassificationError(f"Network error calling Groq API: {e}")

    if resp.status_code != 200:
        raise LLMClassificationError(f"Groq API returned status {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    raw_text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(raw_text, list):
        raw_text = "".join(part.get("text", "") for part in raw_text if isinstance(part, dict))
    raw_text = str(raw_text).strip()

    # Defensive parsing: strip accidental code fences if the model adds them anyway
    raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
    raw_text = re.sub(r'\s*```$', '', raw_text)

    try:
        items = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise LLMClassificationError(f"Could not parse LLM response as JSON: {e}")

    if not isinstance(items, list):
        raise LLMClassificationError("LLM response was not a JSON array as instructed.")

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = item.get("category", "").strip()
        summary = item.get("summary", "").strip()
        if not summary:
            continue
        if category not in VALID_CATEGORIES:
            category = "Other User-Impacting Changes"
        results.append((category, summary))

    return results
