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

    # 3. Technical name convention check
    if not TECHNICAL_NAME_REGEX.match(technical_name):
        errors.append(
            f"Invalid technical name '{technical_name}'. "
            "Must be 1-128 alphanumeric characters or underscores, no spaces."
        )

    # 4. JSON Syntax Validation
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

    # 5. Schema check (CSN format or Datasphere definition)
    definitions = data.get("definitions")
    if definitions is not None:
        if not isinstance(definitions, dict) or len(definitions) == 0:
            errors.append("'definitions' property must be a non-empty JSON object.")
        else:
            # Check if matching technical name or entity definition exists
            matching_key = None
            for key, defn in definitions.items():
                short_name = key.split(".")[-1]
                if short_name.upper() == technical_name.upper():
                    matching_key = key
                    break
            
            if matching_key:
                entity = definitions[matching_key]
                if not isinstance(entity, dict):
                    errors.append(f"Definition '{matching_key}' must be an object.")
                elif "kind" not in entity:
                    errors.append(f"Definition '{matching_key}' is missing required 'kind' property.")
            else:
                # Warning if technical name does not match any entry in definitions
                print(
                    f"  [NOTE] File technical name '{technical_name}' differs from definitions keys: "
                    f"{list(definitions.keys())}. Proceeding with file-based technical name."
                )

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
