"""Finite semantic fixtures, with independently exercised archive bytes in integration."""

from dataclasses import replace

from fixtures.wp01c.rule_types.support import Example, literal, ref
from pydantic import TypeAdapter

from jimu_runtime.bundle.artifacts import ARTIFACT_LAYOUTS, artifact_identity
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, ResolvedArtifact
from jimu_runtime.bundle.integrity import canonical_json
from jimu_runtime.bundle.producer_rules import (
    derive_producer_catalogue,
    validate_producer_rules,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.policies import PolicyArtifact
from jimu_runtime.domain.producers import FactProducerDeclaration
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.bundle_manifest import ArtifactInventoryEntry
from jimu_runtime.domain.validation.definition_inputs import DefinitionValidationInput

LIMITS = {"max_bytes": 65536, "max_depth": 32}


def policy(name, kind, **body):
    return {
        "ref": ref("policy", name),
        "artifact": TypeAdapter(PolicyArtifact)
        .validate_python(
            {
                "schema_version": "1.0.0",
                "policy_id": name,
                "version": "1.0.0",
                "policy": {"policy_kind": kind, **body},
            }
        )
        .model_dump(mode="json"),
    }


def applicability(name, condition=None, constraints=()):
    return policy(
        name,
        "applicability",
        input_domain="rule_applicability",
        context_allowlist=(),
        identity_constraints=constraints,
        condition_ast=literal() if condition is None else condition,
        on_unknown="block",
    )


def artifact(reference, definition):
    pin = ContentRef.model_validate(reference)
    path = ARTIFACT_LAYOUTS[pin.artifact_kind].location
    path = path.replace("*", pin.artifact_id)
    return ResolvedArtifact(
        ArtifactInventoryEntry(content_ref=pin, path=path),
        definition,
        canonical_json(definition.model_dump(mode="json"), **LIMITS),
    )


def include(catalogue, entry):
    return replace(
        catalogue,
        artifacts=(*catalogue.artifacts, entry),
        _by_identity={**catalogue._by_identity, artifact_identity(entry.ref): entry},
    )


class ProducerExample(Example):
    def __init__(self):
        super().__init__()
        self.data["input"].pop("validation_at")
        self.extra = []
        self.requirements = []
        self.systems = ()
        self.catalogue_id = self.catalogue_ref["artifact_id"]
        self.original_profile_applicability = self.profile["definition"][
            "applicability_policy_ref"
        ]
        existing = self.policy_by_name(
            self.original_profile_applicability["artifact_id"]
        )
        existing["artifact"]["policy"]["condition_ast"] = literal()
        existing["artifact"]["policy"]["context_allowlist"] = []
        self.profile["definition"]["rule_set_refs"] = []

    def policy_by_name(self, name):
        return next(
            item
            for item in self.data["input"]["policies"]
            if item["ref"]["artifact_id"] == name
        )

    def configured_question(self, *, mandatory):
        fact = self.question_fact()
        selection_ref = self.profile["definition"]["question_selection_policy_ref"]
        selection = self.policy_by_name(selection_ref["artifact_id"])["artifact"][
            "policy"
        ]
        selection[
            "mandatory_question_refs" if mandatory else "optional_question_refs"
        ].append(fact["selector"]["producer_selector"]["question_ref"])
        return fact

    def rule_use(self, fact, *, mandatory=True, set_applies=None, optional=()):
        self.profile["definition"]["rule_set_refs"] = [self.rule_set_ref]
        self.extra += [
            (self.rule_ref, self.rule(fact, optional=optional)),
            (self.rule_set_ref, self.context("rule_condition").rule_set.definition),
        ]
        self.data["input"]["policies"] += [
            applicability("ruleset-applies", set_applies),
            applicability("rule-applies"),
            policy(
                "rule-policy",
                "rule",
                mandatory=mandatory,
                missing="unknown",
                unknown="unknown",
                error="unknown",
                not_applicable="unknown",
                not_evaluated="unknown",
                required_guard="block_unavailable",
                scan="all_mandatory",
                true_result="violation",
                false_result="no_violation",
                unknown_result="uncertain",
            ),
        ]

    def grant_system(self, *, versions=("1.0.0", "2.0.0"), features=()):
        fact = self.system_fact()
        declaration = self.declarations[-1]
        declaration["producer"]["requirement"].update(
            allowed_implementation_versions=versions,
            required_features=features,
        )
        system = FactProducerDeclaration.model_validate(declaration)
        self.systems = (*self.systems, system)
        self.requirements.append(system.producer.requirement)
        return fact, system

    def guard(self, step_id, expr):
        source = self.policy_by_name("external-skip")["artifact"]["policy"]
        new = policy(
            "skip-" + step_id,
            "tool_skip",
            **{
                **{key: value for key, value in source.items() if key != "policy_kind"},
                "condition_ast": expr,
            },
        )
        self.data["input"]["policies"].append(new)
        step = next(
            step
            for step in self.plan["definition"]["steps"]
            if step["step_id"] == step_id
        )
        step["skip_policy_ref"] = new["ref"]

    def inputs(self):
        return DefinitionValidationInput.model_validate(self.data["input"])

    def catalogue(self):
        inputs = self.inputs()
        pairs = [
            (item.ref.model_dump(), item.definition)
            for item in inputs.definitions
            if item.ref.artifact_kind
            in {
                ArtifactKind.PROFILE,
                ArtifactKind.TOOL,
                ArtifactKind.TOOL_PLAN,
                ArtifactKind.QUESTION,
                ArtifactKind.REGION,
            }
        ]
        pairs += [(item.ref.model_dump(), item.artifact) for item in inputs.policies]
        pairs += self.extra
        return (
            ArtifactCatalogue((), {}, {}, tuple(self.requirements))
            if not pairs
            else self._with_entries(pairs)
        )

    def _with_entries(self, pairs):
        items = tuple(
            artifact(reference, definition) for reference, definition in pairs
        )
        return ArtifactCatalogue(
            items,
            {artifact_identity(item.ref): item for item in items},
            {},
            tuple(self.requirements),
        )

    def derive(self, catalogue=None):
        return derive_producer_catalogue(
            self.catalogue() if catalogue is None else catalogue,
            catalogue_id=self.catalogue_id,
            version="1.0.0",
            schema_version="1.0.0",
            system_declarations=self.systems,
        )

    def check(self, *, definition=None, capabilities=(), grants=(), catalogue=None):
        if catalogue is None:
            catalogue = self.catalogue()
        compiled = self.derive(catalogue) if definition is None else definition
        validate_producer_rules(
            include(catalogue, artifact(self.catalogue_ref, compiled)),
            self.inputs(),
            fact_schemas=self.registry(),
            system_capabilities=capabilities,
            system_grants=grants,
            **LIMITS,
        )
