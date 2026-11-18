"""S1 evidence-only Schema audit; no generated schema becomes an authority."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from jimu_runtime.domain import definition_enums, definition_support
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions import ontology, resources

output = Path(__file__).parent
schemas = output / "schemas"
schemas.mkdir(exist_ok=True)
index = []
for module in (definition_support, ontology, resources):
    for name, model in vars(module).items():
        if isinstance(model, type) and issubclass(model, ContractModel) and model.__module__ == module.__name__:
            schema = model.model_json_schema()
            Draft202012Validator.check_schema(schema)
            assert schema == model.model_json_schema()
            content = json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            (schemas / (name + ".json")).write_text(content)
            index.append({"name": name, "kind": "model", "status": "TEST_PASSED", "sha256": hashlib.sha256(content.encode()).hexdigest()})
for module, names in ((definition_support, ("ShortText", "Description", "Namespace", "SemanticKey", "FieldPath", "PositiveBudget", "NonNegativeBudget", "UTCDateTime")), (resources, ("AssetDataPath", "MimeType")), (definition_enums, ("FactScope", "ProducerKind", "ModelCapability", "CapabilityFamily", "CapabilityFeature", "ContextSource"))):
    for name in names:
        schema = TypeAdapter(getattr(module, name)).json_schema()
        Draft202012Validator.check_schema(schema)
        content = json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        (schemas / (name + ".json")).write_text(content)
        index.append({"name": name, "kind": "alias_or_enum", "status": "TEST_PASSED", "sha256": hashlib.sha256(content.encode()).hexdigest()})
(output / "schema_validation.json").write_text(json.dumps({"dialect": "https://json-schema.org/draft/2020-12/schema", "status": "TEST_PASSED", "schemas": index, "limitations": ["Generated from authoritative Pydantic models; evidence only", "No vocabulary membership, referenced content digest or Bundle admission verified", "WP-01A numeric representation difference retained"]}, indent=2) + "\n")
print(f"{len(index)} generated schemas checked; model schema repeatability passed")
