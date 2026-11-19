"""Resolve which pinned ToolPlan steps need the Object's N04 batch."""

from jimu_runtime.domain.conditions import condition_references
from jimu_runtime.domain.definitions.tools import ToolStep
from jimu_runtime.domain.fact_selectors import FactSelector, QuestionProducerSelector
from jimu_runtime.domain.policies import ToolSkipPolicy
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.tool_mappings import FactInput


def guard_fact_selectors(policy: ToolSkipPolicy) -> tuple[FactSelector, ...]:
    condition = policy.condition_ast
    return () if condition is True else condition_references(condition).fact_selectors


def observation_selectors(
    step: ToolStep, obj: ObjectRecord, guard_selectors: tuple[FactSelector, ...] = ()
) -> tuple[FactSelector, ...]:
    """An excluded Question cannot supply a Fact for this Object."""
    bindings = tuple(
        source.selector
        for binding in step.input_bindings
        if isinstance(source := binding.source, FactInput)
    )
    return tuple(
        selector
        for selector in dict.fromkeys((*bindings, *guard_selectors))
        if isinstance(producer := selector.producer_selector, QuestionProducerSelector)
        and producer.question_ref in obj.effective_question_refs
    )


def needs_observation(
    step: ToolStep, obj: ObjectRecord, skip_policy: ToolSkipPolicy | None = None
) -> bool:
    guards = () if skip_policy is None else guard_fact_selectors(skip_policy)
    return bool(observation_selectors(step, obj, guards))
