"""Live bridge projection of one physical N04 regional completion."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, cast

from pydantic import Field, ValidationError

from jimu_runtime.bundle.schema_validation import compile_offline_response_schema
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.gateway.http_wire import strict_json
from jimu_runtime.inspection.perception.region_request import project_region_response

_RECORD_VERSION = "1.0.0"
_MAX_RECORD_BYTES = 64 * 1024 * 1024


class RegionProjectionError(ValueError):
    """Projection or physical call failure with a stable bridge diagnostic."""

    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.status = status


def _json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _summary(value: bytes) -> dict[str, object]:
    return {"bytes": len(value), "sha256": _sha(value)}


class _Metadata(ContractModel):
    execution_id: RecordId
    attempt_number: Annotated[int, Field(ge=1, le=2**31 - 1)]
    release_id: RecordId
    bundle_digest: Digest
    image_sha256: Digest
    prompt: Annotated[str, Field(min_length=1)]
    response_schema: dict[str, object]
    object_ids: Annotated[list[RecordId], Field(min_length=1)]


def _properties(schema: object) -> dict[str, object]:
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or not isinstance(schema.get("properties"), dict)
    ):
        raise RegionProjectionError("region_invalid_response_schema")
    return dict(schema["properties"])


@dataclass(frozen=True)
class RegionProjectionRequest:
    metadata: _Metadata
    projection_object_id: str
    questions_by_object: dict[str, tuple[str, ...]]

    @classmethod
    def from_wire(
        cls,
        metadata: object,
        *,
        logical_schema: dict[str, object],
        image: bytes,
    ) -> RegionProjectionRequest:
        if not isinstance(metadata, dict) or set(metadata) != {
            "jimu_region_request",
            "jimu_projection_object",
        }:
            raise RegionProjectionError("region_invalid_projection_metadata")
        try:
            batch = _Metadata.model_validate(metadata["jimu_region_request"])
        except ValidationError:
            raise RegionProjectionError("region_invalid_batch_metadata") from None
        if (
            len(set(batch.object_ids)) != len(batch.object_ids)
            or not batch.prompt.strip()
        ):
            raise RegionProjectionError("region_invalid_batch_metadata")
        if _sha(image) != batch.image_sha256:
            raise RegionProjectionError("region_source_image_mismatch")
        projection = metadata["jimu_projection_object"]
        if not isinstance(projection, str) or projection not in batch.object_ids:
            raise RegionProjectionError("region_invalid_projection_object")
        root = _properties(batch.response_schema)
        if set(root) != {"regions"}:
            raise RegionProjectionError("region_invalid_response_schema")
        regions = _properties(root["regions"])
        aliases = {
            f"r{index}": object_id
            for index, object_id in enumerate(batch.object_ids, 1)
        }
        if set(regions) != set(aliases):
            raise RegionProjectionError("region_schema_object_mismatch")
        by_object = {
            object_id: _properties(regions[alias])
            for alias, object_id in aliases.items()
        }
        if not all(by_object.values()) or _json(by_object[projection]) != _json(
            _properties(logical_schema)
        ):
            raise RegionProjectionError("region_schema_question_mismatch")
        if ("$defs" in batch.response_schema) != ("$defs" in logical_schema) or _json(
            batch.response_schema.get("$defs")
        ) != _json(logical_schema.get("$defs")):
            raise RegionProjectionError("region_schema_definitions_mismatch")
        try:
            compile_offline_response_schema(
                cast(dict[str, JSONValue], batch.response_schema)
            )
        except ValueError:
            raise RegionProjectionError("region_invalid_response_schema") from None
        return cls(
            batch, projection, {key: tuple(value) for key, value in by_object.items()}
        )

    @property
    def request_id(self) -> str:
        return "region-" + _sha(
            _json([self.metadata.execution_id, self.metadata.attempt_number])
        )

    def project(self, answer: str) -> dict[str, str]:
        try:
            value = strict_json(answer, exact_numbers=True)
            responses = project_region_response(
                value,
                object_ids=self.metadata.object_ids,
                questions_by_object=self.questions_by_object,
            )
        except ValueError:
            raise RegionProjectionError(
                "region_invalid_completion_association", 502
            ) from None
        return {
            object_id: _exact_json(responses.get(object_id, {}))
            for object_id in self.metadata.object_ids
        }


def _exact_json(value: object) -> str:
    """Keep schema-visible JSON numbers exact through projection."""
    if isinstance(value, dict):
        items = (
            _exact_json(key) + ":" + _exact_json(item) for key, item in value.items()
        )
        return "{" + ",".join(items) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_exact_json(item) for item in value) + "]"
    if isinstance(value, Decimal):
        return str(value)
    return _json(value).decode()


class RegionProjectionStore:
    """Use a local write-ahead dispatch marker scoped to one output directory."""

    def __init__(self, directory: Path, *, wait_seconds: float) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sync_directory(directory.parent)
        self.directory = directory
        self.wait_seconds = wait_seconds

    def resolve(
        self,
        request: RegionProjectionRequest,
        *,
        upstream_request: bytes,
        upstream_configuration: dict[str, object],
        invoke: Callable[[dict[str, object]], str],
        audit: dict[str, object],
    ) -> str:
        request_id = request.request_id
        record_path = self.directory / f"{request_id}.json"
        request_evidence = {
            "batch": request.metadata.model_dump(mode="json"),
            "upstream_configuration": upstream_configuration,
            "wire": _summary(upstream_request),
        }
        fingerprint = _sha(_json(request_evidence))
        audit.update(
            physical_request_id=request_id,
            physical_request_record=f"{self.directory.name}/{record_path.name}",
            physical_request_fingerprint=fingerprint,
            projection_object_id=request.projection_object_id,
            physical_http_dispatches=0,
        )
        try:
            with self._lock(request_id):
                previous = self._read(record_path)
                if previous is not None:
                    return self._reuse(previous, request, fingerprint, audit)

                initial = {
                    "schema_version": _RECORD_VERSION,
                    "physical_request_id": request_id,
                    "fingerprint": fingerprint,
                    "physical_request": request_evidence,
                }
                # A crash can land between durable intent and network I/O.
                # A subsequent bridge cannot establish whether the request ran.
                self._write(
                    record_path,
                    {
                        **initial,
                        "state": "dispatch_intent",
                        "physical_http_dispatches": None,
                    },
                )
                audit["physical_http_dispatches"] = 1
                upstream: dict[str, object] = {}
                try:
                    answer = invoke(upstream)
                    projections = request.project(answer)
                except RegionProjectionError as error:
                    audit["physical_request_state"] = "failed"
                    self._write(
                        record_path,
                        {
                            **initial,
                            "state": "failed",
                            "physical_http_dispatches": 1,
                            "physical_response": upstream,
                            "failure": {
                                "code": str(error),
                                "http_status": error.status,
                            },
                        },
                    )
                    raise
                self._write(
                    record_path,
                    {
                        **initial,
                        "state": "completed",
                        "physical_http_dispatches": 1,
                        "physical_response": upstream,
                        "projections": projections,
                    },
                )
                audit["physical_request_state"] = "completed"
                return projections[request.projection_object_id]
        except OSError:
            raise RegionProjectionError(
                "region_projection_storage_error", 503
            ) from None

    def _reuse(
        self,
        record: dict[str, object],
        request: RegionProjectionRequest,
        fingerprint: str,
        audit: dict[str, object],
    ) -> str:
        if (
            record.get("schema_version") != _RECORD_VERSION
            or record.get("physical_request_id") != request.request_id
        ):
            raise RegionProjectionError("region_projection_record_invalid", 503)
        if record.get("fingerprint") != fingerprint:
            raise RegionProjectionError("region_batch_conflict", 409)
        physical_request = record.get("physical_request")
        if (
            not isinstance(physical_request, dict)
            or _sha(_json(physical_request)) != fingerprint
        ):
            raise RegionProjectionError("region_projection_record_invalid", 503)
        state = record.get("state")
        audit["physical_request_state"] = state
        if state == "dispatch_intent":
            raise RegionProjectionError("region_physical_dispatch_unresolved", 409)
        physical_response = record.get("physical_response")
        if record.get("physical_http_dispatches") != 1 or not isinstance(
            physical_response, dict
        ):
            raise RegionProjectionError("region_projection_record_invalid", 503)
        if state == "failed":
            failure = record.get("failure")
            if (
                isinstance(failure, dict)
                and isinstance(failure.get("code"), str)
                and type(failure.get("http_status")) is int
            ):
                raise RegionProjectionError(failure["code"], failure["http_status"])
        elif state == "completed":
            completion = _retained_completion(
                physical_response, physical_request.get("wire")
            )
            projections = record.get("projections")
            try:
                expected = request.project(completion)
            except RegionProjectionError:
                raise RegionProjectionError(
                    "region_projection_record_invalid", 503
                ) from None
            if projections == expected:
                return expected[request.projection_object_id]
        raise RegionProjectionError("region_projection_record_invalid", 503)

    @contextmanager
    def _lock(self, request_id: str) -> Iterator[None]:
        fd = os.open(
            self.directory / f"{request_id}.lock",
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            deadline = time.monotonic() + self.wait_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RegionProjectionError(
                            "region_physical_call_in_flight", 409
                        ) from None
                    time.sleep(min(remaining, 0.025))
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _read(path: Path) -> dict[str, object] | None:
        try:
            with path.open("rb") as stream:
                data = stream.read(_MAX_RECORD_BYTES + 1)
        except FileNotFoundError:
            return None
        try:
            value = strict_json(data) if len(data) <= _MAX_RECORD_BYTES else None
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise RegionProjectionError("region_projection_record_invalid", 503)
        return value

    @staticmethod
    def _write(path: Path, record: dict[str, object]) -> None:
        data = _json(record) + b"\n"
        if len(data) > _MAX_RECORD_BYTES:
            raise RegionProjectionError("region_projection_record_too_large", 503)
        fd, temporary = tempfile.mkstemp(prefix=".region-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    directory = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _retained_completion(
    physical_response: dict[str, object], expected_wire: object
) -> str:
    upstream = physical_response.get("upstream")
    qwen = physical_response.get("qwen")
    if (
        not isinstance(upstream, dict)
        or upstream.get("status") != 200
        or upstream.get("request") != expected_wire
        or not isinstance(upstream.get("response"), dict)
        or not isinstance(qwen, dict)
        or not isinstance(qwen.get("validated_envelope"), dict)
    ):
        raise RegionProjectionError("region_projection_record_invalid", 503)
    content = qwen.get("unaltered_completion_content")
    if not isinstance(content, str) or _summary(content.encode()) != qwen.get(
        "completion_content"
    ):
        raise RegionProjectionError("region_projection_record_invalid", 503)
    return content
