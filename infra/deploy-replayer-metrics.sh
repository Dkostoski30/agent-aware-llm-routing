#!/usr/bin/env bash
# Points Prometheus at the replayer's run tracker on the node (P3.8): writes the node's InternalIP and the profile's
# replay.tracker_port into infra/manifests/replayer-metrics.yaml and applies it. make up runs it on every profile, since
# every profile replays, after infra/install-monitoring.sh, whose CRDs define the ServiceMonitor kind. Run it again
# after a WSL restart changed the IP; make up does.
# Idempotent: kubectl apply converges.
# Usage: deploy-replayer-metrics.sh PROFILE
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PROFILE   (real | sim | demo)" >&2
  exit 2
fi

source "$(dirname "${BASH_SOURCE[0]}")/profile.sh"

port=$(profile_value "$1" .replay.tracker_port)
# k3s runs one node (P1.0), the WSL VM, and its InternalIP is the VM's address, where make replay listens.
node_ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
if [[ -z $node_ip ]]; then
  echo "deploy-replayer-metrics: the node has no InternalIP" >&2
  exit 1
fi

# env() reads the port as a number, strenv() the IP as a string.
NODE_IP=$node_ip PORT=$port yq '
  (select(.kind == "Service") | .spec.ports[0].port) = env(PORT) |
  (select(.kind == "Endpoints") | .subsets[0].addresses[0].ip) = strenv(NODE_IP) |
  (select(.kind == "Endpoints") | .subsets[0].ports[0].port) = env(PORT)
' "$REPO_ROOT/infra/manifests/replayer-metrics.yaml" | kubectl apply -f -
echo "deploy-replayer-metrics: Prometheus scrapes the replayer at $node_ip:$port"
