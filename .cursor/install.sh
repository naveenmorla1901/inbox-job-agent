#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for the Inbox Job Agent.
# Safe to run repeatedly: it never overwrites an existing .env or profile.yaml.
set -euo pipefail

cd "$(dirname "$0")/.."

# System packages: python venv support + lxml build fallbacks (matches Dockerfile).
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3-venv build-essential libxml2-dev libxslt1-dev

# Project virtualenv + pinned dependencies.
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Local config the app expects. Never clobber real credentials/resume if present.
[ -f .env ] || cp .env.example .env
[ -f config/profile.yaml ] || cp config/profile.example.yaml config/profile.yaml
mkdir -p data secrets

echo "install.sh: environment ready"
