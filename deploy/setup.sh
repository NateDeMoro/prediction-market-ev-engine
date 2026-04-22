#!/usr/bin/env bash
# First-time VPS setup. Run as root on a fresh Ubuntu 24.04 Hetzner box.
#
#   curl -fsSL https://raw.githubusercontent.com/<YOU>/<REPO>/main/deploy/setup.sh | sudo bash
# or scp this file over and: sudo bash setup.sh
set -euo pipefail

ARB_USER="arb"
REPO_DIR="/home/${ARB_USER}/Arbitrage_Betting"

echo "==> apt update + base packages"
apt-get update -y
apt-get install -y python3 python3-pip git ufw curl

echo "==> create unprivileged user ${ARB_USER}"
if ! id "${ARB_USER}" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "${ARB_USER}"
fi
usermod -aG sudo "${ARB_USER}"

echo "==> basic firewall (SSH only; dashboard reached via Tailscale)"
ufw allow 22/tcp
ufw --force enable

echo "==> install Tailscale"
curl -fsSL https://tailscale.com/install.sh | sh

echo "==> install Python deps (system-wide; this box only runs this project)"
pip3 install --break-system-packages requests flask

echo ""
echo "==> Setup complete. Next steps (run as the ${ARB_USER} user):"
echo ""
echo "  1. sudo tailscale up        # prints a URL — open it, auth the box"
echo "  2. su - ${ARB_USER}         # switch to the arb user"
echo "  3. ssh-keygen -t ed25519    # add the pubkey as a Deploy Key on GitHub"
echo "  4. git clone git@github.com:<YOU>/<REPO>.git ${REPO_DIR}"
echo "  5. scp paper_{trades,settlements,closes}.jsonl from local into ${REPO_DIR}/data/"
echo "  6. create ${REPO_DIR}/.env  (see deploy/env.example)"
echo "  7. sudo cp ${REPO_DIR}/deploy/systemd/*.service /etc/systemd/system/"
echo "  8. sudo systemctl daemon-reload"
echo "  9. sudo systemctl enable --now pinnacle-poller kalshi-poller polymarket-poller ev-dashboard"
echo ""
