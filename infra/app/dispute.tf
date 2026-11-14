# Disputes. Code builds facts, a model reviews, grounding checks the model, code decides,
# and only the worker Lambda pays a refund. The customer's narrative is tokenized before any of it.

locals {
  case_writer_role_arn = "arn:aws:iam::${local.account}:role/agentic-bank-app-case-writer"
  case_attributes = [
    "customer", "sk", "case_id", "account_id", "transaction_id", "amount_cents",
    "reason_code", "statement_tok", "answer_tok", "status", "round", "created_at",
    "execution", "task_token", "analyst_queue",
  ]
  writer_role_arn     = "arn:aws:iam::${local.account}:role/agentic-bank-app-data-writer"
  dispute_machine_arn = "${format(local.arn, "states")}:stateMachine:agentic-bank-dispute"
  vault_arn           = "${format(local.arn, "dynamodb")}:table/agentic-bank-vault"
  # Grounding thresholds are not calibrated on this case set; see ADR 0007 for the weak probe result.
  review_model = "nvidia.nemotron-super-3-120b" # the classifier: NVIDIA Nemotron 3 Super (evals/disputes)
  # The other two model consumers stay on gpt-oss-120b: on Nemotron the policy answerer was refused
  # by the grounding check on 6 of 17 answerable questions (11/17 vs 15-16/17) and the Strands
  # investigator cited field names that do not exist in the case file on 7 of 8 cases.
  policy_model       = "openai.gpt-oss-120b-1:0"
  investigator_model = "openai.gpt-oss-120b-1:0"

  # Tools: tokenize (Comprehend, KMS Encrypt, vault PutItem), write the case through the case-writer
  # role, start the workflow and resume it when the customer answers. Still no data-table rights.
  tools_dispute_grants = [
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = local.case_writer_role_arn },
    { Effect = "Allow", Action = "comprehend:DetectPiiEntities", Resource = "*" },
    { Effect = "Allow", Action = "kms:Encrypt", Resource = data.aws_kms_key.pii.arn },
    { Effect = "Allow", Action = "dynamodb:PutItem", Resource = local.vault_arn },
    { Effect = "Allow", Action = "states:StartExecution", Resource = local.dispute_machine_arn },
    # SendTaskSuccess takes a task token, not a resource ARN (AWS authorization reference).
    { Effect = "Allow", Action = "states:SendTaskSuccess", Resource = "*" },
  ]
  dispute_grants = [{ Effect = "Allow", Action = "sts:AssumeRole", Resource = local.writer_role_arn }]
  # The reviewer is a classifier: one model, one guardrail, no data.
  review_grants = [
    { Effect = "Allow", Action = "bedrock:InvokeModel", Resource = "arn:aws:bedrock:us-east-1::foundation-model/${local.review_model}" },
    { Effect = "Allow", Action = "bedrock:ApplyGuardrail", Resource = aws_bedrock_guardrail.grounding.guardrail_arn },
  ]
  # The policy answerer (knowledge.tf): its own model, the same guardrail.
  policy_grants = [
    { Effect = "Allow", Action = "bedrock:InvokeModel", Resource = "arn:aws:bedrock:us-east-1::foundation-model/${local.policy_model}" },
    { Effect = "Allow", Action = "bedrock:ApplyGuardrail", Resource = aws_bedrock_guardrail.grounding.guardrail_arn },
  ]
  analyst_grants = [
    { Effect = "Allow", Action = "dynamodb:Query", Resource = "${local.table_arn}/index/analyst_queue" },
    # UpdateItem: claim the task token on a case (waits.resume). PutItem: AUDIT rows only.
    {
      Effect = "Allow", Action = "dynamodb:UpdateItem", Resource = local.table_arn
      Condition = {
        "ForAllValues:StringEquals" = {
          "dynamodb:Attributes" = ["customer", "sk", "status", "task_token", "analyst_queue", "analyst_reason"]
        }
        "Null" = { "dynamodb:Attributes" = "false" }
      }
    },
    {
      Effect    = "Allow", Action = "dynamodb:PutItem", Resource = local.table_arn
      Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["AUDIT"] } }
    },
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = local.vault_arn },
    { Effect = "Allow", Action = "kms:Decrypt", Resource = data.aws_kms_key.pii.arn },
    # SendTaskSuccess has no resource-level permissions: the task token is the authorization.
    { Effect = "Allow", Action = "states:SendTaskSuccess", Resource = "*" },
  ]
}

# Made in infra/bootstrap by a human: the pipeline may read it, never change its policy.
data "aws_kms_key" "pii" {
  key_id = "alias/agentic-bank-pii"
}

# --- vault: one row per token, the original value KMS-encrypted with context {case_id, token} ---
# No TTL: dispute records are kept (Reg E: 2 years); a bank adds a retention job.
resource "aws_dynamodb_table" "vault" {
  name         = "agentic-bank-vault"
  billing_mode = "PAY_PER_REQUEST"
  point_in_time_recovery { enabled = true } # restore to any second of the last 35 days
  hash_key = "token"

  attribute {
    name = "token"
    type = "S"
  }
}

# --- case-writer: the tools cannot discard the attribute restriction by omitting a session policy ---
# LeadingKeys still comes from the per-customer session. Account trust permits the admin proof.
data "aws_iam_policy_document" "case_writer_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.lambda["tools"].arn, "arn:aws:iam::${local.account}:root"]
    }
  }
}

resource "aws_iam_role" "case_writer" {
  name                 = "agentic-bank-app-case-writer"
  assume_role_policy   = data.aws_iam_policy_document.case_writer_trust.json
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "case_writer" {
  name = "write-case-data"
  role = aws_iam_role.case_writer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
      Resource = aws_dynamodb_table.data.arn
      Condition = {
        "ForAllValues:StringEquals" = { "dynamodb:Attributes" = local.case_attributes }
        "Null"                      = { "dynamodb:Attributes" = "false" }
      }
    }]
  })
}

# --- data-writer: settlement worker only, scoped per customer by its session policy ---
data "aws_iam_policy_document" "writer_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type = "AWS"
      # Only the settlement worker may assume the money writer, never the tools Lambda.
      identifiers = [aws_iam_role.lambda["dispute"].arn]
    }
  }
}

resource "aws_iam_role" "data_writer" {
  name                 = "agentic-bank-app-data-writer"
  assume_role_policy   = data.aws_iam_policy_document.writer_trust.json
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "data_writer" {
  name = "write-bank-data"
  role = aws_iam_role.data_writer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"]
      Resource = aws_dynamodb_table.data.arn
    }]
  })
}

# --- grounding: ApplyGuardrail scores the model's rationale against the case file ---
# Catches invented evidence, not contradictions; code cross-checks facts (decision 0007).
resource "aws_bedrock_guardrail" "grounding" {
  name                      = "agentic-bank-grounding"
  blocked_input_messaging   = "Blocked."
  blocked_outputs_messaging = "Blocked."

  contextual_grounding_policy_config {
    filters_config {
      type      = "GROUNDING"
      threshold = 0.65
    }
    filters_config {
      type      = "RELEVANCE"
      threshold = 0.7
    }
  }
}

resource "aws_bedrock_guardrail_version" "grounding" {
  guardrail_arn = aws_bedrock_guardrail.grounding.guardrail_arn
  description   = "thresholds 0.65 / 0.7"
}

# Prompt-attack filter for the optional red-team flag (proofs/redteam_injection.py --guardrail).
# Nothing in the gateway, the tools Lambda, or the dispute reviewer references this guardrail.
# A later apply creates it; it is not attached to a model call by this stack.
resource "aws_bedrock_guardrail" "prompt_attack" {
  name                      = "agentic-bank-prompt-attack"
  description               = "Optional red-team input filter. Not attached to any model call."
  blocked_input_messaging   = "Blocked."
  blocked_outputs_messaging = "Blocked."

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }
}

resource "aws_bedrock_guardrail_version" "prompt_attack" {
  guardrail_arn = aws_bedrock_guardrail.prompt_attack.guardrail_arn
  description   = "prompt-attack filter, not wired to a model call"
}

# --- the workflow (Step Functions Standard, JSONata) ---
data "aws_iam_policy_document" "workflow_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "workflow" {
  name                 = "agentic-bank-app-workflow"
  assume_role_policy   = data.aws_iam_policy_document.workflow_trust.json
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "workflow" {
  name = "invoke-dispute-steps"
  role = aws_iam_role.workflow.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = [for k in ["dispute", "review"] : aws_lambda_function.fn[k].arn]
    }]
  })
}

# History keeps the AWS-owned key: it holds tokens only, never the narrative (decision 0009).
resource "aws_sfn_state_machine" "dispute" {
  name     = "agentic-bank-dispute"
  type     = "STANDARD"
  role_arn = aws_iam_role.workflow.arn
  definition = templatefile("${path.module}/dispute.asl.json", {
    worker_arn = aws_lambda_function.fn["dispute"].arn
    review_arn = aws_lambda_function.fn["review"].arn
  })
  depends_on = [aws_iam_role_policy.workflow]
}

# --- analyst surface: a function URL with IAM auth; the Lambda also checks the caller's role ---
resource "aws_lambda_function_url" "analyst" {
  function_name      = aws_lambda_function.fn["analyst"].function_name
  authorization_type = "AWS_IAM"
}

# The human analyst. Trust = this account (a bank: Identity Center with MFA, a staff group).
data "aws_iam_policy_document" "analyst_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
  }
}

resource "aws_iam_role" "analyst" {
  name                 = "agentic-bank-app-analyst"
  assume_role_policy   = data.aws_iam_policy_document.analyst_trust.json
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "analyst" {
  name = "call-analyst-url"
  role = aws_iam_role.analyst.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunctionUrl", "lambda:InvokeFunction"]
      Resource = aws_lambda_function.fn["analyst"].arn
    }]
  })
}

output "analyst_url" {
  value = aws_lambda_function_url.analyst.function_url
}
