#!/usr/bin/env bash
# Build the AgentCore Runtime zip for the investigator (direct code deployment: arm64, Python 3.14).
#
#   scripts/build_investigator.sh            -> build/investigator.zip
#
# Terraform (infra/app/investigator.tf, investigator_enabled = true) uploads it to S3 and points
# the Runtime at it. Wheels only (--only-binary), for linux arm64, never this machine's platform.
set -euo pipefail
cd "$(dirname "$0")/.."
out=build/investigator
rm -rf "$out" build/investigator.zip
mkdir -p "$out"
uv export --only-group investigator --no-hashes --no-emit-project > build/investigator-req.txt
uv pip install --quiet --python-platform aarch64-manylinux2014 --python-version 3.14 \
  --only-binary=:all: --target "$out" -r build/investigator-req.txt
cp -r src/agentic_bank "$out/"
cat > "$out/main.py" <<'PY'
from agentic_bank.investigator.runtime import app

app.run()
PY
find "$out" -name __pycache__ -prune -exec rm -rf {} +
chmod -R u=rwX,go=rX "$out"
(cd "$out" && zip -qr ../investigator.zip .)
du -h build/investigator.zip
