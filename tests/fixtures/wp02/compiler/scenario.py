"""Synthetic non-motor vehicle scenario. No live model, tool or business data."""

import json
from dataclasses import replace
from pathlib import Path

from pydantic import TypeAdapter

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.artifacts import DRAFT_2020_12
from jimu_runtime.bundle.capabilities import (
    CapabilityRegistration,
    CapabilitySnapshot,
    DeploymentContract,
    ModelDeploymentRegistration,
    RuntimeProtocolRegistration,
)
from jimu_runtime.bundle.compiler import (
    BundleBuildMetadata,
    BundleCompileInput,
    CompileEnvironment,
    ReferenceSlot,
    SourceArtifact,
    SourceIdentity,
)
from jimu_runtime.bundle.integrity import canonicalize_json, sha256_digest
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import ExternalCapabilityRequirement
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.runtime.bundle_manifest import (
    CompatibilityRequirement,
    SourceArtifactRef,
    SourceProvenance,
)
from jimu_runtime.domain.runtime.tool_executions import ToolResult
from jimu_runtime.domain.validation.review_inputs import ReviewedDeployment

from ..trust.support import AT, END, SOURCE, START, VERSION, pin, trust_source

LIMITS = BundleLimits(
    max_archive_bytes=512 * 1024,
    max_expanded_bytes=2 * 1024 * 1024,
    max_file_bytes=256 * 1024,
    max_files=128,
    max_json_depth=48,
)
VEHICLE = "nonmotor_vehicle"
QUESTION = "parking_state"
PLAN = "parking_geometry"
TOOL = "wheel_boundary"
PARKED = "parking.parked"
OUTSIDE = "parking.outside"
RULE = "synthetic_nonmotor_illegal_parking"
TOOL_CAPABILITY = "fixture.geometry"
PROVIDER = "fixture.remote"
DEPLOYMENT = "fixture.remote.parking"
LOGICAL_MODEL = "fixture.parking"
MODEL_ROLES = (
    ModelCapability.PROFILE_DISCOVERY,
    ModelCapability.OBJECT_GROUNDING,
    ModelCapability.OBJECT_OBSERVATION,
    ModelCapability.CANDIDATE_PREDICTION,
)
MODEL_FEATURES = ("rgb_image", "typed_context", "structured_output", "original_xyxy")
_BOOL = {"type": "boolean"}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 128}
_TYPED_BOOL = {
    "type": "object",
    "properties": {"kind": {"const": "bool"}, "value": _BOOL},
    "required": ["kind", "value"],
    "additionalProperties": False,
}


def _ref(kind, identity, *, schema_ref=False):
    return ReferenceSlot(
        location=(),
        target=(ArtifactKind(kind), identity, VERSION),
        schema_ref=schema_ref,
    )


def _policy_ref(identity):
    return _ref("policy", identity)


def _artifact(kind, identity, path, value):
    slots = []

    def bind(node, location=()):
        if isinstance(node, ReferenceSlot):
            slots.append(
                ReferenceSlot(
                    location=location, target=node.target, schema_ref=node.schema_ref
                )
            )
            return None
        if isinstance(node, dict):
            return {key: bind(item, (*location, key)) for key, item in node.items()}
        if isinstance(node, list):
            return [bind(item, (*location, index)) for index, item in enumerate(node)]
        return node

    raw = json.dumps(bind(value), ensure_ascii=False, indent=2).encode()
    return SourceArtifact(
        kind=ArtifactKind(kind),
        artifact_id=identity,
        version=VERSION,
        path=path,
        content=raw,
        origin=SourceIdentity(artifact_id=f"{kind}:{identity}", version=VERSION),
        reference_slots=tuple(slots),
    )


def _definition(kind, identity, path, id_field, **fields):
    return _artifact(
        kind,
        identity,
        path,
        {"schema_version": VERSION, "version": VERSION, id_field: identity, **fields},
    )


def _schema(kind, identity, directory, value):
    return _artifact(
        kind,
        identity,
        f"{directory}/{identity}.schema.json",
        {
            "$schema": DRAFT_2020_12,
            "$id": f"urn:jimu:fixture:{identity}:{VERSION}",
            **value,
        },
    )


def _record(**fields):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


def _literal(value):
    return {
        "kind": "literal",
        "constant": {
            "type_definition": {"kind": "bool"},
            "value": {"kind": "bool", "value": value},
        },
    }


def _policies():
    wp01b = Path(__file__).parents[2] / "wp01b" / "p1" / "contracts.json"
    previous = json.loads(wp01b.read_bytes())["artifacts"]
    rows = []

    def policy(identity, kind, *, path="policies/registry.json", **fields):
        rows.append(
            _definition(
                "policy",
                identity,
                path,
                "policy_id",
                policy={"policy_kind": kind, **fields},
            )
        )

    for key in (
        "unknown",
        "question_validation",
        "tool_execution",
        "tool_failure",
        "fallback",
        "rule",
        "conflict",
    ):
        document = previous[key]
        data = document["policy"]
        policy(
            document["policy_id"],
            data["policy_kind"],
            **{k: v for k, v in data.items() if k != "policy_kind"},
        )
    policy(
        "coverage",
        "coverage",
        path="policies/coverage.json",
        no_hits="uncertain",
        no_target="uncertain",
        partial_without_match="uncertain",
        partial_with_match="violation",
        complete_negative="all_mandatory_false_with_required_evidence",
        absence_strategy="conservative",
    )
    policy(
        "evidence",
        "evidence",
        path="policies/evidence.json",
        required_evidence_types=[],
        allowed_evidence_types=["original_image"],
        on_missing="block_result",
        original_media_required=True,
        retention_class=_member("retention", "synthetic"),
        retention_duration_seconds=100,
        access_scope="task",
    )
    policy(
        "resources",
        "resource_requirements",
        path="policies/resources.json",
        resource_class="cpu",
        max_concurrency=1,
        timeout_ms=100,
        input_byte_limit=100,
        output_byte_limit=100,
    )
    policy(
        "remote_resources",
        "resource_requirements",
        path="policies/resources.json",
        resource_class="remote",
        remote_quota_id="fixture.quota",
        max_concurrency=1,
        timeout_ms=100,
        input_byte_limit=100,
        output_byte_limit=100,
    )
    policy(
        "provider",
        "provider",
        routing_mode="remote_primary",
        primary_deployment_id=DEPLOYMENT,
        deployments=[
            {
                "provider_id": PROVIDER,
                "deployment_id": DEPLOYMENT,
                "logical_model_id": LOGICAL_MODEL,
                "provider_kind": "remote",
                "actual_version_constraints": {
                    "allowed_deployment_versions": [VERSION],
                    "allowed_model_versions": [VERSION],
                },
                "required_features": list(MODEL_FEATURES),
            }
        ],
        selection="primary_then_explicit_fallback",
    )
    policy(
        "selection",
        "question_selection",
        mode="all_declared",
        mandatory_question_refs=[_ref("question", QUESTION)],
        optional_question_refs=[],
        optional_source="all",
        fallback="include_all_optional",
        ordering="profile_order",
    )
    policy(
        "applicability",
        "applicability",
        input_domain="rule_applicability",
        context_allowlist=[],
        identity_constraints=[],
        condition_ast=_literal(True),
        on_unknown="block",
    )
    policy(
        "skip",
        "tool_skip",
        condition_ast=True,
        on_false="skip_with_reason",
        on_unknown="block",
        on_missing_input="block",
    )
    policy(
        "context_provenance",
        "context_provenance",
        fields=[
            {
                "field_path": ["zone"],
                "schema_ref": _ref("context_schema", "context", schema_ref=True),
                "required": True,
                "time_sensitive": False,
                "allowed_sources": ["trusted_task_metadata"],
            }
        ],
        conflicting_sources="block",
        missing_required="block",
        internal_timezone="UTC",
        naive_time="reject",
    )
    policy(
        "workflow",
        "execution",
        path="policies/execution.json",
        workflow_version=VERSION,
        node_order=["N01", "N02", "N03", "N04", "N05", "N06"],
        coverage_policy_ref=_policy_ref("coverage"),
        context_provenance_policy_ref=_policy_ref("context_provenance"),
        resource_requirements_ref=_policy_ref("resources"),
        artifact_schema_refs=[],
    )
    return rows


def _member(vocabulary, member):
    return {"vocabulary_ref": _ref("vocabulary", vocabulary), "member": member}


def _schema_sources():
    paths = {
        "context_schema": "schemas/context",
        "attribute_schema": "schemas/attributes",
        "response_schema": "prompts/response_contracts",
        "tool_input_schema": "schemas/tools/inputs",
        "tool_output_schema": "schemas/tools/outputs",
    }
    tool_result = TypeAdapter(ToolResult).json_schema()
    for name in ("SucceededToolResult", "PartialToolResult", "FailedToolResult"):
        props = tool_result["$defs"][name]["properties"]
        props["evidence"] = {"type": "array", "maxItems": 0}
        if name != "FailedToolResult":
            props["payload"] = _TYPED_BOOL
    coord = {"type": "integer", "minimum": 0, "maximum": 8192}
    bbox = _record(x1=coord, x2=coord, y1=coord, y2=coord)
    schemas = [
        ("context_schema", "context", _record(zone=_TEXT)),
        ("attribute_schema", "image", _TEXT),
        ("response_schema", "model-input", _record(image=_TEXT)),
        (
            "response_schema",
            "profile_discovery-result",
            _record(
                profile_hits={
                    "type": "array",
                    "items": _TEXT,
                    "minItems": 0,
                    "maxItems": 16,
                }
            ),
        ),
        (
            "response_schema",
            "object_grounding-result",
            _record(
                boxes={"type": "array", "items": bbox, "minItems": 0, "maxItems": 16}
            ),
        ),
        ("response_schema", "object_observation-result", _record(parking_state=_BOOL)),
        (
            "response_schema",
            "candidate_prediction-result",
            _record(
                categories={
                    "type": "array",
                    "items": _TEXT,
                    "minItems": 0,
                    "maxItems": 16,
                }
            ),
        ),
        ("response_schema", "parking_state-result", _TYPED_BOOL),
        ("tool_input_schema", "geometry-input", _record(image=_TEXT)),
        ("tool_output_schema", "geometry-result", tool_result),
    ]
    return tuple(
        _schema(kind, identity, paths[kind], value) for kind, identity, value in schemas
    )


def _model_sources():
    prompts = [
        _definition(
            "prompt_template",
            identity,
            f"prompts/templates/{identity}.json",
            "prompt_template_id",
            purpose=purpose,
            template_text=f"Fixture {identity}.",
            variables=[],
            response_schema_ref=_ref("response_schema", f"{identity}-result"),
            rendering_policy="literal_substitution",
        )
        for identity, purpose in (
            *((role.value, role.value) for role in MODEL_ROLES),
            (QUESTION, "object_observation"),
        )
    ]
    bindings = [
        _definition(
            "model_binding",
            role.value,
            "models/bindings.json",
            "model_binding_id",
            capability=role.value,
            logical_model_id=LOGICAL_MODEL,
            provider_policy_ref=_policy_ref("provider"),
            fallback_policy_ref=_policy_ref("fallback"),
            resource_requirements_ref=_policy_ref("remote_resources"),
            input_schema_ref=_ref("response_schema", "model-input", schema_ref=True),
            output_schema_ref=_ref(
                "response_schema", f"{role.value}-result", schema_ref=True
            ),
            prompt_template_ref=_ref("prompt_template", role.value),
        )
        for role in MODEL_ROLES
    ]
    return (*prompts, *bindings)


def _fact_schema(identity, key, producer):
    wp01b = Path(__file__).parents[2] / "wp01b" / "r1" / "contracts.json"
    previous = json.loads(wp01b.read_bytes())
    value = previous["fact_schema"]
    value.update(
        schema_id=identity,
        semantic_key=key,
        payload_schema_ref=None,
        allowed_producer_kinds=[producer],
        evidence_policy_ref=_policy_ref("evidence"),
        value_schema_generation=previous["spec"],
    )
    return _artifact(
        "fact_schema", identity, f"schemas/facts/definitions/{identity}.json", value
    )


def _tool_requirement():
    return {
        "family": "tool_implementation",
        "capability_id": TOOL_CAPABILITY,
        "contract_version": VERSION,
        "allowed_implementation_versions": [VERSION],
        "required_features": ["geometry_output"],
    }


def _model_requirement():
    return {
        "family": "model_deployment",
        "capability_id": DEPLOYMENT,
        "contract_version": VERSION,
        "allowed_implementation_versions": [VERSION],
        "required_features": list(MODEL_FEATURES),
    }


def _scenario_definitions():
    parked = _ref("fact_schema", "parked")
    outside = _ref("fact_schema", "outside")
    parked_selector = {
        "scope": "object",
        "fact_key": PARKED,
        "fact_schema_ref": parked,
        "field_path": [],
        "producer_selector": {
            "producer_kind": "vlm_question",
            "question_ref": _ref("question", QUESTION),
            "output_name": "primary",
        },
    }
    outside_selector = {
        "scope": "object",
        "fact_key": OUTSIDE,
        "fact_schema_ref": outside,
        "field_path": [],
        "producer_selector": {
            "producer_kind": "tool",
            "tool_plan_ref": _ref("tool_plan", PLAN),
            "step_id": TOOL,
            "tool_ref": _ref("tool", TOOL),
            "output_name": "outside",
        },
    }
    return (
        *(
            _definition(
                "vocabulary",
                identity,
                f"vocabularies/{identity}.json",
                "vocabulary_id",
                semantic_domain=domain,
                members=[member],
            )
            for identity, domain, member in (
                ("entity-type", "entity.type", "physical_object"),
                ("lifecycle", "entity.lifecycle", "enabled"),
                ("region-type", "region.type", "parking_area"),
                ("scene-type", "region.scene", "parking_fixture"),
                ("retention", "evidence.retention_class", "synthetic"),
            )
        ),
        _definition(
            "entity",
            VEHICLE,
            "ontology/entities.json",
            "entity_id",
            name="Synthetic non-motor vehicle",
            entity_type=_member("entity-type", "physical_object"),
            description="Fixture only",
            attribute_schema_refs=[],
            lifecycle=_member("lifecycle", "enabled"),
        ),
        _definition(
            "region",
            "test_parking",
            "ontology/regions.json",
            "region_id",
            name="Synthetic inspection area",
            region_type=_member("region-type", "parking_area"),
            scene_type=_member("scene-type", "parking_fixture"),
            asset_refs=[],
            context_schema_ref=_ref("context_schema", "context"),
        ),
        _fact_schema("parked", PARKED, "vlm_question"),
        _fact_schema("outside", OUTSIDE, "tool"),
        _definition(
            "facet",
            "parking",
            "capabilities/facets.json",
            "facet_id",
            name="Parking observations",
            description="Fixture only",
            fact_schema_refs=[parked],
        ),
        _definition(
            "question",
            QUESTION,
            "capabilities/questions.json",
            "question_id",
            objective="Inspect synthetic parking state",
            prompt_template_ref=_ref("prompt_template", QUESTION),
            facet_refs=[_ref("facet", "parking")],
            context_allowlist=[],
            primary_fact_key=PARKED,
            primary_fact_schema_ref=parked,
            evidence_policy_ref=_policy_ref("evidence"),
            unknown_policy_ref=_policy_ref("unknown"),
            validation_policy_ref=_policy_ref("question_validation"),
        ),
        _definition(
            "tool",
            TOOL,
            "tools/registry.json",
            "tool_id",
            tool_type="geometry",
            provider_kind="builtin",
            input_schema_ref=_ref(
                "tool_input_schema", "geometry-input", schema_ref=True
            ),
            output_schema_ref=_ref(
                "tool_output_schema", "geometry-result", schema_ref=True
            ),
            resource_requirements_ref=_policy_ref("resources"),
            execution_policy_ref=_policy_ref("execution"),
            evidence_policy_ref=_policy_ref("evidence"),
            implementation_requirement=_tool_requirement(),
            capability_tags=["geometry"],
            fact_outputs=[
                {
                    "output_name": "outside",
                    "fact_key": OUTSIDE,
                    "fact_schema_ref": outside,
                    "scope": "object",
                    "required": True,
                    "value_path": [],
                }
            ],
        ),
        _definition(
            "tool_plan",
            PLAN,
            "tools/plans.json",
            "tool_plan_id",
            steps=[
                {
                    "step_id": TOOL,
                    "tool_ref": _ref("tool", TOOL),
                    "depends_on": [],
                    "input_bindings": [
                        {
                            "source": {
                                "source_kind": "image",
                                "selector": {
                                    "schema_ref": _ref(
                                        "attribute_schema", "image", schema_ref=True
                                    ),
                                    "field_path": [],
                                },
                            },
                            "target": {
                                "schema_ref": _ref(
                                    "tool_input_schema",
                                    "geometry-input",
                                    schema_ref=True,
                                ),
                                "field_path": ["image"],
                            },
                            "required": True,
                            "on_missing": "block",
                        }
                    ],
                    "fact_output_mappings": [
                        {"output_name": "outside", "target_fact_key": OUTSIDE}
                    ],
                    "requirement": "required",
                    "failure_policy_ref": _policy_ref("failure"),
                    "skip_policy_ref": _policy_ref("skip"),
                }
            ],
        ),
        _definition(
            "rule",
            RULE,
            "rules/registry.json",
            "rule_id",
            violation_category="synthetic_parking",
            applicability_policy_ref=_policy_ref("applicability"),
            required_fact_selectors=[parked_selector, outside_selector],
            optional_fact_selectors=[],
            condition_ast={
                "kind": "all",
                "arguments": [
                    {"kind": "fact_ref", "selector": parked_selector},
                    {"kind": "fact_ref", "selector": outside_selector},
                ],
            },
            rule_policy_ref=_policy_ref("rule"),
            evidence_policy_ref=_policy_ref("evidence"),
        ),
        _definition(
            "rule_set",
            "parking_rules",
            "rules/sets.json",
            "rule_set_id",
            rule_refs=[_ref("rule", RULE)],
            applicability_policy_ref=_policy_ref("applicability"),
            conflict_policy_ref=_policy_ref("conflict"),
            candidate_binding_ref=_ref("model_binding", "candidate_prediction"),
        ),
        _definition(
            "profile",
            VEHICLE,
            "capabilities/profiles.json",
            "profile_id",
            entity_ref=_ref("entity", VEHICLE),
            discovery_binding_ref=_ref("model_binding", "profile_discovery"),
            grounding_binding={
                "kind": "model",
                "model_binding_ref": _ref("model_binding", "object_grounding"),
                "input_bindings": [
                    {
                        "source": {
                            "source_kind": "image",
                            "selector": {
                                "schema_ref": _ref(
                                    "attribute_schema", "image", schema_ref=True
                                ),
                                "field_path": [],
                            },
                        },
                        "target": {
                            "schema_ref": _ref(
                                "response_schema", "model-input", schema_ref=True
                            ),
                            "field_path": ["image"],
                        },
                        "required": True,
                        "on_missing": "block",
                    }
                ],
            },
            observation_binding_ref=_ref("model_binding", "object_observation"),
            facet_refs=[_ref("facet", "parking")],
            question_refs=[_ref("question", QUESTION)],
            question_selection_policy_ref=_policy_ref("selection"),
            rule_set_refs=[_ref("rule_set", "parking_rules")],
            tool_plan_ref=_ref("tool_plan", PLAN),
            applicability_policy_ref=_policy_ref("applicability"),
        ),
    )


def source_scenario() -> BundleCompileInput:
    sources = (
        *_schema_sources(),
        *_model_sources(),
        *_policies(),
        *_scenario_definitions(),
    )
    return approve_source_bytes(
        BundleCompileInput(
            metadata=BundleBuildMetadata(
                bundle_id="fixture.nonmotor.parking",
                bundle_version=VERSION,
                compiler_version=VERSION,
                created_at=START,
                source=SourceProvenance(source_system=SOURCE, artifacts=()),
                compatibility=CompatibilityRequirement(
                    minimum_runtime_version_inclusive=VERSION,
                    maximum_runtime_version_exclusive="2.0.0",
                ),
                required_capabilities=tuple(
                    ExternalCapabilityRequirement.model_validate(row)
                    for row in (_tool_requirement(), _model_requirement())
                ),
                producer_catalogue_id="fixture.parking.producers",
                producer_catalogue_version=VERSION,
            ),
            artifacts=sources,
        )
    )


def approve_source_bytes(source: BundleCompileInput) -> BundleCompileInput:
    """Declare synthetic provenance for an independently authored test input."""
    rows = tuple(
        SourceArtifactRef(
            artifact_id=item.origin.artifact_id,
            version=item.origin.version,
            digest=sha256_digest(item.content),
        )
        for item in (*source.artifacts, *source.assets)
    )
    metadata = replace(
        source.metadata,
        source=SourceProvenance(
            source_system=source.metadata.source.source_system,
            artifacts=rows,
        ),
    )
    return replace(source, metadata=metadata)


def capability_snapshot() -> CapabilitySnapshot:
    sources = {item.artifact_id: item for item in _schema_sources()}

    def schema_ref(identity):
        item = sources[identity]
        raw = canonicalize_json(
            item.content,
            max_bytes=LIMITS.max_file_bytes,
            max_depth=LIMITS.max_json_depth,
        )
        return SchemaRef.model_validate(
            {
                "artifact_kind": item.kind.value,
                "artifact_id": identity,
                "version": VERSION,
                "digest": sha256_digest(raw),
                "schema_dialect": DRAFT_2020_12,
            }
        )

    return CapabilitySnapshot.model_validate(
        {
            "pin": pin("fixture.capabilities"),
            "runtime_version": VERSION,
            "bundle_schema_versions": (VERSION,),
            "valid_from": START,
            "valid_until": END,
            "capabilities": tuple(
                CapabilityRegistration.model_validate(
                    {
                        "family": row["family"],
                        "capability_id": row["capability_id"],
                        "contract_version": VERSION,
                        "implementation_version": VERSION,
                        "supported_features": row["required_features"],
                        "evaluations": (),
                        "valid_from": START,
                        "valid_until": END,
                        "revoked_at": None,
                    }
                )
                for row in (_tool_requirement(), _model_requirement())
            ),
            "model_deployments": (
                ModelDeploymentRegistration.model_validate(
                    {
                        "identity": ReviewedDeployment(
                            provider_id=PROVIDER,
                            deployment_id=DEPLOYMENT,
                            logical_model_id=LOGICAL_MODEL,
                            provider_kind="remote",
                            deployment_version=VERSION,
                            model_version=VERSION,
                        ),
                        "supported_features": MODEL_FEATURES,
                        "contracts": tuple(
                            DeploymentContract(
                                capability=role,
                                input_schema_ref=schema_ref("model-input"),
                                output_schema_ref=schema_ref(f"{role.value}-result"),
                            )
                            for role in MODEL_ROLES
                        ),
                        "valid_from": START,
                        "valid_until": END,
                        "revoked_at": None,
                    }
                ),
            ),
            "model_reviews": (),
            "supported_workflow_versions": (VERSION,),
            "runtime_protocols": tuple(
                RuntimeProtocolRegistration(
                    role=role,
                    input_schema_ref=schema_ref(input_schema),
                    output_schema_ref=schema_ref(output_schema),
                    protocol_version=VERSION,
                    valid_from=START,
                    valid_until=END,
                    revoked_at=None,
                )
                for role, input_schema, output_schema in (
                    *(
                        (role, "model-input", f"{role.value}-result")
                        for role in MODEL_ROLES
                    ),
                    ("tool_execution", "geometry-input", "geometry-result"),
                )
            ),
        }
    )


def compile_environment(tmp_path: Path) -> CompileEnvironment:
    return CompileEnvironment(
        capabilities=capability_snapshot(),
        trust=trust_source(tmp_path, (), ()),
        validation_at=AT,
    )
