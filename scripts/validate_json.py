#!/usr/bin/env python3
"""
scripts/validate_json.py

Validates SAP Datasphere JSON artifacts prior to deployment:
- Validates JSON syntax.
- Validates supported artifact directory / type against configuration.
- Checks technical naming conventions (alphanumeric and underscores).
- Validates basic CSN schema structure (definitions, kind, elements).
"""

import argparse
import json
import re
import sys
from pathlib import Path

TECHNICAL_NAME_REGEX = re.compile(r"^[A-Za-z0-9_]{1,128}$")


def is_fact_entity(entity_def):
    """
    Checks if an entity definition in CSN JSON represents a Fact layer.
    Prioritizes '@ObjectModel.modelingPattern': {'#': 'ANALYTICAL_FACT'} directly inside the file,
    along with other standard CSN modeling annotations.
    """
    if not isinstance(entity_def, dict):
        return False

    # 1. Primary check: @ObjectModel.modelingPattern == ANALYTICAL_FACT
    modeling_pattern = entity_def.get("@ObjectModel.modelingPattern")
    if isinstance(modeling_pattern, dict):
        val = str(modeling_pattern.get("#", "")).upper()
        if "ANALYTICAL_FACT" in val or "FACT" in val:
            return True
    elif isinstance(modeling_pattern, str):
        val = modeling_pattern.upper()
        if "ANALYTICAL_FACT" in val or "FACT" in val:
            return True

    # 2. Check @ObjectModel.dataCategory
    data_cat = entity_def.get("@ObjectModel.dataCategory")
    if isinstance(data_cat, dict):
        val = str(data_cat.get("#", "")).upper()
        if "FACT" in val:
            return True
    elif isinstance(data_cat, str) and "FACT" in data_cat.upper():
        return True

    # 3. Check @Analytics.dataCategory
    analytics_cat = entity_def.get("@Analytics.dataCategory")
    if isinstance(analytics_cat, dict):
        val = str(analytics_cat.get("#", "")).upper()
        if "FACT" in val:
            return True
    elif isinstance(analytics_cat, str) and "FACT" in analytics_cat.upper():
        return True

    # 4. Check semanticUsage
    usage = entity_def.get("semanticUsage")
    if isinstance(usage, str) and "FACT" in usage.upper():
        return True

    return False


def load_deployment_config(config_path="config/deployment.json"):
    """Loads deployment configuration containing supported artifact types."""
    cfg_file = Path(config_path)
    if not cfg_file.exists():
        print(f"[WARN] Config file {config_path} not found. Using defaults.")
        return {
            "artifact_mapping": {
                "local-tables": {"cli_command": "objects local-tables"},
                "remote-tables": {"cli_command": "objects remote-tables"},
                "views": {"cli_command": "objects views"},
                "analytic-models": {"cli_command": "objects analytic-models"}
            }
        }
    with open(cfg_file, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_artifact(artifact_entry, supported_types):
    """Validates a single artifact file. Returns (bool, list_of_errors)."""
    errors = []
    file_path = Path(artifact_entry["file_path"])
    artifact_type = artifact_entry["artifact_type"]
    technical_name = artifact_entry["technical_name"]

    # 1. Existence check
    if not file_path.exists():
        return False, [f"File does not exist: {file_path}"]

    # 2. Supported artifact type check
    if artifact_type not in supported_types:
        errors.append(
            f"Unsupported artifact directory '{artifact_type}'. "
            f"Allowed types: {list(supported_types.keys())}"
        )

    # 3. JSON Syntax Validation
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as err:
        errors.append(f"Invalid JSON syntax at line {err.lineno}, column {err.colno}: {err.msg}")
        return False, errors
    except Exception as err:
        errors.append(f"Failed to read file: {str(err)}")
        return False, errors

    if not isinstance(data, dict):
        errors.append("Root JSON element must be an object (dictionary).")
        return False, errors

    # 4. Schema check (CSN format or Datasphere definition)
    definitions = data.get("definitions")
    if definitions is not None:
        if not isinstance(definitions, dict) or len(definitions) == 0:
            errors.append("'definitions' property must be a non-empty JSON object.")
        else:
            bld = data.get("businessLayerDefinitions")
            target_key = None
            if isinstance(bld, dict) and bld:
                bld_key = next(iter(bld))
                if bld_key in definitions:
                    target_key = bld_key
                else:
                    for k in definitions:
                        if k.split(".")[-1] == bld_key.split(".")[-1]:
                            target_key = k
                            break

            if not target_key:
                stem = file_path.stem
                for k in definitions:
                    if k.split(".")[-1] == stem:
                        target_key = k
                        break
            if not target_key:
                for k, v in definitions.items():
                    if isinstance(v, dict) and v.get("kind") in ("entity", "analyticModel"):
                        target_key = k
                        break
            if not target_key:
                target_key = next(iter(definitions))

            technical_name = target_key.split(".")[-1]
            artifact_entry["technical_name"] = technical_name

            if not TECHNICAL_NAME_REGEX.match(technical_name):
                errors.append(
                    f"Invalid technical name '{technical_name}'. "
                    "Must be 1-128 alphanumeric characters or underscores, no spaces."
                )

            entity = definitions[target_key]
            if not isinstance(entity, dict):
                errors.append(f"Definition '{target_key}' must be an object.")
            elif "kind" not in entity:
                errors.append(
                    f"Definition '{target_key}' is missing required 'kind' property."
                )
            else:
                # 5. Fact layer auto-remediation (Detected via @ObjectModel.modelingPattern in file content)
                if artifact_type == "views":
                    if is_fact_entity(entity):
                        modified = False
                        if entity.get("@DataWarehouse.consumption.external") is not True:
                            entity["@DataWarehouse.consumption.external"] = True
                            modified = True

                        # Ensure modeling pattern is explicitly defined
                        if not entity.get("@ObjectModel.modelingPattern"):
                            entity["@ObjectModel.modelingPattern"] = {"#": "ANALYTICAL_FACT"}
                            modified = True

                        if modified:
                            try:
                                with open(file_path, "w", encoding="utf-8") as f:
                                    json.dump(data, f, indent=2)
                                    f.write("\n")
                                print(f"  [AUTO-FIX] Set '@DataWarehouse.consumption.external: true' (Exposed for Consumption) for Fact in {file_path}")
                            except Exception as write_err:
                                errors.append(f"Failed to auto-update Fact annotations in {file_path}: {write_err}")
    else:
        errors.append("'definitions' property is required to derive the technical name.")

    return len(errors) == 0, errors


def main():
    parser = argparse.ArgumentParser(description="Validate Datasphere JSON artifacts.")
    parser.add_argument(
        "--input",
        default="changed_artifacts.json",
        help="Path to JSON file containing list of changed artifacts to validate"
    )
    parser.add_argument(
        "--config",
        default="config/deployment.json",
        help="Path to deployment configuration file"
    )
    args = parser.parse_args()

    input_file = Path(args.input)
    if not input_file.exists():
        print(f"[INFO] Input file {args.input} does not exist. Nothing to validate.")
        sys.exit(0)

    with open(input_file, "r", encoding="utf-8") as f:
        artifacts = json.load(f)

    if not artifacts:
        print("[INFO] No artifacts found in input list to validate.")
        sys.exit(0)

    config = load_deployment_config(args.config)
    supported_types = config.get("artifact_mapping", {})

    print(f"[INFO] Validating {len(artifacts)} artifact(s)...")
    total_errors = 0

    for item in artifacts:
        valid, errors = validate_artifact(item, supported_types)
        if valid:
            print(f"  [VALID] {item['file_path']} ({item['artifact_type']}: {item['technical_name']})")
        else:
            total_errors += len(errors)
            print(f"  [INVALID] {item['file_path']}:")
            for err in errors:
                print(f"    - {err}")

    if total_errors > 0:
        print(f"\n[ERROR] Validation failed with {total_errors} error(s). Halting pipeline.")
        sys.exit(1)

    print("\n[SUCCESS] All artifacts passed validation.")
    sys.exit(0)


if __name__ == "__main__":
    main()
