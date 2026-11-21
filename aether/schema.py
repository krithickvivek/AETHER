"""Persistence schema versioning and tolerant, validated loading.

Every persisted record carries a `schema_version`. Records saved before versioning existed have
none and are treated as version 0 (the "legacy" layout). Loading always goes through `upgrade()`,
which walks a record from its version to SCHEMA_VERSION one registered migration at a time, so the
same migrations can later be reused when runs move into SQLite.

Rules for loading, applied by every loader in AETHER:
  * A record from a NEWER schema version is refused (SchemaError): loading and re-saving it
    could silently drop data this code does not understand.
  * Missing optional fields get safe defaults.
  * Missing or wrongly typed essential fields raise SchemaError naming the field and where it is.
  * Unknown fields are never an error. They are reported in a warnings list, and the top-level
    run record keeps them and writes them back when it is saved again.

The version number is stored only in the saved data. It is not a field of the in-memory objects,
so it never takes part in equality or in what agents may update.

To change the stored format: raise SCHEMA_VERSION, add a migration for each kind in MIGRATIONS
(keyed by (kind, from_version)), and add a test with a sample record of the old version.
"""

from dataclasses import MISSING, fields
from typing import Callable

from aether.models import Plan, PlanStep, TaskBrief

SCHEMA_VERSION = 1   # the version this code writes
LEGACY_VERSION = 0   # records saved before schema versioning existed

# The kinds of persisted record. All kinds share one version number.
KIND_CONTEXT = "context"              # a run's context.json
KIND_RUN_RESULT = "run_result"        # a RunResult (the UI's view of a run)
KIND_HISTORY_ENTRY = "history_entry"  # one entry of .aether/history.json
KINDS = (KIND_CONTEXT, KIND_RUN_RESULT, KIND_HISTORY_ENTRY)

VERSION_KEY = "schema_version"


class SchemaError(ValueError):
    """A saved record cannot be loaded: wrong shape, missing essential data, or an unknown version.

    A ValueError, so existing code that already handles ValueError when loading keeps working."""


def _v0_to_v1(data: dict) -> dict:
    """Version 1 is the legacy layout plus the explicit schema_version marker: no field changes."""
    return dict(data)


# (kind, from_version) -> function returning the record at from_version + 1. Migrations must not
# modify their input.
MIGRATIONS: dict[tuple[str, int], Callable[[dict], dict]] = {
    (kind, LEGACY_VERSION): _v0_to_v1 for kind in KINDS
}


def read_version(data: dict, where: str = "Record") -> int:
    """The schema version of a record: LEGACY_VERSION if it has none."""
    version = data.get(VERSION_KEY, LEGACY_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise SchemaError(f"{where} has an invalid {VERSION_KEY} ({version!r}); it must be a whole number "
                          f"from {LEGACY_VERSION} to {SCHEMA_VERSION}.")
    return version


def upgrade(data, kind: str, where: str = "Record") -> dict:
    """A copy of the record, migrated to SCHEMA_VERSION. The input is not modified."""
    if kind not in KINDS:
        raise ValueError(f"Unknown record kind {kind!r}.")
    if not isinstance(data, dict):
        raise SchemaError(f"{where} must be an object, not {type(data).__name__}.")
    version = read_version(data, where)
    if version > SCHEMA_VERSION:
        raise SchemaError(f"{where} uses schema version {version}, but this copy of AETHER only understands "
                          f"versions up to {SCHEMA_VERSION}. It was probably saved by a newer AETHER; update AETHER "
                          "to open it. It has not been changed.")
    record = dict(data)
    while version < SCHEMA_VERSION:
        step = MIGRATIONS.get((kind, version))
        if step is None:
            raise SchemaError(f"{where}: there is no migration from schema version {version} for {kind} records.")
        record = step(record)
        version += 1
    record[VERSION_KEY] = SCHEMA_VERSION
    return record


# ---------------------------------------------------------------- validated building blocks

def check_type(value, expected: tuple, where: str, name: str) -> None:
    """Raise SchemaError unless the value is one of the expected types (a bool is not a number)."""
    if isinstance(value, bool) and bool not in expected:
        raise SchemaError(f"{where}: {name!r} must be {_describe(expected)}, not true/false.")
    if not isinstance(value, expected):
        raise SchemaError(f"{where}: {name!r} must be {_describe(expected)}, not {type(value).__name__}.")


def _describe(expected: tuple) -> str:
    names = {str: "text", int: "a whole number", float: "a number", list: "a list", dict: "an object",
             type(None): "null", bool: "true/false"}
    parts = [names.get(t, t.__name__) for t in expected]
    return " or ".join(parts)


def check_items(values: list, expected: tuple, where: str, name: str) -> None:
    for index, item in enumerate(values):
        check_type(item, expected, f"{where}.{name}[{index}]", "value")


def build(cls, data, where: str, types: dict, warnings: list[str]):
    """Create a dataclass from a dict, validating it.

    Fields without a default are required. `types` maps field names to the accepted types. Unknown
    keys are ignored but reported in `warnings`."""
    if not isinstance(data, dict):
        raise SchemaError(f"{where} must be an object, not {type(data).__name__}.")
    specs = fields(cls)
    names = {spec.name for spec in specs}
    missing = [spec.name for spec in specs
               if spec.default is MISSING and spec.default_factory is MISSING and spec.name not in data]
    if missing:
        raise SchemaError(f"{where} is missing required field(s): {', '.join(missing)}.")
    for name in sorted(set(data) - names - {VERSION_KEY}):
        warnings.append(f"{where}: ignored unknown field {name!r}.")
    values = {}
    for name in names & data.keys():
        if name in types:
            check_type(data[name], types[name], where, name)
        values[name] = data[name]
    return cls(**values)


def brief_from_dict(data, where: str, warnings: list[str]) -> TaskBrief:
    brief = build(TaskBrief, data, where, {"requirement": (str,), "features": (list,)}, warnings)
    check_items(brief.features, (str,), where, "features")
    return brief


def plan_from_dict(data, where: str, warnings: list[str]) -> Plan:
    plan = build(Plan, data, where, {"requirement": (str,), "planner_name": (str,), "steps": (list,)}, warnings)
    plan.steps = [build(PlanStep, step, f"{where}.steps[{index}]", {"phase": (str,), "description": (str,)}, warnings)
                  for index, step in enumerate(plan.steps)]
    return plan
