#!/usr/bin/env bash
# Installs the inference gateway (P1.2): the Gateway API and Inference Extension (GAIE) CRDs, agentgateway as the
# Gateway, and llm-d-router's Endpoint Picker with the InferencePool over the vLLM pods. Runs after make vllm-deploy.
# Since GAIE v1.6.0 the full Endpoint Picker lives in llm-d-router; GAIE keeps the InferencePool API (spike
# 2026-10-27, experiments/log.md).
# Idempotent: kubectl apply and helm upgrade --install converge to the same state on every run.
set -euo pipefail

# The newest releases at least two weeks old on 2026-10-27 (versions.md, Container and cluster). Manifests are
# sha256-checked and charts pinned by digest, so a re-pushed tag can't change what make up installs. The charts'
# images come from the chart, as for the monitoring stack (P1.1); only the EPP image is pinned by digest on top
# (infra/helm/router-values.yaml).
GATEWAY_API_VERSION=v1.6.3
GATEWAY_API_SHA256=356c2b8e286a964dc20995435659ad1095386329974793af10bbfe3eff977fe5
GAIE_VERSION=v1.6.2
GAIE_SHA256=f410dfca474608c02903087b47f1ac081706dfb6dfe7f83d00a7cc7e00c4ad0d
# agentgateway v1.6.0
AGENTGATEWAY_CRDS_CHART=oci://cr.agentgateway.dev/charts/agentgateway-crds@sha256:dd7f965d43e54dffa9c95af089b6f762397a23c963a159ad6d4992f6c582292a
AGENTGATEWAY_CHART=oci://cr.agentgateway.dev/charts/agentgateway@sha256:d3edf2b33cbf2e9d59406c207b8d3554597799707b8803b5f62e698fe5376e28
# llm-d-router v0.11.0
ROUTER_CHART=oci://ghcr.io/llm-d/charts/llm-d-router-gateway@sha256:63a79a842974691be73d44918c7086bcc52d6fee546ff39113517a23c03eeab0

AGENTGATEWAY_NAMESPACE=agentgateway-system
# The vLLM pods' namespace: an InferencePool selects pods in its own namespace only.
POOL_NAMESPACE=default
GATEWAY=inference-gateway

# CRDs are API objects only; 60 s turns an API server that never accepts them into a failure instead of a hang.
CRD_TIMEOUT=60s
# The first install pulls the agentgateway controller, its proxy and the EPP (a few hundred MB); 300 s covers that on
# a slow connection, a cached install is Ready in seconds.
INSTALL_TIMEOUT=300s

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# apply_crds <url> <sha256>: applies a release's CRD manifest once its checksum matches (as infra/bootstrap.sh does).
apply_crds() {
  curl -fsSL -o "$work/crds.yaml" "$1"
  echo "$2  $work/crds.yaml" | sha256sum --check --quiet --strict
  # Server-side apply, as the Gateway API install guide says: its CRDs are too large for the last-applied annotation
  # that client-side apply writes.
  kubectl apply --server-side -f "$work/crds.yaml"
}

apply_crds "https://github.com/kubernetes-sigs/gateway-api/releases/download/$GATEWAY_API_VERSION/standard-install.yaml" \
  "$GATEWAY_API_SHA256"
apply_crds "https://github.com/kubernetes-sigs/gateway-api-inference-extension/releases/download/$GAIE_VERSION/v1-manifests.yaml" \
  "$GAIE_SHA256"
kubectl wait --for condition=Established --timeout "$CRD_TIMEOUT" \
  crd/gateways.gateway.networking.k8s.io crd/httproutes.gateway.networking.k8s.io \
  crd/inferencepools.inference.networking.k8s.io

helm upgrade --install agentgateway-crds "$AGENTGATEWAY_CRDS_CHART" \
  --namespace "$AGENTGATEWAY_NAMESPACE" --create-namespace \
  --wait --timeout "$INSTALL_TIMEOUT"
helm upgrade --install agentgateway "$AGENTGATEWAY_CHART" \
  --namespace "$AGENTGATEWAY_NAMESPACE" \
  --values "$REPO_ROOT/infra/helm/agentgateway-values.yaml" \
  --wait --timeout "$INSTALL_TIMEOUT"

# The release name names the InferencePool (vllm-pool) and the EPP (vllm-pool-epp).
helm upgrade --install vllm-pool "$ROUTER_CHART" \
  --namespace "$POOL_NAMESPACE" \
  --values "$REPO_ROOT/infra/helm/router-values.yaml" \
  --wait --timeout "$INSTALL_TIMEOUT"

kubectl apply -f "$REPO_ROOT/infra/manifests/inference-gateway.yaml"
# The controller creates the proxy Deployment once it has accepted the Gateway, so the rollout can only be awaited
# after Programmed.
kubectl wait --for condition=Programmed --timeout "$INSTALL_TIMEOUT" --namespace "$POOL_NAMESPACE" "gateway/$GATEWAY"
kubectl rollout status --timeout "$INSTALL_TIMEOUT" --namespace "$POOL_NAMESPACE" "deployment/$GATEWAY"
