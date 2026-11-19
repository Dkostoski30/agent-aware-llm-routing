#!/usr/bin/env bash
# The Endpoint Picker image built from gateway/ (P3.1), imported into k3s as infra/deploy-agent.sh does for the agent.
# The tag is a hash of every file in gateway/, so a change to the code, go.sum or the Dockerfile gives a new tag, and the
# picker deployed is always the build of the files in the repo.
# Usage: picker-image.sh            builds and imports the image, unless k3s has it already
#        picker-image.sh --values   prints the Helm values naming the tag, nothing else: no Docker and no sudo, so
#                                   infra/set-routing.sh can call it hours into an experiment, after sudo has stopped
#                                   remembering the password (P2.8)
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# registry/repository in infra/helm/router-values.yaml, which the chart joins with a slash.
REPOSITORY=thesis/picker

tag=$(cd "$REPO_ROOT/gateway" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)

case "${1:-}" in
  --values)
    # Double quotes: Helm must read the tag as a string even when its 12 hex digits look like a number.
    TAG=$tag yq -n '.router.epp.image.tag = strenv(TAG) | .router.epp.image.tag style="double"'
    exit 0
    ;;
  "") ;;
  *)
    echo "usage: $0 [--values]" >&2
    exit 2
    ;;
esac

image=$REPOSITORY:$tag
if sudo k3s crictl inspecti "$image" >/dev/null 2>&1; then
  echo "picker-image: $image is already in k3s"
  exit 0
fi
# The first build pulls golang:1.26.8 and downloads the module's dependencies; Docker caches both for the next.
docker build --tag "$image" "$REPO_ROOT/gateway"
# k3s does not share Docker's image store. The image is tens of MB, far from the 9 GB vLLM import that crashed k3s
# (P0.7, experiments/log.md).
docker save "$image" | sudo k3s ctr images import -
