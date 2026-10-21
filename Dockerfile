FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels . pytest pytest-asyncio

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/loopsmith

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels LoopSmith pytest pytest-asyncio \
    && rm -rf /wheels

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" loopsmith \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home loopsmith \
    && mkdir -p /workspace /home/loopsmith/.loopsmith \
    && chown -R loopsmith:loopsmith /workspace /home/loopsmith
COPY deploy/healthcheck.py deploy/container.toml /opt/loopsmith/deploy/

USER loopsmith
RUN git config --global --add safe.directory /workspace
WORKDIR /workspace
HEALTHCHECK --interval=5s --timeout=4s --start-period=10s --retries=12 \
    CMD ["python", "/opt/loopsmith/deploy/healthcheck.py"]
CMD ["loopsmith-core"]
