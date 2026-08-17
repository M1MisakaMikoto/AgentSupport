ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

USER root
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY docs ./docs
COPY vendor/trae-agent-src ./vendor/trae-agent-src
ENV PYTHONPATH=/app/src:/app/vendor/trae-agent-src
ARG INSTALL_DEPS=true
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$INSTALL_DEPS" = "true" ]; then pip install ".[trae,distributed]"; fi

USER root
RUN if ! getent group agent > /dev/null; then addgroup --system agent; fi \
    && if ! id agent > /dev/null 2>&1; then adduser --system --ingroup agent agent; fi \
    && mkdir -p /workspace-data \
    && chown -R agent:agent /app /workspace-data
ENV ALEMBIC_CONFIG=/app/alembic.ini
WORKDIR /app/src
USER agent

EXPOSE 8080
CMD ["uvicorn", "session_runner.main:app", "--host", "0.0.0.0", "--port", "8080"]
