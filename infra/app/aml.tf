# AML alert triage, v1 (docs/aml.md, slice 3). Code raises the alert, a model recommends, code
# checks the model, and a person decides: nothing here closes an alert or contacts anyone.
#
#   aml Lambda (open): rules + snapshot -> PutItem rows into partition CASE#<alert_id> -> start
#   state machine agentic-bank-aml-triage:
#     Review  aml-review Lambda: data-reader session pinned to CASE#<alert_id>, one gpt-oss call
#     Check   aml Lambda: reads the case again itself, triage.check on the model's answer
#     Record  aml Lambda: RECOMMENDATION row, analyst_queue = "aml", status awaiting_analyst
#
# Same table as the bank data (keys customer, sk): the case partition key "CASE#A-0091" can never
# equal a Cognito subject, and IAM LeadingKeys conditions keep each role on its side of that line.
#
# Cost, on demand only: per alert ~15k input + ~1.5k output gpt-oss tokens (~$0.003), ~300 small
# DynamoDB writes (~$0.0004), 4 state transitions and ~15 s of Lambda: well under a cent. Idle: $0.

locals {
  aml_model       = "openai.gpt-oss-120b-1:0" # the baseline that won the evals (docs/aml.md)
  aml_machine_arn = "${format(local.arn, "states")}:stateMachine:agentic-bank-aml-triage"
  # Every attribute the aml Lambda may write: case rows (customer, sk, seq, body) and the queue row.
  aml_attributes = [
    "customer", "sk", "seq", "body", "analyst_queue", "status", "recommendation", "failure",
    "advice", "model_id", "execution", "created_at",
  ]
  # Only partitions that start CASE#A-: never a customer's rows, so never money or PII.
  aml_case_only = {
    "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["CASE#A-*"] }
    "Null"                    = { "dynamodb:LeadingKeys" = "false" }
  }
  aml_grants = [
    {
      Effect = "Allow", Action = "dynamodb:PutItem", Resource = local.table_arn
      Condition = merge(local.aml_case_only, {
        "ForAllValues:StringEquals" = { "dynamodb:Attributes" = local.aml_attributes }
      })
    },
    { Effect = "Allow", Action = "dynamodb:Query", Resource = local.table_arn, Condition = local.aml_case_only },
    { Effect = "Allow", Action = "states:StartExecution", Resource = local.aml_machine_arn },
  ]
  # The reviewer: its own role has NO table rights. It reads only by assuming the data-reader role
  # with a session policy for its one case (shared/data.py session_policy), and calls one model.
  aml_review_grants = [
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = local.data_reader_role_arn },
    { Effect = "Allow", Action = "bedrock:InvokeModel", Resource = "arn:aws:bedrock:us-east-1::foundation-model/${local.aml_model}" },
  ]
}

# The AML zip: build/aml-lambda from scripts/build_aml_lambda.sh (pydantic wheels + a symlink to
# src/agentic_bank, followed here, so the zip always has the current source).
data "archive_file" "aml_lambda" {
  output_file_mode = "0644"
  type             = "zip"
  source_dir       = "${path.module}/../../build/aml-lambda"
  excludes = [
    "**/__pycache__", "**/*.pyc", "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
    "**/*.tfvars", "**/*.tfstate*", "**/.aws", "**/credentials", "**/backend.hcl",
    "**/tfplan", "**/plan.txt", "**/*.log", "**/build", "**/.git",
  ]
  output_path = "${path.module}/build/aml-lambda.zip"
}

# --- the workflow role: invoke the two AML functions, nothing else ---
resource "aws_iam_role" "aml_workflow" {
  name                 = "agentic-bank-app-aml-workflow"
  assume_role_policy   = data.aws_iam_policy_document.workflow_trust.json # states, this account
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "aml_workflow" {
  name = "invoke-aml-steps"
  role = aws_iam_role.aml_workflow.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [for k in ["aml", "aml-review"] : aws_lambda_function.fn[k].arn]
    }]
  })
}

# History keeps the AWS-owned key. It holds the model's answer (txn ids, KYC field names, a
# narrative) but no snapshot: the reviewer reads the case from DynamoDB, not from the input.
resource "aws_sfn_state_machine" "aml" {
  name     = "agentic-bank-aml-triage"
  type     = "STANDARD"
  role_arn = aws_iam_role.aml_workflow.arn
  definition = templatefile("${path.module}/aml.asl.json", {
    worker_arn = aws_lambda_function.fn["aml"].arn
    review_arn = aws_lambda_function.fn["aml-review"].arn
  })
  depends_on = [aws_iam_role_policy.aml_workflow]
}
