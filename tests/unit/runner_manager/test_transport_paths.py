"""Path translation for the WSL Docker transport."""

from pathlib import Path

from runner_manager.manager import RunnerManager
from runner_manager.settings import ManagerSettings


def _manager(transport: str) -> RunnerManager:
    return RunnerManager(
        ManagerSettings(
            platform_url="http://platform",
            docker_transport=transport,
            staging_root=Path("staging"),
        )
    )


def test_wsl_transport_rewrites_windows_paths():
    manager = _manager("wsl")
    translated = manager.transport_path(r"C:\Users\dev\builds\abc")
    assert translated == "/mnt/c/Users/dev/builds/abc"


def test_local_transport_keeps_host_paths(tmp_path: Path):
    manager = _manager("local")
    assert manager.transport_path(tmp_path) == str(tmp_path.resolve())


def test_wsl_argv_prefixes_docker():
    manager = _manager("wsl")
    assert manager.docker("build", "-t", "x", "/mnt/c/tmp") == [
        "wsl",
        "-d",
        "Ubuntu",
        "--",
        "docker",
        "build",
        "-t",
        "x",
        "/mnt/c/tmp",
    ]
