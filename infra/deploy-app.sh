#!/usr/bin/env bash
# Deploys the app backend into k3s (P2.10b): builds its image, imports it into k3s's containerd, puts
# config/PROFILE.yaml into the ConfigMap app-config and applies infra/manifests/app.yaml (claim, Deployment, Service).
# The build steps are infra/deploy-agent.sh's, for the app's files.
# Idempotent: an image whose tag is already in k3s is not built again, and kubectl apply converges; the claim and its
# data are left as they are.
# Usage: deploy-app.sh PROFILE
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PROFILE   (real | sim | demo)" >&2
  exit 2
fi

source "$(dirname "${BASH_SOURCE[0]}")/profile.sh"

# Fails here, before the build, if the profile does not exist or has no app section.
profile_value "$1" .app.session_ttl_seconds >/dev/null
config=$REPO_ROOT/config/$1.yaml

# The image is local and the volume is a directory on the node, so a rollout is Ready in seconds; 180 s is the
# agent's timeout and leaves a wide margin for the first volume binding.
ROLLOUT_TIMEOUT=180s

# The build context holds exactly what goes into the image, nothing else from the repo: the backend's modules and its
# dependencies exported from uv.lock with their hashes.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/app_backend"
cp "$REPO_ROOT"/app/app_backend/*.py "$work/app_backend/"
cp "$REPO_ROOT/app/Dockerfile" "$work/"
# --no-header: the header names the output path, a new temp directory each time, which would change the tag.
(cd "$REPO_ROOT" && uv export --frozen --package app --no-dev --no-emit-workspace --no-header --quiet \
  --output-file "$work/requirements.txt")

# The tag is a hash of the build context, so a change to the code, a dependency or the Dockerfile gives a new tag,
# and a new tag rolls out a new pod.
tag=$(cd "$work" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
image=thesis/app:$tag

if sudo k3s crictl inspecti "$image" >/dev/null 2>&1; then
  echo "deploy-app: $image is already in k3s"
else
  docker build --tag "$image" "$work"
  # k3s does not share Docker's image store. The image is a slim Python with FastAPI, uvicorn and PyYAML, far from
  # the 9 GB vLLM import that crashed k3s (P0.7, experiments/log.md).
  docker save "$image" | sudo k3s ctr images import -
fi

kubectl create configmap app-config --from-file=profile.yaml="$config" --dry-run=client -o yaml | kubectl apply -f -
config_hash=$(sha256sum "$config" | cut -c1-12)
IMAGE=$image CONFIG_HASH=$config_hash yq '
  (select(.kind == "Deployment") | .spec.template.spec.containers[0].image) = strenv(IMAGE) |
  (select(.kind == "Deployment") | .spec.template.metadata.annotations["thesis/config-hash"]) = strenv(CONFIG_HASH)
' "$REPO_ROOT/infra/manifests/app.yaml" | kubectl apply -f -

kubectl rollout status deployment/app --timeout "$ROLLOUT_TIMEOUT"
kubectl get pods -l app.kubernetes.io/name=app -o wide
kubectl get pvc app-data
