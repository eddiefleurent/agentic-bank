#!/usr/bin/env bash
# Build the source folder of the AML Lambdas' zip: pydantic wheels plus a link to our package.
#
#   scripts/build_aml_lambda.sh            -> build/aml-lambda/   (run before terraform plan)
#
# Terraform (infra/app/aml.tf) zips the folder. agentic_bank is a symlink to src/, so the zip always
# holds the current source; rerun this only when the aml-lambda dependency group changes (or on a
# fresh checkout). Wheels only, for the Lambda platform (x86_64, Python 3.14), never this machine's.
set -euo pipefail
cd "$(dirname "$0")/.."
out=build/aml-lambda
rm -rf "$out"
mkdir -p "$out"
uv export --only-group aml-lambda --no-hashes --no-emit-project > build/aml-lambda-req.txt
uv pip install --quiet --python-platform x86_64-manylinux2014 --python-version 3.14 \
  --only-binary=:all: --target "$out" -r build/aml-lambda-req.txt
find "$out" -name __pycache__ -prune -exec rm -rf {} +
ln -s ../../src/agentic_bank "$out/agentic_bank"
du -sh "$out"
