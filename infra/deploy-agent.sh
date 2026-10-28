#!/usr/bin/env bash
# Deploys Redis and the agent runtime into k3s (P1.3b): builds the agent image, imports it into k3s's containerd,
# puts config/PROFILE.yaml into the ConfigMap agent-config and applies both manifests. Runs after
# infra/install-gateway.sh, since the agent sends every LLM call through the Gateway.
# Idempotent: an image whose tag is already in k3s is not built again, and kubectl apply converges.
# Usage: deploy-agent.sh PROFILE
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PROFILE   (real | sim | demo)" >&2
  exit 2
fi

source "$(dirname "${BASH_SOURCE[0]}")/profile.sh"

# Fails here, before the build, if the profile does not exist.
profile_value "$1" .agent.tool_choice >/dev/null
config=$REPO_ROOT/config/$1.yaml

# The first deploy pulls the Redis image (~40 MB); the agent image is already local. 180 s covers that pull on a
# slow connection, a cached rollout is Ready in seconds.
ROLLOUT_TIMEOUT=180s

# The build context holds exactly what goes into the image, nothing else from the repo: the runtime's modules, its
# dependencies exported from uv.lock with their hashes, and the schemas it validates against (D12).
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/agent_runtime" "$work/schemas"
cp "$REPO_ROOT"/agent/agent_runtime/*.py "$work/agent_runtime/"
cp "$REPO_ROOT"/schemas/*.json "$work/schemas/"
cp "$REPO_ROOT/agent/Dockerfile" "$work/"
# --no-header: the header names the output path, a new temp directory each time, which would change the tag.
(cd "$REPO_ROOT" && uv export --frozen --package agent --no-dev --no-emit-workspace --no-header --quiet \
  --output-file "$work/requirements.txt")

# The tag is a hash of the build context, so a change to the code, a dependency, a schema or the Dockerfile gives a
# new tag, and a new tag rolls out a new pod.
tag=$(cd "$work" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
image=thesis/agent:$tag

if sudo k3s crictl inspecti "$image" >/dev/null 2>&1; then
  echo "deploy-agent: $image is already in k3s"
else
  docker build --tag "$image" "$work"
  # k3s does not share Docker's image store. The image is ~220 MB, far from the 9 GB vLLM import that crashed k3s
  # (P0.7, experiments/log.md).
  docker save "$image" | sudo k3s ctr images import -
fi

kubectl apply -f "$REPO_ROOT/infra/manifests/redis.yaml"

kubectl create configmap agent-config --from-file=profile.yaml="$config" --dry-run=client -o yaml | kubectl apply -f -
config_hash=$(sha256sum "$config" | cut -c1-12)
IMAGE=$image CONFIG_HASH=$config_hash yq '
  (select(.kind == "Deployment") | .spec.template.spec.containers[0].image) = strenv(IMAGE) |
  (select(.kind == "Deployment") | .spec.template.metadata.annotations["thesis/config-hash"]) = strenv(CONFIG_HASH)
' "$REPO_ROOT/infra/manifests/agent.yaml" | kubectl apply -f -

kubectl rollout status deployment/redis --timeout "$ROLLOUT_TIMEOUT"
kubectl rollout status deployment/agent --timeout "$ROLLOUT_TIMEOUT"
kubectl get pods -l 'app.kubernetes.io/name in (redis, agent)' -o wide
