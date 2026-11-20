"""Closed, independent domains; wire strings never authorize cross-domain casts."""

from enum import Enum, unique
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


class WireEnum(Enum):
    """Accept exact wire strings or this enum, rejecting all other enum domains."""

    @classmethod
    def _decode(cls, value: object) -> Self:
        if isinstance(value, cls):
            return value
        if type(value) is str:
            return cls(value)
        raise ValueError(f"expected {cls.__name__} or its exact wire string")

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: object, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_before_validator_function(
            cls._decode, handler(source)
        )


@unique
class ArtifactKind(WireEnum):
    ENTITY = "entity"
    REGION = "region"
    PROFILE = "profile"
    FACET = "facet"
    QUESTION = "question"
    TOOL = "tool"
    TOOL_PLAN = "tool_plan"
    RULE = "rule"
    RULE_SET = "rule_set"
    MODEL_BINDING = "model_binding"
    FACT_SCHEMA = "fact_schema"
    PROMPT_TEMPLATE = "prompt_template"
    RESPONSE_SCHEMA = "response_schema"
    ATTRIBUTE_SCHEMA = "attribute_schema"
    CONTEXT_SCHEMA = "context_schema"
    TOOL_INPUT_SCHEMA = "tool_input_schema"
    TOOL_OUTPUT_SCHEMA = "tool_output_schema"
    OBJECT_SCHEMA = "object_schema"
    DECISION_SCHEMA = "decision_schema"
    VALUE_SCHEMA = "value_schema"
    POLICY = "policy"
    VOCABULARY = "vocabulary"
    GEOMETRY_RESOURCE = "geometry_resource"
    STATIC_ASSET = "static_asset"
    FACT_PRODUCER_CATALOG = "fact_producer_catalog"


@unique
class RuntimeRecordKind(WireEnum):
    TASK = "task"
    EXECUTION = "execution"
    NODE_RUN = "node_run"
    ATTEMPT = "attempt"
    OBJECT = "object"
    FACT = "fact"
    EVIDENCE = "evidence"
    TOOL_EXECUTION = "tool_execution"
    MODEL_INVOCATION = "model_invocation"
    DECISION = "decision"
    RELEASE = "release"
    ACTIVATION = "activation"
    CONTEXT_SNAPSHOT = "context_snapshot"
    GROUNDING_RECORD = "grounding_record"
    QUESTION_SELECTION_RECORD = "question_selection_record"
    AUTHORITATIVE_RESULT_SELECTION = "authoritative_result_selection"
    EXECUTION_COVERAGE = "execution_coverage"
    PROFILE_ELIGIBILITY_EVALUATION = "profile_eligibility_evaluation"
    RULE_SET_APPLICABILITY_EVALUATION = "rule_set_applicability_evaluation"
    RULE_APPLICABILITY_EVALUATION = "rule_applicability_evaluation"
    APPLICABILITY_TRACE = "applicability_trace"
    UNRESOLVED_OBLIGATION = "unresolved_obligation"
    REPLAY_RUN = "replay_run"
    EXECUTION_EVENT = "execution_event"
    CANDIDATE_PREDICTION = "candidate_prediction"
    OBSERVATION_BATCH = "observation_batch"
    TOOL_PLAN_RUN_SUMMARY = "tool_plan_run_summary"


@unique
class TaskStatus(WireEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@unique
class ExecutionStatus(WireEnum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL_FAILED = "partial_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@unique
class NodeRunStatus(WireEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@unique
class AttemptStatus(WireEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@unique
class FactStatus(WireEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    NOT_EVALUATED = "not_evaluated"


@unique
class DecisionResult(WireEnum):
    VIOLATION = "violation"
    NO_VIOLATION = "no_violation"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"
