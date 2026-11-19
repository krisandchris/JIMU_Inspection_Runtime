"""An isolated HTTP provider with an independent transcript and no access to gold."""

import asyncio
import base64
import binascii
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

from cases import CATEGORY, PROFILE, QUESTION_IDS, Box, Frame, ModelStimulus

type Role = Literal["discovery", "grounding", "observation", "candidate"]


@dataclass(frozen=True, slots=True)
class Request:
    role: Role
    request_id: str
    request_digest: str
    image_digest: str
    box: Box | None
    object_id: str | None
    prompt_digest: str
    response_contract_digest: str


@dataclass(frozen=True, slots=True)
class Reply:
    request: Request
    status: int
    body: bytes | None = field(repr=False)

    @property
    def response_digest(self) -> str | None:
        return None if self.body is None else sha256(self.body).hexdigest()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected wire object")
    return value


def _box(value: object) -> Box:
    raw = _object(value)["xyxy"]
    if (
        not isinstance(raw, list)
        or len(raw) != 4
        or not all(
            type(item) in {int, float} and float(item).is_integer() for item in raw
        )
    ):
        raise ValueError("expected integral fixture geometry in the model prompt")
    x0, y0, x1, y1 = map(int, raw)
    if not (0 <= x0 < x1 <= 64 and 0 <= y0 < y1 <= 48):
        raise ValueError("model request box exceeds the original source frame")
    return x0, y0, x1, y1


class ParkingProvider:
    def __init__(
        self,
        stimulus: ModelStimulus,
        frame: Frame,
        *,
        model_id: str,
        deployment_version: str,
        model_version: str,
    ) -> None:
        self.stimulus = stimulus
        self.frame = frame
        self.model_id = model_id
        self.deployment_version = deployment_version
        self.model_version = model_version
        self.requests: list[Request] = []
        self.replies: list[Reply] = []
        self.errors: list[str] = []
        self._handlers: set[asyncio.Task[None]] = set()
        self._counter: Counter[tuple[Role, Box | None]] = Counter()
        self.release_delayed = asyncio.Event()
        self.url: str | None = None

    @asynccontextmanager
    async def serving(self) -> AsyncIterator["ParkingProvider"]:
        server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        socket = server.sockets[0]
        self.url = f"http://127.0.0.1:{socket.getsockname()[1]}/v1/chat/completions"
        async with server:
            try:
                yield self
            finally:
                self.release_delayed.set()
                if self._handlers:
                    await asyncio.wait_for(
                        asyncio.gather(*tuple(self._handlers)),
                        timeout=3,
                    )

    def _parse(self, request: bytes) -> Request:
        envelope = _object(json.loads(request))
        if envelope["model"] != self.model_id:
            raise ValueError(
                "outbound model ID does not match trusted loopback registration"
            )
        messages = envelope["messages"]
        if not isinstance(messages, list) or len(messages) != 1:
            raise ValueError("expected exactly one request turn")
        message = _object(messages[0])
        parts = message["content"]
        if message["role"] != "user" or not isinstance(parts, list) or len(parts) != 2:
            raise ValueError("expected one application prompt and the original image")
        prompt = _object(parts[0])
        image = _object(parts[1])
        image_url = _object(image["image_url"])["url"]
        if (
            prompt["type"] != "text"
            or image["type"] != "image_url"
            or not isinstance(image_url, str)
            or not image_url.startswith("data:image/png;base64,")
        ):
            raise ValueError("original is absent from the actual HTTP wire")
        image_bytes = base64.b64decode(image_url.split(",", 1)[1], validate=True)
        if image_bytes != self.frame.content:
            raise ValueError("physical POST did not use the exact original image")
        text = prompt["text"]
        if not isinstance(text, str):
            raise ValueError("model request lacks a prompt")
        _, model_input_text, instructions_text = text.rsplit("\n", 2)
        model_input = _object(json.loads(model_input_text))
        instructions = _object(json.loads(instructions_text))
        image_input = _object(model_input["image"])
        if image_input.get("sha256") != self.frame.digest or not image_input.get(
            "media_id"
        ):
            raise ValueError("model input metadata does not pin the original")
        contract = _object(_object(envelope["response_format"])["json_schema"])
        if contract.get("strict") is not True:
            raise ValueError("expected a strict HTTP response format")
        schema = _object(contract["schema"])
        props = _object(schema["properties"])
        box = object_id = None
        role: Role
        if "eligible_profile_ids" in instructions:
            role = "discovery"
            if instructions["eligible_profile_ids"] != [PROFILE] or set(props) != {
                "profile_hits"
            }:
                raise ValueError("discovery is not scoped to the eligible Profile")
        elif "profile_id" in instructions:
            role = "grounding"
            if instructions["profile_id"] != PROFILE or set(props) != {"boxes"}:
                raise ValueError("grounding does not use the fixed Profile")
        elif "questions" in instructions:
            role = "observation"
            box = _box(model_input["bbox"])
            object_id = instructions.get("object_id")
            if (
                set(_object(instructions["questions"])) != QUESTION_IDS
                or set(props) != QUESTION_IDS
            ):
                raise ValueError(
                    "observation request is not jointly covering all three slots"
                )
        elif "registered_category_ids" in instructions:
            role = "candidate"
            box = _box(instructions["bbox"])
            object_id = instructions.get("object_id")
            if instructions["registered_category_ids"] != [CATEGORY] or set(props) != {
                "categories"
            }:
                raise ValueError("candidate call has foreign categories")
        else:
            raise ValueError("unrecognized model prompt role")
        if role in {"observation", "candidate"} and (
            not isinstance(object_id, str) or not object_id
        ):
            raise ValueError(
                "object-bound model request does not identify its current Object"
            )
        ordinal = len(self.requests) + 1
        return Request(
            role,
            f"wp05-http-{ordinal}",
            sha256(request).hexdigest(),
            self.frame.digest,
            box,
            object_id,
            sha256(text.encode()).hexdigest(),
            sha256(
                json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def _reply(self, request: Request) -> Reply:
        key = request.role, request.box
        self._counter[key] += 1
        code = 200
        if request.role == "discovery":
            value = {"profile_hits": list(self.stimulus.hits)}
        elif request.role == "grounding":
            if self.stimulus.delay_grounding:
                return Reply(request, 0, None)
            value = {"boxes": [{"xyxy": list(box)} for box in self.stimulus.boxes]}
        elif request.role == "observation":
            assert request.box is not None
            value = self.stimulus.question_reply(request.box)
            if self.stimulus.first_batch_extra_slot and self._counter[key] == 1:
                value["unregistered_probe_slot"] = "must_not_become_fact"
        elif self.stimulus.delay_candidate:
            return Reply(request, 0, None)
        elif self.stimulus.candidate_unavailable:
            code = 503
            value = {"error": {"code": "fixture_candidate_temporarily_unavailable"}}
        else:
            value = {"categories": []}
        if code == 200:
            value = {
                "model": self.model_id,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(value, separators=(",", ":")),
                        },
                    }
                ],
            }
        return Reply(request, code, json.dumps(value, separators=(",", ":")).encode())

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            async with asyncio.timeout(5):
                headers = await reader.readuntil(b"\r\n\r\n")
                method, *fields = headers.split(b"\r\n")
                if method != b"POST /v1/chat/completions HTTP/1.1":
                    raise ValueError("unexpected loopback endpoint method or path")
                lengths = [
                    int(line.split(b":", 1)[1])
                    for line in fields
                    if line.lower().startswith(b"content-length:")
                ]
                if len(lengths) != 1 or not 0 < lengths[0] <= 131072:
                    raise ValueError("unbounded loopback HTTP content length")
                parsed = self._parse(await reader.readexactly(lengths[0]))
                self.requests.append(parsed)
                reply = self._reply(parsed)
                self.replies.append(reply)
            if reply.body is None:
                await self.release_delayed.wait()
                return
            headers = (
                f"HTTP/1.1 {reply.status} Fixture\r\n"
                f"X-Request-Id: {parsed.request_id}\r\n"
                f"X-Jimu-Deployment-Version: {self.deployment_version}\r\n"
                f"X-Jimu-Model-Version: {self.model_version}\r\n"
                f"Content-Length: {len(reply.body)}\r\n"
                "Content-Type: application/json\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(headers.encode("ascii") + reply.body)
            await writer.drain()
        except (
            ValueError,
            KeyError,
            TypeError,
            UnicodeError,
            binascii.Error,
            TimeoutError,
        ) as exc:
            self.errors.append(str(exc))
        except (ConnectionError, asyncio.IncompleteReadError):
            # A local timeout/interrupted client can close its own socket.
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self._handlers.discard(task)
