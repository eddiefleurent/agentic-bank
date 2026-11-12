#!/usr/bin/env bash
# Proves the pipeline ceiling: the CI apply role cannot create a role without the workload
# boundary, cannot touch the CI roles, and cannot pass roles to other services.
# Needs credentials that may call iam:SimulatePrincipalPolicy (the owner's admin profile).
set -euo pipefail
acct=$(aws sts get-caller-identity --query Account --output text)
apply="arn:aws:iam::${acct}:role/agentic-bank-ci-apply-v2"
boundary="arn:aws:iam::${acct}:policy/agentic-bank-workload-boundary"
app_role="arn:aws:iam::${acct}:role/agentic-bank-app-test"
fail=0

# check <expected allowed|denied> <label> <action> <resource> [context entries...]
check() {
  local want=$1 label=$2 action=$3 resource=$4
  shift 4
  local got
  got=$(aws iam simulate-principal-policy --policy-source-arn "$apply" \
    --action-names "$action" --resource-arns "$resource" "$@" \
    --query 'EvaluationResults[0].EvalDecision' --output text)
  [ "$got" = "allowed" ] || got=denied
  if [ "$got" = "$want" ]; then echo "PASS  $label ($got)"; else echo "FAIL  $label (want $want, got $got)"; fail=1; fi
}

ctx_boundary=(--context-entries "ContextKeyName=iam:PermissionsBoundary,ContextKeyValues=${boundary},ContextKeyType=string")
ctx_other=(--context-entries "ContextKeyName=iam:PermissionsBoundary,ContextKeyValues=arn:aws:iam::aws:policy/AdministratorAccess,ContextKeyType=string")
pass_to() { echo --context-entries "ContextKeyName=iam:PassedToService,ContextKeyValues=$1,ContextKeyType=string"; }

check denied  "CreateRole without boundary"         iam:CreateRole "$app_role"
check denied  "CreateRole with another boundary"    iam:CreateRole "$app_role" "${ctx_other[@]}"
check allowed "CreateRole with workload boundary"   iam:CreateRole "$app_role" "${ctx_boundary[@]}"
check denied  "CreateRole agentic-bank-ci-plan"     iam:CreateRole "arn:aws:iam::${acct}:role/agentic-bank-ci-plan" "${ctx_boundary[@]}"
check denied  "PutRolePolicy on agentic-bank-ci-apply-v2" iam:PutRolePolicy "$apply"
check denied  "AttachRolePolicy on an app role"     iam:AttachRolePolicy "$app_role"
check denied  "DeleteRolePermissionsBoundary"       iam:DeleteRolePermissionsBoundary "$app_role"
# shellcheck disable=SC2046
check allowed "PassRole to lambda"                  iam:PassRole "$app_role" $(pass_to lambda.amazonaws.com)
# shellcheck disable=SC2046
check denied  "PassRole to ec2"                     iam:PassRole "$app_role" $(pass_to ec2.amazonaws.com)
check allowed "apigateway:POST on /apis"            apigateway:POST "arn:aws:apigateway:us-east-1::/apis"
check denied  "apigateway:POST on /domainnames"     apigateway:POST "arn:aws:apigateway:us-east-1::/domainnames"
# the pipeline builds the dispute workflow but can never edit the PII key's policy.
key=$(aws kms describe-key --key-id alias/agentic-bank-pii --query KeyMetadata.Arn --output text)
check denied  "kms:PutKeyPolicy on the PII key"     kms:PutKeyPolicy "$key"
check allowed "states:CreateStateMachine"           states:CreateStateMachine "arn:aws:states:us-east-1:${acct}:stateMachine:agentic-bank-dispute"
# shellcheck disable=SC2046
check allowed "PassRole to states"                  iam:PassRole "$app_role" $(pass_to states.amazonaws.com)

# The boundary itself: the ceiling of every role the pipeline creates.
boundary_doc=$(aws iam get-policy-version --policy-arn "$boundary" \
  --version-id "$(aws iam get-policy --policy-arn "$boundary" --query Policy.DefaultVersionId --output text)" \
  --query PolicyVersion.Document --output json)
bcheck() {
  local want=$1 label=$2 action=$3 resource=$4 got
  got=$(aws iam simulate-custom-policy --policy-input-list "$boundary_doc" \
    --action-names "$action" --resource-arns "$resource" \
    --query 'EvaluationResults[0].EvalDecision' --output text)
  [ "$got" = "allowed" ] || got=denied
  if [ "$got" = "$want" ]; then echo "PASS  boundary: $label ($got)"; else echo "FAIL  boundary: $label (want $want, got $got)"; fail=1; fi
}
table="arn:aws:dynamodb:us-east-1:${acct}:table/agentic-bank-data"
bcheck allowed "UpdateItem on the data table"        dynamodb:UpdateItem "$table"
bcheck denied  "DeleteTable on the data table"       dynamodb:DeleteTable "$table"
bcheck denied  "AssumeRole agentic-bank-ci-apply-v2"    sts:AssumeRole "$apply"

exit $fail
