#!/usr/bin/env bash
# Install gVisor (runsc) from the official apt repository and register it with Docker.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "run as root (sudo)" >&2; exit 1; fi
apt-get update
apt-get install -y apt-transport-https ca-certificates curl gnupg
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update
apt-get install -y runsc
runsc install
systemctl reload docker || systemctl restart docker
docker run --rm --runtime=runsc hello-world
echo "gVisor is installed and registered as the 'runsc' runtime"
