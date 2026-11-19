"""Synthetic packaging inputs independently hashed for B2.2 checks."""

import json
from hashlib import sha256
from pathlib import Path

from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest

FIXTURES = Path(__file__).resolve().parents[2]
DRAFT = "https://json-schema.org/draft/2020-12/schema"
DEFAULT_ID = "synthetic"
VERSION = "1.0.0"


def frozen(batch, member):
    return json.loads((FIXTURES / "wp01b" / batch / "contracts.json").read_bytes())[
        member
    ]


def wire(value):
    # The synthetic fixture values use only ASCII and simple integers, for
    # which this Python serialization gives the independent expected bytes.
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return sha256(raw).hexdigest()


def pinned(kind, artifact_id, document, version=VERSION):
    return {
        "artifact_kind": kind,
        "artifact_id": artifact_id,
        "version": version,
        "digest": digest(wire(document)),
    }


def schema(alias, **keywords):
    return {"$schema": DRAFT, "$id": alias, **keywords}


def vocabulary(artifact_id="types", members=("synthetic",)):
    return {
        "schema_version": VERSION,
        "vocabulary_id": artifact_id,
        "version": VERSION,
        "semantic_domain": "fixture",
        "members": list(members),
    }


class Payloads:
    def __init__(self):
        self.entries = []
        self.files = {}

    def add(self, path, kind, artifact_id, document, *, pointer=None, version=VERSION):
        ref = pinned(kind, artifact_id, document, version)
        entry = {"path": path, "content_ref": ref}
        if pointer is None:
            self.files[path] = wire(document)
        else:
            entry["json_pointer"] = pointer
        self.entries.append(entry)
        return ref

    def manifest(self):
        return BundleManifest.model_validate_json(
            wire(
                {
                    "format": "jimu.runtime-bundle",
                    "schema_version": VERSION,
                    "bundle_id": "artifact-fixtures",
                    "bundle_version": VERSION,
                    "compiler_version": VERSION,
                    "created_at": "2026-11-17T02:30:00Z",
                    "source": {"source_system": "fixtures", "artifacts": []},
                    "artifacts": self.entries,
                    "compatibility": {
                        "minimum_runtime_version_inclusive": VERSION,
                        "maximum_runtime_version_exclusive": "2.0.0",
                    },
                    "required_capabilities": [],
                    "integrity": {
                        "hash_algorithm": "sha256",
                        "canonicalization_id": "jcs-sha256-v1",
                        "root_digest": "0" * 64,
                        "signatures": [],
                    },
                }
            )
        )

    def catalogue(self):
        return build_catalogue(
            self.manifest(), self.files, max_bytes=100_000, max_depth=64
        )


def add_entity(bundle, vocabulary_ref, **overrides):
    document = frozen("s1", "entity")
    member = {"vocabulary_ref": vocabulary_ref, "member": "synthetic"}
    document.update(entity_type=member, lifecycle=member, attribute_schema_refs=[])
    document.update(overrides)
    return bundle.add(
        "ontology/entities.json", "entity", document["entity_id"], document
    )


def applicability(profile_id="p", kind="profile"):
    return {
        "schema_version": VERSION,
        "policy_id": "synthetic.applicability",
        "version": VERSION,
        "policy": {
            "policy_kind": "applicability",
            "input_domain": "rule_applicability",
            "context_allowlist": [],
            "identity_constraints": [
                {
                    "artifact_kind": kind,
                    "artifact_id": profile_id,
                    "version_selector": VERSION,
                }
            ],
            "condition_ast": {
                "kind": "literal",
                "constant": {
                    "type_definition": {"kind": "bool"},
                    "value": {"kind": "bool", "value": True},
                },
            },
            "on_unknown": "block",
        },
    }


def add_schema(bundle, path, kind, artifact_id, alias, **keywords):
    return bundle.add(path, kind, artifact_id, schema(alias, **keywords))


def as_schema_ref(ref):
    return {**ref, "schema_dialect": DRAFT}


def profile_closure():
    """Catalogue-level fixtures. Model quality and type-view bindings are separate."""
    bundle = Payloads()
    vocab = bundle.add("vocabularies/types.json", "vocabulary", "types", vocabulary())
    entity = add_entity(bundle, vocab)
    image = add_schema(
        bundle,
        "schemas/attributes/image.schema.json",
        "attribute_schema",
        "image",
        "urn:bundle:image:1",
        type="string",
    )
    context = add_schema(
        bundle,
        "schemas/context/context.schema.json",
        "context_schema",
        "context",
        "urn:bundle:context:1",
        type="object",
        properties={"scene": {"type": "string"}},
    )
    response = add_schema(
        bundle,
        "prompts/response_contracts/response.schema.json",
        "response_schema",
        "response",
        "urn:bundle:response:1",
        type="boolean",
    )
    resource_policies = frozen("p1", "artifacts")
    retention = bundle.add(
        "vocabularies/retention.json",
        "vocabulary",
        "retention",
        vocabulary("retention"),
    )
    resource_policies["evidence"]["policy"]["retention_class"]["vocabulary_ref"] = (
        retention
    )
    policies = {}
    dedicated = {"resource_requirements": "resources", "evidence": "evidence"}
    for kind in (
        "resource_requirements",
        "evidence",
        "unknown",
        "question_validation",
        "fallback",
    ):
        document = resource_policies[kind]
        policies[kind] = bundle.add(
            f"policies/{dedicated.get(kind, kind)}.json",
            "policy",
            document["policy_id"],
            document,
        )
    provider = frozen("p1", "local_provider")
    policies["provider"] = bundle.add(
        "policies/provider.json", "policy", provider["policy_id"], provider
    )

    partial = frozen("o1", "prompt")
    partial.update(
        prompt_template_id="fragment",
        template_text="A simple fragment.",
        variables=[],
        response_schema_ref=response,
    )
    partial_ref = bundle.add(
        "prompts/templates/fragment.json", "prompt_template", "fragment", partial
    )
    prompt = frozen("o1", "prompt")
    prompt.update(partial_refs=[partial_ref], response_schema_ref=response)
    for variable in prompt["variables"]:
        variable["schema_ref"] = as_schema_ref(
            context if variable["source"] == "context" else image
        )
    prompt_ref = bundle.add(
        "prompts/templates/main.json",
        "prompt_template",
        prompt["prompt_template_id"],
        prompt,
    )

    model = frozen("m1", "model")
    model.update(
        provider_policy_ref=policies["provider"],
        resource_requirements_ref=policies["resource_requirements"],
        fallback_policy_ref=policies["fallback"],
        input_schema_ref=as_schema_ref(context),
        output_schema_ref=as_schema_ref(response),
        prompt_template_ref=prompt_ref,
    )
    model_ref = bundle.add(
        "models/bindings.json", "model_binding", model["model_binding_id"], model
    )

    definition = FactSchemaDefinition.model_validate(frozen("a2", "fact_schema"))
    child = generate_value_schema(
        definition.type_definition, definition.value_schema_generation
    )
    schema_ref = bundle.add(
        "schemas/values/value.schema.json",
        "value_schema",
        child.child_ref.artifact_id,
        json.loads(child.canonical_schema_bytes),
        version=child.child_ref.version,
    )
    fact = frozen("a2", "fact_schema")
    fact.update(payload_schema_ref=schema_ref, evidence_policy_ref=policies["evidence"])
    fact_ref = bundle.add(
        "schemas/facts/definitions/fact.json", "fact_schema", fact["schema_id"], fact
    )
    facet = frozen("o1", "facet")
    facet["fact_schema_refs"] = [fact_ref]
    facet_ref = bundle.add(
        "capabilities/facets.json", "facet", facet["facet_id"], facet
    )
    question = frozen("o1", "question")
    question["context_allowlist"][0]["schema_ref"] = as_schema_ref(context)
    question.update(
        prompt_template_ref=prompt_ref,
        facet_refs=[facet_ref],
        primary_fact_schema_ref=fact_ref,
        evidence_policy_ref=policies["evidence"],
        unknown_policy_ref=policies["unknown"],
        validation_policy_ref=policies["question_validation"],
    )
    question_ref = bundle.add(
        "capabilities/questions.json", "question", question["question_id"], question
    )
    selection = resource_policies["question_selection"]
    selection["policy"]["mandatory_question_refs"] = [question_ref]
    selection_ref = bundle.add(
        "policies/registry.json", "policy", selection["policy_id"], selection
    )

    plan = frozen("m1", "plan")
    plan["steps"] = []
    plan_ref = bundle.add("tools/plans.json", "tool_plan", plan["tool_plan_id"], plan)
    profile = frozen("o1", "profile")
    app = applicability(profile["profile_id"])
    app_ref = bundle.add("policies/applicability.json", "policy", app["policy_id"], app)
    profile.update(
        entity_ref=entity,
        discovery_binding_ref=model_ref,
        observation_binding_ref=model_ref,
        grounding_binding={"kind": "model", "model_binding_ref": model_ref},
        facet_refs=[facet_ref],
        question_refs=[question_ref],
        question_selection_policy_ref=selection_ref,
        tool_plan_ref=plan_ref,
        applicability_policy_ref=app_ref,
    )
    profile_ref = bundle.add(
        "capabilities/profiles.json", "profile", profile["profile_id"], profile
    )
    return bundle, profile_ref, question_ref, plan_ref, app_ref, prompt_ref, partial_ref
