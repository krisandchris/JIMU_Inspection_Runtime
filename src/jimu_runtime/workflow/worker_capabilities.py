"""Persist the trusted capability inputs that reproduce an Execution's pins."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.fact_selectors import ToolContentRef
from jimu_runtime.domain.primitives import Version
from jimu_runtime.domain.runtime.executions import ExecutionRecord
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.gateway.routing import DeploymentOwnership


class ToolOwnershipPin(ContractModel):
    tool_ref: ToolContentRef
    implementation_version: Version
    implementation_proof_ref: TrustPin


class HistoricalCapabilities(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    validation_at: UTCDateTime
    snapshot: CapabilitySnapshot
    ownership: Annotated[tuple[DeploymentOwnership, ...], Field(max_length=1024)] = ()
    tool_ownership: Annotated[tuple[ToolOwnershipPin, ...], Field(max_length=1024)] = ()

    @model_validator(mode="after")
    def verify_ownership(self) -> Self:
        identities = {row.deployment for row in self.ownership}
        if len(identities) != len(self.ownership) or not identities <= {
            entry.identity
            for entry in self.snapshot.model_deployments
            if entry.is_current(self.validation_at)
        }:
            raise ValueError(
                "historical ownership must match the frozen capability registry"
            )
        if len({item.tool_ref for item in self.tool_ownership}) != len(
            self.tool_ownership
        ):
            raise ValueError("historical Tool ownership pins must be unique")
        return self

    def check_execution(self, execution: ExecutionRecord) -> None:
        if (
            self.validation_at > execution.created_at
            or not self.snapshot.is_current(self.validation_at)
            or self.snapshot.runtime_version != execution.pins.runtime_version
            or not {
                (row.tool_ref, row.implementation_version)
                for row in self.tool_ownership
            }
            <= {
                (row.tool_ref, row.implementation_version)
                for row in execution.pins.tool_pins
            }
        ):
            raise ValueError("historical capability snapshot does not match Execution")


def _identity(scope: IdempotencyScope, execution: ExecutionRecord) -> dict[str, str]:
    return {
        "project_id": scope.project_id,
        "client_id": scope.client_id,
        "execution_id": execution.execution_id,
        "input_digest": execution.pins.input_digest,
    }


async def record_historical_capabilities(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution: ExecutionRecord,
    historical: HistoricalCapabilities,
) -> None:
    historical.check_execution(execution)
    body = historical.stable_json()
    await connection.execute(
        text("""
            INSERT INTO jimu_workflow.execution_capabilities
                (project_id, client_id, execution_id, input_digest, record_digest, record)
            VALUES (:project_id, :client_id, :execution_id, :input_digest,
                :record_digest, CAST(:body AS json))
        """),
        {
            **_identity(scope, execution),
            "record_digest": sha256_digest(body.encode("utf-8")),
            "body": body,
        },
    )


async def read_historical_capabilities(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution: ExecutionRecord,
) -> HistoricalCapabilities | None:
    row = (
        await connection.execute(
            text("""
                SELECT input_digest, record_digest, record::text AS record
                FROM jimu_workflow.execution_capabilities
                WHERE project_id = :project_id AND client_id = :client_id
                    AND execution_id = :execution_id
            """),
            _identity(scope, execution),
        )
    ).one_or_none()
    if row is None:
        return None
    if (
        row.input_digest != execution.pins.input_digest
        or row.record_digest != sha256_digest(row.record.encode("utf-8"))
    ):
        raise ValueError("historical capability snapshot failed digest verification")
    historical = HistoricalCapabilities.model_validate_json(row.record)
    historical.check_execution(execution)
    return historical
