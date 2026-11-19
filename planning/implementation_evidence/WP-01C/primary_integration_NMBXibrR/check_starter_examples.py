"""Run from the WP-01C worktree root against the unmodified starter examples."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest
from jimu_runtime.domain.runtime.decisions import DecisionRecord
from jimu_runtime.domain.runtime.intake import TaskSubmission

folder = Path("examples/nonmotor_vehicle_illegal_parking")
results = {}
for filename, root, schema in (
    ("task-submission.json", TaskSubmission, "runtime.intake.TaskSubmission"),
    ("decision-result.json", DecisionRecord, "runtime.decisions.DecisionRecord"),
):
    payload = (folder / filename).read_bytes()
    contract = Path("schemas/generated") / f"{schema}.schema.json"
    shape_errors = list(Draft202012Validator(json.loads(contract.read_bytes())).iter_errors(json.loads(payload)))
    assert shape_errors
    try:
        root.model_validate_json(payload)
    except ValidationError as exc:
        model_errors = exc.errors()
    else:
        raise AssertionError(f"accepted illustrative input: {filename}")
    assert any(i["loc"] == ("schema_version",) and i["type"] == "missing" for i in model_errors)
    results[filename] = {"input_sha256": hashlib.sha256(payload).hexdigest(), "schema_errors": len(shape_errors), "model_errors": len(model_errors), "missing_version": True}

fragment = (folder / "bundle-fragment.yaml").read_bytes()
try:
    BundleManifest.model_validate_json(fragment)
except ValidationError as exc:
    assert exc.errors()[0]["type"] == "json_invalid"
else:
    raise AssertionError("accepted illustrative YAML as persisted manifest JSON")
results["bundle-fragment.yaml"] = {"input_sha256": hashlib.sha256(fragment).hexdigest(), "manifest_json": "invalid_json"}
print(json.dumps(results, indent=2))
