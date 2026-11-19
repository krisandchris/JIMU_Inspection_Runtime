"""The closed Bundle artifact catalogue and its authoritative artifact roots."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import PolicyKind
from jimu_runtime.domain.definitions.capabilities import (
    FacetDefinition,
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.geometry import GeometryResourceDefinition
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.ontology import EntityDefinition, RegionDefinition
from jimu_runtime.domain.definitions.prompts import PromptTemplateDefinition
from jimu_runtime.domain.definitions.resources import (
    StaticAssetDescriptor,
    VocabularyDefinition,
)
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.policies import PolicyArtifact
from jimu_runtime.domain.producers import ProducerCatalogue
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

type ArtifactIdentity = tuple[ArtifactKind, str, str]
type ArtifactLocation = tuple[str | int, ...]


class BundleArtifactError(ValueError):
    """Stable rejection code and origin, suitable for compiler/admission reporting."""

    def __init__(
        self, code: str, message: str, path: str, json_location: ArtifactLocation = ()
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.json_location = json_location
        super().__init__(f"{code}: {path} {json_location}: {message}")


@dataclass(frozen=True, slots=True)
class ArtifactLayout:
    location: str
    root: type[ContractModel] | TypeAdapter[PolicyArtifact] | None = None
    identity_field: str | None = None

    def matches(self, path: str) -> bool:
        if "*" not in self.location:
            return path == self.location
        prefix, extension = self.location.split("*")
        if not path.startswith(prefix) or not path.endswith(extension):
            return False
        basename = path[len(prefix) : len(path) - len(extension)]
        return bool(basename) and "/" not in basename


_POLICY_ADAPTER = TypeAdapter[PolicyArtifact](PolicyArtifact)

ARTIFACT_LAYOUTS: Mapping[ArtifactKind, ArtifactLayout] = MappingProxyType(
    {
        ArtifactKind.ENTITY: ArtifactLayout(
            "ontology/entities.json", EntityDefinition, "entity_id"
        ),
        ArtifactKind.REGION: ArtifactLayout(
            "ontology/regions.json", RegionDefinition, "region_id"
        ),
        ArtifactKind.PROFILE: ArtifactLayout(
            "capabilities/profiles.json", ProfileDefinition, "profile_id"
        ),
        ArtifactKind.FACET: ArtifactLayout(
            "capabilities/facets.json", FacetDefinition, "facet_id"
        ),
        ArtifactKind.QUESTION: ArtifactLayout(
            "capabilities/questions.json", QuestionDefinition, "question_id"
        ),
        ArtifactKind.TOOL: ArtifactLayout(
            "tools/registry.json", ToolDefinition, "tool_id"
        ),
        ArtifactKind.TOOL_PLAN: ArtifactLayout(
            "tools/plans.json", ToolPlanDefinition, "tool_plan_id"
        ),
        ArtifactKind.RULE: ArtifactLayout(
            "rules/registry.json", RuleDefinition, "rule_id"
        ),
        ArtifactKind.RULE_SET: ArtifactLayout(
            "rules/sets.json", RuleSetDefinition, "rule_set_id"
        ),
        ArtifactKind.MODEL_BINDING: ArtifactLayout(
            "models/bindings.json", ModelBinding, "model_binding_id"
        ),
        ArtifactKind.FACT_SCHEMA: ArtifactLayout(
            "schemas/facts/definitions/*.json", FactSchemaDefinition, "schema_id"
        ),
        ArtifactKind.PROMPT_TEMPLATE: ArtifactLayout(
            "prompts/templates/*.json", PromptTemplateDefinition, "prompt_template_id"
        ),
        ArtifactKind.RESPONSE_SCHEMA: ArtifactLayout(
            "prompts/response_contracts/*.schema.json"
        ),
        ArtifactKind.ATTRIBUTE_SCHEMA: ArtifactLayout(
            "schemas/attributes/*.schema.json"
        ),
        ArtifactKind.CONTEXT_SCHEMA: ArtifactLayout("schemas/context/*.schema.json"),
        ArtifactKind.TOOL_INPUT_SCHEMA: ArtifactLayout(
            "schemas/tools/inputs/*.schema.json"
        ),
        ArtifactKind.TOOL_OUTPUT_SCHEMA: ArtifactLayout(
            "schemas/tools/outputs/*.schema.json"
        ),
        ArtifactKind.OBJECT_SCHEMA: ArtifactLayout("schemas/objects/*.schema.json"),
        ArtifactKind.DECISION_SCHEMA: ArtifactLayout("schemas/decisions/*.schema.json"),
        ArtifactKind.VALUE_SCHEMA: ArtifactLayout("schemas/values/*.schema.json"),
        ArtifactKind.POLICY: ArtifactLayout(
            "policies/*.json", _POLICY_ADAPTER, "policy_id"
        ),
        ArtifactKind.VOCABULARY: ArtifactLayout(
            "vocabularies/*.json", VocabularyDefinition, "vocabulary_id"
        ),
        ArtifactKind.GEOMETRY_RESOURCE: ArtifactLayout(
            "resources/geometry/*.json",
            GeometryResourceDefinition,
            "geometry_resource_id",
        ),
        ArtifactKind.STATIC_ASSET: ArtifactLayout(
            "resources/assets/*", StaticAssetDescriptor, "asset_id"
        ),
        ArtifactKind.FACT_PRODUCER_CATALOG: ArtifactLayout(
            "facts/producers.json", ProducerCatalogue, "catalogue_id"
        ),
    }
)

_DEDICATED_POLICIES = {
    "policies/execution.json": PolicyKind.EXECUTION,
    "policies/evidence.json": PolicyKind.EVIDENCE,
    "policies/coverage.json": PolicyKind.COVERAGE,
    "policies/resources.json": PolicyKind.RESOURCE_REQUIREMENTS,
}
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_INVALID_URI = re.compile(r"[^\x21-\x7e]|[<>\x22{}|\\^\x60]|%(?![0-9A-Fa-f]{2})")
_VALUE_SCHEMA_ID = re.compile(r"urn:jimu:value-schema:([^:]+):([0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class SchemaResource:
    """Draft 2020-12 syntax validation, with the original pinned bytes.

    The root URI is an offline alias. The inventory pins authority through the
    ContentRef and locator. Only the value-schema generator fixes an alias format.
    """

    resource_id: str
    canonical_bytes: bytes

    @property
    def document(self) -> dict[str, JSONValue]:
        """Return an isolated copy of the already validated document."""
        return cast(dict[str, JSONValue], json.loads(self.canonical_bytes))


def valid_uri_reference(value: str) -> bool:
    if _INVALID_URI.search(value):
        return False
    try:
        split = urlsplit(value)
        return split.scheme not in {"http", "https"} or bool(split.hostname)
    except ValueError:
        return False


def absolute_schema_id(value: str) -> bool:
    return (
        bool(_URI_SCHEME.match(value))
        and valid_uri_reference(value)
        and "#" not in value
    )


def artifact_identity(ref: ContentRef) -> ArtifactIdentity:
    return ref.artifact_kind, ref.artifact_id, ref.version


def validate_artifact_root(
    ref: ContentRef,
    path: str,
    value: JSONValue,
    canonical_bytes: bytes,
) -> ContractModel | SchemaResource:
    """Apply the approved kind/path/model mapping to located content."""
    layout = ARTIFACT_LAYOUTS[ref.artifact_kind]
    if not layout.matches(path):
        raise BundleArtifactError(
            "artifact_path_mismatch",
            f"{ref.artifact_kind.value} requires {layout.location}",
            path,
        )
    if layout.root is None:
        return _validate_schema(ref, path, value, canonical_bytes)

    root: ContractModel
    try:
        if isinstance(layout.root, TypeAdapter):
            policy = layout.root.validate_json(canonical_bytes)
            root = policy
            expected = _DEDICATED_POLICIES.get(path)
            if expected is not None and policy.policy.policy_kind != expected:
                raise BundleArtifactError(
                    "policy_path_mismatch",
                    f"this path requires a {expected.value} policy",
                    path,
                )
        else:
            root = layout.root.model_validate_json(canonical_bytes)
    except ValidationError as error:
        issue = error.errors(include_input=False, include_url=False)[0]
        raise BundleArtifactError(
            "artifact_schema_invalid", issue["msg"], path, issue["loc"]
        ) from error
    if layout.identity_field is None or (
        getattr(root, layout.identity_field) != ref.artifact_id
        or not isinstance(value, dict)
        or value.get("version") != ref.version
    ):
        raise BundleArtifactError(
            "artifact_identity_mismatch",
            "root ID and version differ from inventory",
            path,
        )
    return root


def _validate_schema(
    ref: ContentRef, path: str, value: JSONValue, canonical_bytes: bytes
) -> SchemaResource:
    if not isinstance(value, dict) or value.get("$schema") != DRAFT_2020_12:
        raise BundleArtifactError(
            "schema_dialect_mismatch",
            "schema root requires an explicit Draft 2020-12 dialect",
            path,
        )
    resource_id = value.get("$id")
    if not isinstance(resource_id, str) or not absolute_schema_id(resource_id):
        raise BundleArtifactError(
            "schema_id_invalid",
            "schema root requires an absolute, fragment-free URI",
            path,
            ("$id",),
        )
    if ref.artifact_kind is ArtifactKind.VALUE_SCHEMA:
        match = _VALUE_SCHEMA_ID.fullmatch(resource_id)
        if (
            match is None
            or match[1] != ref.version
            or f"value-schema.{match[2]}" != ref.artifact_id
        ):
            raise BundleArtifactError(
                "schema_identity_mismatch",
                "value-schema identity differs from its generated ID",
                path,
                ("$id",),
            )
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as error:
        raise BundleArtifactError(
            "schema_draft_invalid",
            "document contains a malformed Draft 2020-12 keyword",
            path,
            tuple(error.path),
        ) from error
    return SchemaResource(resource_id, canonical_bytes)
