"""Load Draft 2020-12 schemas and validate command JSON output."""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_DIR = os.path.join(ROOT, "docs", "orgmode", "reference", "json")


def schema_path(name):
    return os.path.join(SCHEMA_DIR, name)


def load_schema(name):
    with open(schema_path(name), encoding="utf-8") as handle:
        return json.load(handle)


def validate(instance, name):
    import jsonschema

    schema = load_schema(name)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.absolute_path))
    if errors:
        lines = [f"{name}: {error.message}" for error in errors]
        raise AssertionError("\n".join(lines))