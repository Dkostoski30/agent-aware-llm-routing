#!/usr/bin/env bash
# Tears down what make up builds (P1.0): uninstalls its Helm releases, then stops k3s and every container it runs,
# which frees the GPU and the RAM. Kept on purpose: the k3s install, the images in k3s's containerd (the vLLM image
# is ~9 GB; pulling or importing it again is slow and crashed k3s once, P0.7) and the HF model cache. make up
# installs every release again from the scripts. The Gateway API and GAIE CRDs, applied with kubectl, stay; make up
# applies them again unchanged. agentgateway's CRDs are templates of the agentgateway-crds release, so they go with it.
# Idempotent: with k3s already stopped only the stop step runs, and it finds nothing to stop.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Installed with k3s (infra/install-k3s.sh); stops the k3s service, kills its containers and unmounts their volumes.
K3S_KILLALL=/usr/local/bin/k3s-killall.sh

# Pods of a release get the default 30 s grace period; 120 s covers that plus the API calls with a wide margin.
UNINSTALL_TIMEOUT=120s

# The releases are gone by then, so a namespace holds almost nothing; 120 s turns a namespace stuck on a finalizer
# into a failure instead of a hang.
NAMESPACE_DELETE_TIMEOUT=120s

# uninstall_release <release> <namespace>: removes the release and waits until its pods are gone.
uninstall_release() {
  helm uninstall "$1" --namespace "$2" --ignore-not-found --wait --timeout "$UNINSTALL_TIMEOUT"
}

if systemctl is-active --quiet k3s; then
  # Reverse order of make up. The Gateway goes first, while its controller still runs to remove the proxy it created.
  # A cluster from before P1.2 has no agentgateway CRDs, and kubectl delete fails on a kind it does not know.
  if kubectl get crd agentgatewayparameters.agentgateway.dev >/dev/null 2>&1; then
    kubectl delete -f "$REPO_ROOT/infra/manifests/inference-gateway.yaml" --ignore-not-found --wait \
      --timeout "$UNINSTALL_TIMEOUT"
  fi
  uninstall_release vllm-pool default
  uninstall_release agentgateway agentgateway-system
  uninstall_release agentgateway-crds agentgateway-system
  uninstall_release vllm default
  uninstall_release monitoring monitoring
  uninstall_release nvidia-device-plugin nvidia-device-plugin
  kubectl delete namespace agentgateway-system monitoring nvidia-device-plugin --ignore-not-found --wait \
    --timeout "$NAMESPACE_DELETE_TIMEOUT"
else
  echo "down: k3s is not running, no releases to uninstall"
fi

if [[ -x $K3S_KILLALL ]]; then
  "$K3S_KILLALL"
else
  echo "down: $K3S_KILLALL not found, k3s is not installed"
fi
