#!/usr/bin/env bash
# Installs the pinned dev tools (P0.0) and prints every version at the end.
# Idempotent: each tool is installed only when its pinned version is missing, so a second run changes nothing.
# Only the apt packages need sudo; everything else goes to ~/.local and ~/.nvm.
set -euo pipefail

# Release versions and the SHA-256 of their linux-amd64 download, as published by each project.
UV_VERSION=0.13.0
UV_SHA256=1468ebd5a5541121837c5a2817b9972ba6090fa6caa3d142620850a47fb75154
GO_VERSION=1.27.2
GO_SHA256=ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5
GOLANGCI_LINT_VERSION=2.14.0
GOLANGCI_LINT_SHA256=ab90aeb7b066f92a33415b638a50fe5344bbb75a0d32ad30cc248d88f81032ab
HELM_VERSION=4.3.0
HELM_SHA256=86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb
K9S_VERSION=0.51.0
K9S_SHA256=c3752ad51a5a4015a113819c4eeb6e55a4d0e4b8e652494797532f6fc8161dd7
YQ_VERSION=4.54.1
YQ_SHA256=8e34fc298390875de416e6a4afcb8cabeceb25d9aa8506c1a2f9353cf702ea5f
# nvm checks the SHA-256 of the Node.js download, npm the integrity of the Angular CLI package,
# and uv the hashes of huggingface_hub from PyPI.
NVM_VERSION=0.40.8
NODE_VERSION=24.21.0
ANGULAR_CLI_VERSION=22.2.2
HUGGINGFACE_HUB_VERSION=2.2.0

# Ubuntu 24.04 packages. Their versions are recorded in versions.md but not pinned, because Ubuntu
# removes old versions from the archive after security updates and a pinned reinstall would fail.
# curl and ca-certificates are needed by the downloads below.
APT_PACKAGES=(git make jq build-essential curl ca-certificates)

OPT_DIR="$HOME/.local/opt"
BIN_DIR="$HOME/.local/bin"
export NVM_DIR="$HOME/.nvm"
PROFILE="$HOME/.profile"
PROFILE_MARKER="# thesis: loads nvm (added by infra/bootstrap.sh)"

# WSL appends the Windows PATH, which has its own npm and ng; the pinned tools must come first.
export PATH="$BIN_DIR:$PATH"

install_apt_packages() {
  local missing=() pkg
  for pkg in "${APT_PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
      missing+=("$pkg")
    fi
  done
  if ((${#missing[@]} > 0)); then
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
  fi
}

# download <url> <sha256> <file>: fails when the checksum does not match.
download() {
  curl -fsSL -o "$3" "$1"
  echo "$2  $3" | sha256sum --check --quiet --strict
}

# link <target>: points ~/.local/bin/<name of target> at target, unless it already does.
link() {
  local path
  path="$BIN_DIR/$(basename "$1")"
  if [[ ! -L $path || $(readlink "$path") != "$1" ]]; then
    ln -sfn "$1" "$path"
  fi
}

# install_tarball <name> <version> <url> <sha256> <path of each binary inside the archive>...
# Unpacks into ~/.local/opt/<name>-<version>; the rename at the end makes a half-finished
# install impossible, so an existing directory always means a complete install.
install_tarball() {
  local name=$1 version=$2 url=$3 sha256=$4
  shift 4
  local dest="$OPT_DIR/$name-$version"
  if [[ ! -d $dest ]]; then
    local work="$WORK_DIR/$name"
    mkdir -p "$work/unpacked"
    download "$url" "$sha256" "$work/archive.tar.gz"
    tar -xzf "$work/archive.tar.gz" -C "$work/unpacked"
    mv "$work/unpacked" "$dest"
  fi
  local binary
  for binary in "$@"; do
    link "$dest/$binary"
  done
}

# install_binary <name> <version> <url> <sha256>: same as install_tarball for a single executable.
install_binary() {
  local name=$1 version=$2 url=$3 sha256=$4
  local dest="$OPT_DIR/$name-$version"
  if [[ ! -d $dest ]]; then
    local work="$WORK_DIR/$name"
    mkdir -p "$work/unpacked"
    download "$url" "$sha256" "$work/unpacked/$name"
    chmod +x "$work/unpacked/$name"
    mv "$work/unpacked" "$dest"
  fi
  link "$dest/$name"
}

install_node() {
  if [[ ! -d $NVM_DIR ]]; then
    git -c advice.detachedHead=false clone --quiet --depth 1 --branch "v$NVM_VERSION" https://github.com/nvm-sh/nvm.git "$NVM_DIR"
  elif [[ $(git -C "$NVM_DIR" describe --tags) != "v$NVM_VERSION" ]]; then
    git -C "$NVM_DIR" fetch --quiet --depth 1 origin tag "v$NVM_VERSION"
    git -C "$NVM_DIR" -c advice.detachedHead=false checkout --quiet "v$NVM_VERSION"
  fi

  # nvm.sh is not written for errexit/nounset, so both are off while nvm runs;
  # the active Node.js version is checked right after.
  set +eu
  source "$NVM_DIR/nvm.sh"
  [[ $(nvm version "$NODE_VERSION") == "v$NODE_VERSION" ]] || nvm install "$NODE_VERSION"
  [[ $(nvm version default) == "v$NODE_VERSION" ]] || nvm alias default "$NODE_VERSION"
  nvm use --silent "$NODE_VERSION"
  set -eu
  if [[ $(node --version) != "v$NODE_VERSION" ]]; then
    echo "bootstrap: Node.js v$NODE_VERSION is not active after nvm" >&2
    exit 1
  fi

  local installed
  installed=$(npm ls --global --depth=0 --json | jq -r '.dependencies["@angular/cli"].version // ""')
  if [[ $installed != "$ANGULAR_CLI_VERSION" ]]; then
    npm install --global "@angular/cli@$ANGULAR_CLI_VERSION"
  fi
}

install_hf() {
  if ! uv tool list | grep -qx "huggingface-hub v$HUGGINGFACE_HUB_VERSION"; then
    uv tool install "huggingface_hub==$HUGGINGFACE_HUB_VERSION"
  fi
}

# ~/.profile from Ubuntu already puts ~/.local/bin first; this adds nvm so node, npm and ng
# resolve to the pinned versions in every login shell.
configure_profile() {
  if ! grep -sqxF "$PROFILE_MARKER" "$PROFILE"; then
    printf '\n%s\n%s\n%s\n' \
      "$PROFILE_MARKER" \
      'export NVM_DIR="$HOME/.nvm"' \
      '. "$NVM_DIR/nvm.sh"' >>"$PROFILE"
  fi
}

print_versions() {
  echo "== Dev tool versions"
  dpkg-query -W -f='${Package} ${Version}\n' "${APT_PACKAGES[@]}"
  git --version
  make --version | sed -n 1p
  jq --version
  yq --version
  python3 --version
  uv --version
  echo "node $(node --version)"
  echo "npm $(npm --version)"
  NG_CLI_ANALYTICS=false ng version
  go version
  golangci-lint version
  helm version --short
  k9s version --short
  hf version
}

install_apt_packages

mkdir -p "$OPT_DIR" "$BIN_DIR"
WORK_DIR=$(mktemp -d "$OPT_DIR/.bootstrap.XXXXXX")
trap 'rm -rf "$WORK_DIR"' EXIT

install_tarball uv "$UV_VERSION" \
  "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" \
  "$UV_SHA256" uv-x86_64-unknown-linux-gnu/uv uv-x86_64-unknown-linux-gnu/uvx
install_tarball go "$GO_VERSION" \
  "https://go.dev/dl/go$GO_VERSION.linux-amd64.tar.gz" \
  "$GO_SHA256" go/bin/go go/bin/gofmt
install_tarball golangci-lint "$GOLANGCI_LINT_VERSION" \
  "https://github.com/golangci/golangci-lint/releases/download/v$GOLANGCI_LINT_VERSION/golangci-lint-$GOLANGCI_LINT_VERSION-linux-amd64.tar.gz" \
  "$GOLANGCI_LINT_SHA256" "golangci-lint-$GOLANGCI_LINT_VERSION-linux-amd64/golangci-lint"
install_tarball helm "$HELM_VERSION" \
  "https://get.helm.sh/helm-v$HELM_VERSION-linux-amd64.tar.gz" \
  "$HELM_SHA256" linux-amd64/helm
install_tarball k9s "$K9S_VERSION" \
  "https://github.com/derailed/k9s/releases/download/v$K9S_VERSION/k9s_Linux_amd64.tar.gz" \
  "$K9S_SHA256" k9s
install_binary yq "$YQ_VERSION" \
  "https://github.com/mikefarah/yq/releases/download/v$YQ_VERSION/yq_linux_amd64" \
  "$YQ_SHA256"

install_node
install_hf
configure_profile
print_versions
