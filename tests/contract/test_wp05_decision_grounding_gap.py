"""A successful aggregate N03 needs exact evidence for each omitted Profile."""

from pathlib import Path

import pytest

from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.nodes.envelopes import NodeError
from jimu_runtime.domain.nodes.grounding import _GroundingInvocationGap
from jimu_runtime.domain.runtime.coverage import (
    CoverageReason,
    DependencyNotEvaluatedObligation,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.perception import (
    GroundingOutcome,
    ModelGroundingProvenance,
)
from jimu_runtime.domain.validation.coverage_sources import (
    validate_decision_coverage_sources,
)
from jimu_runtime.workflow import worker_coverage
from tests.fixtures.wp03.coverage.scenario import scenario


def test_mixed_grounding_needs_selected_attempt_and_original_profile_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    def capture(coverage, records, sources, **kwargs):
        validate_decision_coverage_sources(coverage, records, sources, **kwargs)
        captured.append((records, sources, kwargs["authority"]))

    monkeypatch.setattr(worker_coverage, "validate_decision_coverage_sources", capture)
    case = scenario(tmp_path, mode="mixed")
    finalization = case.finalize()
    ((records, sources, authority),) = captured
    usable, failed = sources.grounding_records
    assert isinstance(failed.producer, ModelGroundingProvenance)
    failed_id = failed.producer.model_invocation_ref.record_id

    def replace_reason(reasons):
        return tuple(
            CoverageReason.GROUNDING_NOT_EVALUATED
            if reason is CoverageReason.GROUNDING_FAILED
            else reason
            for reason in reasons
        )

    coverage = finalization.coverage
    obligations = tuple(
        row.model_copy(
            update={
                "outcome": row.outcome.model_copy(
                    update={"reason": CoverageReason.GROUNDING_NOT_EVALUATED}
                )
            }
        )
        if row.profile_ref == failed.profile_ref
        and isinstance(row.outcome, DependencyNotEvaluatedObligation)
        else row
        for row in coverage.mandatory_rule_coverage.obligations
    )
    coverage = coverage.model_copy(
        update={
            "profiles": tuple(
                profile.model_copy(
                    update={
                        "grounding_outcome": GroundingOutcome.NOT_EVALUATED,
                        "reasons": replace_reason(profile.reasons),
                    }
                )
                if profile.profile_ref == failed.profile_ref
                else profile
                for profile in coverage.profiles
            ),
            "reasons": replace_reason(coverage.reasons),
            "mandatory_rule_coverage": coverage.mandatory_rule_coverage.model_copy(
                update={"obligations": obligations}
            ),
        }
    )
    node = next(row for row in sources.node_runs if row.node_id is NodeId.N03)
    node = node.model_copy(
        update={
            "output_refs": tuple(
                ref
                for ref in node.output_refs
                if ref.record_id not in (failed.grounding_record_id, failed_id)
            ),
        }
    )
    assert any(
        ref.record_kind is RuntimeRecordKind.GROUNDING_RECORD
        for ref in node.output_refs
    )
    sources = sources.model_copy(
        update={
            "grounding_records": (usable,),
            "node_runs": tuple(
                node if row.node_id is NodeId.N03 else row for row in sources.node_runs
            ),
        }
    )
    records = records.model_copy(
        update={
            "model_invocations": tuple(
                row
                for row in records.model_invocations
                if row.model_invocation_id != failed_id
            )
        }
    )
    no_send = _GroundingInvocationGap(
        schema_version="1.0.0",
        execution_id=failed.execution_id,
        node_run_id=failed.node_run_id,
        attempt_id=failed.attempt_id,
        profile_ref=failed.profile_ref,
        binding_ref=failed.producer.model_binding_ref,
        dispatch_id=failed_id,
        boundary="not_dispatched",
        error=NodeError(
            code="model.withheld",
            error_class="resource",
            retryable=False,
            message="The fixture journal did not admit this dispatch.",
            validation_issues=(),
        ),
    )
    validate_decision_coverage_sources(
        coverage,
        records,
        sources,
        authority=authority,
        grounding_gaps=(no_send,),
    )
    for invalid in (
        (),
        (no_send.model_copy(update={"profile_ref": usable.profile_ref}),),
        (no_send.model_copy(update={"attempt_id": sources.attempts[0].attempt_id}),),
        (no_send, no_send),
    ):
        with pytest.raises(ValueError):
            validate_decision_coverage_sources(
                coverage,
                records,
                sources,
                authority=authority,
                grounding_gaps=invalid,
            )
