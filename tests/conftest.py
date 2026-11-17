"""Deterministic CPU fixtures and network prohibition for the whole test suite."""

import json
import socket
from pathlib import Path
from typing import cast

import pytest


@pytest.fixture(scope="session")
def foundation_fixtures() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures" / "foundation_contracts.json"
    return cast(dict[str, object], json.loads(path.read_text()))


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network is forbidden in WP-01A tests")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
