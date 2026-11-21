# Sourced by every infra script that reads config/<profile>.yaml (D11): infra/local/vllm-common.sh (P0.3) and
# infra/install-monitoring.sh (P1.1). Also node_ip, for the scripts that point a Service with no selector at a
# process on the node: infra/deploy-replayer-metrics.sh (P3.8) and infra/deploy-app.sh (P4.6a).

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# profile_value <profile> <yq path>: prints one value from the profile; fails if the profile or the key is missing.
profile_value() {
  local config=$REPO_ROOT/config/$1.yaml
  if [[ ! -f $config ]]; then
    echo "profile: no profile $config" >&2
    return 1
  fi
  if ! yq -e "$2" "$config"; then
    echo "profile: $2 missing in $config" >&2
    return 1
  fi
}

# node_ip: prints the k3s node's InternalIP. k3s runs one node (P1.0), the WSL VM, so this is where a process on the host
# listens; a WSL restart changes it, and make up writes it again. Fails if the node has none.
node_ip() {
  local ip
  ip=$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
  if [[ -z $ip ]]; then
    echo "profile: the node has no InternalIP" >&2
    return 1
  fi
  echo "$ip"
}
