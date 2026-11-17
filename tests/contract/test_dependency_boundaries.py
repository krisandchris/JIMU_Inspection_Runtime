"""Inspect Python imports, not a Runtime Rule AST or workflow graph."""

import ast
import sys
import tomllib
from pathlib import Path


def test_domain_import_graph_is_pure_and_acyclic():
    root = Path("src/jimu_runtime")
    paths = sorted(root.rglob("*.py"))
    graph = {}
    for path in paths:
        module = ".".join(path.relative_to("src").with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        graph[module] = set()
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, (
                    "unresolved relative imports hide dependency edges"
                )
                names = [node.module or ""]
            else:
                names = []
            for imported in names:
                top = imported.split(".")[0]
                assert top in sys.stdlib_module_names | {
                    "pydantic",
                    "pydantic_core",
                    "jimu_runtime",
                }
                if top == "jimu_runtime":
                    graph[module].add(imported)
            if isinstance(node, ast.Name):
                assert node.id != "Any", "normative contracts may not use unbounded Any"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "__import__"}

    active, done = set(), set()

    def visit(module):
        assert module not in active, f"circular dependency: {module}"
        if module in done:
            return
        active.add(module)
        for dependency in graph.get(module, set()):
            visit(dependency)
        active.remove(module)
        done.add(module)

    for module in graph:
        visit(module)


def test_only_cpu_contract_runtime_dependency_declared():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert project["project"]["dependencies"] == ["pydantic>=2.12.5,<2.13"]
    lock = tomllib.loads(Path("uv.lock").read_text())
    forbidden = {
        "torch",
        "cuda",
        "vllm",
        "sam3",
        "transformers",
        "fastapi",
        "sqlalchemy",
        "redis",
        "openai",
        "httpx",
    }
    installed = {p["name"] for p in lock["package"]}
    assert not installed & forbidden
    registries = {
        p["source"]["registry"] for p in lock["package"] if "registry" in p["source"]
    }
    assert registries == {"https://pypi.org/simple"}
