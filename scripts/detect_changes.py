#!/usr/bin/env python3
"""
scripts/detect_changes.py

Detects newly added, modified, and renamed Datasphere JSON artifacts in Git commits.
Extracts artifact type and technical name from repository paths.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run_git_command(args):
    """Executes a git command and returns standard output lines."""
    try:
        result = subprocess.run(
            ["git"] + args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.stdout.strip().splitlines()
    except subprocess.CalledProcessError as err:
        print(f"[ERROR] Git command failed: {' '.join(args)}", file=sys.stderr)
        print(f"[ERROR] {err.stderr.strip()}", file=sys.stderr)
        return []


def is_valid_artifact_path(path_str):
    """Checks if path is under artifacts/<type>/.../<name>.json (supports nested folders)."""
    norm = Path(path_str).as_posix()
    parts = norm.split("/")
    return (
        len(parts) >= 3 and
        parts[0] == "artifacts" and
        parts[-1].lower().endswith(".json") and
        not parts[-1].startswith(".")
    )


def extract_artifact_info(path_str, status_char):
    """Parses artifact metadata from path."""
    p = Path(path_str)
    artifact_type = p.parts[1]
    technical_name = extract_technical_name(p)

    status_map = {
        "A": "added",
        "M": "modified",
        "R": "renamed",
        "C": "copied",
    }
    status = status_map.get(status_char, "modified")

    return {
        "file_path": p.as_posix(),
        "artifact_type": artifact_type,
        "technical_name": technical_name,
        "change_type": status
    }


def extract_technical_name(file_path):
    """Reads the primary technical name from definitions, prioritizing entity kind or file stem."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        definitions = data.get("definitions")
        if isinstance(definitions, dict) and definitions:
            stem = Path(file_path).stem
            # 1. Exact match with file stem
            for k in definitions:
                if k.split(".")[-1] == stem:
                    return stem

            # 2. First definition with kind in ('entity', 'analyticModel'), skipping 'context'
            for k, v in definitions.items():
                if isinstance(v, dict) and v.get("kind") in ("entity", "analyticModel"):
                    return k.split(".")[-1]

            return next(iter(definitions)).split(".")[-1]
    except (OSError, json.JSONDecodeError):
        pass
    return Path(file_path).stem


def detect_all_artifacts():
    """Scans all JSON files currently present in artifacts/ (including subfolders)."""
    artifacts = []
    artifacts_dir = Path("artifacts")
    if not artifacts_dir.exists():
        return artifacts

    for file_path in artifacts_dir.rglob("*.json"):
        if file_path.is_file() and not file_path.name.startswith("."):
            norm_parts = file_path.as_posix().split("/")
            if len(norm_parts) >= 3 and norm_parts[0] == "artifacts":
                artifacts.append({
                    "file_path": file_path.as_posix(),
                    "artifact_type": norm_parts[1],
                    "technical_name": extract_technical_name(file_path),
                    "change_type": "existing"
                })
    return artifacts


def detect_git_changes(base_ref=None, head_ref="HEAD"):
    """Detects changed files between two git commits/refs."""
    if not base_ref or base_ref == "0000000000000000000000000000000000000000":
        # First commit or new branch: compare with HEAD~1 or take initial commit
        commit_count = len(run_git_command(["rev-list", "--count", "HEAD"]))
        if commit_count > 1:
            base_ref = "HEAD~1"
        else:
            # Only 1 commit exists in repo: list all files in HEAD
            files = run_git_command(["ls-tree", "-r", "--name-only", "HEAD"])
            artifacts = []
            for f in files:
                if is_valid_artifact_path(f):
                    artifacts.append(extract_artifact_info(f, "A"))
            return artifacts

    diff_lines = run_git_command([
        "diff",
        "--name-status",
        "--find-renames",
        f"{base_ref}..{head_ref}"
    ])

    changed_artifacts = []
    seen = set()

    for line in diff_lines:
        parts = line.split("\t")
        if not parts:
            continue

        status_code = parts[0][0].upper()

        if status_code == "D":
            # Deletions are skipped per POC scope
            print(f"[INFO] Skipping deleted artifact: {parts[-1]}")
            continue

        # For renames (e.g. R100\told\tnew), the target is the last element
        target_path = parts[-1]

        if is_valid_artifact_path(target_path):
            if target_path not in seen:
                seen.add(target_path)
                changed_artifacts.append(extract_artifact_info(target_path, status_code))

    return changed_artifacts


def main():
    parser = argparse.ArgumentParser(description="Detect changed SAP Datasphere JSON artifacts.")
    parser.add_argument("--base", help="Base commit hash or ref")
    parser.add_argument("--head", default="HEAD", help="Head commit hash or ref (default: HEAD)")
    parser.add_argument("--all", action="store_true", help="Scan and include all artifacts (ignores git diff)")
    parser.add_argument("--output", default="changed_artifacts.json", help="Path to write JSON output")
    args = parser.parse_args()

    if args.all:
        print("[INFO] --all flag specified. Scanning all repository artifacts...")
        changed = detect_all_artifacts()
    else:
        print(f"[INFO] Detecting git changes between {args.base or 'HEAD~1'} and {args.head}...")
        changed = detect_git_changes(args.base, args.head)

    print(f"[INFO] Detected {len(changed)} changed artifact(s):")
    for item in changed:
        print(f"  - [{item['change_type'].upper()}] {item['artifact_type']}/{item['technical_name']} ({item['file_path']})")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(changed, f, indent=2)
    print(f"[INFO] Saved changed artifacts list to {args.output}")

    # GitHub Actions step output
    gh_output = os.getenv("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"has_changes={'true' if changed else 'false'}\n")
            f.write(f"count={len(changed)}\n")
            # Compact json string for direct usage in workflow steps if needed
            f.write(f"artifacts_json={json.dumps(changed)}\n")


if __name__ == "__main__":
    main()
