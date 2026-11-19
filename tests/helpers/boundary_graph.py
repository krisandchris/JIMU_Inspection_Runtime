"""Resolve explicit local imports without importing or executing inspected code."""

import ast
import sys
from pathlib import Path


def _bound_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.name if isinstance(node, ast.TypeAlias) else node.target]
            )
            for target in targets:
                names.update(
                    part.id for part in ast.walk(target) if isinstance(part, ast.Name)
                )
    return names


def import_graph(source_root: Path, package="jimu_runtime"):
    """Reject unresolved local imports and forbidden dependencies; return real edges."""
    trees = {}
    for path in sorted((source_root / package).rglob("*.py")):
        module = ".".join(path.relative_to(source_root).with_suffix("").parts)
        module = module.removesuffix(".__init__")
        assert module not in trees, f"ambiguous local module: {module}"
        trees[module] = ast.parse(path.read_text())
    assert package in trees, f"missing package: {package}"
    symbols = {name: _bound_names(tree) for name, tree in trees.items()}
    graph = {name: set() for name in trees}

    def add_local(owner, dependency):
        assert dependency in trees, f"unresolved local import: {dependency}"
        # Track explicit content dependencies, not implicit initializer edges:
        # a package reexporting A while A imports sibling B is not an A/package
        # cycle. A self-qualified access to a symbol also adds no synthetic edge.
        if dependency != owner:
            graph[owner].add(dependency)

    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "Any", "normative contracts may not use unbounded Any"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "__import__"}
            if isinstance(node, ast.Import):
                imports = [(alias.name, None) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, (
                    "unresolved relative imports hide dependency edges"
                )
                imports = [(node.module or "", node.names)]
            else:
                continue
            for imported, aliases in imports:
                top = imported.split(".")[0]
                assert top in sys.stdlib_module_names | {
                    "pydantic",
                    "pydantic_core",
                    package,
                    "rfc8785",
                    "jsonschema",
                    "sqlalchemy",
                    "httpx",
                    "PIL",
                    "fastapi",
                    "starlette",
                    "psycopg",
                }, f"forbidden dependency: {imported}"
                if top == "rfc8785":
                    assert module in {
                        f"{package}.domain.fact_schemas.canonicalization",
                        f"{package}.domain.fact_schemas.projection",
                        f"{package}.bundle.integrity",
                    }, f"rfc8785 import outside canonicalization modules: {module}"
                if top == "jsonschema":
                    assert module in {
                        f"{package}.bundle.artifacts",
                        f"{package}.bundle.schema_validation",
                    }, f"jsonschema import outside Bundle schema validation: {module}"
                if top == "httpx":
                    assert module == f"{package}.gateway.http", (
                        f"httpx import outside the trusted HTTP adapter: {module}"
                    )
                if top in {"fastapi", "starlette"}:
                    assert module.startswith(f"{package}.api.") or module == (
                        f"{package}.deployment.service"
                    ), f"ASGI framework import outside HTTP composition: {module}"
                if top == "psycopg":
                    assert module == f"{package}.deployment.database", (
                        f"native PostgreSQL import outside database bootstrap: {module}"
                    )
                if top == "PIL":
                    assert module in {
                        f"{package}.inspection.media.decoder",
                        f"{package}.inspection.geometry._cpu",
                    }, (
                        f"Pillow import outside the image decoder or isolated CPU tool: {module}"
                    )
                if top == "sqlalchemy":
                    assert module in {
                        f"{package}.workflow.postgres",
                        f"{package}.workflow.intake",
                        f"{package}.workflow.repository",
                        f"{package}.workflow.jobs",
                        f"{package}.workflow.worker",
                        f"{package}.workflow.worker_state",
                        f"{package}.workflow.worker_schedule",
                        f"{package}.workflow.worker_completion",
                        f"{package}.workflow.worker_poll",
                        f"{package}.gateway.journal",
                        f"{package}.gateway.bridge",
                        f"{package}.gateway.bridge_tools",
                        f"{package}.workflow.worker_capabilities",
                        f"{package}.workflow.worker_inspection",
                        f"{package}.workflow.worker_request_evidence",
                        f"{package}.tools.step_attempts",
                        f"{package}.inspection.media.persistence",
                        f"{package}.inspection.media.retention",
                        f"{package}.replay.artifact",
                        f"{package}.replay.artifact_journal",
                        f"{package}.replay.artifact_source",
                        f"{package}.replay.inference",
                        f"{package}.replay.inference_repository",
                        f"{package}.evaluation.history",
                        f"{package}.api.app",
                        f"{package}.api.records",
                        f"{package}.api.boundary",
                        f"{package}.observability.serve",
                        f"{package}.observability.postgres",
                        f"{package}.deployment.database",
                        f"{package}.deployment.service",
                    }, (
                        f"sqlalchemy import outside approved PostgreSQL modules: {module}"
                    )
                if top != package:
                    continue
                assert imported in trees, f"unresolved local import: {imported}"
                if aliases is None:
                    add_local(module, imported)
                else:
                    for alias in aliases:
                        assert alias.name != "*", (
                            "local star imports hide reexport edges"
                        )
                        child = f"{imported}.{alias.name}"
                        if child in trees:
                            add_local(module, child)
                        else:
                            assert alias.name in symbols[imported], (
                                f"unresolved local symbol: {child}"
                            )
                            add_local(module, imported)
    _assert_domain_independence(graph, package)
    _assert_no_dependency_on_layer(graph, package, "bundle", "workflow")
    return graph


def _assert_domain_independence(graph, package):
    for target_layer in (
        "bundle",
        "workflow",
        "gateway",
        "tools",
        "workers",
        "inspection",
        "replay",
        "evaluation",
        "api",
        "deployment",
        "observability",
    ):
        _assert_no_dependency_on_layer(graph, package, "domain", target_layer)


def _assert_no_dependency_on_layer(graph, package, source_layer, target_layer):
    source = f"{package}.{source_layer}"
    target = f"{package}.{target_layer}"
    pending = [
        name for name in graph if name == source or name.startswith(f"{source}.")
    ]
    seen = set(pending)
    while pending:
        owner = pending.pop()
        for dependency in graph[owner]:
            assert dependency != target and not dependency.startswith(f"{target}."), (
                f"{source_layer.title()} import into {target_layer.title()}: "
                f"{owner} -> {dependency}"
            )
            if dependency not in seen:
                seen.add(dependency)
                pending.append(dependency)


def assert_acyclic(graph):
    active, done = set(), set()

    def visit(module):
        assert module not in active, f"circular dependency: {module}"
        if module in done:
            return
        active.add(module)
        for dependency in graph[module]:
            visit(dependency)
        active.remove(module)
        done.add(module)

    for module in graph:
        visit(module)
