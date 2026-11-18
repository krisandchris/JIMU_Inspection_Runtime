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
                }, f"forbidden dependency: {imported}"
                if top == "rfc8785":
                    assert module in {
                        f"{package}.domain.fact_schemas.canonicalization",
                        f"{package}.domain.fact_schemas.projection",
                    }, f"rfc8785 import outside F1: {module}"
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
    return graph


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
