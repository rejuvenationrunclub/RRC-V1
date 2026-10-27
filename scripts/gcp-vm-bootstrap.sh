#!/usr/bin/env bash
# One-time setup of a fresh Debian 12 Google Compute Engine VM to run the RRC bot.
# Idempotent: safe to re-run. Usage (on the VM, from the repo checkout):
#   sudo scripts/gcp-vm-bootstrap.sh
#
# Installs: OS updates + automatic security updates, a 1 GB swap file (the
# e2-micro has ~1 GB RAM), Docker Engine + Compose plugin from Docker's official
# apt repository, Docker log rotation (protects the 30 GB disk), and the
# backup directory.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0" >&2; exit 1; }

# shellcheck source=/dev/null
. /etc/os-release
if [ "${ID:-}" != "debian" ]; then
    echo "Warning: written for Debian 12; detected '${ID:-unknown}'. Continuing." >&2
fi
export DEBIAN_FRONTEND=noninteractive

step() { printf '\n==> %s\n' "$*"; }

step "System updates and automatic security updates"
apt-get update -q
apt-get upgrade -y -q
apt-get install -y -q ca-certificates curl git unattended-upgrades
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

step "1 GB swap file"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    if [ ! -f /swapfile ]; then
        fallocate -l 1G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab

step "Docker Engine + Compose plugin"
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
        "https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

step "Docker log rotation"
if [ ! -f /etc/docker/daemon.json ]; then
    install -d /etc/docker
    cat >/etc/docker/daemon.json <<'EOF'
{
  "log-driver": "local",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
    systemctl restart docker
fi
systemctl enable --now docker

step "Backup directory"
install -d -m 700 /var/backups/rrc

step "Done"
docker --version
docker compose version
free -h | sed -n '1,3p'
echo
echo "Next: create .env (docs/DEPLOY_GCP.md step 6), then: sudo docker compose --profile bot up -d --build"
