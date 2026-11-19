"""Shared vocabulary for Tool diagnostics safe to retain in scoped evidence."""

from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolResult,
    NonKnownToolOutputState,
    ReturnedToolAttempt,
    ToolExecution,
)

TOOL_VALIDATION_DIAGNOSTIC = "Tool reported a validation diagnostic."
TOOL_NON_KNOWN_DIAGNOSTIC = "Tool reported a non-known output."
TOOL_INVALID_OUTPUT_DIAGNOSTIC = "Tool output was missing or invalid."
TOOL_UNVERIFIED_VERSION_DIAGNOSTIC = (
    "The actual pinned implementation could not be verified."
)
TOOL_REPORTED_DIAGNOSTIC_CODE = "tool_reported_diagnostic"
TOOL_REPORTED_FAILURE_CODE = "tool_reported_failure"
TOOL_INVALID_OUTPUT_CODE = "invalid_named_output"


def tool_diagnostics_are_normalized(execution: ToolExecution) -> bool:
    reason = execution.implementation_version_unavailable_reason
    if reason is not None and reason != TOOL_UNVERIFIED_VERSION_DIAGNOSTIC:
        return False
    attempt = execution.attempt_result
    if not isinstance(attempt, ReturnedToolAttempt):
        return True
    result = attempt.result
    if (
        isinstance(result, FailedToolResult)
        and result.error.code != TOOL_REPORTED_FAILURE_CODE
    ):
        return False
    if any(
        issue.code != TOOL_REPORTED_DIAGNOSTIC_CODE
        or issue.message != TOOL_VALIDATION_DIAGNOSTIC
        or issue.path
        or issue.related_refs
        for issue in result.diagnostics
    ):
        return False
    for state in result.output_states:
        if not isinstance(state, NonKnownToolOutputState):
            continue
        evidence = state.reason
        if (evidence.reason_code, evidence.message) not in {
            (f"tool_reported_{state.fact_status.value}", TOOL_NON_KNOWN_DIAGNOSTIC),
            (TOOL_INVALID_OUTPUT_CODE, TOOL_INVALID_OUTPUT_DIAGNOSTIC),
        }:
            return False
    return True
