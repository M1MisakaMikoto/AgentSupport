import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"


def imported_modules(package: Path) -> set[str]:
    imports: set[str] = set()
    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        relative = source.relative_to(SRC).with_suffix("")
        source_parts = relative.parts
        package_parts = source_parts[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module_parts = tuple(node.module.split(".")) if node.module else ()
                if node.level:
                    keep = len(package_parts) - (node.level - 1)
                    imports.add(".".join((*package_parts[:keep], *module_parts)))
                elif node.module:
                    imports.add(node.module)
    return imports


def test_session_runner_depends_on_shared_contracts_not_platform_internals():
    imports = imported_modules(SRC / "session_runner")
    assert not {
        name for name in imports if name == "agent_platform" or name.startswith("agent_platform.")
    }


def test_platform_application_does_not_depend_on_outer_layers():
    imports = imported_modules(SRC / "agent_platform" / "application")
    forbidden = ("agent_platform.adapters", "agent_platform.bootstrap", "agent_platform.serving")
    assert not {name for name in imports if name.startswith(forbidden)}


def test_platform_domain_does_not_depend_on_infrastructure_frameworks():
    imports = imported_modules(SRC / "agent_platform" / "domain")
    forbidden = ("fastapi", "httpx", "redis", "sqlalchemy")
    assert not {name for name in imports if name.startswith(forbidden)}
