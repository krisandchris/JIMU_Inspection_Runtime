"""Loopback-only fixture for the approved synthetic parking frame."""

import base64
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import socket
from typing import Any

MODEL_ID = "synthetic-local-parking"
PROVIDER_PORT = 18081


@contextmanager
def synthetic_provider(frame: bytes) -> Iterator[None]:
    image = "data:image/png;base64," + base64.b64encode(frame).decode("ascii")
    replies = {
        ("profile_hits",): {"profile_hits": ["nonmotor_vehicle"]},
        ("boxes",): {"boxes": [{"xyxy": [40, 23, 52, 33]}]},
        ("parking_position", "parking_state", "vehicle_type"): {
            "vehicle_type": "electric_bicycle",
            "parking_state": "parked",
            "parking_position": "outside_parking_area",
        },
        ("categories",): {"categories": []},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            try:
                size = int(self.headers["Content-Length"])
                if self.path != "/v1/chat/completions" or not 0 < size <= 131072:
                    raise ValueError
                body = json.loads(self.rfile.read(size))
                contract = body["response_format"]["json_schema"]
                slots = tuple(sorted(contract["schema"]["properties"]))
                if (
                    contract["strict"] is not True
                    or body["model"] != MODEL_ID
                    or body["messages"][0]["content"][1]["image_url"]["url"] != image
                ):
                    raise ValueError
                answer = replies[slots]
                request_id = "synthetic.compose." + slots[0]
            except (KeyError, IndexError, TypeError, ValueError, OSError):
                self.send_error(400)
                return
            response = json.dumps(
                {
                    "id": request_id,
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(answer),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("X-Request-ID", request_id)
            self.send_header("X-Jimu-Deployment-Version", "1.0.0")
            self.send_header("X-Jimu-Model-Version", "1.0.0")
            self.end_headers()
            self.wfile.write(response)

    class LocalServer(HTTPServer):
        request_queue_size = 4

        def get_request(self) -> tuple[socket, tuple[str, int]]:
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

    with LocalServer(("127.0.0.1", PROVIDER_PORT), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()
