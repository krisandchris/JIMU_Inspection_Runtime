"""Synthetic real-module and reexport graph regressions, without executing modules."""

import pytest
from helpers.boundary_graph import assert_acyclic, import_graph


def sources(tmp_path, files):
    for name, content in {"__init__.py": "", **files}.items():
        path = tmp_path / "demo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def test_from_submodule_alias_cycle(tmp_path):
    root = sources(
        tmp_path,
        {"a.py": "from demo import b as other", "b.py": "import demo.a as first"},
    )
    graph = import_graph(root, "demo")
    assert "demo.b" in graph["demo.a"]
    with pytest.raises(AssertionError, match="circular"):
        assert_acyclic(graph)


def test_reexport_cycle(tmp_path):
    root = sources(
        tmp_path,
        {
            "__init__.py": "from demo.a import Thing",
            "a.py": "from demo import Thing\nclass Thing: pass",
        },
    )
    with pytest.raises(AssertionError, match="circular"):
        assert_acyclic(import_graph(root, "demo"))


def test_actual_symbol_is_not_a_submodule_or_fake_self_edge(tmp_path):
    root = sources(
        tmp_path,
        {
            "__init__.py": "from demo.a import Thing",
            "a.py": "class Thing: pass",
            "b.py": "from demo import Thing as Renamed",
        },
    )
    graph = import_graph(root, "demo")
    assert "demo.Thing" not in graph
    assert all(owner not in dependencies for owner, dependencies in graph.items())
    assert graph["demo"] == {"demo.a"}
    assert_acyclic(graph)


def test_package_reexport_and_sibling_import_have_no_initializer_cycle(tmp_path):
    root = sources(
        tmp_path,
        {
            "__init__.py": "from demo.a import Thing",
            "a.py": "from demo import b\nclass Thing: pass",
            "b.py": "answer = 42",
        },
    )
    graph = import_graph(root, "demo")
    assert graph["demo.a"] == {"demo.b"}
    assert_acyclic(graph)


@pytest.mark.parametrize(
    "code",
    [
        "import demo.absent",
        "from demo import absent",
        "from demo import *",
        "from . import absent",
    ],
)
def test_unresolved_and_ambiguous_imports_rejected(tmp_path, code):
    root = sources(tmp_path, {"a.py": code})
    with pytest.raises(AssertionError):
        import_graph(root, "demo")


@pytest.mark.parametrize(
    "code",
    [
        "import torch",
        "from fastapi import FastAPI",
        "import rfc8785",
        "from jsonschema import Draft202012Validator",
        "x: Any",
        "eval('1')",
        "exec('x=1')",
        "__import__('os')",
    ],
)
def test_existing_purity_rules_preserved(tmp_path, code):
    root = sources(tmp_path, {"a.py": code})
    with pytest.raises(AssertionError):
        import_graph(root, "demo")


def test_rfc8785_only_in_f1(tmp_path):
    root = sources(
        tmp_path,
        {
            "domain/__init__.py": "",
            "domain/fact_schemas/__init__.py": "",
            "domain/fact_schemas/projection.py": "import rfc8785",
        },
    )
    assert_acyclic(import_graph(root, "demo"))


def test_direct_symbol_and_type_alias_imports(tmp_path):
    root = sources(
        tmp_path,
        {
            "a.py": "type Items = tuple[str, ...]\nanswer: int = 42",
            "b.py": "from demo.a import Items, answer",
        },
    )
    assert_acyclic(import_graph(root, "demo"))


@pytest.mark.parametrize(
    "code",
    ["from demo.bundle import integrity", "import demo.bundle.integrity as bytes"],
)
def test_domain_cannot_import_bundle(tmp_path, code):
    root = sources(
        tmp_path,
        {
            "domain/__init__.py": "",
            "domain/boundary.py": code,
            "bundle/__init__.py": "",
            "bundle/integrity.py": "pass",
        },
    )
    with pytest.raises(AssertionError, match="Domain import into Bundle"):
        import_graph(root, "demo")


def test_bundle_parser_libraries_are_confined_to_respective_modules(tmp_path):
    root = sources(
        tmp_path,
        {
            "bundle/__init__.py": "",
            "bundle/artifacts.py": "from jsonschema import Draft202012Validator",
            "bundle/integrity.py": "import rfc8785",
        },
    )
    assert_acyclic(import_graph(root, "demo"))
    with pytest.raises(AssertionError, match="jsonschema import outside"):
        import_graph(
            sources(root, {"bundle/integrity.py": "import jsonschema"}), "demo"
        )
    with pytest.raises(AssertionError, match="rfc8785 import outside"):
        import_graph(
            sources(
                root,
                {
                    "bundle/integrity.py": "pass",
                    "bundle/artifacts.py": "import rfc8785",
                },
            ),
            "demo",
        )


def test_package_reexport_cannot_hide_an_indirect_domain_bundle_dependency(tmp_path):
    root = sources(
        tmp_path,
        {
            "__init__.py": "from demo.bundle.integrity import Value",
            "domain/__init__.py": "from demo import Value",
            "bundle/__init__.py": "",
            "bundle/integrity.py": "class Value: pass",
        },
    )
    with pytest.raises(AssertionError, match="Domain import into Bundle"):
        import_graph(root, "demo")


@pytest.mark.parametrize("layer", ["domain", "bundle"])
def test_inner_layers_cannot_import_workflow_through_package_reexport(tmp_path, layer):
    root = sources(
        tmp_path,
        {
            "__init__.py": "from demo.workflow.postgres import Engine",
            f"{layer}/__init__.py": "from demo import Engine",
            "workflow/__init__.py": "",
            "workflow/postgres.py": "class Engine: pass",
        },
    )
    with pytest.raises(AssertionError, match=f"{layer.title()} import into Workflow"):
        import_graph(root, "demo")


@pytest.mark.parametrize(
    "module",
    [
        "domain/database",
        "bundle/store",
        "workflow/providers",
        "workflow/worker_unregistered",
        "api/endpoints",
    ],
)
def test_sqlalchemy_import_is_restricted_to_postgres_workflow_modules(tmp_path, module):
    root = sources(
        tmp_path,
        {
            "workflow/__init__.py": "",
            "workflow/postgres.py": "from sqlalchemy import URL",
            "workflow/repository.py": "from sqlalchemy import text",
            f"{module}.py": "import sqlalchemy.ext.asyncio",
        },
    )
    with pytest.raises(AssertionError, match="sqlalchemy import outside"):
        import_graph(root, "demo")
