#!/usr/bin/env python3
"""
scripts/deploy.py

Orchestrates the deployment of SAP Datasphere artifacts using the official SAP Datasphere CLI.
Workflow:
1. Reads the list of changed artifacts (or all artifacts).
2. Orders artifacts according to dependency hierarchy (tables -> views -> models).
3. For each artifact:
   - Queries SAP Datasphere to check whether the object already exists in the target space.
   - If it exists, executes `datasphere objects <type> update ...`.
   - If it does not exist, executes `datasphere objects <type> create ...`.
4. Captures output, errors, and reports detailed status.
5. Supports dry-run simulation mode for CI testing.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def load_json(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_cli_command(command_parts, dry_run=False, mask_secrets=True):
    """Executes a command using subprocess, returns (exit_code, stdout, stderr)."""
    display_cmd = " ".join(command_parts)
    print(f"\n[CLI EXEC] {display_cmd}")

    if dry_run:
        print("[DRY-RUN] Command execution skipped (dry-run mode).")
        return 0, "[DRY-RUN] Execution succeeded (simulated)", ""

    try:
        start_time = time.time()
        result = subprocess.run(
            command_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True  # Required for npx/datasphere on Windows runners
        )
        duration = round(time.time() - start_time, 2)
        print(f"[CLI FINISHED in {duration}s] Exit code: {result.returncode}")

        if result.stdout.strip():
            print(f"[STDOUT]\n{result.stdout.strip()}")
        if result.stderr.strip():
            print(f"[STDERR]\n{result.stderr.strip()}", file=sys.stderr)

        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as err:
        print(f"[EXCEPTION] Failed to run command: {err}", file=sys.stderr)
        return 1, "", str(err)


def check_object_exists(cli_base, artifact_type, space_id, technical_name, dry_run=False):
    """
    Checks if an object exists in the target space using `read`.
    Command syntax:
      datasphere objects <type> read --space <space_id> --technical-name <name>
    """
    if dry_run:
        print(f"[DRY-RUN] Checking existence for {artifact_type} '{technical_name}' (simulated: not found).")
        return False

    cmd = [
        cli_base,
        "objects",
        artifact_type,
        "read",
        "--space", space_id,
        "--technical-name", technical_name
    ]

    ret, stdout, stderr = run_cli_command(cmd)
    if ret == 0:
        # Successfully read the object; it exists
        return True

    # Check common 404 / Not Found message indicators in output
    not_found_indicators = ["not found", "404", "does not exist", "cannot find", "unknown"]
    combined_output = (stdout + " " + stderr).lower()
    if any(ind in combined_output for ind in not_found_indicators) or ret != 0:
        return False

    return False


def deploy_artifact(cli_base, artifact, space_id, auto_deploy=True, dry_run=False):
    """
    Performs create or update on the given artifact.
    """
    artifact_type = artifact["artifact_type"]
    technical_name = artifact["technical_name"]
    file_path = str(Path(artifact["file_path"]).resolve())

    print(f"\n{'='*70}")
    print(f"Deploying [{artifact_type.upper()}] {technical_name}")
    print(f"Source file: {file_path}")
    print(f"Target space: {space_id}")
    print(f"{'='*70}")

    # Check if object already exists
    exists = check_object_exists(cli_base, artifact_type, space_id, technical_name, dry_run)
    action = "update" if exists else "create"
    print(f"[INFO] Object '{technical_name}' {'already exists -> UPDATING' if exists else 'does not exist -> CREATING'}")

    cmd = [
        cli_base,
        "objects",
        artifact_type,
        action,
        "--space", space_id,
        "--technical-name", technical_name,
        "--file-path", f'"{file_path}"'
    ]

    if not auto_deploy:
        cmd.append("--no-deploy")

    ret, stdout, stderr = run_cli_command(cmd, dry_run=dry_run)

    return {
        "technical_name": technical_name,
        "artifact_type": artifact_type,
        "action": action,
        "file_path": file_path,
        "success": (ret == 0),
        "exit_code": ret,
        "stdout": stdout,
        "stderr": stderr
    }


def main():
    parser = argparse.ArgumentParser(description="Deploy SAP Datasphere artifacts via CLI.")
    parser.add_argument(
        "--input",
        default="changed_artifacts.json",
        help="Path to JSON file containing changed artifacts"
    )
    parser.add_argument(
        "--config",
        default="config/deployment.json",
        help="Path to deployment configuration"
    )
    parser.add_argument(
        "--space",
        default=os.getenv("DSP_SPACE_ID"),
        help="Target SAP Datasphere Space ID (or set DSP_SPACE_ID env var)"
    )
    parser.add_argument(
        "--cli-bin",
        default="npx datasphere",
        help="CLI executable command (default: 'npx datasphere')"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes"),
        help="Simulate deployment without calling live Datasphere APIs"
    )
    args = parser.parse_args()

    # Load configuration
    config = load_deployment_config = load_json(args.config) if Path(args.config).exists() else {}
    mapping = config.get("artifact_mapping", {})
    space_id = args.space or config.get("settings", {}).get("default_space")

    if not space_id:
        print("[ERROR] Space ID is required! Pass --space or set DSP_SPACE_ID environment variable.", file=sys.stderr)
        sys.exit(1)

    # Load artifacts list
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[INFO] Changed artifacts file '{args.input}' not found. Nothing to deploy.")
        sys.exit(0)

    artifacts = load_json(args.input)
    if not artifacts:
        print("[INFO] No artifacts to deploy in changed list. Finished successfully.")
        sys.exit(0)

    # Sort artifacts according to priority (tables -> views -> models)
    def get_priority(item):
        t = item.get("artifact_type", "")
        return mapping.get(t, {}).get("deployment_priority", 99)

    artifacts.sort(key=get_priority)

    print(f"[INFO] Deployment starting for {len(artifacts)} artifact(s) into space '{space_id}'...")
    if args.dry_run:
        print("[INFO] *** DRY-RUN MODE ACTIVE: No actual modifications will be made to Datasphere ***")

    results = []
    failed_count = 0

    for artifact in artifacts:
        res = deploy_artifact(
            cli_base=args.cli_bin,
            artifact=artifact,
            space_id=space_id,
            auto_deploy=config.get("settings", {}).get("auto_deploy", True),
            dry_run=args.dry_run
        )
        results.append(res)
        if not res["success"]:
            failed_count += 1
            print(f"[ERROR] Deployment failed for {artifact['artifact_type']}/{artifact['technical_name']}!", file=sys.stderr)
            # Break on first error to prevent broken dependencies cascade
            break

    # Summary
    print("\n" + "=" * 70)
    print("DEPLOYMENT EXECUTION SUMMARY")
    print("=" * 70)
    for r in results:
        status_label = "SUCCESS" if r["success"] else "FAILED"
        print(f"[{status_label}] {r['artifact_type'].upper()} : {r['technical_name']} ({r['action'].upper()})")

    if failed_count > 0:
        print(f"\n[FAILURE] Deployment completed with {failed_count} failure(s). Check logs above.")
        sys.exit(1)

    print(f"\n[SUCCESS] Successfully deployed all {len(results)} artifact(s).")
    sys.exit(0)


if __name__ == "__main__":
    main()

