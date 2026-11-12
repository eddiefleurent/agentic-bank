# Dispute investigator on AgentCore Runtime. PLANNED, NOT DEPLOYED: everything here has count 0
# until investigator_enabled = true, so merging this file changes nothing in AWS on its own.
#
#   Step Functions Review -> review Lambda (REVIEWER=runtime) -> InvokeAgentRuntime
#     -> Runtime microVM: Strands supervisor + 2 specialists (gpt-oss-120b, not the classifier's
#        Nemotron; see dispute.tf) -> grounding guardrail -> ReviewOutcome -> Decide (code, unchanged)
#
# Why the Lambda stays in front: Step Functions' optimized AgentCore integration targets harnesses
# (invokeHarness), not a custom Runtime, and InvokeAgentRuntime replies with a stream. The Lambda
# reads the stream, checks the reply's shape and turns any error into a failure marker.
#
# Cost: Runtime bills per second of CPU and memory while a session works (I/O wait on the model
# is not billed as CPU), so idle is about $0. Each case: ~9k gpt-oss tokens (~$0.002) + one
# ApplyGuardrail call + a few seconds of Runtime. The S3 object is ~30 MB (cents per month).
#
# Before enabling (not done): build/investigator.zip from scripts/build_investigator.sh; the
# workload boundary (infra/bootstrap) must allow s3:GetObject on the code bucket and logs under
# /aws/bedrock-agentcore/runtimes/*, and the apply role must be able to create the bucket.

variable "investigator_enabled" {
  description = "Deploy the AgentCore Runtime investigator and point the review Lambda at it."
  type        = bool
  default     = false
}

variable "investigator_zip" {
  description = "Path of the Runtime zip built by scripts/build_investigator.sh."
  type        = string
  default     = "../../build/investigator.zip"
}

locals {
  investigator = var.investigator_enabled ? 1 : 0
  # Built from names, not resource references: the data-reader trust lists the Runtime role, so a
  # reference back to the data-reader role would be a dependency cycle.
  data_reader_role_arn = "arn:aws:iam::${local.account}:role/agentic-bank-app-data-reader"
  runtime_arn_any      = "${format(local.arn, "bedrock-agentcore")}:runtime/*"
  investigator_runtime_arn = (
    var.investigator_enabled ? aws_bedrockagentcore_agent_runtime.investigator[0].agent_runtime_arn : ""
  )
  review_investigator_env = var.investigator_enabled ? {
    REVIEWER                 = "runtime"
    INVESTIGATOR_RUNTIME_ARN = local.investigator_runtime_arn
  } : {}
  # The review Lambda may invoke this one Runtime (and its endpoints), nothing else.
  review_investigator_grants = var.investigator_enabled ? [{
    Effect   = "Allow", Action = "bedrock-agentcore:InvokeAgentRuntime"
    Resource = [local.investigator_runtime_arn, "${local.investigator_runtime_arn}/*"]
  }] : []
}

# --- code bucket: private, versioned, the Runtime reads one object by version ---
resource "aws_s3_bucket" "agent_code" {
  count  = local.investigator
  bucket = "agentic-bank-agent-code-${local.account}"
}

resource "aws_s3_bucket_public_access_block" "agent_code" {
  count                   = local.investigator
  bucket                  = aws_s3_bucket.agent_code[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "agent_code" {
  count  = local.investigator
  bucket = aws_s3_bucket.agent_code[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_object" "investigator" {
  count       = local.investigator
  bucket      = aws_s3_bucket.agent_code[0].id
  key         = "investigator/investigator.zip"
  source      = var.investigator_zip
  source_hash = filemd5(var.investigator_zip)
  depends_on  = [aws_s3_bucket_versioning.agent_code]
}

# --- execution role: model + guardrail, read this customer's rows via data-reader, its own logs ---
data "aws_iam_policy_document" "investigator_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["${format(local.arn, "bedrock-agentcore")}:*"]
    }
  }
}

resource "aws_iam_role" "investigator" {
  count                = local.investigator
  name                 = "agentic-bank-app-investigator"
  assume_role_policy   = data.aws_iam_policy_document.investigator_trust.json
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "investigator" {
  count = local.investigator
  name  = "investigate-disputes"
  role  = aws_iam_role.investigator[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Its own model (investigator_model) and the classifier's guardrail; no other model.
      { Effect = "Allow", Action = "bedrock:InvokeModel", Resource = "arn:aws:bedrock:us-east-1::foundation-model/${local.investigator_model}" },
      { Effect = "Allow", Action = "bedrock:ApplyGuardrail", Resource = aws_bedrock_guardrail.grounding.guardrail_arn },
      # History: only through data-reader, whose session policy pins LeadingKeys to the customer.
      # No table rights of its own, so no write path exists from the agent.
      { Effect = "Allow", Action = "sts:AssumeRole", Resource = local.data_reader_role_arn },
      { Effect = "Allow", Action = "s3:GetObject*", Resource = "${aws_s3_bucket.agent_code[0].arn}/investigator/*" },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${format(local.arn, "logs")}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      # Workload identity tokens: AgentCore Runtime fetches one per invocation for the agent.
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT", "bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
        Resource = "${format(local.arn, "bedrock-agentcore")}:workload-identity-directory/default*"
      },
    ]
  })
}

# --- the Runtime: direct code deployment (zip), arm64, Python 3.14 ---
resource "aws_bedrockagentcore_agent_runtime" "investigator" {
  count              = local.investigator
  agent_runtime_name = "agentic_bank_investigator"
  description        = "Dispute investigator: recommends only; code decides, people deny."
  role_arn           = aws_iam_role.investigator[0].arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["main.py"]
      runtime     = "PYTHON_3_14"
      code {
        s3 {
          bucket     = aws_s3_bucket.agent_code[0].id
          prefix     = aws_s3_object.investigator[0].key
          version_id = aws_s3_object.investigator[0].version_id
        }
      }
    }
  }

  # PUBLIC: the microVM reaches Bedrock, STS and DynamoDB over public endpoints with TLS, like our
  # Lambdas. A bank uses network_mode VPC with interface endpoints and no internet route.
  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  # One case per session: end idle sessions fast and cap any one at 5 minutes.
  lifecycle_configuration = [{
    idle_runtime_session_timeout = 60
    max_lifetime                 = 300
  }]

  environment_variables = {
    MODEL_ID             = local.investigator_model
    GUARDRAIL_ID         = aws_bedrock_guardrail.grounding.guardrail_id
    GUARDRAIL_VERSION    = aws_bedrock_guardrail_version.grounding.version
    TABLE_ARN            = local.table_arn
    DATA_READER_ROLE_ARN = local.data_reader_role_arn
  }

  depends_on = [aws_iam_role_policy.investigator]
}

output "investigator_runtime_arn" {
  value = local.investigator_runtime_arn
}
