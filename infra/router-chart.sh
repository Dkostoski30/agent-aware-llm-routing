# The chart of the Endpoint Picker and the InferencePool, llm-d-router v0.11.0, pinned by digest (versions.md). Sourced by
# infra/install-gateway.sh and infra/set-routing.sh, so a routing variant cannot run on another chart than make up's.
# shellcheck shell=bash disable=SC2034
ROUTER_CHART=oci://ghcr.io/llm-d/charts/llm-d-router-gateway@sha256:63a79a842974691be73d44918c7086bcc52d6fee546ff39113517a23c03eeab0

# router_chart_path prints the path of a local copy of ROUTER_CHART, and pulls it first if it is not there. make up
# fills it (install-gateway.sh), so a routing switch hours into an experiment reads the disk and not ghcr.io: WSL's DNS
# stopped resolving for minutes on 2026-11-12 and -13, once at make up and once at a switch, and the runs failed (P3.7b,
# experiments/log.md). The copy is named by the digest, so a new pin pulls a new copy, and helm pull checks the digest.
# It is pulled into a temporary folder beside it and renamed into place, so a pull cut off halfway is never taken for a
# copy. helm's own output goes to stderr: stdout is the path.
router_chart_path() {
  local charts=${XDG_CACHE_HOME:-$HOME/.cache}/thesis/charts
  local copy=$charts/llm-d-router-gateway-${ROUTER_CHART##*@sha256:}
  if [[ ! -d $copy ]]; then
    mkdir -p "$charts"
    local pulling
    pulling=$(mktemp -d "$charts/pulling.XXXXXX")
    if ! helm pull "$ROUTER_CHART" --untar --untardir "$pulling" >&2; then
      rm -rf "$pulling"
      return 1
    fi
    mv "$pulling" "$copy"
  fi
  echo "$copy/llm-d-router-gateway"
}
