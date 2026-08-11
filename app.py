"""
app.py

A small Flask app with one job: given a package name, current version,
and target version, produce a report of every user-impacting change
introduced between those two versions (official release notes first,
falling back to CHANGELOG, then GitHub commits).

Supports both PyPI (Python) and npm (JavaScript) packages.
"""

import os
import traceback

from flask import Flask, render_template, request, send_file

from services.version_service import (
    fetch_package_info,
    get_upgrade_path,
    PackageNotFoundError,
    VersionNotFoundError,
)
from services.source_service import fetch_notes_for_version
from services.security_advisory_service import get_advisories_for_version
from services.report_service import build_upgrade_summary_report

app = Flask(__name__)

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)
LATEST_REPORT_PATH = os.path.join(REPORTS_DIR, "latest_report.txt")


def _form_defaults(package="", current_version="", target_version="",
                    ecosystem="pypi", include_prereleases=False):
    return dict(package=package, current_version=current_version, target_version=target_version,
                ecosystem=ecosystem, include_prereleases=include_prereleases)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", report=None, error=None, **_form_defaults())


@app.route("/analyze", methods=["POST"])
def analyze():
    package = request.form.get("package", "").strip()
    current = request.form.get("current_version", "").strip()
    target = request.form.get("target_version", "").strip()
    ecosystem = request.form.get("ecosystem", "pypi").strip()
    include_prereleases = request.form.get("include_prereleases") == "on"

    form_values = _form_defaults(package, current, target, ecosystem, include_prereleases)

    if ecosystem not in ("pypi", "npm"):
        ecosystem = "pypi"

    if not package or not current or not target:
        return render_template("index.html", report=None,
                                error="Please fill in the package name, current version, and target version.",
                                **form_values)

    try:
        package_info = fetch_package_info(ecosystem, package)
        all_versions = package_info["all_versions"]
        repo = package_info["repo"]

        path = get_upgrade_path(all_versions, current, target, include_prereleases=include_prereleases)

        if not path:
            return render_template("index.html", report=None,
                                    error=("No intermediate versions were found between those two versions "
                                           "(they may all be prereleases - try enabling 'Include prereleases')."),
                                    **form_values)

        if not repo:
            registry_name = "npm" if ecosystem == "npm" else "PyPI"
            return render_template(
                "index.html", report=None,
                error=(f"Could not find a GitHub repository for '{package}' on {registry_name}, "
                       "so no release notes or changelog could be retrieved."),
                **form_values,
            )

        version_notes = []
        no_notes_versions = []
        unavailable_versions = []
        discrepancies = {}
        npm_deprecations = {}
        security_advisories = {}
        prev = current
        for v in path:
            result = fetch_notes_for_version(package, repo, v, prev, ecosystem=ecosystem)

            if result["notes"]:
                version_notes.append((v, result["notes"]))
            elif result["unavailable"]:
                unavailable_versions.append(v)
            else:
                no_notes_versions.append(v)

            if result["discrepancy"] and result["cross_check"]:
                discrepancies[v] = {"primary": result["notes"], "secondary": result["cross_check"]}

            if result["npm_deprecation"]:
                npm_deprecations[v] = result["npm_deprecation"]

            advisories = get_advisories_for_version(ecosystem, package, v)
            if advisories:
                security_advisories[v] = advisories

            prev = v

        if not version_notes and not npm_deprecations and not security_advisories:
            if unavailable_versions and not no_notes_versions:
                return render_template(
                    "index.html", report=None,
                    error=("Sources were unavailable after retries for every version in this range "
                           "(likely a rate limit). Please try again later, or set a GITHUB_TOKEN."),
                    **form_values,
                )
            return render_template(
                "index.html", report=None,
                error=("No release notes, changelog, or commit history could be found for any "
                       "version in this range. Nothing to report."),
                **form_values,
            )

        report_text = build_upgrade_summary_report(
            package, ecosystem, current, target, path, version_notes, no_notes_versions,
            unavailable_versions=unavailable_versions, discrepancies=discrepancies,
            npm_deprecations=npm_deprecations, security_advisories=security_advisories,
        )

        with open(LATEST_REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report_text)

        return render_template("index.html", report=report_text, error=None, **form_values)

    except PackageNotFoundError as e:
        return render_template("index.html", report=None, error=str(e), **form_values)
    except VersionNotFoundError as e:
        return render_template("index.html", report=None, error=str(e), **form_values)
    except ConnectionError as e:
        return render_template("index.html", report=None, error=str(e), **form_values)
    except ValueError as e:
        return render_template("index.html", report=None, error=str(e), **form_values)
    except Exception:
        traceback.print_exc()
        return render_template("index.html", report=None,
                                error="An unexpected error occurred while generating the report. Please try again.",
                                **form_values)


@app.route("/download", methods=["GET"])
def download():
    if not os.path.exists(LATEST_REPORT_PATH):
        return "No report available yet. Please run an analysis first.", 404
    return send_file(LATEST_REPORT_PATH, as_attachment=True, download_name="upgrade_impact_report.txt")


if __name__ == "__main__":
    app.run(debug=True)
