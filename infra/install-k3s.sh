#!/usr/bin/env bash
# Installs k3s inside WSL2 and makes the GPU available to pods (P0.6).
# k3s runs its own containerd, not Docker's; when it starts it finds nvidia-container-runtime (installed in P0.2)
# on PATH and adds an "nvidia" runtime to its containerd config. The NVIDIA device plugin then advertises the GPU
# as nvidia.com/gpu. If the plugin cannot, pods still get the GPU through the nvidia runtime alone (PLAN.md
# section 4, ladder step 3), so a missing nvidia.com/gpu is reported, not treated as a failure.
# Idempotent: k3s is installed only when the pinned version is missing and restarted only when its config changed;
# kubectl apply and helm upgrade --install converge to the same state on every run.
set -euo pipefail

# k3s: the "stable" channel on 2026-10-26. Device plugin: newest release; reports the WSL2 GPU as one device
# (versions.md, Container and cluster).
K3S_VERSION=v1.36.5+k3s1
DEVICE_PLUGIN_CHART_VERSION=0.20.1
DEVICE_PLUGIN_REPO=https://nvidia.github.io/k8s-device-plugin

K3S_CONFIG=/etc/rancher/k3s/config.yaml
K3S_KUBECONFIG=/etc/rancher/k3s/k3s.yaml
CONTAINERD_CONFIG=/var/lib/rancher/k3s/agent/etc/containerd/config.toml

# The node registers and containerd writes its config a few seconds after k3s starts; kubelet sees the device
# plugin's GPU a few seconds after the plugin pod is Ready. 120 s covers both on this laptop with a wide margin.
WAIT_SECONDS=120

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# write_root_file <path> <content>: writes the file as root only when its content differs; returns 1 if unchanged.
write_root_file() {
  if [[ -f $1 && $(sudo cat "$1") == "$2" ]]; then
    return 1
  fi
  sudo install -d -m 0755 "$(dirname "$1")"
  printf '%s\n' "$2" | sudo tee "$1" >/dev/null
  sudo chmod 0644 "$1"
}

# wait_for <description> <command...>: retries the command every 2 s until it succeeds or WAIT_SECONDS pass.
# On timeout it prints the last attempt's output, so the cause is visible.
wait_for() {
  local description=$1 waited=0 output
  shift
  until output=$("$@" 2>&1); do
    if ((waited >= WAIT_SECONDS)); then
      echo "install-k3s: timed out after ${WAIT_SECONDS}s waiting for $description" >&2
      echo "last output: $output" >&2
      exit 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
}

# Flags live in k3s's config file instead of the install command, so changing one does not need a reinstall.
# Traefik is off: the thesis uses its own gateway (P1.2), and Traefik would take ports 80/443 and memory.
# ServiceLB stays on: it makes LoadBalancer Services reachable on the node IP.
install_k3s() {
  local config_changed=false installed=""
  if write_root_file "$K3S_CONFIG" 'write-kubeconfig-mode: "0644"
disable:
  - traefik'; then
    config_changed=true
  fi
  if command -v k3s >/dev/null; then
    installed=$(k3s --version | awk 'NR == 1 {print $3}')
  fi
  if [[ $installed != "$K3S_VERSION" ]]; then
    # The install script of the pinned tag, not get.k3s.io (which changes); it checks the binary's sha256 itself.
    curl -fsSL "https://raw.githubusercontent.com/k3s-io/k3s/$K3S_VERSION/install.sh" |
      INSTALL_K3S_VERSION=$K3S_VERSION sh -s - server
  elif [[ $config_changed == true ]]; then
    sudo systemctl restart k3s
  elif ! systemctl is-active --quiet k3s; then
    sudo systemctl start k3s
  fi
}

copy_kubeconfig() {
  wait_for "the k3s kubeconfig" test -f "$K3S_KUBECONFIG"
  install -d -m 0700 "$HOME/.kube"
  if [[ ! -f $HOME/.kube/config ]] || ! cmp -s "$K3S_KUBECONFIG" "$HOME/.kube/config"; then
    install -m 0600 "$K3S_KUBECONFIG" "$HOME/.kube/config"
  fi
}

check_nvidia_runtime() {
  wait_for "the k3s containerd config" sudo test -f "$CONTAINERD_CONFIG"
  if ! sudo grep -q nvidia "$CONTAINERD_CONFIG"; then
    echo "install-k3s: no nvidia runtime in $CONTAINERD_CONFIG: nvidia-container-runtime was not on PATH when" \
      "k3s started. Run make docker-gpu, then sudo systemctl restart k3s, then rerun." >&2
    exit 1
  fi
}

# k3s also ships an identical RuntimeClass nvidia; applying ours keeps the cluster from silently depending on that
# addon. Server-side apply takes shared ownership of the existing object without a last-applied warning.
apply_runtimeclass() {
  kubectl apply --server-side -f "$REPO_ROOT/infra/manifests/runtimeclass-nvidia.yaml"
}

install_device_plugin() {
  helm upgrade --install nvidia-device-plugin nvidia-device-plugin \
    --repo "$DEVICE_PLUGIN_REPO" --version "$DEVICE_PLUGIN_CHART_VERSION" \
    --namespace nvidia-device-plugin --create-namespace \
    --values "$REPO_ROOT/infra/helm/device-plugin-values.yaml" \
    --wait
}

allocatable_gpus() {
  kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
}

gpu_is_allocatable() {
  [[ $(allocatable_gpus) -ge 1 ]]
}

report_gpu() {
  local gpus
  if ! kubectl get nodes >/dev/null; then
    echo "install-k3s: Kubernetes API not reachable (kubectl get nodes failed); cannot check nvidia.com/gpu" >&2
    exit 1
  fi
  # The node is reachable, so a timeout here can only mean the plugin never registered the GPU: ladder step 3.
  if (wait_for "allocatable nvidia.com/gpu" gpu_is_allocatable) 2>/dev/null; then
    gpus=$(allocatable_gpus)
    echo "node reports allocatable nvidia.com/gpu: $gpus (ladder step 1: device plugin)"
  else
    echo "node reports no allocatable nvidia.com/gpu after ${WAIT_SECONDS}s: make gpu-check will use ladder step 3" \
      "(runtimeClassName nvidia, no GPU request). Plugin logs: kubectl logs -n nvidia-device-plugin ds/nvidia-device-plugin"
  fi
}

print_versions() {
  echo "== k3s and device plugin versions"
  k3s --version | head -1
  helm list -n nvidia-device-plugin --filter '^nvidia-device-plugin$' --output json |
    jq -r '.[] | "device plugin chart \(.chart), app \(.app_version)"'
}

install_k3s
copy_kubeconfig
wait_for "the k3s node to be Ready" kubectl wait --for=condition=Ready node --all --timeout=2s
check_nvidia_runtime
apply_runtimeclass
install_device_plugin
report_gpu
print_versions
