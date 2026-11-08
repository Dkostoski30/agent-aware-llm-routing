# The app backend image (P2.10b). Built only by infra/deploy-app.sh, which assembles the build context:
# app_backend/*.py and requirements.txt exported from uv.lock with hashes.
# The agent's base image (agent/Dockerfile, versions.md): Python 3.12.15-slim, the repo's dev Python.
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# No .pyc files in the image; output goes straight to the pod log instead of sitting in a buffer.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
# --require-hashes: a package whose file does not match uv.lock's hash fails the build.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app_backend/ app_backend/

# No root needed: the backend serves HTTP on an unprivileged port and writes only its SQLite file, on a volume that
# k3s's local-path storage creates writable for every user.
RUN useradd --system --no-create-home app
USER app

CMD ["python", "-m", "app_backend.app"]
