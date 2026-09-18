"""Entry point: ``python -m runner_manager``."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="AgentSupport host-side runner manager")
    parser.add_argument(
        "--host", default=os.getenv("AGENTSUPPORT_MANAGER_HOST", "0.0.0.0")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("AGENTSUPPORT_MANAGER_PORT", "8020"))
    )
    arguments = parser.parse_args()
    uvicorn.run("runner_manager.app:app", host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
