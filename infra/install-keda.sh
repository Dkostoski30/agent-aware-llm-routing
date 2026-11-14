#!/usr/bin/env bash
# Installs KEDA (the operator and its metrics API server) into the namespace keda (P3.9a), on every profile. It needs
# nothing from the profile: the ScaledObjects that use it carry the targets (P3.9b). Its CRDs are templates of the
# release, so make down removes them with it.
# Idempotent: helm upgrade --install converges to the same release on every run.
# Usage: install-keda.sh
set -euo pipefail

# Tested on Kubernetes 1.34-1.36 (k3s here is v1.36.5); released 2026-09-23 (versions.md, Container and cluster).
CHART_VERSION=2.21.0
CHART_REPO=https://kedacore.github.io/charts
NAMESPACE=keda

# The first install pulls the operator and metrics server images (about 100 MB each); 300 s covers that on a slow
# connection, a cached install is Ready in seconds.
INSTALL_TIMEOUT=300s

if [[ $# -ne 0 ]]; then
  echo "usage: $0" >&2
  exit 2
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

helm upgrade --install keda keda \
  --repo "$CHART_REPO" --version "$CHART_VERSION" \
  --namespace "$NAMESPACE" --create-namespace \
  --values "$REPO_ROOT/infra/helm/keda-values.yaml" \
  --wait --timeout "$INSTALL_TIMEOUT"
