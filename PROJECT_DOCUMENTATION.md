# Dependency Upgrade Impact Analyzer — Production Reference

**Last updated:** covers the codebase as of the latest changes (106/106 tests passing)

This document explains what the tool does, how it works internally, every data source it uses, its determinism and reliability guarantees, its known limitations, and what you need to configure to run it in production. Read the **Reliability & Determinism** and **Known Limitations** sections before trusting this in a CI/CD gate — they are the most important parts of this document.

---

## 1. What this tool does

Given a package name, a current version, and a target version, the analyzer answers one question:

> **"If I upgrade this dependency, what can break in MY project?"**

It does **not** just summarize a changelog. It runs every release-note entry through a five-stage pipeline:

```
Official release note → Normalized change → Project usage → Risk → Recommendation
```

The output is a structured report — not a flat dump of changelog text — that tells you what changed, whether your code is actually exposed, where, how confident the tool is, and what official source backs the claim.

Supports **PyPI (Python)** and **npm (JavaScript)** packages.

---

## 2. Architecture overview

```
app.py                          Flask routes: form -> analysis -> report -> download
services/
  version_service.py            PyPI/npm version listing, upgrade-path calculation, semver parsing
  semver_service.py             MAJOR/MINOR/PATCH/PRERELEASE classification
  source_service.py             Generic changelog/release-note fetching + retries + caching
  known_sources.py              Dedicated official-source adapters for major packages
  npm_source_service.py         npm-specific source branch (registry deprecation notices)
  security_advisory_service.py  GitHub Security Advisory (CVE) lookups
  llm_classifier.py             Optional Groq-based classification (off by default)
  cache_service.py              File-based cache, keyed by ecosystem:package:version:source
  report_service.py             The core: normalization, risk engine, project scanner, report builder
tests/
  test_golden_dataset.py        Frozen regression cases for known extraction/risk bugs
  test_report_service.py        Categorization, noise filtering, project scanning
  test_semver_service.py        Version bump classification
  test_source_service.py        Changelog section extraction (Markdown + RST)
  test_new_features.py          Caching, cross-checking, LLM fallback, pre-release toggling
```

`report_service.py` is intentionally the largest module (~1,900 lines) because it owns the entire normalization -> risk -> report pipeline. Everything upstream of it (version resolution, source fetching) is comparatively simple and stable.

---

## 3. The five-stage pipeline, in detail

### Stage 1 — Fetch the official release note
For each intermediate version between current and target, sources are tried **in this exact priority order**, stopping at the first one with content:

1. **Known official adapter** (see Section 4) — reads the exact file that generates the project's own docs site
2. **GitHub Release notes** (the GitHub Releases API, attached to the version's tag)
3. **CHANGELOG.md / CHANGES.md / HISTORY.md** (and case variants) in the repo
4. **GitHub commit messages** between the previous and current tag (last resort — least structured, least reliable)

For npm packages specifically, the registry's own `deprecated` field is checked in parallel — a first-party signal distinct from changelog scraping.

**Cross-checking:** when a curated source (known adapter or CHANGELOG) is used, GitHub Releases is also checked as an independent second opinion. If they disagree significantly (word-overlap below 15%), both are shown side-by-side under "Source Discrepancies" instead of one being silently preferred.

### Stage 2 — Normalize into a structured change
Raw prose is never used directly as a title. Each entry is parsed into:

| Field | Meaning |
|---|---|
| `change_type` | REMOVAL / DEPRECATION / BEHAVIOR_CHANGE / COMPATIBILITY_CHANGE / BUILD_REQUIREMENT / SECURITY / NEW_FEATURE / PERFORMANCE / BUG_FIX / OTHER |
| `affected_api` | The exact API, only if explicitly named as the subject (never guessed) |
| `affected_operation` | A plain-English description when no API name applies (e.g. "shape attribute assignment") |
| `replacement_api` | The specific replacement, only if the source names one |
| `extraction_confidence` | HIGH / MEDIUM / LOW |
| `old_behavior` / `new_behavior` | Extracted when the text states them explicitly |
| `source_excerpt` | The original text, always preserved for verification |
| `source_url` | Direct link to the exact release note / file / release page |

**The core extraction rule: an API mentioned only as a suggested replacement is never reported as the thing being removed/deprecated.** This was the single biggest accuracy bug found during development — see Section 8.

### Stage 3 — Project usage (only when `PROJECT_SCAN_ROOT` is set)
Uses Python's `ast` module — not substring matching — to detect actual usage:

- Import detection: `import numpy as np`, `import numpy as anything`, `from numpy import row_stack`, `from numpy import row_stack as rs`
- Qualified calls: `np.row_stack(...)`, `numpy.row_stack(...)`
- Aliased calls: `stack = np.row_stack; stack(...)`
- Wildcard imports (`from numpy import *`) are flagged as **POTENTIAL**, not CONFIRMED, since static analysis can't prove the symbol actually came from that module
- Attribute-assignment detection (e.g. `arr.shape = new_shape`) via `ast.Assign` target inspection — separate from call detection, needed for changes like NumPy's shape/dtype-mutation deprecations
- Comments and string literals are **never** treated as usage (only real AST nodes count)
- `.venv`, `venv`, `node_modules`, `__pycache__`, `.git`, `dist`, `build` are always skipped

Every usage evidence status is one of exactly five values, and they are never conflated:

| Status | Meaning |
|---|---|
| `CONFIRMED` | AST-confirmed direct usage found |
| `POTENTIAL` | Plausible but unproven (e.g. via wildcard import) |
| `NOT DETECTED` | Scanned, and genuinely not found |
| `UNKNOWN` | Scanned, but **no detector exists** for this specific kind of change (e.g. in-place `.resize()` calls aren't currently AST-checked) |
| `NOT SCANNED` | `PROJECT_SCAN_ROOT` isn't configured at all |

**`UNKNOWN` and `NOT SCANNED` are deliberately distinct** — the tool never claims "not detected" when it never actually looked.

### Stage 4 — Risk
Two independent inputs combine into one risk level:

- **Baseline severity** (the release-note's own severity, independent of you): REMOVAL to HIGH, DEPRECATION to LOW, BEHAVIOR_CHANGE to MEDIUM, SECURITY to HIGH, etc.
- **Usage status** (your actual exposure)

| Baseline | Usage | Risk |
|---|---|---|
| HIGH | CONFIRMED | **CRITICAL** |
| HIGH | POTENTIAL | MEDIUM |
| HIGH | NOT DETECTED | LOW |
| HIGH | NOT SCANNED / UNKNOWN | **UNKNOWN** (never silently downgraded to "safe") |
| MEDIUM | CONFIRMED | HIGH |
| INFO (features, perf, bug fixes) | — | INFO (never risk-scored) |

### Stage 5 — Recommendation
Extracted from the source text when it names a concrete replacement (`"in favor of X"` -> `"Replace with X"`); falls back to a category-appropriate generic instruction only when the release note itself doesn't specify one. **Never fabricates a migration path the source didn't mention.**

---

## 4. Data sources — the complete list

### Primary sources (used for every package)
- **PyPI JSON API** (`pypi.org/pypi/{package}/json`) — version list, GitHub repo discovery
- **npm Registry API** (`registry.npmjs.org`) — version list, GitHub repo discovery, per-version `deprecated` field
- **GitHub Releases API** — official release notes
- **raw.githubusercontent.com** — CHANGELOG/CHANGES/HISTORY files, and all known-adapter source files
- **GitHub Compare API** — commit messages (last resort only)
- **GitHub Security Advisory Database** (`api.github.com/advisories`) — structured CVE data, cross-checked against the exact affected version range using a PEP 440-style range parser

### Dedicated official-source adapters (verified against live repos)
These read the exact source file that generates the project's own documentation, rather than guessing at generic changelog conventions:

| Package | Source file pattern |
|---|---|
| Django | `docs/releases/{version}.txt` |
| NumPy | `doc/source/release/{version}-notes.rst` |
| pandas | `doc/source/whatsnew/v{version}.rst` |
| Seaborn | `doc/whatsnew/v{version}.rst` |
| Flask | `CHANGES.rst` |
| FastAPI | `docs/en/docs/release-notes.md` (single file, all versions) |
| Matplotlib | `doc/users/prev_whats_new/whats_new_{minor}.0.rst` (minor/major releases only — patch releases have no dedicated file and fall back to the generic chain) |
| pydantic | `HISTORY.md` |
| scikit-learn | `doc/whats_new/v{minor}.rst` |
| SQLAlchemy | `doc/build/changelog/changelog_{XY}.rst` — **note:** this uses a non-standard Sphinx directive format (`.. changelog:: / :version: X.Y.Z`), not plain headings, and has its own dedicated parser |
| TensorFlow | `RELEASE.md` (single file, all versions) |

**Packages intentionally without a dedicated adapter:** requests, LangChain, PyTorch, Streamlit. Verified that their generic-chain resolution (GitHub Releases / CHANGELOG) is already reliable — for PyTorch specifically, `RELEASE.md` is a *release-process* document, not a changelog, so building an adapter for it would have been actively wrong.

### Sources considered but not integrated (documented for future work)
- **PyPI "yanked" metadata** — PyPI lets maintainers officially mark a release as yanked with a reason. Not currently read.
- **npm side:** only the `deprecated` field is read; no other npm-specific metadata is used.
- **RSS/Atom release feeds** — many projects publish one; more brittle to parse generically than the sources above, so deprioritized.
- **Libraries.io** — a secondary aggregator; ranked below primary sources deliberately, not used.
- **Project dependency files for transitive risk** (`requirements.txt`, `pyproject.toml` dependency lists, `poetry.lock`, etc.) — the tool reads `pyproject.toml`/`setup.cfg`/`setup.py` **only** for the project's own declared Python version requirement (Section 5). It does **not** parse dependency trees to assess transitive exposure through other packages. If your project doesn't use the affected API directly but a dependency does, this tool will not tell you that.

---

## 5. Project-level configuration checks

- **Python version compatibility**: the tool parses the release notes for explicit statements like `"drops support for Python 3.11"` / `"supports Python versions 3.12-3.14"` (regex-based, grounded in real NumPy phrasing — verified live). It then reads **your own** `pyproject.toml` (`requires-python`), `setup.cfg` (`python_requires`), or `setup.py` (`python_requires`) and cross-checks using `packaging.specifiers.SpecifierSet`. Result is always one of `COMPATIBLE` / `INCOMPATIBLE` / `UNKNOWN` — never silently assumed.
- **Build/toolchain requirements** (Cython version, MSVC version, etc.): the tool checks whether your project appears to build native/Cython extensions at all (presence of `.pyx`/`.pxd` files, or `cythonize`/`Extension(`/`build_ext` in `setup.py`/`pyproject.toml`) before treating these as anything more than informational. A pure-Python project is not told a Cython requirement is "breaking."

---

## 6. Reliability & Determinism — read this before production use

This is the most important section for a CI/CD gate use case. Being precise here rather than optimistic:

### What IS deterministic (same input -> same output, always)
- Version resolution, semver classification, upgrade-path calculation
- The **default classification path** (keyword/regex-based categorization in `report_service.py`) — pure functions, no randomness, no network variability once source text is fixed
- The AST-based project scanner — deterministic by construction (same file contents -> same AST -> same findings)
- The risk engine (baseline severity x usage status -> risk) — a fixed lookup table
- Caching: once a version's release notes are fetched, they're cached to disk keyed by `ecosystem:package:version:source` (30-day TTL for found results). **Re-running the same query returns byte-identical source text**, which is what makes repeatable results possible at all — release notes for a published version don't change, so once cached, extraction is fully reproducible.

### What is NOT deterministic, and why
- **The optional LLM classification path** (`llm_classifier.py`, active only if `GROQ_API_KEY` is set). This is the one component that can genuinely violate an "exact same output every time" requirement. Two important caveats:
  1. Even with `temperature=0`, LLM inference is **not guaranteed bit-for-bit deterministic** across calls on most hosted inference providers (including Groq), due to batching and floating-point non-associativity on GPU inference. This is a property of the underlying LLM serving stack, not something this codebase can fix.
  2. **Recommendation for production: leave `GROQ_API_KEY` unset.** The tool was explicitly designed so the LLM path is optional and the keyword-based classifier is a fully-functional, fully-deterministic fallback — not a degraded mode. Given a hard "exact same output every time" requirement, **the keyword-based path is what should run in production**, not the LLM path.
- **First-time network fetches**: if a source has never been cached and the upstream file changes between two runs (rare — release notes for a *published* version essentially never change, but not impossible), output could differ. Once cached, this risk disappears for that version.
- **GitHub API rate limiting**: without `GITHUB_TOKEN`, hitting the 60/hour limit will cause `UNKNOWN`/unavailable results that a token would have resolved. This doesn't produce *wrong* output (the tool never silently guesses), but it can produce *less complete* output run-to-run if you're near the rate limit boundary. **Set `GITHUB_TOKEN` in production** to eliminate this variable.

### On the "90% reliability" target
Being direct rather than optimistic: **no fake reliability percentage is claimed here**. What can honestly be said, based on what was actually tested:

- A frozen golden-dataset test suite (`test_golden_dataset.py`) encodes every known extraction failure found during development (see Section 8) as a permanent regression test. **106/106 tests pass** as of this document.
- The known false-positive patterns that were found and fixed (resize/reshape/finfo/cross misattribution, section-bleed merge bug, citation clutter, malformed fragment leakage) are now covered by regression tests, so they cannot silently reappear.
- Reliability is **necessarily uneven across packages**: it is highest for the 11 packages with dedicated adapters (Section 4) and for PyPI/npm packages with clean, conventional CHANGELOG files. It is lowest for packages with unconventional changelog formats, since the extraction heuristics are pattern-based, not a full NLP model.
- **This has not been measured as a single number against a large, representative sample of packages.** Treat "90%+" as a design target the architecture was built toward, not a benchmarked, certified statistic. If you need a certified number, you would need to build a labeled evaluation set across many real packages and measure precision/recall against it — that work has not been done here.

---

## 7. Known limitations (explicit, not hidden)

1. **Transitive dependency risk is not assessed.** If your project doesn't directly call an affected API but a dependency does, this tool gives no signal either way.
2. **Recommendation extraction is heuristic**, not a full NLP parse. It correctly extracts the majority of "in favor of X" / "replaced by X" patterns but can occasionally miss an unusually-phrased replacement — in which case it says `"Not explicitly specified"` rather than guessing.
3. **The project-usage AST scanner has no detector for every possible change type** — notably, in-place method-call patterns like `arr.resize(...)` (as opposed to `np.resize(...)`) are not currently AST-checked. These correctly report `UNKNOWN` rather than a false `NOT DETECTED`, but that means real usage could exist undetected.
4. **A known remaining noise case**: some multi-paragraph narrative asides (e.g. an algorithmic explanation citing an academic paper) can still occasionally get merged with an adjacent unrelated sentence during paragraph-boundary detection. Malformed-fragment detection catches the clearest cases (unbalanced parentheses, dangling citation numbers) but is not a complete fix for every possible merge artifact.
5. **Python-compatibility and build-requirement detection are both regex-based** against real, verified phrasing from NumPy's actual release notes. They will not catch every project's idiosyncratic wording for the same concept.
6. **Matplotlib's adapter only covers X.Y.0 releases** — patch releases fall back to the generic chain since Matplotlib itself doesn't publish per-patch release notes.
7. **No formal, large-scale golden dataset benchmark exists** (see Section 6) — only the specific failure cases found and fixed during development are covered by regression tests.

---

## 8. Bugs found and fixed during development (for context on what "reliable" means here)

Documenting these because they show the *kind* of failure mode this architecture is designed to catch, and because they're now permanent regression tests:

- **Section-bleed bug**: an unrecognized RST subsection heading let unrelated content merge into the previous change's description. Fixed with block-boundary tracking that treats *every* heading as a hard stop, not just recognized ones.
- **Subject-vs-replacement confusion**: `"numpy.resize deprecation expired"` was reported when the actual subject was in-place array resizing (no API name in the source), and `np.resize` was only mentioned as the *replacement*. Fixed by distinguishing text before/after "in favor of"/"via"/"instead use" trigger phrases.
- **Argument-specific behavior mislabeled as full deprecation**: `"Passing None as dtype to np.finfo will now raise a TypeError"` was reported as `"numpy.finfo deprecated"` — implying the whole function was going away, when only one argument's behavior changed. Fixed by detecting "no longer supports X" / "will now raise/require Y" patterns and classifying them as `BEHAVIOR_CHANGE` (baseline severity MEDIUM, not HIGH) with a specific title instead of a blanket label.
- **A single-character regex typo** (backtick-backtick-question-mark instead of a proper `{0,2}` quantifier) silently broke attribute-assignment detection for any text without literal Markdown backticks — a good example of why the golden-dataset suite matters: it caught this immediately when the underlying phrasing didn't have Markdown formatting.
- **`UNKNOWN` vs `NOT DETECTED` conflation**: a scanned project with no applicable detector for a given change type was reporting "not detected" — falsely implying a check had run. Fixed to report `UNKNOWN` in that specific case.

---

## 9. Configuration reference

| Variable | Required? | Effect if unset |
|---|---|---|
| `GITHUB_TOKEN` | Strongly recommended | GitHub API capped at 60 req/hour instead of 5000; more `UNKNOWN`/unavailable results under load |
| `PROJECT_SCAN_ROOT` | Recommended for any real use | No project-usage scanning at all; every finding reports `NOT SCANNED` and risk stays capped at `UNKNOWN` for HIGH/MEDIUM baseline changes |
| `GROQ_API_KEY` | **Do not set in production** if exact reproducibility is required | Falls back to the fully deterministic keyword-based classifier (recommended default) |
| `GROQ_MODEL` | Optional | Defaults to `llama-3.3-70b-versatile` (only relevant if `GROQ_API_KEY` is set) |

No other configuration exists. There is no database, no external state beyond the on-disk `cache/` directory (safe to delete any time — it will simply refetch).

---

## 10. Running it

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=your_token_here
export PROJECT_SCAN_ROOT=/path/to/the/project/you/want/checked
python app.py
```

Then open `http://127.0.0.1:5000`. Enter package name, current version, target version, choose PyPI or npm, optionally include pre-releases, and click Analyze. Download the report as `.txt` from the result page.

### Running the test suite
```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

---

## 11. Summary: what to actually rely on in production

- **Trust**: version resolution, the deterministic keyword classifier, the AST-based project scanner, the risk engine's arithmetic, and the explicit uncertainty states (`NOT SCANNED`/`UNKNOWN` are never silently hidden).
- **Verify manually before fully automating a CI gate on it**: recommendation text (always cross-check the linked source), any package without a dedicated adapter, and any finding whose `extraction_confidence` is `LOW`.
- **Do not enable** `GROQ_API_KEY` if byte-for-byte reproducibility across runs is a hard requirement — the deterministic path already covers the full pipeline without it.
