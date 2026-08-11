# Dependency Upgrade Impact Analyzer

Enter a package name, current version, and target version. The app finds
every version in between, pulls the official changes for each one, and
gives you one merged report you can download as a `.txt` file.

Supports **PyPI (Python)** and **npm (JavaScript)** packages.

## Setup

```
pip install -r requirements.txt
```

For running the test suite:

```
pip install -r requirements-dev.txt
```

## Optional environment variables

### `GITHUB_TOKEN` (recommended)
Without a token, GitHub allows only 60 API requests/hour, which this app
can burn through quickly (it checks multiple sources per version, plus
cross-checking and security advisories). With a token, that limit becomes
5000/hour.

1. Create a token at https://github.com/settings/tokens (no special scopes needed)
2. `export GITHUB_TOKEN=your_token_here`

### `ANTHROPIC_API_KEY` (optional)
If set, the app uses Claude to classify and summarize each change with
full context, instead of keyword matching. This fixes false positives
like "Improved test coverage" getting flagged just because it contains
the word "test". If not set, the app automatically falls back to the
keyword-based classifier - nothing breaks either way.

```
export ANTHROPIC_API_KEY=your_key_here
```

## Run

```
python app.py
```

Then open http://127.0.0.1:5000 in your browser.

## How it decides what to include

For each intermediate version, it checks sources in this order:

1. **Known official adapter**, for packages that publish detailed release
   notes as files in their own repo (see list below) - the most reliable
   path, since it reads the exact source that powers the project's docs site.
2. **GitHub Release notes** (official, attached to the version's tag)
3. **CHANGELOG.md / HISTORY.md / CHANGES.md** in the repo
4. **GitHub commit messages** between the previous and current tag (last resort)

For npm packages, it also checks the registry's own `deprecated` field on
each version - a first-party signal separate from changelog scraping.

Separately, every version is checked against **GitHub's Security Advisory
database** (the same structured CVE data behind Dependabot), which is
more reliable than grepping changelog text for the word "security".

**Cross-checking:** when a curated source (known adapter or CHANGELOG) is
used, the app also checks GitHub Releases as an independent second
opinion. If the two disagree significantly, the report shows both side by
side under "Source Discrepancies" instead of silently picking one.

Lines are filtered to keep only user-impacting changes (breaking changes,
new features, security fixes, bug fixes, performance, deprecations,
API/config/DB changes), classified with Claude if `ANTHROPIC_API_KEY` is
set (otherwise keyword-based), and duplicates across versions are merged.

### Packages with dedicated official-source adapters
Django, NumPy, pandas, Seaborn, Flask, FastAPI, Matplotlib, pydantic,
scikit-learn, SQLAlchemy, TensorFlow.

Requests, LangChain, PyTorch, and Streamlit don't have dedicated adapters
because the generic chain already resolves them well (their GitHub
Releases or CHANGELOG files are already reliable and consistently named).
Any other package automatically uses the generic chain too.

## Reliability features

- **Caching** - results are cached on disk, keyed by
  `ecosystem:package:version:source`, so re-running the same query
  doesn't re-hit GitHub/PyPI/npm. Cache lives in `cache/` (gitignored).
- **Retries with backoff** - network calls retry up to 3 times with
  exponential backoff before giving up.
- **Graceful degradation** - if a source is unavailable after retries
  (e.g. a rate limit) for one version, that version is marked
  `⚠ Source unavailable after retries` and the rest of the report still
  runs - one bad version never kills the whole analysis.
- **Explicit uncertainty** - a version with no source found at all is
  marked `⚠ Unverified`, never silently dropped.
- **Semver labeling** - every version in the path is tagged
  MAJOR/MINOR/PATCH/PRERELEASE, and a breaking change found in a
  non-major release is flagged as a semver violation.
- **Pre-release toggle** - alpha/beta/rc versions are excluded by
  default; check "Include prereleases" to include them. This is
  deterministic - the same input always produces the same path.

## Known limitations

- Only works for packages hosted on PyPI or npm with a linked GitHub repository.
- Keyword-based categorization (the default, when `ANTHROPIC_API_KEY`
  isn't set) is fast and free but can occasionally miscategorize a line.
  Setting `ANTHROPIC_API_KEY` fixes most of this.
- Cross-checking and security advisories add extra GitHub API calls, so
  they benefit the most from setting `GITHUB_TOKEN`.
- If a project's changelog format is unusual, some versions may show up
  under "Unverified" - check the noted list at the bottom of the report.
- 100% accuracy isn't achievable: some projects have incomplete release
  notes, and undocumented changes can't be verified against any source.
  The goal is high-confidence and explicit about uncertainty, not a
  promise that every change is captured.

## Running the tests

```
python -m pytest tests/ -v
```

Covers version parsing, semver classification, changelog section
extraction (Markdown and RST/Setext formats), noise filtering, caching
(including source-keyed caching), source discrepancy detection, security
advisory range matching, pre-release determinism, and LLM fallback
behavior.
