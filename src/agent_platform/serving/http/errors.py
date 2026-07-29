from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...application.platform_service import ServiceError


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.status_code >= 500 or exc.status_code == 429,
                "operation": f"{request.method} {request.url.path}",
                "correlation_id": request.state.correlation_id,
                "details": exc.details,
            },
        )
