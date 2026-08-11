"""
report_service.py

Responsible for:
- Filtering raw changelog text down to user-impacting lines only (noise cancellation)
- Bucketing each line into a category (Breaking Changes, New Features, etc.)
- Labeling each change with its semver bump type (major/minor/patch/prerelease)
- Reporting breaking changes and removals without speculative semver claims
- Merging/deduplicating changes across all intermediate versions
- Keeping every change traceable back to its source URL
- Assembling the final plain-text report
"""

import ast
import os
import re

from services.semver_service import classify_bump, BUMP_LABELS
from services import llm_classifier

# Order matters: first matching category wins.
CATEGORY_KEYWORDS = [
    ("Breaking Changes", [
        "breaking change", "breaking:", "backward incompatible", "backwards incompatible",
        "no longer support", "removed support", "removal of", "has been removed",
        "was removed", "no longer available",
    ]),
    ("Security Updates", [
        "security", "cve-", "vulnerability", "vulnerable",
    ]),
    ("Expired Deprecations", [
        "expired deprecation", "removed", "removal", "has been removed", "was removed",
        "removed support", "removed in", "removed from",
    ]),
    ("New Deprecations", [
        "deprecat",
    ]),
    ("Compatibility Changes", [
        "compatib", "overflowerror", "return behavior", "behavior change",
        "consistent", "support dropped", "python 3.11", "requires python",
        "requires cython", "toolchain",
    ]),
    ("Build / Toolchain Changes", [
        "cython", "msvc", "toolchain", "build requirement", "build compatibility",
    ]),
    ("New Features", [
        "new feature", "feature:", "added support", "now supports", "introduce",
        "added the ability", "adds support", "descending=true", "descending=True",
        "support descending", "now support",
    ]),
    ("Bug Fixes", [
        "fix", "fixed", "bug",
    ]),
    ("Performance Improvements", [
        "performance", "faster", "optimi", "speed up", "reduce memory", "reduced memory",
        "significantly improve performance",
    ]),
    ("Database / ORM Changes", [
        "migration", "queryset", "orm ", " orm", "database",
    ]),
    ("API Changes", [
        "api change", "signature change", "renamed", "changed default", "config change",
        "configuration change", "serializ", "validation",
    ]),
    ("Deprecations", [
        "deprecat",
    ]),
]

# Lines mentioning these are dropped entirely - not user-impacting.
IGNORE_KEYWORDS = [
    "docs", "documentation", "readme", "typo", "typos", "ci/cd", "github actions",
    "unit test", "test suite", "tests", "testing", "formatting", "contributor",
    "internal refactor", "refactor", "tooling", "metadata", "version bump",
    "release date", "example", "tutorial", "comment", "code style",
    "thanks to", "thank you", "thanks @", "contributed by",
    "pre-commit", "linting", "lint", "type hint only", "mypy",
    "backport", "cherry-pick", "see new features below", "see compatibility changes",
    "see the documentation", "see below", "see above",
]

# Lines that are pure noise regardless of content: bare references, empty
# bullet artifacts, or single-word remnants left over from bad splitting.
_PURE_REFERENCE_RE = re.compile(r'^\(?#\d+\)?[.,]?$')
_PURE_LINK_RE = re.compile(r'^\[?#?\d+\]?\(https?://\S+\)$')
_ONLY_PUNCTUATION_RE = re.compile(r'^[\W_]+$')

CATEGORY_ORDER = [cat for cat, _ in CATEGORY_KEYWORDS] + ["Other User-Impacting Changes"]

SKIP_DIR_NAMES = {
    ".venv", "venv", "env", "ENV", ".ENV", "site-packages", "__pycache__",
    ".git", "node_modules", "dist", "build", ".eggs",
}
SOURCE_EXTENSIONS = {".py", ".pyi", ".js", ".ts", ".tsx", ".jsx"}

# The "does my project actually use this API" scan is opt-in and OFF by
# default. Without an explicit target, it would silently scan whatever
# directory the Flask process happens to be launched from - which is
# this tool's own source tree, not the user's project - and every
# line would come back "Not detected", which is actively misleading
# rather than just unhelpful. Set PROJECT_SCAN_ROOT to the path of the
# codebase you actually want checked (e.g. your Django/NumPy project)
# to enable it.
PROJECT_SCAN_ROOT = os.environ.get("PROJECT_SCAN_ROOT", "").strip() or None


_BARE_SUBHEADER_RE = re.compile(r'^\*{1,2}[A-Za-z][A-Za-z /]*\*{1,2}:?$')
_BULLET_START_RE = re.compile(r'^[-*•]\s+')
# Only treat "1. " as a numbered-list marker, never "2.32.0" style version numbers.
_NUMBERED_LIST_RE = re.compile(r'^\d+\.\s+(?=[A-Za-z])')
_SETEXT_UNDERLINE_RE = re.compile(r'^[-=~^]{3,}\s*$')
_RST_ROLE_RE = re.compile(r':[a-z:]+:`([^`]+)`')  # e.g. :cve:`2024-1234` -> 2024-1234
_RST_DOUBLE_BACKTICK_RE = re.compile(r'``([^`]+)``')  # e.g. ``code`` -> code
_RST_HYPERLINK_RE = re.compile(r'`([^`<]+?)\s*<[^>]+>`_+')  # e.g. `here <url>`_ -> here
_RST_DIRECTIVE_RE = re.compile(r'^\.\.[A-Za-z0-9_-]+::')
_API_CANDIDATE_RE = re.compile(r'(?<![A-Za-z0-9_])((?:np|numpy)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)')
_SECTION_NAMES = {
    "expired deprecations": "Expired Deprecations",
    "new deprecations": "New Deprecations",
    "breaking changes": "Breaking Changes",
    "compatibility changes": "Compatibility Changes",
    "build / toolchain changes": "Build / Toolchain Changes",
    "performance improvements": "Performance Improvements",
    "new features": "New Features",
    "security updates": "Security Updates",
    "bug fixes": "Bug Fixes",
    "api changes": "API Changes",
    "database / orm changes": "Database / ORM Changes",
    "deprecations": "New Deprecations",
}


def _normalize_path(path):
    return os.path.normpath(path).replace('\\', '/')


def _should_skip_path(path):
    normalized = _normalize_path(path)
    parts = [part for part in normalized.split('/') if part not in {'', '.'}]
    return any(part in SKIP_DIR_NAMES for part in parts)


def _iter_source_files(root='.'):
    for current_root, dirs, files in os.walk(root, topdown=True):
        dirs[:] = [d for d in dirs if not _should_skip_path(os.path.join(current_root, d))]
        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            full_path = os.path.join(current_root, filename)
            if _should_skip_path(full_path):
                continue
            yield full_path


def _is_test_path(path):
    normalized = _normalize_path(path).lower()
    parts = [part for part in normalized.split('/') if part not in {'', '.'}]
    if any(part in {'tests', 'test'} for part in parts):
        return True
    filename = os.path.basename(normalized)
    return filename.startswith('test_') or filename.endswith('_test.py') or filename.endswith('_test.pyi')


def _scan_files_summary(root='.'):
    scanned_files = 0
    skipped_files = 0
    skipped_dirs = set()
    for current_root, dirs, files in os.walk(root, topdown=True):
        dirs[:] = [d for d in dirs if not _should_skip_path(os.path.join(current_root, d))]
        for dirname in dirs:
            full_path = os.path.join(current_root, dirname)
            if _should_skip_path(full_path):
                skipped_dirs.add(_normalize_path(full_path))
        for filename in files:
            full_path = os.path.join(current_root, filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            if _should_skip_path(full_path):
                skipped_files += 1
                continue
            scanned_files += 1
    return {
        'files_scanned': scanned_files,
        'files_skipped': skipped_files,
        'skipped_directories': sorted(skipped_dirs),
    }


def _normalize_api_name(api_name):
    if not api_name:
        return None
    name = api_name.strip().strip('`"\'')
    name = name.split('(', 1)[0].split('[', 1)[0]
    name = name.rstrip('.,:;')
    if not name:
        return None
    if name.startswith('np.') and not name.startswith('numpy.'):
        name = 'numpy' + name[2:]
    if name.lower() in {'e.g', 'i.e', 'etc'}:
        return None
    if '/' in name or ' ' in name:
        return None
    if name.lower().endswith(('.py', '.pyi', '.pxd', '.rst', '.md', '.txt', '.json', '.html', '.js', '.ts', '.tsx', '.jsx')):
        return None
    return name


def _extract_api_candidates(text):
    candidates = []
    seen = set()
    for match in _API_CANDIDATE_RE.finditer(text or ''):
        candidate = _normalize_api_name(match.group(1))
        if not candidate:
            continue
        if candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        candidates.append(candidate)
    return candidates


def _looks_like_continuation_fragment(line):
    lower = line.lower()
    fragments = [
        'this change', 'this means', 'this makes', 'as a result',
        'the above means', 'the above', 'for example', 'for instance',
        'in particular', 'it also', 'it now', 'it is now', 'this also',
        'see ', 'see the documentation', 'see new features', 'see compatibility changes',
        'the following', 'these changes', 'the change', 'these are',
        'note that',
    ]
    return any(fragment in lower for fragment in fragments)


def _clean_markup(line):
    """Strip Sphinx/RST inline role markup so report text reads as plain English."""
    line = _RST_ROLE_RE.sub(r'\1', line)
    line = _RST_HYPERLINK_RE.sub(r'\1', line)
    line = _RST_DOUBLE_BACKTICK_RE.sub(r'\1', line)
    return line


def _group_into_paragraphs(lines):
    """Split raw lines into logical change paragraphs while preserving
    supporting paragraphs under the same change.

    IMPORTANT: every heading - whether or not it's one of the known
    top-level category names in _SECTION_NAMES - hard-ends any pending
    merge. Earlier this only reset on *recognized* section names, which
    let content from an unrelated subsection (e.g. a "Compatibility
    notes" heading NumPy uses that isn't in _SECTION_NAMES) silently
    bleed into and get appended onto the previous change's paragraph,
    producing garbled, unrelated merged sentences in the report. A
    `block_id` that increments on every heading crossing - recognized
    or not - is what actually prevents that, and it works for any
    project's changelog structure, not just the ones we happened to
    special-case.
    """
    paragraphs = []
    current = []
    current_section = None
    current_is_change = False
    block_id = 0
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.rstrip()

        is_heading = False
        if stripped.strip().startswith("#"):
            is_heading = True
        elif i + 1 < n and stripped.strip() and _SETEXT_UNDERLINE_RE.match(lines[i + 1].strip()):
            is_heading = True
            i += 1

        if is_heading:
            if current:
                paragraphs.append((current_section, current, current_is_change, block_id))
                current = []
                current_is_change = False
            block_id += 1  # Any heading - known or not - ends the current block.
            i += 1
            continue

        if not stripped.strip():
            if current:
                paragraphs.append((current_section, current, current_is_change, block_id))
                current = []
                current_is_change = False
            i += 1
            continue

        normalized_heading = stripped.strip().lower()
        if normalized_heading in _SECTION_NAMES:
            current_section = stripped.strip()
            if current:
                paragraphs.append((current_section, current, current_is_change, block_id))
                current = []
                current_is_change = False
            block_id += 1
            i += 1
            continue

        if current and not current_is_change and not _is_noise(stripped):
            if re.search(r'\b(removed|removal|deprecated|deprecation|added|support|now|changed|requires|compatib|performance|security|bug|fix|improv|migration|behavior|crash|error|improved|improve|cython|msvc|toolchain)\b', stripped.lower()):
                current_is_change = True

        current.append(stripped.strip())
        i += 1

    if current:
        paragraphs.append((current_section, current, current_is_change, block_id))

    logical_entries = []
    for section, paragraph, is_change, p_block_id in paragraphs:
        if len(paragraph) == 1 and _BARE_SUBHEADER_RE.match(paragraph[0]):
            continue
        if not paragraph:
            continue
        same_block_as_previous = (
            logical_entries and logical_entries[-1][2] == p_block_id
        )
        if is_change or len(logical_entries) == 0 or not logical_entries[-1][0] == section \
                or not same_block_as_previous:
            logical_entries.append([section, paragraph, p_block_id])
        else:
            logical_entries[-1][1].extend(paragraph)
    return [(section, lines) for section, lines, _ in logical_entries]


def _split_into_lines(text):
    """Break changelog text into one clean entry per logical change."""
    lines = text.splitlines()
    paragraphs = _group_into_paragraphs(lines)

    entries = []
    for section, paragraph in paragraphs:
        if len(paragraph) == 1 and _BARE_SUBHEADER_RE.match(paragraph[0]):
            continue

        paragraph_text = []
        for pline in paragraph:
            if _BARE_SUBHEADER_RE.match(pline):
                continue
            if _is_noise(pline):
                continue
            paragraph_text.append(pline)

        if not paragraph_text:
            continue

        is_bullet_paragraph = any(
            _BULLET_START_RE.match(pline) or _NUMBERED_LIST_RE.match(pline)
            for pline in paragraph_text
        )

        if is_bullet_paragraph:
            paragraph_entries = []
            for pline in paragraph_text:
                is_new_bullet = bool(_BULLET_START_RE.match(pline)) or bool(_NUMBERED_LIST_RE.match(pline))
                cleaned = _BULLET_START_RE.sub('', pline)
                cleaned = _NUMBERED_LIST_RE.sub('', cleaned)
                if is_new_bullet or not paragraph_entries:
                    paragraph_entries.append((section, cleaned))
                else:
                    paragraph_entries[-1] = (section, paragraph_entries[-1][1].rstrip() + " " + cleaned)
            entries.extend(paragraph_entries)
        else:
            entries.append((section, " ".join(paragraph_text)))

    entries = [(section, _clean_markup(text)) for section, text in entries]
    return entries


def _is_noise(line):
    lower = line.lower()
    if any(kw in lower for kw in IGNORE_KEYWORDS):
        return True
    if re.match(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*\s*=\s*)?np\.', line):
        return True
    if re.match(r'^\.\.\s*(currentmodule|code-block|note|versionchanged|deprecated|include|class|function|role)::', line):
        return True
    if _RST_DIRECTIVE_RE.match(line):
        return True
    if re.match(r'^\s*arr\s*=\s*np\.', line) or re.match(r'^\s*[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*np\.', line):
        return True
    if re.match(r'^\s*import\s+numpy', line) or re.match(r'^\s*from\s+numpy', line):
        return True
    if re.match(r'^\(?gh-[a-z0-9]+\)?$', line, re.I):
        return True
    if re.match(r'^\(?gh-\d+\)?$', line, re.I):
        return True
    if _PURE_REFERENCE_RE.match(line) or _PURE_LINK_RE.match(line):
        return True
    if _ONLY_PUNCTUATION_RE.match(line):
        return True
    if len(line) < 3:
        return True
    if lower in {"ok", "ok.", "yes", "no"}:
        return True
    if _looks_like_continuation_fragment(line):
        return True
    stripped_of_refs = re.sub(r'\(?#\d+\)?|\bhttps?://\S+', '', line).strip()
    if len(stripped_of_refs) < 8:
        return True
    return False


def _is_meaningful_change(line):
    lower = line.lower()
    if _is_noise(line):
        return False
    if _looks_like_continuation_fragment(line):
        return False
    if re.search(r'\b(removed|removal|deprecated|deprecation|added|support|now|changed|drop|raises|requires|compatib|performance|security|bug|fix|improv|migration|behavior|crash|error|improved|improve)\b', lower):
        return True
    if re.search(r'\b(?:np|numpy)\.[a-z0-9_.]+', lower):
        return True
    if re.search(r'\b[a-z0-9_]+\.[a-z0-9_]+', lower):
        return True
    return False


def _classify_change_category(line, section=None):
    lower = line.lower()
    section_text = (section or '').lower()

    if re.search(r'\bbreaking change\b', lower):
        return "Breaking Changes"

    if 'expired deprecations' in section_text or 'expired deprecation' in section_text:
        return "Expired Deprecations"

    if 'breaking changes' in section_text:
        return "Breaking Changes"

    if 'new deprecations' in section_text or 'deprecations' in section_text:
        if re.search(r'\b(deprecated since|previously deprecated|now deprecated|deprecated in)\b', lower):
            return "Expired Deprecations"
        return "New Deprecations"

    if re.search(r'\b(expired deprecations|expired deprecation)\b', lower):
        return "Expired Deprecations"

    if re.search(r'\b(removed|removal|removed support|no longer support|no longer supported|has been removed|was removed|removed in|removed from|expired deprecation)\b', lower):
        return "Expired Deprecations"

    if re.search(r'\bdeprecat', lower):
        if re.search(r'\b(deprecated since|previously deprecated|now deprecated|deprecated in)\b', lower):
            return "Expired Deprecations"
        return "New Deprecations"

    if re.search(r'\b(overflowerror|return behavior|behavior change|compatib|support dropped|python 3\.11|requires python|requires cython|toolchain|msvc|cython)\b', lower):
        if re.search(r'\b(cython|toolchain|msvc|build requirement|build compatibility)\b', lower):
            return "Build / Toolchain Changes"
        return "Compatibility Changes"

    if re.search(r'\b(descending=true|descending=True|descending sort|descending sorts)\b', lower):
        return "New Features"

    if re.search(r'\b(searchsorted|binary search|cache locality|faster|speedup|performance|optimiz)\b', lower):
        return "Performance Improvements"

    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in lower for kw in keywords):
            return category

    return "Other User-Impacting Changes"


def categorize_notes(version, notes):
    """
    Keyword-based categorization (the always-available fallback path).
    notes: {"source": ..., "text": ..., "url": ...}
    Returns: dict category -> list of (version, line, url)
    """
    categorized = {cat: [] for cat in CATEGORY_ORDER}
    lines = _split_into_lines(notes["text"])
    url = notes.get("url", "")

    for section, line in lines:
        if not _is_meaningful_change(line):
            continue

        target_category = _classify_change_category(line, section)
        labeled_text = f"{line}"
        if section:
            labeled_text = f"{labeled_text} [Section: {section}]"
        categorized[target_category].append((version, labeled_text, url))

    return categorized


def categorize_notes_smart(version, notes):
    """
    Preferred entry point: uses Claude for contextual classification when
    ANTHROPIC_API_KEY is set (avoids false positives like "Improved test
    coverage" matching on the word "test"), and transparently falls back
    to the keyword-based categorizer if the LLM is disabled or the call
    fails for any reason. The report must never come up empty just
    because an optional API call didn't work.
    """
    if llm_classifier.is_enabled():
        try:
            llm_results = llm_classifier.classify_notes_with_llm(version, notes["text"])
            categorized = {cat: [] for cat in CATEGORY_ORDER}
            url = notes.get("url", "")
            for category, summary in llm_results:
                categorized[category].append((version, summary, url))
            return categorized
        except llm_classifier.LLMClassificationError:
            pass  # fall through to keyword-based path
    return categorize_notes(version, notes)


def merge_categories(all_categorized):
    """Merge categorized changes from every version, dropping near-duplicates."""
    merged = {cat: [] for cat in CATEGORY_ORDER}
    seen = {cat: set() for cat in CATEGORY_ORDER}

    for categorized in all_categorized:
        for cat, entries in categorized.items():
            for version, line, url in entries:
                key = re.sub(r'\W+', '', line.lower())[:100]
                if key in seen[cat]:
                    continue
                seen[cat].add(key)
                merged[cat].append((version, line, url))

    return merged


def _find_project_usage(api_name, scan_root):
    if not api_name or not scan_root:
        return []

    normalized = _normalize_api_name(api_name)
    if not normalized:
        return []

    if not normalized.startswith('numpy.'):
        return []

    target_parts = normalized.split('.')
    if len(target_parts) < 2:
        return []

    module_name = target_parts[0]
    symbol_name = target_parts[-1]
    module_path = '.'.join(target_parts[:-1])

    preferred_symbol = '.'.join(target_parts)
    alias_map = {'numpy': 'numpy'}
    usage_hits = []
    for full_path in _iter_source_files(scan_root):
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, 'r', encoding='utf-8') as handle:
                source = handle.read()
        except (OSError, UnicodeDecodeError):
            continue

        try:
            tree = ast.parse(source, filename=full_path)
        except SyntaxError:
            continue

        module_aliases = {}
        imported_names = {}
        wildcard_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == 'numpy':
                        module_aliases[alias.asname or 'numpy'] = 'numpy'
                        imported_names[alias.asname or 'numpy'] = 'numpy'
                    elif alias.name.startswith('numpy.'):
                        base = alias.name.split('.')[1]
                        module_aliases[alias.asname or base] = alias.name
            elif isinstance(node, ast.ImportFrom):
                if node.module == 'numpy':
                    for alias in node.names:
                        if alias.name == '*':
                            wildcard_imports.add('numpy')
                        else:
                            imported_names[alias.asname or alias.name] = f'numpy.{alias.name}'

        # Simple variable-reassignment tracking: `stack = np.row_stack` then
        # `stack(...)` later. Deliberately limited to a single Name target
        # assigned directly from an Attribute/Name we already resolve -
        # this is best-effort static analysis, not full data-flow tracing,
        # so it won't catch reassignment through more complex expressions.
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                value = node.value
                resolved = None
                if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                    if value.value.id in module_aliases:
                        resolved = f"numpy.{value.attr}"
                elif isinstance(value, ast.Name) and value.id in imported_names:
                    resolved = imported_names[value.id]
                if resolved:
                    imported_names[target_name] = resolved

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                resolved = None
                detection_method = None
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name):
                        alias = func.value.id
                        if alias in module_aliases:
                            resolved = f"numpy.{func.attr}"
                            detection_method = "ast_attribute_call"
                    elif isinstance(func.value, ast.Attribute):
                        if isinstance(func.value.value, ast.Name) and func.value.value.id == 'numpy':
                            resolved = f"numpy.{func.attr}"
                            detection_method = "ast_attribute_call"
                elif isinstance(func, ast.Name):
                    if func.id in imported_names:
                        resolved = imported_names[func.id]
                        detection_method = "ast_call"

                if resolved and resolved == preferred_symbol:
                    line_no = getattr(node, 'lineno', 1)
                    snippet = None
                    try:
                        snippet = ast.get_source_segment(source, node)
                    except Exception:
                        snippet = None
                    usage_hits.append({
                        'path': _normalize_path(os.path.relpath(full_path, scan_root)),
                        'line': line_no,
                        'code': snippet,
                        'kind': 'confirmed',
                        'confidence': 'HIGH',
                        'detection_method': detection_method,
                        'is_test': _is_test_path(full_path),
                    })
                    break

        if not usage_hits and wildcard_imports:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == symbol_name:
                    snippet = None
                    try:
                        snippet = ast.get_source_segment(source, node)
                    except Exception:
                        snippet = None
                    usage_hits.append({
                        'path': _normalize_path(os.path.relpath(full_path, scan_root)),
                        'line': getattr(node, 'lineno', 1),
                        'code': snippet,
                        'kind': 'potential',
                        'confidence': 'LOW',
                        'detection_method': 'ast_wildcard_import_name_match',
                        'is_test': _is_test_path(full_path),
                    })
                    break

    return usage_hits


def _find_attribute_assignment_usage(attr_name, scan_root):
    """
    Detects `something.<attr_name> = ...` assignments (e.g. `arr.shape =
    ...`, `arr.dtype = ...`). Static analysis alone can't prove the
    target is actually a NumPy array without full type inference, so
    every hit is reported as 'potential' with MEDIUM confidence, never
    'confirmed' - this is an honest limitation, not a gap papered over.
    """
    if not attr_name or not scan_root:
        return []

    hits = []
    for full_path in _iter_source_files(scan_root):
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, 'r', encoding='utf-8') as handle:
                source = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source, filename=full_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            targets = None
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            if not targets:
                continue
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == attr_name:
                    snippet = None
                    try:
                        snippet = ast.get_source_segment(source, node)
                    except Exception:
                        snippet = None
                    hits.append({
                        'path': _normalize_path(os.path.relpath(full_path, scan_root)),
                        'line': getattr(node, 'lineno', 1),
                        'code': snippet,
                        'kind': 'potential',
                        'confidence': 'MEDIUM',
                        'detection_method': 'ast_attribute_assignment (type not verified)',
                        'is_test': _is_test_path(full_path),
                    })
                    break
    return hits


_PY_DROP_RE = re.compile(
    r'(?:drops?\s+support\s+for|no\s+longer\s+supports?|removes?\s+support\s+for)\s+Python\s+([\d]+\.[\d]+)',
    re.I,
)
_PY_SUPPORT_RE = re.compile(
    r'supports?\s+Python\s+versions?\s+([\d.]+(?:\s*[-\u2013\u2014]\s*[\d.]+)?)',
    re.I,
)
_REPLACEMENT_RE = re.compile(
    r'(?:in favor of|use|replaced by|migrate to|instead,?\s+use|should use)\s+'
    r'(?:an?|the)?\s*`{0,2}([A-Za-z_][\w.]*(?:\(\))?)`{0,2}',
    re.I,
)

# Marks where a subject clause ("what is changing") ends and an
# alternative/replacement clause ("what to use instead") begins. This is
# the key fix for a real bug: without this split, the FIRST numpy.X
# mention anywhere in a paragraph was taken as "the API", even when that
# mention was actually the *recommended replacement*, not the subject.
# Verified against NumPy 2.5.0's actual text: "Setting the shape
# attribute is deprecated ... As an alternative ... via np.reshape" was
# being reported as "numpy.reshape deprecated" - backwards.
_ALTERNATIVE_TRIGGER_RE = re.compile(
    r'\b(?:as an alternative|in favor of|instead,?\s+use|use\s+\S+.*?\s+instead|'
    r'replaced by|migrate to|should use|you can (?:create|use))\b',
    re.I,
)

# Where a subject clause ends when there's no explicit alternative
# ("X is deprecated" / "X has been removed" / "X is no longer supported").
_SUBJECT_VERB_BOUNDARY_RE = re.compile(
    r'\b(?:is deprecated|has been deprecated|are deprecated|is removed|'
    r'has been removed|was removed|are removed|is no longer supported|'
    r'are no longer supported|no longer support)\b',
    re.I,
)

# Official-category -> change_type, per the normalized change model.
_CATEGORY_TO_CHANGE_TYPE = {
    "Breaking Changes": "REMOVAL",
    "Expired Deprecations": "REMOVAL",
    "New Deprecations": "DEPRECATION",
    "Compatibility Changes": "BEHAVIOR_CHANGE",
    "API Changes": "BEHAVIOR_CHANGE",
    "Database / ORM Changes": "COMPATIBILITY_CHANGE",
    "Security Updates": "SECURITY",
    "Build / Toolchain Changes": "BUILD_REQUIREMENT",
    "New Features": "NEW_FEATURE",
    "Performance Improvements": "PERFORMANCE",
    "Bug Fixes": "BUG_FIX",
}

# Release-note-only severity (before project exposure is considered).
_SEVERITY_BY_CHANGE_TYPE = {
    "REMOVAL": "HIGH",
    "DEPRECATION": "LOW",
    "BEHAVIOR_CHANGE": "MEDIUM",
    "COMPATIBILITY_CHANGE": "MEDIUM",
    "BUILD_REQUIREMENT": "MEDIUM",
    "PYTHON_REQUIREMENT": "HIGH",
    "SECURITY": "HIGH",
    "NEW_FEATURE": "INFO",
    "PERFORMANCE": "INFO",
    "BUG_FIX": "INFO",
    "OTHER": "LOW",
}

# Categories that go through the full risk/recommendation pipeline (i.e.
# things that can plausibly break YOUR code). New Features, Performance,
# and Bug Fixes are informational and handled separately.
_RISK_CATEGORY_MAP = {
    "Breaking Changes": "HIGH",
    "Expired Deprecations": "HIGH",
    "Security Updates": "HIGH",
    "Compatibility Changes": "MEDIUM",
    "API Changes": "MEDIUM",
    "Database / ORM Changes": "MEDIUM",
    "New Deprecations": "LOW",
}

# Project scan status - four explicit states, per the requirement that
# "not scanned" must never be conflated with "not affected".
NOT_SCANNED = "NOT_SCANNED"
SCANNED_NOT_FOUND = "SCANNED_NOT_FOUND"
SCANNED_POTENTIAL = "SCANNED_POTENTIAL"
SCANNED_CONFIRMED = "SCANNED_CONFIRMED"

_RISK_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4, "UNKNOWN": -1}


_FILE_PATH_ERROR_RE = re.compile(r'\S+\.\w+:\d+:\d+')  # e.g. path/file.pxd:11:13 - example error output
_DANGLING_CITATION_RE = re.compile(r'^\(?\d+\)\s')      # e.g. "13860) that was introduced..." - orphaned fragment


def _looks_like_noise_fragment(line):
    """Broader noise check for the dump-style sections (New Features,
    Build/Toolchain) that don't go through the full change pipeline:
    catches example error output and orphaned citation fragments left
    over from paragraph splitting, in addition to the standard
    continuation-fragment phrases."""
    if _looks_like_continuation_fragment(line):
        return True
    if _FILE_PATH_ERROR_RE.search(line):
        return True
    if _DANGLING_CITATION_RE.match(line.strip()):
        return True
    return False


def _strip_section_suffix(text):
    """categorize_notes() appends ' [Section: X]' to each line for
    traceability - pull that back out as data instead of leaving it
    embedded in text that titles/recommendations get generated from."""
    match = re.search(r'^(.*?)\s*\[Section:\s*(.+?)\]\s*$', text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text.strip(), None


def _split_subject_and_alternative(clean_text):
    """
    Splits a changelog sentence into the subject clause (what is actually
    changing) and the alternative clause (what's recommended instead), if
    any. This is the fix for the resize/reshape bug: API names must only
    be pulled from the subject clause, never from the alternative clause.
    """
    m = _ALTERNATIVE_TRIGGER_RE.search(clean_text)
    if m:
        return clean_text[:m.start()].strip(), clean_text[m.start():].strip()
    return clean_text.strip(), ""


_ATTRIBUTE_ASSIGNMENT_RE = re.compile(r'setting the\s+`{0,2}(\w+)`{0,2}\s+attribute', re.I)
_INPLACE_RESIZE_RE = re.compile(r'resiz\w*\s+.*?\bin[- ]place\b|\bin[- ]place\b.*?resiz', re.I)


_TRAILING_CITATION_RE = re.compile(
    r'\s*\((?:gh-\d+|deprecated since [\d.]+|deprecation since [\d.]+)\)\.?\s*$', re.I
)


def _strip_trailing_citations(text):
    """Repeatedly strips trailing '(gh-1234)' / '(deprecated since X.Y)'
    citation parentheticals so they don't end up embedded in a title or
    operation description as if they were part of the API name."""
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_CITATION_RE.sub('', text).rstrip()
    return text


def _extract_operation_phrase(subject_clause):
    """
    When no explicit API is named in the subject clause, extract a
    faithful description of *what* is changing from the subject text
    itself (never invented) - e.g. "Setting the shape attribute" or
    "Resizing a Numpy array in place" - rather than forcing an API name
    that isn't actually there.
    """
    attr_match = _ATTRIBUTE_ASSIGNMENT_RE.search(subject_clause)
    if attr_match:
        return f"{attr_match.group(1)} attribute assignment", attr_match.group(1)

    m = _SUBJECT_VERB_BOUNDARY_RE.search(subject_clause)
    phrase = subject_clause[:m.start()].strip() if m else subject_clause.strip()
    phrase = _clean_markup(phrase).rstrip(',:;')
    phrase = _strip_trailing_citations(phrase)
    if not phrase:
        return None, None
    if len(phrase) > 90:
        phrase = phrase[:87].rstrip() + "..."
    return phrase, None


def _identify_change_subject(clean_text):
    """
    The core extraction fix: identifies what is ACTUALLY changing,
    distinguishing it from any API merely mentioned as a replacement.
    Returns (affected_api, affected_operation, replacement_api,
    extraction_confidence, attribute_name). attribute_name is set only
    when the operation is a specific attribute assignment (e.g. "shape"),
    so the project scanner can check for it via AST.
    Never guesses an API name that isn't explicitly the subject.
    """
    subject_clause, alternative_clause = _split_subject_and_alternative(clean_text)

    subject_apis = _extract_api_candidates(subject_clause)
    replacement_apis = _extract_api_candidates(alternative_clause) if alternative_clause else []
    if not replacement_apis:
        rep_match = _REPLACEMENT_RE.search(clean_text)
        if rep_match:
            candidate = rep_match.group(1).rstrip('.,;:')
            if candidate.lower() not in {'a', 'an', 'the', 'it', 'this'}:
                replacement_apis = [candidate]

    replacement_api = replacement_apis[0] if replacement_apis else None

    if subject_apis:
        # An explicit API is named as the subject - highest confidence.
        return subject_apis[0], None, replacement_api, "HIGH", None

    operation, attr_name = _extract_operation_phrase(subject_clause)
    if operation:
        # A concrete operation was extracted (e.g. attribute assignment,
        # in-place resizing) even though no literal API name appears -
        # this is a faithful extraction, not a guess, so confidence stays
        # reasonably high, but we do NOT fabricate an API symbol for it.
        return None, operation, replacement_api, "MEDIUM", attr_name

    # Nothing confidently identified - explicitly say so rather than guess.
    return None, None, replacement_api, "LOW", None


def _refine_change_type(category, clean_text):
    base = _CATEGORY_TO_CHANGE_TYPE.get(category, "OTHER")
    lower = clean_text.lower()
    if base == "REMOVAL" and not re.search(r'\bremov', lower):
        if re.search(r'\bdeprecat', lower):
            return "DEPRECATION"
    return base


def _extract_old_new_behavior(clean_text, change_type):
    """
    Best-effort extraction of before/after behavior, only attempted for
    BEHAVIOR_CHANGE / COMPATIBILITY_CHANGE entries where release notes
    commonly use a "Previously, X. Now, Y." structure. Returns (None,
    None) rather than guessing when that structure isn't present.
    """
    if change_type not in ("BEHAVIOR_CHANGE", "COMPATIBILITY_CHANGE"):
        return None, None
    m = re.search(r'Previously,?\s+(.+?)\.\s+(?:Now,?|now,?)\s+(.+?)\.', clean_text, re.I | re.S)
    if m:
        old = m.group(1).strip()
        new = m.group(2).strip()
        if len(old) > 150:
            old = old[:147].rstrip() + "..."
        if len(new) > 150:
            new = new[:147].rstrip() + "..."
        return old, new
    return None, None


def _detect_python_compatibility(raw_texts):
    """
    Scans the *raw* (unsplit) release notes text for explicit Python
    version support statements. Deliberately conservative: if a project
    doesn't say it in these terms, nothing is reported here rather than
    guessing from indirect signals.
    """
    dropped = []
    supported = None
    for text in raw_texts:
        for m in _PY_DROP_RE.finditer(text):
            v = m.group(1)
            if v not in dropped:
                dropped.append(v)
        m = _PY_SUPPORT_RE.search(text)
        if m and not supported:
            supported = m.group(1).rstrip(". ")
    return {"dropped": dropped, "supported": supported}


def _generate_change_title(affected_api, affected_operation, change_type, category):
    """Short, scannable title built strictly from already-extracted fields."""
    subject = affected_api or affected_operation or "Unspecified change"
    if change_type == "REMOVAL":
        return f"{subject} removed" if affected_api else subject
    if change_type == "DEPRECATION":
        label = "deprecation expired" if category == "Expired Deprecations" else "deprecated"
        return f"{subject} {label}" if affected_api else f"{subject} ({label})"
    if change_type in ("BEHAVIOR_CHANGE", "COMPATIBILITY_CHANGE"):
        return f"{subject} behavior changed" if affected_api else subject
    return subject


def _generate_recommendation(change_type, affected_api, affected_operation, replacement_api):
    """
    Recommendation is built strictly from already-extracted fields (never
    re-parses raw text), so it can't recommend something that wasn't
    actually identified as the replacement.
    """
    subject = affected_api or affected_operation

    if change_type == "REMOVAL":
        if replacement_api:
            return f"Replace with {replacement_api}."
        if subject:
            return f"Remove or update any usage of {subject} before upgrading."
        return "Review and update this change before upgrading."

    if change_type == "DEPRECATION":
        if replacement_api:
            return f"Migrate to {replacement_api} before this is removed in a future release."
        if subject:
            return f"Plan to migrate away from {subject}; it will be removed in a future release."
        return "Review this deprecation and plan a migration path."

    if change_type in ("BEHAVIOR_CHANGE", "COMPATIBILITY_CHANGE"):
        return "Review any code relying on the previous behavior, and test after upgrading."

    if change_type == "SECURITY":
        return "Upgrade promptly; this addresses a security issue."

    return "Review this change before upgrading."


def _compute_baseline_severity(change_type):
    """Severity from the release note alone, before project exposure."""
    return _SEVERITY_BY_CHANGE_TYPE.get(change_type, "LOW")


def _usage_status_from_hits(usage_hits, scan_root):
    if not scan_root:
        return NOT_SCANNED
    confirmed = [h for h in usage_hits if h['kind'] == 'confirmed']
    potential = [h for h in usage_hits if h['kind'] == 'potential']
    if confirmed:
        return SCANNED_CONFIRMED
    if potential:
        return SCANNED_POTENTIAL
    return SCANNED_NOT_FOUND


def _compute_change_risk(baseline_severity, usage_status):
    """
    The risk engine: combines release-note severity with actual project
    exposure. Matches the exact matrix requested - e.g. a removed API
    (HIGH baseline) with confirmed usage is CRITICAL, the same removal
    with no confirmed usage is only LOW, and if the project was never
    scanned at all the result is UNKNOWN - never silently downgraded to
    something that reads as "safe".
    """
    if baseline_severity == "INFO":
        return "INFO"
    if usage_status == NOT_SCANNED:
        return "UNKNOWN" if baseline_severity in ("HIGH", "MEDIUM") else baseline_severity
    if usage_status == SCANNED_CONFIRMED:
        return {"HIGH": "CRITICAL", "MEDIUM": "HIGH", "LOW": "MEDIUM"}.get(baseline_severity, baseline_severity)
    if usage_status == SCANNED_POTENTIAL:
        return {"HIGH": "MEDIUM", "MEDIUM": "MEDIUM", "LOW": "LOW"}.get(baseline_severity, baseline_severity)
    if usage_status == SCANNED_NOT_FOUND:
        return {"HIGH": "LOW", "MEDIUM": "LOW", "LOW": "LOW"}.get(baseline_severity, baseline_severity)
    return baseline_severity


def _build_changes(merged, scan_root):
    """
    Transforms merged (category -> [(version, text, url)]) into
    normalized Change dicts, running the full pipeline requested:
    raw note -> subject/replacement extraction -> project usage ->
    combined risk -> recommendation. Preserves the original excerpt on
    every entry so every claim is traceable back to source text.
    """
    changes = []
    for category in _RISK_CATEGORY_MAP:
        for version, text, url in merged.get(category, []):
            clean_text, section = _strip_section_suffix(text)
            change_type = _refine_change_type(category, clean_text)

            affected_api, affected_operation, replacement_api, extraction_confidence, attr_name = \
                _identify_change_subject(clean_text)

            if scan_root and affected_api:
                usage_hits = _find_project_usage(affected_api, scan_root)
            elif scan_root and attr_name:
                usage_hits = _find_attribute_assignment_usage(attr_name, scan_root)
            else:
                usage_hits = []
            usage_status = _usage_status_from_hits(usage_hits, scan_root)
            confirmed = [h for h in usage_hits if h['kind'] == 'confirmed']
            potential = [h for h in usage_hits if h['kind'] == 'potential']

            baseline_severity = _compute_baseline_severity(change_type)
            old_behavior, new_behavior = _extract_old_new_behavior(clean_text, change_type)

            changes.append({
                "version": version,
                "category": category,
                "change_type": change_type,
                "title": _generate_change_title(affected_api, affected_operation, change_type, category),
                "affected_api": affected_api,
                "affected_operation": affected_operation,
                "replacement_api": replacement_api,
                "old_behavior": old_behavior,
                "new_behavior": new_behavior,
                "source_excerpt": clean_text,
                "extraction_confidence": extraction_confidence,
                "baseline_severity": baseline_severity,
                "risk": _compute_change_risk(baseline_severity, usage_status),
                "recommendation": _generate_recommendation(change_type, affected_api, affected_operation, replacement_api),
                "usage_status": usage_status,
                "usage_hits": confirmed or potential,
                "url": url,
            })
    return changes


def _compute_overall_status(changes, scan_root):
    """
    Overall status drives the verdict. Deliberately status-based rather
    than a single numeric aggregate: whether the project was scanned at
    all changes what we're even allowed to claim.
    """
    high_severity = [c for c in changes if c["baseline_severity"] == "HIGH"]
    if not scan_root:
        return "UNKNOWN" if high_severity else "LOW"
    confirmed = [c for c in high_severity if c["usage_status"] == SCANNED_CONFIRMED]
    potential = [c for c in high_severity if c["usage_status"] == SCANNED_POTENTIAL]
    if confirmed:
        return "CRITICAL"
    if potential:
        return "MEDIUM"
    return "LOW"


def _generate_verdict(overall_status, changes, scan_root, python_compat):
    high_severity = [c for c in changes if c["baseline_severity"] == "HIGH"]
    confirmed = [c for c in high_severity if c["usage_status"] == SCANNED_CONFIRMED]
    potential = [c for c in high_severity if c["usage_status"] == SCANNED_POTENTIAL]

    lines = []
    if overall_status == "UNKNOWN":
        lines.append("⚠ CONDITIONAL — PROJECT SCAN REQUIRED")
        lines.append("")
        lines.append("The release contains potentially breaking changes, but")
        lines.append("project-level exposure could not be determined.")
        lines.append("Set PROJECT_SCAN_ROOT to your project's path and re-run.")
    elif overall_status == "CRITICAL":
        lines.append("✗ UPGRADE BLOCKED / MIGRATION REQUIRED")
        lines.append("")
        lines.append("Confirmed usage of APIs or behaviors affected by the target")
        lines.append("release was detected:")
        for c in confirmed:
            lines.append(f"  - {c['title']}")
    elif overall_status == "MEDIUM":
        lines.append("⚠ UPGRADE WITH CAUTION")
        lines.append("")
        lines.append("Potential affected usage was detected. Review the listed")
        lines.append("files before upgrading:")
        for c in potential:
            lines.append(f"  - {c['title']}")
    else:
        lines.append("✓ LOW RISK — NO DIRECT IMPACT DETECTED")
        lines.append("")
        if scan_root:
            lines.append("No confirmed usage of the identified breaking APIs was found.")
        else:
            lines.append("No breaking, removed, or security-relevant changes were identified.")
        lines.append("Environment and build requirements should still be verified.")

    if python_compat["dropped"]:
        lines.append("")
        lines.append("Note: this release drops support for Python "
                      f"{', '.join(python_compat['dropped'])}. Verify your runtime.")

    return "\n".join(lines)


def _compute_analysis_confidence(version_notes, no_notes_versions, unavailable_versions,
                                  changes, scan_root):
    """
    A measurable confidence score, not a fabricated percentage. Built
    from actual signals: what fraction of versions had a real source,
    what fraction of changes had a confidently-identified subject, and
    whether project scanning ran at all. Each component is 0-100 based
    on a real ratio; the overall score is their average, rounded.
    """
    components = {}

    total_versions = len(version_notes) + len(no_notes_versions) + len(unavailable_versions)
    components["Release-note retrieval"] = (
        round(100 * len(version_notes) / total_versions) if total_versions else 100
    )

    risk_relevant = [c for c in changes if c["baseline_severity"] != "INFO"]
    if risk_relevant:
        confidently_identified = sum(
            1 for c in risk_relevant if c["extraction_confidence"] in ("HIGH", "MEDIUM")
        )
        components["API/operation identification"] = round(100 * confidently_identified / len(risk_relevant))
    else:
        components["API/operation identification"] = 100

    components["Project scan"] = 100 if scan_root else 0

    overall = round(sum(components.values()) / len(components))

    def _label(score):
        if score >= 85:
            return "HIGH"
        if score >= 60:
            return "MEDIUM"
        return "LOW"

    lines = ["## ANALYSIS CONFIDENCE", ""]
    for name, score in components.items():
        lines.append(f"{name}: {_label(score)} ({score}%)")
    lines.append("")
    lines.append(f"Overall Analysis Confidence: {overall}%")
    if not scan_root:
        lines.append("(Capped by project scan not being performed - set PROJECT_SCAN_ROOT to improve this.)")
    return "\n".join(lines), overall


def build_upgrade_summary_report(package, ecosystem, current, target, path, version_notes,
                                  no_notes_versions, unavailable_versions=None, discrepancies=None,
                                  npm_deprecations=None, security_advisories=None):
    """
    The risk-oriented report: raw release note -> normalized change ->
    project usage -> risk -> recommendation, instead of a flat dump of
    changelog text under category headers. Answers "what can break in MY
    project" rather than "what did the changelog say."
    """
    unavailable_versions = unavailable_versions or []
    discrepancies = discrepancies or {}
    npm_deprecations = npm_deprecations or {}
    security_advisories = security_advisories or {}
    scan_root = PROJECT_SCAN_ROOT

    bump_by_version = {}
    prev = current
    for v in path:
        bump_by_version[v] = classify_bump(prev, v)
        prev = v
    # Release Type for the whole upgrade = the highest-severity bump crossed
    overall_bump = max((bump_by_version[v] for v in path), key=lambda b: ["patch", "minor", "major", "prerelease"].index(b)) if path else "patch"

    all_categorized = [categorize_notes_smart(v, notes) for v, notes in version_notes]
    for v, notice in npm_deprecations.items():
        extra = {cat: [] for cat in CATEGORY_ORDER}
        extra["New Deprecations"].append((v, notice["text"], notice["url"]))
        all_categorized.append(extra)
    for v, advisories in security_advisories.items():
        extra = {cat: [] for cat in CATEGORY_ORDER}
        for adv in advisories:
            summary = f"[{adv.get('severity', 'unknown').upper()}] {adv.get('summary', '')}".strip()
            extra["Security Updates"].append((v, summary, adv.get("url", "")))
        all_categorized.append(extra)
    merged = merge_categories(all_categorized)

    raw_texts = [notes["text"] for _, notes in version_notes]
    python_compat = _detect_python_compatibility(raw_texts)

    changes = _build_changes(merged, scan_root)
    overall_status = _compute_overall_status(changes, scan_root)

    confirmed_apis = {c["affected_api"] or c["affected_operation"] for c in changes
                       if c["usage_status"] == SCANNED_CONFIRMED}
    potential_apis = {c["affected_api"] or c["affected_operation"] for c in changes
                       if c["usage_status"] == SCANNED_POTENTIAL}
    production_files = set()
    test_files = set()
    for c in changes:
        for hit in c["usage_hits"]:
            (test_files if hit["is_test"] else production_files).add(hit["path"])

    lines = []
    lines.append("=" * 60)
    lines.append("## UPGRADE SUMMARY")
    lines.append(f"Package: {package}")
    lines.append(f"From: {current}")
    lines.append(f"To: {target}")
    lines.append(f"Release Type: {BUMP_LABELS[overall_bump]}")
    lines.append(f"Overall Risk: {overall_status}")

    if python_compat["dropped"] or python_compat["supported"]:
        lines.append("Python Compatibility:")
        for v in python_compat["dropped"]:
            lines.append(f"⚠ Python {v} is no longer supported.")
        if python_compat["supported"]:
            lines.append(f"✓ Python {python_compat['supported']} supported.")

    lines.append("Project Scan:")
    if scan_root:
        summary = _scan_files_summary(scan_root)
        lines.append(f"Status: SCANNED ({scan_root})")
        lines.append(f"Files scanned: {summary['files_scanned']}")
        lines.append(f"{'⚠' if confirmed_apis else '✓'} "
                      f"{len(confirmed_apis) or 'No'} confirmed affected API(s)/operation(s)")
        lines.append(f"{'⚠' if potential_apis else '✓'} "
                      f"{len(potential_apis) or 'No'} potential affected API(s)/operation(s)")
        lines.append(f"{'⚠' if production_files else '✓'} "
                      f"{len(production_files) or 'No'} affected production file(s)")
        lines.append(f"{'⚠' if test_files else '✓'} "
                      f"{len(test_files) or 'No'} affected test file(s)")
    else:
        lines.append("Status: NOT SCANNED")
        lines.append("Set PROJECT_SCAN_ROOT to your project's path to determine actual exposure.")

    breaking = [c for c in changes if c["category"] in ("Breaking Changes", "Expired Deprecations")]
    if breaking:
        lines.append("=" * 60)
        lines.append("## BREAKING / MIGRATION IMPACT")
        for i, c in enumerate(breaking, 1):
            lines.append(f"{i}. Change: {c['title']}")
            lines.append(f"   Type: {c['change_type']}")
            lines.append(f"   Affected API/Operation: {c['affected_api'] or c['affected_operation'] or 'Not explicitly identified'}")
            if c["old_behavior"]:
                lines.append(f"   Old Behavior: {c['old_behavior']}")
            if c["new_behavior"]:
                lines.append(f"   New Behavior: {c['new_behavior']}")
            lines.append(f"   Replacement: {c['replacement_api'] or 'Not explicitly specified'}")

            if c["usage_status"] == NOT_SCANNED:
                lines.append("   Project Usage: NOT SCANNED")
            elif c["usage_status"] == SCANNED_CONFIRMED:
                lines.append(f"   Project Usage: CONFIRMED ({len(c['usage_hits'])} location(s))")
                for hit in c["usage_hits"][:3]:
                    label = 'test' if hit['is_test'] else 'production'
                    lines.append(f"     File: {hit['path']}")
                    lines.append(f"     Line: {hit['line']} [{label}]")
                    if hit.get('code'):
                        lines.append(f"     Code: {hit['code']}")
                    lines.append(f"     Detection: {hit.get('detection_method', 'AST')}")
            elif c["usage_status"] == SCANNED_POTENTIAL:
                lines.append(f"   Project Usage: POTENTIAL ({len(c['usage_hits'])} location(s), unproven)")
                for hit in c["usage_hits"][:3]:
                    label = 'test' if hit['is_test'] else 'production'
                    lines.append(f"     File: {hit['path']}")
                    lines.append(f"     Line: {hit['line']} [{label}]")
                    if hit.get('code'):
                        lines.append(f"     Code: {hit['code']}")
            else:
                lines.append("   Project Usage: SCANNED - NOT FOUND")

            lines.append(f"   Confidence: {c['extraction_confidence']}")
            lines.append(f"   Impact: {c['risk']}")
            lines.append(f"   Recommendation: {c['recommendation']}")
            if c["url"]:
                lines.append(f"   Source: {c['url']}")
            lines.append(f"   Source Excerpt: \"{c['source_excerpt'][:200].strip()}\"")

    deprecations = [c for c in changes if c["category"] == "New Deprecations"]
    if deprecations:
        lines.append("=" * 60)
        lines.append("## DEPRECATIONS")
        for i, c in enumerate(deprecations, 1):
            lines.append(f"{i}. Change: {c['title']}")
            lines.append(f"   Affected API/Operation: {c['affected_api'] or c['affected_operation'] or 'Not explicitly identified'}")
            lines.append(f"   Replacement: {c['replacement_api'] or 'Not explicitly specified'}")
            lines.append(f"   Confidence: {c['extraction_confidence']}")
            lines.append(f"   Recommendation: {c['recommendation']}")
            if c["url"]:
                lines.append(f"   Source: {c['url']}")

    compat = [c for c in changes if c["category"] in ("Compatibility Changes", "API Changes", "Database / ORM Changes")]
    if compat:
        lines.append("=" * 60)
        lines.append("## COMPATIBILITY CHANGES")
        for i, c in enumerate(compat, 1):
            lines.append(f"{i}. Change: {c['title']}")
            lines.append(f"   Affected API/Operation: {c['affected_api'] or c['affected_operation'] or 'Not explicitly identified'}")
            if c["old_behavior"]:
                lines.append(f"   Old Behavior: {c['old_behavior']}")
            if c["new_behavior"]:
                lines.append(f"   New Behavior: {c['new_behavior']}")
            lines.append(f"   Confidence: {c['extraction_confidence']}")
            lines.append(f"   Impact: {c['risk']}")
            lines.append(f"   Recommendation: {c['recommendation']}")
            if c["url"]:
                lines.append(f"   Source: {c['url']}")

    build_toolchain = merged.get("Build / Toolchain Changes", [])
    if build_toolchain:
        lines.append("=" * 60)
        lines.append("## BUILD / TOOLCHAIN")
        seen_titles = set()
        for _, text, _ in build_toolchain:
            clean_text, _ = _strip_section_suffix(text)
            if _looks_like_noise_fragment(clean_text):
                continue
            title = clean_text if len(clean_text) <= 90 else clean_text[:87].rstrip() + "..."
            if title.lower() not in seen_titles:
                seen_titles.add(title.lower())
                lines.append(f"- {title}")

    security = [c for c in changes if c["category"] == "Security Updates"]
    if security:
        lines.append("=" * 60)
        lines.append("## SECURITY")
        for i, c in enumerate(security, 1):
            lines.append(f"{i}. {c['title']}")
            lines.append(f"   Recommendation: {c['recommendation']}")
            if c["url"]:
                lines.append(f"   Source: {c['url']}")

    new_features = merged.get("New Features", [])
    if new_features:
        lines.append("=" * 60)
        lines.append("## NEW FEATURES")
        for _, text, _ in new_features:
            clean_text, _ = _strip_section_suffix(text)
            if _looks_like_noise_fragment(clean_text):
                continue
            lines.append(f"- {clean_text}")

    performance = merged.get("Performance Improvements", [])
    if performance:
        lines.append("=" * 60)
        lines.append("## PERFORMANCE")
        for _, text, _ in performance:
            clean_text, _ = _strip_section_suffix(text)
            lines.append(f"- {clean_text}")

    bug_fixes = merged.get("Bug Fixes", [])
    if bug_fixes:
        lines.append("=" * 60)
        lines.append("## BUG FIXES")
        for _, text, _ in bug_fixes:
            clean_text, _ = _strip_section_suffix(text)
            lines.append(f"- {clean_text}")

    if discrepancies:
        lines.append("=" * 60)
        lines.append("## SOURCE DISCREPANCIES")
        for v, pair in discrepancies.items():
            lines.append(f"⚠ Sources disagree for {v}:")
            lines.append(f"  {pair['primary'].get('source', 'Source A')}: {pair['primary']['text'][:200].strip()}")
            lines.append(f"  {pair['secondary'].get('source', 'Source B')}: {pair['secondary']['text'][:200].strip()}")

    if unavailable_versions or no_notes_versions:
        lines.append("=" * 60)
        lines.append("## VERIFICATION NOTES")
        for v in unavailable_versions:
            lines.append(f"⚠ {v}: source unavailable after retries - re-run later or set GITHUB_TOKEN")
        for v in no_notes_versions:
            lines.append(f"⚠ {v}: unverified - no official source found")

    if scan_root:
        summary = _scan_files_summary(scan_root)
        lines.append("=" * 60)
        lines.append("## CODE SCAN SUMMARY")
        lines.append(f"Scanned project: {scan_root}")
        lines.append(f"Files scanned: {summary['files_scanned']}")
        lines.append(f"Files skipped: {summary['files_skipped']}")
        lines.append(f"Confirmed affected APIs: {len(confirmed_apis)}")
        lines.append(f"Potential affected APIs: {len(potential_apis)}")
        lines.append(f"Affected production files: {len(production_files)}")
        lines.append(f"Affected test files: {len(test_files)}")

    lines.append("=" * 60)
    confidence_section, _overall_confidence = _compute_analysis_confidence(
        version_notes, no_notes_versions, unavailable_versions, changes, scan_root,
    )
    lines.append(confidence_section)

    lines.append("=" * 60)
    lines.append("## VERDICT")
    lines.append(_generate_verdict(overall_status, changes, scan_root, python_compat))

    return "\n".join(lines)


def build_report(package, ecosystem, current, target, path, version_notes, no_notes_versions,
                  unavailable_versions=None, discrepancies=None, npm_deprecations=None,
                  security_advisories=None):
    """
    version_notes: list of (version, notes_dict) for versions where we found something.
    no_notes_versions: versions genuinely checked with nothing found (unverified).
    unavailable_versions: versions where a network/rate-limit error prevented checking at all.
    discrepancies: dict version -> {"primary": notes, "secondary": notes} where two
                   official sources disagreed and neither was silently preferred.
    npm_deprecations: dict version -> notes dict (npm registry deprecation notices).
    security_advisories: dict version -> list of advisory dicts from GitHub Security Advisories.
    """
    unavailable_versions = unavailable_versions or []
    discrepancies = discrepancies or {}
    npm_deprecations = npm_deprecations or {}
    security_advisories = security_advisories or {}

    # --- Semver bump for every version in the path ---
    bump_by_version = {}
    prev = current
    for v in path:
        bump_by_version[v] = classify_bump(prev, v)
        prev = v

    all_categorized = [categorize_notes_smart(v, notes) for v, notes in version_notes]

    # Fold in npm deprecation notices and security advisories as their own entries,
    # so they show up even for versions where the main changelog chain found nothing.
    for v, notice in npm_deprecations.items():
        extra = {cat: [] for cat in CATEGORY_ORDER}
        extra["Deprecations"].append((v, notice["text"], notice["url"]))
        all_categorized.append(extra)

    for v, advisories in security_advisories.items():
        extra = {cat: [] for cat in CATEGORY_ORDER}
        for adv in advisories:
            summary = f"[{adv.get('severity', 'unknown').upper()}] {adv.get('summary', '')}".strip()
            extra["Security Updates"].append((v, summary, adv.get("url", "")))
        all_categorized.append(extra)

    merged = merge_categories(all_categorized)

    verified_versions = {v for v, _ in version_notes}
    summary_stats = {
        'confirmed_apis': set(),
        'potential_apis': set(),
        'production_files': set(),
        'test_files': set(),
    }

    lines = []
    lines.append(f"Package: {package} ({'npm' if ecosystem == 'npm' else 'PyPI'})")
    lines.append(f"Current Version: {current}")
    lines.append(f"Target Version: {target}")
    lines.append("")
    lines.append("Release Path")
    for v in path:
        bump_label = BUMP_LABELS[bump_by_version[v]]
        tag = f"  - {v} [{bump_label}]"
        if v in unavailable_versions:
            tag += "  ⚠ Source unavailable after retries"
        elif v not in verified_versions and v not in npm_deprecations and v not in security_advisories:
            tag += "  ⚠ Unverified (no official source found)"
        lines.append(tag)
    lines.append("")
    lines.append("=" * 60)

    for cat in CATEGORY_ORDER:
        entries = merged[cat]
        lines.append("")
        lines.append(cat)
        lines.append("-" * len(cat))
        if not entries:
            if cat == "Security Updates":
                lines.append("No security-related changes were identified in the official release notes.")
            else:
                lines.append("(No changes found in this category)")
        else:
            for version, text, url in entries:
                bump = bump_by_version.get(version, "patch")
                bump_label = BUMP_LABELS[bump]
                line_out = f"[{version} · {bump_label}] {text}"
                if "[Section:" in text:
                    section_text = re.search(r'\[Section: (.+?)\]', text)
                    if section_text:
                        line_out += f"\n    Section: {section_text.group(1)}"

                if PROJECT_SCAN_ROOT:
                    api_candidates = _extract_api_candidates(text)
                    if api_candidates:
                        api_name = api_candidates[0]
                        usage_hits = _find_project_usage(api_name, PROJECT_SCAN_ROOT)
                        if usage_hits:
                            confirmed = [hit for hit in usage_hits if hit['kind'] == 'confirmed']
                            potential = [hit for hit in usage_hits if hit['kind'] == 'potential']
                            if confirmed:
                                summary_stats['confirmed_apis'].add(api_name)
                                for hit in confirmed:
                                    if hit['is_test']:
                                        summary_stats['test_files'].add(hit['path'])
                                    else:
                                        summary_stats['production_files'].add(hit['path'])
                                line_out += f"\n    Confirmed Usage: {api_name}"
                                for hit in confirmed[:3]:
                                    location = f"{hit['path']}:{hit['line']}"
                                    label = 'test' if hit['is_test'] else 'production'
                                    line_out += f"\n      - {location} [{label}]"
                            elif potential:
                                summary_stats['potential_apis'].add(api_name)
                                for hit in potential:
                                    if hit['is_test']:
                                        summary_stats['test_files'].add(hit['path'])
                                    else:
                                        summary_stats['production_files'].add(hit['path'])
                                line_out += f"\n    Potential Usage: {api_name}"
                                line_out += f"\n    Confidence: Low"
                                for hit in potential[:3]:
                                    location = f"{hit['path']}:{hit['line']}"
                                    label = 'test' if hit['is_test'] else 'production'
                                    line_out += f"\n      - {location} [{label}]"
                            # No "else: Not detected" here on purpose - silence for
                            # the common case keeps the report readable; a per-line
                            # "not detected" for every single change is just noise.
                if url:
                    line_out += f"\n    Source: {url}"
                lines.append(line_out)
        lines.append("")
        lines.append("=" * 60)

    if discrepancies:
        lines.append("")
        lines.append("Source Discrepancies")
        lines.append("-" * len("Source Discrepancies"))
        lines.append("Two official sources disagreed for the same version - shown")
        lines.append("side by side rather than silently picking one:")
        for v, pair in discrepancies.items():
            lines.append("")
            lines.append(f"⚠ Source discrepancy detected for {v}:")
            lines.append(f"  {pair['primary'].get('source', 'Source A')}:")
            lines.append(f"    {pair['primary']['text'][:300].strip()}")
            lines.append(f"    Source: {pair['primary'].get('url', '')}")
            lines.append(f"  {pair['secondary'].get('source', 'Source B')}:")
            lines.append(f"    {pair['secondary']['text'][:300].strip()}")
            lines.append(f"    Source: {pair['secondary'].get('url', '')}")
        lines.append("")
        lines.append("=" * 60)

    if unavailable_versions:
        lines.append("")
        lines.append("Note: The following version(s) could not be checked because")
        lines.append("sources were unavailable after retries (e.g. a rate limit).")
        lines.append("Re-run later, or set a GITHUB_TOKEN, to check them:")
        for v in unavailable_versions:
            lines.append(f"  - {v}")

    if no_notes_versions:
        lines.append("")
        lines.append("Note: No official release notes, changelog, or commit data")
        lines.append("could be found for the following version(s). Treat these as")
        lines.append("unverified - check the project's release page manually:")
        for v in no_notes_versions:
            lines.append(f"  - {v}")

    if PROJECT_SCAN_ROOT:
        summary = _scan_files_summary(PROJECT_SCAN_ROOT)
        lines.append("")
        lines.append("=" * 60)
        lines.append("")
        lines.append("CODE SCAN SUMMARY")
        lines.append("-" * len("CODE SCAN SUMMARY"))
        lines.append(f"Scanned project: {PROJECT_SCAN_ROOT}")
        lines.append(f"Files scanned: {summary['files_scanned']}")
        lines.append(f"Files skipped: {summary['files_skipped']}")
        lines.append("Skipped directories:")
        for skipped_dir in summary['skipped_directories']:
            lines.append(f"- {skipped_dir}")
        lines.append(f"Confirmed affected APIs: {len(summary_stats['confirmed_apis'])}")
        lines.append(f"Potential affected APIs: {len(summary_stats['potential_apis'])}")
        lines.append(f"Affected production files: {len(summary_stats['production_files'])}")
        lines.append(f"Affected test files: {len(summary_stats['test_files'])}")

    return "\n".join(lines)
