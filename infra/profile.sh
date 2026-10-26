# Sourced by every infra script that reads config/<profile>.yaml (D11): infra/local/vllm-common.sh (P0.3) and
# infra/install-monitoring.sh (P1.1).

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
