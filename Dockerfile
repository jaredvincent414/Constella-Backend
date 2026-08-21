# Constella backend.
#
# Postgres and Redis are NOT in here — docker-compose.yml runs those for local
# development only. A deployment points DATABASE_URL and REDIS_URL at managed
# instances; this image is the API process and nothing else.

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall the world. numpy and
# asyncpg ship wheels for this base image, so no build toolchain is needed.
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache .

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# Non-root: nothing here needs to write to the filesystem.
RUN useradd --create-home --uid 10001 constella
USER constella

EXPOSE 8000

# Migrations are deliberately NOT run here. A container that migrates on start
# races itself the moment you run more than one, and an image that mutates the
# database on boot is one you cannot roll back by redeploying the old tag. Run
# `alembic upgrade head` as a release step instead — see README "Deploying".
#
# No --reload: it watches the filesystem and forks a reloader, neither of which
# belongs in a deployment.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
