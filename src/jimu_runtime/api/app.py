"""Compose only the local candidate routes with explicit trusted services."""

from fastapi import APIRouter, FastAPI
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.api.admin_routes import mount_administration
from jimu_runtime.api.auth import ApiAuthority
from jimu_runtime.api.boundary import ApiLimits, HttpBoundary, SafeRoute
from jimu_runtime.api.contracts import (
    ActivateRelease,
    ApproveRelease,
    ErrorResponse,
    RegisterRelease,
)
from jimu_runtime.api.evidence_routes import mount_evidence
from jimu_runtime.api.inference_routes import mount_inference, mount_replays
from jimu_runtime.api.ports import (
    Administration,
    ArtifactReplayPort,
    EvidencePort,
    InferenceReplayPort,
)
from jimu_runtime.api.records import RuntimeRecords
from jimu_runtime.api.route_support import Routing
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import ShortText
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.intake import TaskSubmission
from jimu_runtime.domain.runtime.replay import (
    ArtifactReplayRequest,
    InferenceReplayRequest,
)
from jimu_runtime.workflow.intake import IntakeService
from jimu_runtime.workflow.jobs import DurableJobs

_ERRORS = {
    400: "Ambiguous or invalid transport metadata",
    401: "Authentication required",
    403: "Action denied",
    404: "Resource unavailable",
    408: "Request body deadline exceeded",
    409: "State or identity conflict",
    410: "Evidence expired",
    413: "Byte budget exceeded",
    414: "Query byte budget exceeded",
    415: "Unsupported content type",
    422: "Invalid request contract",
    500: "Runtime request failed",
    503: "Runtime temporarily unavailable",
}

DEFAULT_LIMITS = ApiLimits()


def create_app(
    engine: AsyncEngine,
    intake: IntakeService,
    *,
    authority: ApiAuthority,
    administration: Administration | None = None,
    artifact_replay: ArtifactReplayPort | None = None,
    inference_replay: InferenceReplayPort | None = None,
    evidence: EvidencePort | None = None,
    limits: ApiLimits = DEFAULT_LIMITS,
) -> FastAPI:
    """Return an ASGI app. The host owns service lifecycle, bind address and credentials."""
    app = LocalApi(
        title="JIMU Inspection Runtime",
        version="1.0.0",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )
    router = APIRouter(
        prefix="/api/v1",
        route_class=SafeRoute,
        responses={
            status: {"description": detail, "model": ErrorResponse}
            for status, detail in _ERRORS.items()
        },
    )
    app.state.api_limits = limits
    boundary = HttpBoundary(authority, limits)
    routes = Routing(boundary, RuntimeRecords(engine))
    mount_inference(router, routes, intake, DurableJobs(engine))
    mount_replays(router, routes, artifact=artifact_replay, inference=inference_replay)
    schemas: list[type[ContractModel]] = [TaskSubmission]
    if administration is not None:
        mount_administration(router, boundary, administration)
        schemas.extend((RegisterRelease, ApproveRelease, ActivateRelease))
    if artifact_replay is not None:
        schemas.append(ArtifactReplayRequest)
    if inference_replay is not None:
        schemas.append(InferenceReplayRequest)
    if evidence is not None:
        mount_evidence(router, routes, evidence)
    app.include_router(router)
    app.input_models = tuple(schemas)
    return app


class LocalApi(FastAPI):
    input_models: tuple[type[ContractModel], ...] = ()

    def openapi(self) -> dict[str, JSONValue]:
        if self.openapi_schema is not None:
            return self.openapi_schema
        schema = super().openapi()
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["RuntimeBearer"] = {
            "type": "http",
            "scheme": "bearer",
        }
        model_schemas = components.setdefault("schemas", {})
        for model in self.input_models:
            generated = model.model_json_schema(
                ref_template="#/components/schemas/JimuInput_{model}"
            )
            for name, definition in generated.pop("$defs", {}).items():
                key = f"JimuInput_{name}"
                if key in model_schemas and model_schemas[key] != definition:
                    raise ValueError(f"conflicting OpenAPI input component: {key}")
                model_schemas[key] = definition
            model_schemas[f"JimuInput_{model.__name__}"] = generated
        identifier = TypeAdapter[str](RecordId).json_schema()
        replay_key = TypeAdapter[str](ShortText).json_schema()
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                headers = {"X-Jimu-Project": identifier, "X-Jimu-Client": identifier}
                if method == "post" and path.endswith("/bundles"):
                    headers["Idempotency-Key"] = identifier
                if method == "post" and path.endswith("-replay"):
                    headers["Idempotency-Key"] = replay_key
                for name, header_schema in headers.items():
                    operation.setdefault("parameters", []).append(
                        {
                            "name": name,
                            "in": "header",
                            "required": True,
                            "schema": header_schema,
                        }
                    )
                operation["security"] = [{"RuntimeBearer": []}]
        return schema
