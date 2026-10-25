#!/usr/bin/env bash
# Installs Docker Engine and the NVIDIA Container Toolkit inside WSL2 (P0.2), so containers can use the GPU.
# Docker Engine runs in this distro, not Docker Desktop. No NVIDIA driver or CUDA is installed here:
# the GPU libraries come from the Windows driver through /usr/lib/wsl/lib.
# Idempotent: files are written and packages installed only when they differ from what is pinned here,
# and Docker is restarted only when its config changed, so a second run changes nothing.
set -euo pipefail

# Newest versions available on 2026-10-25 (versions.md, Container and cluster).
DOCKER_VERSION=5:29.9.2-1~ubuntu.24.04~noble
CONTAINERD_VERSION=2.4.2-3~ubuntu.24.04~noble
BUILDX_VERSION=0.38.1-1~ubuntu.24.04~noble
NVIDIA_CTK_VERSION=1.20.1-1

PACKAGES=(
  "docker-ce=$DOCKER_VERSION"
  "docker-ce-cli=$DOCKER_VERSION"
  "containerd.io=$CONTAINERD_VERSION"
  "docker-buildx-plugin=$BUILDX_VERSION"
  "nvidia-container-toolkit=$NVIDIA_CTK_VERSION"
  "nvidia-container-toolkit-base=$NVIDIA_CTK_VERSION"
  "libnvidia-container-tools=$NVIDIA_CTK_VERSION"
  "libnvidia-container1=$NVIDIA_CTK_VERSION"
)

# Fingerprints of the apt signing keys, read on 2026-10-25 from the keys served over HTTPS
# (neither vendor's install page lists them). A changed key stops the script instead of being trusted.
DOCKER_KEY_URL=https://download.docker.com/linux/ubuntu/gpg
DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
NVIDIA_KEY_URL=https://nvidia.github.io/libnvidia-container/gpgkey
NVIDIA_KEY_FINGERPRINT=C95B321B61E88C1809C4F759DDCAE044F796ECB0

DOCKER_KEYRING=/etc/apt/keyrings/docker.asc
NVIDIA_KEYRING=/etc/apt/keyrings/nvidia-container-toolkit.asc
DAEMON_JSON=/etc/docker/daemon.json

# systemd starts dockerd; WSL enables it only through /etc/wsl.conf and only after a restart of WSL.
ensure_systemd() {
  if ! grep -qxE 'systemd *= *true' /etc/wsl.conf 2>/dev/null; then
    printf '[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf >/dev/null
    echo "systemd enabled in /etc/wsl.conf: run wsl.exe --shutdown (from Windows or this shell), reopen and rerun"
    exit 0
  fi
  if [[ $(ps -p 1 -o comm=) != systemd ]]; then
    echo "systemd is enabled but not running yet: run wsl.exe --shutdown (from Windows or this shell), reopen and rerun"
    exit 0
  fi
}

# write_root_file <path> <content>: writes the file as root only when its content differs.
write_root_file() {
  if [[ ! -f $1 || $(cat "$1") != "$2" ]]; then
    printf '%s\n' "$2" | sudo tee "$1" >/dev/null
    sudo chmod 0644 "$1"
  fi
}

# install_key <url> <fingerprint> <keyring>: fails unless the download is exactly one key with that fingerprint.
install_key() {
  local key="$WORK_DIR/key.asc" pubs fingerprint
  curl -fsSL -o "$key" "$1"
  pubs=$(GNUPGHOME="$WORK_DIR" gpg --quiet --show-keys --with-colons "$key" | awk -F: '/^pub:/ {n++} END {print n + 0}')
  fingerprint=$(GNUPGHOME="$WORK_DIR" gpg --quiet --show-keys --with-colons "$key" | awk -F: '/^fpr:/ {print $10; exit}')
  if [[ $pubs != 1 || $fingerprint != "$2" ]]; then
    echo "install-docker-gpu: key from $1 has fingerprint $fingerprint ($pubs keys), expected $2" >&2
    exit 1
  fi
  sudo install -d -m 0755 "$(dirname "$3")"
  write_root_file "$3" "$(cat "$key")"
}

configure_repos() {
  install_key "$DOCKER_KEY_URL" "$DOCKER_KEY_FINGERPRINT" "$DOCKER_KEYRING"
  write_root_file /etc/apt/sources.list.d/docker.sources "Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: $DOCKER_KEYRING"

  install_key "$NVIDIA_KEY_URL" "$NVIDIA_KEY_FINGERPRINT" "$NVIDIA_KEYRING"
  # $(ARCH) is expanded by apt, not by the shell.
  write_root_file /etc/apt/sources.list.d/nvidia-container-toolkit.list \
    "deb [signed-by=$NVIDIA_KEYRING] https://nvidia.github.io/libnvidia-container/stable/deb/\$(ARCH) /"
}

install_packages() {
  local missing=() pkg
  for pkg in "${PACKAGES[@]}"; do
    if [[ $(dpkg-query -W -f='${Status} ${Version}' "${pkg%%=*}" 2>/dev/null) != "install ok installed ${pkg#*=}" ]]; then
      missing+=("$pkg")
    fi
  done
  if ((${#missing[@]} > 0)); then
    sudo apt-get update
    # --allow-downgrades puts a package that apt upgrade moved back on its pin.
    # --no-install-recommends leaves out docker-ce-rootless-extras, which the thesis does not use.
    sudo apt-get install -y --allow-downgrades --no-install-recommends "${missing[@]}"
  fi
}

ensure_docker_group() {
  if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
    sudo usermod -aG docker "$USER"
  fi
}

# nvidia-ctk adds the nvidia runtime to daemon.json and rewrites it on every run, even unchanged;
# dockerd reads the file only when it starts, so it is restarted only when the content changed.
configure_runtime() {
  local before="" after
  if [[ -f $DAEMON_JSON ]]; then
    before=$(sudo cat "$DAEMON_JSON")
  fi
  sudo nvidia-ctk --quiet runtime configure --runtime=docker
  after=$(sudo cat "$DAEMON_JSON")
  if [[ $before != "$after" ]]; then
    sudo systemctl restart docker
  fi
}

# Docker Desktop's distro runs in the same WSL2 VM and takes memory from it (versions.md, Machine).
# Skipped when wsl.exe is not reachable (Windows interop off), so the check cannot stop the install.
warn_docker_desktop() {
  local running
  if ! command -v wsl.exe >/dev/null; then
    return
  fi
  running=$(wsl.exe --list --running --quiet | iconv -f UTF-16LE -t UTF-8 | tr -d '\r')
  if grep -qx docker-desktop <<<"$running"; then
    echo "warning: Docker Desktop is running; quit it before measuring, it shares this VM's memory" >&2
  fi
}

print_versions() {
  local names=() pkg
  for pkg in "${PACKAGES[@]}"; do
    names+=("${pkg%%=*}")
  done
  echo "== Docker and NVIDIA Container Toolkit versions"
  dpkg-query -W -f='${Package} ${Version}\n' "${names[@]}"
  sudo docker version --format 'docker client {{.Client.Version}}, server {{.Server.Version}}'
  containerd --version
  nvidia-ctk --version
  sudo docker info --format 'docker runtimes: {{range $name, $_ := .Runtimes}}{{$name}} {{end}}'
}

ensure_systemd

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

configure_repos
install_packages
ensure_docker_group
configure_runtime
warn_docker_desktop
print_versions

if ! id -nG | tr ' ' '\n' | grep -qx docker; then
  echo "this shell is not in group docker yet: run wsl.exe --shutdown (from Windows or this shell), reopen, then run docker without sudo"
fi
