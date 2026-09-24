"""Shared helpers for schema tests. jsonschema is a dev-only dependency."""

import json
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError:  # pragma: no cover - exercised only without dev dependencies
    Draft202012Validator = None

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

HAVE_JSONSCHEMA = Draft202012Validator is not None


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _registry():
    # Schemas reference each other by relative file name; resolve them by $id, never over the network.
    registry = Registry()
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        schema = load_json(path)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def validator(schema_name: str):
    schema = load_json(SCHEMAS / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=_registry())


def errors(validator_obj, instance) -> list[str]:
    return sorted(e.message for e in validator_obj.iter_errors(instance))
