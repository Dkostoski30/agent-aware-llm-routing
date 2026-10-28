# The agent runtime image (P1.3b). Built only by infra/deploy-agent.sh, which assembles the build context:
# agent_runtime/*.py, requirements.txt exported from uv.lock with hashes, and schemas/*.json.
# Python 3.12.15 (2026-09-30), the newest 3.12 release at least two weeks old on 2026-10-27; the repo's dev Python is
# 3.12 too (versions.md, Container and cluster).
FROM python:3.12.15-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# No .pyc files in the image; output goes straight to the pod log instead of sitting in a buffer.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
# --require-hashes: a package whose file does not match uv.lock's hash fails the build.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY schemas/ schemas/
COPY agent_runtime/ agent_runtime/

# No root needed: the runtime only serves HTTP on an unprivileged port and talks to the gateway and Redis.
RUN useradd --system --no-create-home agent
USER agent

CMD ["python", "-m", "agent_runtime.app"]
