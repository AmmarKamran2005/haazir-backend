# HAAZIR API
#
# Single stage. The dependency install is its own layer above the source copy, so editing a
# router rebuilds in seconds instead of reinstalling asyncpg and its build chain every time.

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl is here for the healthcheck below and nothing else.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . "sentry-sdk[fastapi]>=2.20"

COPY alembic.ini ./
COPY alembic ./alembic

# Never run as root. A container that only serves HTTP has no reason to be able to write to
# its own image.
RUN useradd --create-home --uid 10001 haazir && chown -R haazir:haazir /app
USER haazir

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# One worker on purpose. `services/realtime.py` holds SSE subscribers in an in-process dict,
# so a second worker would serve half the subscribers a state change the other half never
# sees. Scaling past one instance means moving the hub to Redis first (§8).
CMD ["uvicorn", "haazir.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
