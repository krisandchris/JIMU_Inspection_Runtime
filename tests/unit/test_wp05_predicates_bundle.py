"""Exercise the prepared predicate on a re-opened, signed WP-02 fixture."""

from types import SimpleNamespace

from fixtures.wp02.admission.support import build_scenario, sign_bundle, submit
from fixtures.wp02.compiler.scenario import LIMITS
from unit.test_wp05_predicates import fact, value

from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.semantic_inputs import build_fact_schema_registry
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_enums import ConditionInputDomain
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.applicability import RuleTruth
from jimu_runtime.domain.validation.rule_type_inputs import (
    RuleConditionContext,
    RuleTypeBindings,
)
from jimu_runtime.inspection.rules import PreparedPredicate


def test_rules_and_fact_slots_from_a_signed_admitted_catalogue(tmp_path):
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    directory = tmp_path / "runtime-store"
    admission = submit(
        LocalBundleStore(directory),
        signed.archive,
        "accept-predicate",
        signed.environment,
    )
    assert admission.accepted
    archive = LocalBundleStore(directory).read_archive(admission.receipt.receipt_id)
    verified = read_bundle(archive, LIMITS)
    catalogue = build_catalogue(
        verified.manifest,
        verified.payloads,
        max_bytes=LIMITS.max_file_bytes,
        max_depth=LIMITS.max_json_depth,
    )

    def registrations(kind, **additional):
        return tuple(
            {"ref": entry.ref.model_dump(), "definition": entry.value, **additional}
            for entry in catalogue.of_kind(kind)
        )

    (producers,) = registrations(ArtifactKind.FACT_PRODUCER_CATALOG)
    (profile,) = registrations(ArtifactKind.PROFILE, registration_kind="profile")
    (rule_set,) = registrations(ArtifactKind.RULE_SET)
    (rule,) = registrations(ArtifactKind.RULE)
    bindings = RuleTypeBindings.model_validate(
        {
            "profiles": (profile,),
            "questions": registrations(ArtifactKind.QUESTION),
            "plans": registrations(ArtifactKind.TOOL_PLAN),
            "tools": registrations(ArtifactKind.TOOL),
            "producer_catalogue": producers,
        }
    )
    context = RuleConditionContext.model_validate(
        {
            "use_role": "rule_condition",
            "profile_ref": profile["ref"],
            "rule_ref": rule["ref"],
            "rule_set": rule_set,
            "context_registrations": (),
            "descriptors": (),
        }
    )
    predicate = PreparedPredicate(
        catalogue=catalogue,
        input_domain=ConditionInputDomain.RULE_CONDITION,
        context=context,
        bindings=bindings,
        fact_schemas=build_fact_schema_registry(catalogue),
    )
    inputs = SimpleNamespace(
        declarations=[
            item.model_dump(mode="json")
            for item in producers["definition"].declarations
        ],
        catalogue_ref=producers["ref"],
        profile={"ref": profile["ref"]},
    )
    selectors = rule["definition"].required_fact_selectors
    selected = tuple(
        fact(
            inputs,
            {"selector": selector.model_dump(mode="json")},
            value=value("bool", True),
        )
        for selector in selectors
    )
    assert (
        predicate.evaluate(
            execution_id="execution", object_id="object", facts=selected
        ).truth
        is RuleTruth.TRUE
    )
    decisive = selected[0].model_copy(update={"value": value("bool", False)})
    incomplete = predicate.evaluate(
        execution_id="execution", object_id="object", facts=(decisive,)
    )
    assert incomplete.truth is RuleTruth.FALSE
    assert incomplete.diagnostic_codes == ("predicate.fact.missing",)
