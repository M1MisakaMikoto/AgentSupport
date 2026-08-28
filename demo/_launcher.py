import uvicorn
from agentsupport.api import create_app
from agentsupport.config import Settings
from agentsupport.services import AgentSupportService

class StubRuntimeDriver:
    async def start(self, session_id, workspace_path, lease_epoch, workspace_id=None, read_only_mounts=None, runtime_operation_id=None):
        return "local-runner"
    async def stop(self, container_id, *, force=False):
        return True
    async def inspect(self, container_id):
        return {"status": "running"}
    async def endpoint(self, container_id):
        return None

service = AgentSupportService(Settings())
service.runtime_driver = StubRuntimeDriver()
uvicorn.run(create_app(service), host="127.0.0.1", port=8000, log_level="info")