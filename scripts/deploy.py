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


def referenced_object_names(value):
    """Returns technical names found in CSN reference arrays."""
    references = set()
    if isinstance(value, dict):
        ref = value.get("ref")
        if isinstance(ref, list) and ref and all(isinstance(part, str) for part in ref):
            references.add(ref[0].split(".")[-1])
        for child in value.values():
            references.update(referenced_object_names(child))
    elif isinstance(value, list):
        for child in value:
            references.update(referenced_object_names(child))
    return references


def order_artifacts(artifacts, mapping):
    """Orders artifacts after their referenced artifacts, with configured priority as fallback."""
    by_name = {item["technical_name"]: item for item in artifacts}
    dependencies = {item["technical_name"]: set() for item in artifacts}

    for artifact in artifacts:
        data = load_json(artifact["file_path"])
        references = referenced_object_names(data)
        dependencies[artifact["technical_name"]] = {
            name for name in references
            if name in by_name and name != artifact["technical_name"]
        }

    ordered = []
    remaining = set(by_name)
    while remaining:
        ready = [
            name for name in remaining
            if not (dependencies[name] & remaining)
        ]
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ValueError(f"Circular artifact dependency detected: {cycle}")

        ready.sort(
            key=lambda name: (
                mapping.get(by_name[name].get("artifact_type", ""), {})
                .get("deployment_priority", 99),
                name,
            )
        )
        ordered.extend(by_name[name] for name in ready)
        remaining.difference_update(ready)

    return ordered


NUMERIC_CDS_TYPES = {
    "cds.decimal",
    "cds.integer",
    "cds.integer64",
    "cds.double",
    "cds.float",
    "cds.decimalfloat"
}


def build_analytic_model_csn(fact_name, fact_entity, model_prefix="AM_"):
    """Dynamically builds an Analytic Model CSN definition from a Fact View entity."""
    am_name = f"{model_prefix}{fact_name}"
    fact_label = fact_entity.get("@EndUserText.label", fact_name)
    am_label = f"Analytic Model for {fact_label}"

    elements = {}
    fact_elements = fact_entity.get("elements", {})

    for col_name, col_def in fact_elements.items():
        col_type = col_def.get("type", "").lower()
        is_measure = (
            col_type in NUMERIC_CDS_TYPES or
            col_def.get("@Analytics.measure") is True or
            col_def.get("@Aggregation.default") is not None
        )

        new_element = {}
        for prop in ["type", "length", "precision", "scale"]:
            if prop in col_def:
                new_element[prop] = col_def[prop]

        if is_measure and not col_def.get("key", False):
            new_element["semantics"] = {
                "measure": True,
                "aggregation": "sum"
            }
        else:
            new_element["semantics"] = {
                "dimension": True
            }

        elements[col_name] = new_element

    return {
        "$schema": "https://sap.com/datasphere/csn/v1.0",
        "definitions": {
            am_name: {
                "kind": "analyticModel",
                "@EndUserText.label": am_label,
                "sources": {
                    fact_name: {
                        "entity": {
                            "ref": [fact_name]
                        }
                    }
                },
                "elements": elements
            }
        }
    }


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
            display_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=True
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

    # Auto-ensure Fact views are exposed for consumption prior to deployment
    if artifact_type == "views":
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                v_data = json.load(f)
            v_defs = v_data.get("definitions", {})
            v_entity = v_defs.get(technical_name)
            if not v_entity:
                for k, v in v_defs.items():
                    if k.split(".")[-1] == technical_name:
                        v_entity = v
                        technical_name_key = k
                        break
            else:
                technical_name_key = technical_name

            if v_entity:
                is_fact = (
                    v_entity.get("@Analytics.dataCategory", "").upper() == "FACT" or
                    v_entity.get("semanticUsage", "").upper() == "FACT" or
                    "FACT" in technical_name.upper()
                )
                if is_fact:
                    modified = False
                    if v_entity.get("@DataWarehouse.consumption.external") is not True:
                        v_entity["@DataWarehouse.consumption.external"] = True
                        modified = True
                    if v_entity.get("@Analytics.dataCategory", "").upper() != "FACT":
                        v_entity["@Analytics.dataCategory"] = "FACT"
                        modified = True

                    if modified:
                        v_defs[technical_name_key] = v_entity
                        with open(file_path, "w", encoding="utf-8") as f:
                            json.dump(v_data, f, indent=2)
                            f.write("\n")
                        print(f"[INFO] Auto-ensured '{technical_name}' has '@DataWarehouse.consumption.external: true' and '@Analytics.dataCategory: FACT'")
        except Exception as f_err:
            print(f"[WARN] Could not verify Fact consumption annotations: {f_err}")

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
        "--file-path", f'"{file_path}"'
    ]

    cmd.append("--verbose")

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

    try:
        artifacts = order_artifacts(artifacts, mapping)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as err:
        print(f"[ERROR] Could not determine artifact deployment order: {err}", file=sys.stderr)
        sys.exit(1)

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

        # APPROACH B HOOK: Auto-create Analytic Model for successfully deployed Fact Views
        auto_am = config.get("settings", {}).get("auto_create_analytic_model_for_facts", False)
        if auto_am and artifact.get("artifact_type") == "views":
            try:
                fact_data = load_json(artifact["file_path"])
                entity_name = artifact["technical_name"]
                definitions = fact_data.get("definitions", {})
                entity_def = definitions.get(entity_name, {})
                if not entity_def:
                    for k, v in definitions.items():
                        if k.split(".")[-1] == entity_name:
                            entity_def = v
                            break

                is_fact = (
                    entity_def.get("@Analytics.dataCategory", "").upper() == "FACT" or
                    entity_def.get("semanticUsage", "").upper() == "FACT" or
                    "FACT" in entity_name.upper()
                )

                if is_fact:
                    am_prefix = config.get("settings", {}).get("analytic_model_prefix", "AM_")
                    am_name = f"{am_prefix}{entity_name}"

                    # Skip hook if an explicit analytic model file is already in the queue
                    already_in_queue = any(
                        a.get("artifact_type") == "analytic-models" and a.get("technical_name") == am_name
                        for a in artifacts
                    )
                    if already_in_queue:
                        print(f"\n[HOOK] Analytic Model '{am_name}' already exists in deployment queue. Skipping hook.")
                    else:
                        print(f"\n[HOOK] Fact view '{entity_name}' deployed successfully.")
                        print(f"[HOOK] Building and deploying Analytic Model '{am_name}' into space '{space_id}'...")

                        am_payload = build_analytic_model_csn(entity_name, entity_def, am_prefix)
                        temp_am_path = Path(f".tmp_{am_name}.json")
                        with open(temp_am_path, "w", encoding="utf-8") as tmp_f:
                            json.dump(am_payload, tmp_f, indent=2)

                        try:
                            am_artifact = {
                                "artifact_type": "analytic-models",
                                "technical_name": am_name,
                                "file_path": str(temp_am_path.resolve())
                            }
                            am_res = deploy_artifact(
                                cli_base=args.cli_bin,
                                artifact=am_artifact,
                                space_id=space_id,
                                auto_deploy=config.get("settings", {}).get("auto_deploy", True),
                                dry_run=args.dry_run
                            )
                            results.append(am_res)
                            if not am_res["success"]:
                                failed_count += 1
                                print(f"[ERROR] Auto-deployment of Analytic Model '{am_name}' failed!", file=sys.stderr)
                                break
                        finally:
                            if temp_am_path.exists():
                                temp_am_path.unlink()
            except Exception as hook_err:
                print(f"[WARN] Failed to execute Analytic Model hook for {artifact['technical_name']}: {hook_err}", file=sys.stderr)

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
