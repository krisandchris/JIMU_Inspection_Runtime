"""Inspect Python imports, not a Runtime Rule AST or workflow graph."""

import tomllib
from pathlib import Path

from helpers.boundary_graph import assert_acyclic, import_graph


def test_runtime_layers_have_a_pure_domain_and_an_acyclic_import_graph():
    assert_acyclic(import_graph(Path("src")))


def test_only_approved_runtime_dependencies_declared():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert project["project"]["dependencies"] == [
        "pydantic>=2.12.5,<2.13",
        "rfc8785>=0.1.4,<0.1.5",
        "jsonschema>=4.25.1,<4.26",
        "sqlalchemy[asyncio]>=2.0.52,<2.0.53",
        "psycopg[binary]>=3.3.5,<3.3.6",
        "httpx>=0.28.1,<0.29",
        "pillow>=12.3.0,<12.4",
        "fastapi>=0.141.1,<0.142",
        "starlette>=1.6.0,<1.7",
        "uvicorn>=0.52.4,<0.53",
    ]
    lock = tomllib.loads(Path("uv.lock").read_text())
    forbidden = {
        "torch",
        "cuda",
        "vllm",
        "sam3",
        "transformers",
        "redis",
        "openai",
    }
    installed = {p["name"] for p in lock["package"]}
    assert not installed & forbidden
    registries = {
        p["source"]["registry"] for p in lock["package"] if "registry" in p["source"]
    }
    assert registries == {"https://pypi.org/simple"}

    canonicalizers = [p for p in lock["package"] if p["name"] == "rfc8785"]
    assert len(canonicalizers) == 1
    assert canonicalizers[0]["version"] == "0.1.4"
    assert not canonicalizers[0].get("dependencies", [])

    schema_validators = [p for p in lock["package"] if p["name"] == "jsonschema"]
    assert len(schema_validators) == 1
    assert schema_validators[0]["version"] == "4.25.1"

    packages = {p["name"]: p for p in lock["package"]}
    assert packages["sqlalchemy"]["version"] == "2.0.52"
    assert packages["psycopg"]["version"] == "3.3.5"
    assert packages["psycopg-binary"]["version"] == packages["psycopg"]["version"]

    assert packages["httpx"]["version"] == "0.28.1"
    assert packages["httpcore"]["version"] == "1.0.9"
    assert packages["h11"]["version"] == "0.16.0"

    assert packages["pillow"]["version"] == "12.3.0"
    assert not packages["pillow"].get("dependencies", [])

    assert packages["fastapi"]["version"] == "0.141.1"
    assert packages["starlette"]["version"] == "1.6.0"
    assert packages["uvicorn"]["version"] == "0.52.4"
