"""Evidence-only O1 schemas; no generated file is an authoritative contract."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions import capabilities, prompts

output = Path(__file__).parent
schema_dir = output / "schemas"
schema_dir.mkdir(exist_ok=True)
index = []
for module in (capabilities, prompts):
    for name, model in vars(module).items():
        if isinstance(model, type) and issubclass(model, ContractModel) and model.__module__ == module.__name__:
            schema = model.model_json_schema()
            Draft202012Validator.check_schema(schema)
            assert schema == model.model_json_schema()
            content = json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            (schema_dir / (name + ".json")).write_text(content)
            index.append({"name": name, "kind": "model", "sha256": hashlib.sha256(content.encode()).hexdigest(), "status": "TEST_PASSED"})
for module, names in ((capabilities, ("FacetRefs", "GroundingInputSource", "GroundingInputBindings", "GroundingBinding")), (prompts, ("PromptVariableName", "TemplateText", "PromptPurpose", "PromptSource"))):
    for name in names:
        schema = TypeAdapter(getattr(module, name)).json_schema()
        Draft202012Validator.check_schema(schema)
        content = json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        (schema_dir / (name + ".json")).write_text(content)
        index.append({"name": name, "kind": "alias", "sha256": hashlib.sha256(content.encode()).hexdigest(), "status": "TEST_PASSED"})
(output / "schema_validation.json").write_text(json.dumps({"status": "TEST_PASSED", "schemas": index, "limitations": ["No finite definition/descriptor/vocabulary/model/policy relation admission", "No partial closure or runtime rendering", "A numeric representation differences retained"]}, indent=2) + "\n")
print(f"{len(index)} O1 generated schemas checked; model repeatability passed")
