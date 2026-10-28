#!/usr/bin/env bash
# Installs Prometheus and Grafana (kube-prometheus-stack) into the namespace monitoring (P1.1). Runs before
# make vllm-deploy: the vllm chart's PodMonitor needs the CRDs this chart installs.
# Idempotent: helm upgrade --install converges to the same release on every run.
# Usage: install-monitoring.sh PROFILE
set -euo pipefail

# Newest release at least two weeks old on 2026-10-26 (versions.md, Container and cluster).
CHART_VERSION=92.3.0
CHART_REPO=https://prometheus-community.github.io/helm-charts
NAMESPACE=monitoring

# The first install pulls Prometheus, Grafana, the operator and kube-state-metrics (~600 MB); 600 s covers that on a
# slow connection, a cached install is Ready in about a minute.
INSTALL_TIMEOUT=600s

if [[ $# -ne 1 ]]; then
  echo "usage: $0 PROFILE   (real | sim | demo)" >&2
  exit 2
fi

source "$(dirname "${BASH_SOURCE[0]}")/profile.sh"

interval=$(profile_value "$1" .monitoring.scrape_interval_seconds)

helm upgrade --install monitoring kube-prometheus-stack \
  --repo "$CHART_REPO" --version "$CHART_VERSION" \
  --namespace "$NAMESPACE" --create-namespace \
  --values "$REPO_ROOT/infra/helm/monitoring-values.yaml" \
  --set "prometheus.prometheusSpec.scrapeInterval=${interval}s" \
  --wait --timeout "$INSTALL_TIMEOUT"
