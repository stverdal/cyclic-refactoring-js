#!/usr/bin/env python3
# quality_single_summary_jsts.py
#
# Minimal JS/TS quality summary: parses JUnit XML (test results) + ESLint JSON.
#
# Usage:
#   python quality_single_summary_jsts.py <METRICS_DIR> <OUT_JSON> [--with-provenance]
# Example:
#   python quality_single_summary_jsts.py results/my-js-repo/main/code_quality_checks metrics.json

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def read_text(p):
    p = Path(p)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def read_json(p):
    p = Path(p)
    if not p.exists():
        return None
    try:
        txt = p.read_text(encoding="utf-8")
        return json.loads(txt)
    except Exception:
        return None


# -------------------- Test Results (JUnit XML) --------------------

def junit_counts(p):
    """Parse a JUnit XML file (jest-junit, vitest, mocha-junit-reporter)."""
    p = Path(p)
    if not p.exists():
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    try:
        root = ET.parse(p).getroot()
    except Exception:
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    suites = root.findall(".//testsuite") if root.tag != "testsuite" else [root]
    t = f = e = sk = 0
    for s in suites:
        t += int(s.attrib.get("tests", 0))
        f += int(s.attrib.get("failures", 0))
        e += int(s.attrib.get("errors", 0))
        sk += int(s.attrib.get("skipped", 0))
    return {"tests": t, "failures": f, "errors": e, "skipped": sk}


# -------------------- ESLint JSON --------------------

def eslint_summary(p):
    """Parse ESLint JSON output (array of file results)."""
    data = read_json(p)
    if not data or not isinstance(data, list):
        return {"issues": 0, "errors": 0, "warnings": 0, "fixable": 0}

    total_errors = 0
    total_warnings = 0
    total_fixable = 0

    for file_result in data:
        if not isinstance(file_result, dict):
            continue
        total_errors += int(file_result.get("errorCount", 0))
        total_warnings += int(file_result.get("warningCount", 0))
        total_fixable += int(file_result.get("fixableErrorCount", 0))
        total_fixable += int(file_result.get("fixableWarningCount", 0))

    return {
        "issues": total_errors + total_warnings,
        "errors": total_errors,
        "warnings": total_warnings,
        "fixable": total_fixable,
    }


# -------------------- Collector --------------------

def collect(folder: Path, with_prov: bool):
    folder = Path(folder)

    data = {
        "schema_version": 1,
        "language": "javascript",

        # Test results (JUnit XML from jest-junit, vitest, mocha, etc.)
        "test_results": junit_counts(folder / "test_results.xml"),

        # ESLint
        "eslint": eslint_summary(folder / "eslint.json"),

        # Schema alignment: these are Python-specific, null for JS/TS
        "pytest": None,
        "coverage": None,
        "ruff": None,
        "mypy": None,
        "radon_cc": None,
        "radon_mi": None,
        "vulture": None,
        "bandit": None,
        "pip_audit": None,
        "pyexamine": None,

        # Schema alignment: C#-specific, null for JS/TS
        "dotnet_test": None,
    }

    if with_prov:
        def safe_strip(p: Path):
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                return ""

        def safe_text(p: Path):
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                return ""

        data["provenance"] = {
            "run_started_utc": safe_strip(folder / "run_started_utc.txt"),
            "node_version": safe_strip(folder / "node_version.txt"),
            "npm_version": safe_strip(folder / "npm_version.txt"),
            "git_sha": safe_strip(folder / "git_sha.txt"),
            "git_branch": safe_strip(folder / "git_branch.txt"),
            "tool_versions": safe_text(folder / "tool_versions.txt"),
            "npm_ls": safe_text(folder / "npm_ls.txt"),
            "src_paths": safe_text(folder / "src_paths.txt").splitlines(),
        }

    return data


# -------------------- CLI --------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python quality_single_summary_jsts.py <METRICS_DIR> <OUT_JSON> [--with-provenance]",
            file=sys.stderr,
        )
        sys.exit(2)

    metrics_dir = Path(sys.argv[1])
    out_json = Path(sys.argv[2])
    with_prov = "--with-provenance" in sys.argv[3:]

    out_json.parent.mkdir(parents=True, exist_ok=True)
    report = collect(metrics_dir, with_prov)
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out_json}")
