# Applied once by a human with admin rights, using LOCAL state (a state bucket cannot store the
# state of its own creation). It creates the Terraform state bucket and the CI roles GitHub
# Actions uses. The pipeline never applies this root: it must not be able to edit its own trust.
terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "github_oidc_subject_prefix" {
  description = "Exact repo prefix of the GitHub OIDC sub claim (before :ref or :pull_request)."
  type        = string
}

variable "region" {
  type    = string
  default = "us-east-1"
}

# An account can hold only one OIDC provider per issuer URL. Pass false if it already exists.
variable "create_oidc_provider" {
  type    = bool
  default = true
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "agentic-bank" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  bucket   = "agentic-bank-tfstate-${data.aws_caller_identity.current.account_id}"
  oidc_url = "token.actions.githubusercontent.com"
  # The "sub" claim in GitHub's token says which repo and which trigger a job runs for.
  # This repo uses GitHub's immutable subject form, which embeds the permanent numeric owner and
  # repo IDs: repo:<owner>@<owner_id>/<repo>@<repo_id>. A name-only prefix never matches it, and
  # the IDs stop a deleted-and-recreated repo with the same name from assuming the role.
  # Read it from: gh api repos/<owner>/<repo>/actions/oidc/customization/sub
  sub_prefix = var.github_oidc_subject_prefix
}

# --- state bucket ---
resource "aws_s3_bucket" "state" {
  bucket = local.bucket
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" } # every state version is recoverable
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" } # AWS-managed key
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "tls_only" {
  statement {
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket     = aws_s3_bucket.state.id
  policy     = data.aws_iam_policy_document.tls_only.json
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- GitHub OIDC: GitHub signs a token per job; AWS checks it and issues 1-hour credentials ---
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://${local.oidc_url}"
}

locals {
  oidc_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

# A trust policy answers "who may assume this role". The sub condition is what limits a role
# to this repo and to the main branch. Pull requests get no AWS role: in a public repository a
# pull request runs code its author controls, and ci.yml needs no AWS access.
data "aws_iam_policy_document" "trust" {
  for_each = {
    plan  = ["${local.sub_prefix}:ref:refs/heads/main"] # main only
    apply = ["${local.sub_prefix}:ref:refs/heads/main"] # main only
  }
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_url}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_url}:sub"
      values   = each.value
    }
  }
}

# S3 SSE-KMS with the AWS-managed key: callers need kms rights, but only when going through S3.
data "aws_iam_policy_document" "state_kms" {
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${var.region}.amazonaws.com"]
    }
  }
}

# --- plan role: reads everything, writes only the state lock file ---
resource "aws_iam_role" "plan" {
  name               = "agentic-bank-ci-plan"
  assume_role_policy = data.aws_iam_policy_document.trust["plan"].json
}

resource "aws_iam_role_policy_attachment" "plan_read" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# ReadOnlyAccess is kept: terraform plan refreshes ~15 services, and a hand-made list would drift
# with every new resource type. It also reads data, though, so bulk data reads are denied: plan
# needs table and log-group metadata, never their contents (the seed item needs only GetItem).
data "aws_iam_policy_document" "plan" {
  source_policy_documents = [data.aws_iam_policy_document.state_kms.json]
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*.tflock"]
  }
  statement {
    sid    = "NoBulkDataReads"
    effect = "Deny"
    actions = [
      "dynamodb:Scan", "dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:PartiQLSelect",
      "dynamodb:ExportTableToPointInTime", "dynamodb:GetRecords",
      "logs:GetLogEvents", "logs:FilterLogEvents", "logs:StartQuery", "logs:StartLiveTail",
      "states:GetExecutionHistory", "states:DescribeExecution",
      "cognito-idp:ListUsers", "cloudtrail:LookupEvents",
    ]
    resources = ["*"]
  }
  statement {
    sid           = "NoObjectReadsOutsideState"
    effect        = "Deny"
    actions       = ["s3:GetObject", "s3:GetObjectVersion"]
    not_resources = ["${aws_s3_bucket.state.arn}/*"]
  }
}

resource "aws_iam_role_policy" "plan" {
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan.json
}

# --- apply role: main branch only. Named -v2: the first apply role was retired and deleted. ---
resource "aws_iam_role" "apply_v2" {
  name               = "agentic-bank-ci-apply-v2"
  assume_role_policy = data.aws_iam_policy_document.trust["apply"].json
}

resource "aws_iam_role_policy" "apply_v2" {
  role   = aws_iam_role.apply_v2.id
  policy = data.aws_iam_policy_document.apply.json
}

data "aws_iam_policy_document" "apply" {
  source_policy_documents = [data.aws_iam_policy_document.state_kms.json]
  statement {
    sid       = "StateBucket"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }
  statement {
    sid       = "StateBucketList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }
  statement {
    sid       = "AppParameters"
    actions   = ["ssm:*"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/agentic-bank/*"]
  }
  statement {
    sid       = "ParameterListing"
    actions   = ["ssm:DescribeParameters"]
    resources = ["*"]
  }
  # --- app roles: create and edit only agentic-bank-app-*, never agentic-bank-ci-* ---
  statement {
    sid = "AppRoles"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole", "iam:UntagRole",
      "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:UpdateAssumeRolePolicy",
    ]
    resources = [local.app_roles]
  }
  # Stops privilege escalation: a role without the boundary would have no ceiling.
  statement {
    sid       = "RequireBoundary"
    effect    = "Deny"
    actions   = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.boundary_arn]
    }
  }
  statement {
    sid       = "KeepBoundary"
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary"]
    resources = ["*"]
  }
  # PassRole hands a role to a service. Only our app roles, only to the two services that run them.
  statement {
    sid       = "PassAppRoles"
    actions   = ["iam:PassRole"]
    resources = [local.app_roles]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com", "bedrock-agentcore.amazonaws.com", "states.amazonaws.com"]
    }
  }
  statement {
    sid       = "Lambdas"
    actions   = ["lambda:*"]
    resources = ["${format(local.arn_prefix, "lambda")}:function:agentic-bank-*"]
  }
  statement {
    sid     = "LambdaLogs"
    actions = ["logs:*"]
    resources = [
      "${format(local.arn_prefix, "logs")}:log-group:/aws/lambda/agentic-bank-*",
      "${format(local.arn_prefix, "logs")}:log-group:/aws/apigateway/agentic-bank-*",
    ]
  }
  # HTTP API access logs are a "vended" log delivery: these calls take no resource ARN.
  statement {
    sid = "ApiAccessLogDelivery"
    actions = [
      "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
    ]
    resources = ["*"]
  }
  # The pipeline may change the bank's tables and user pool, never delete them.
  statement {
    sid       = "NoDataLoss"
    effect    = "Deny"
    actions   = ["dynamodb:DeleteTable", "cognito-idp:DeleteUserPool"]
    resources = ["*"]
  }
  statement {
    sid       = "Tables"
    actions   = ["dynamodb:*"]
    resources = ["${format(local.arn_prefix, "dynamodb")}:table/agentic-bank-*"]
  }
  statement {
    sid       = "UserPools"
    actions   = ["cognito-idp:*"]
    resources = ["${format(local.arn_prefix, "cognito-idp")}:userpool/*"]
  }
  # The approvals HTTP API. API Gateway invokes the Lambda through its resource policy: no PassRole.
  statement {
    sid       = "HttpApis"
    actions   = ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE", "apigateway:TagResource", "apigateway:UntagResource"]
    resources = ["arn:aws:apigateway:${var.region}::/apis", "arn:aws:apigateway:${var.region}::/apis/*", "arn:aws:apigateway:${var.region}::/tags/*"]
  }
  statement {
    sid       = "StateMachines"
    actions   = ["states:*"]
    resources = ["${format(local.arn_prefix, "states")}:stateMachine:agentic-bank-*", "${format(local.arn_prefix, "states")}:execution:agentic-bank-*"]
  }
  # Guardrail IDs are random: no name pattern exists. CreateGuardrail has no resource yet.
  statement {
    sid       = "Guardrails"
    actions   = ["bedrock:*Guardrail*", "bedrock:TagResource", "bedrock:UntagResource", "bedrock:ListTagsForResource"]
    resources = ["${format(local.arn_prefix, "bedrock")}:guardrail/*"]
  }
  # Read the PII key made here; the pipeline can never change its policy (no kms:PutKeyPolicy).
  statement {
    sid       = "ReadPiiKey"
    actions   = ["kms:DescribeKey"]
    resources = [aws_kms_key.pii.arn]
  }
  # These calls take no resource ARN (the pool or domain does not exist yet, or is looked up by name).
  statement {
    sid       = "UnscopedReads"
    actions   = ["cognito-idp:CreateUserPool", "cognito-idp:DescribeUserPoolDomain", "logs:DescribeLogGroups", "bedrock:CreateGuardrail", "bedrock:ListGuardrails", "kms:ListAliases", "states:ValidateStateMachineDefinition"]
    resources = ["*"]
  }
  # Gateway and policy engine IDs are random, so no name pattern fits; the region deny still applies.
  statement {
    sid       = "AgentCore"
    actions   = ["bedrock-agentcore:*"]
    resources = ["*"]
  }
  # Guardrail: even a future wide policy cannot act in another region. Global services
  # (IAM, STS, ...) have no region, so they are exempt from the deny.
  statement {
    sid         = "DenyOtherRegions"
    effect      = "Deny"
    not_actions = ["iam:*", "sts:*", "organizations:*", "route53:*", "cloudfront:*", "support:*"]
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }
}

# --- workload boundary: the ceiling for every role the pipeline creates ---
# The apply role can create roles and write their inline policies, which alone would let it mint an
# admin role. A permissions boundary caps a role: its effective rights are policy AND boundary. The
# apply role may only create roles that carry this boundary, so a pipeline-made role can never do
# more than what is listed here, whatever inline policy it gets.
locals {
  arn_prefix   = "arn:aws:%s:${var.region}:${data.aws_caller_identity.current.account_id}"
  app_roles    = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/agentic-bank-app-*"
  boundary_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/agentic-bank-workload-boundary"
}

data "aws_iam_policy_document" "boundary" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${format(local.arn_prefix, "logs")}:log-group:/aws/lambda/agentic-bank-*"]
  }
  statement {
    sid       = "ReadBankData"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = ["${format(local.arn_prefix, "dynamodb")}:table/agentic-bank-*"]
  }
  statement {
    sid       = "AssumeDataReader"
    actions   = ["sts:AssumeRole"]
    resources = [for r in ["data-reader", "proposer", "approver", "case-writer", "data-writer"] : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/agentic-bank-app-${r}"]
  }
  # Proposals, approvals and revocations. Which attributes and rows a role may write is narrowed
  # by its own policy and session policy; this is only the ceiling.
  statement {
    sid       = "WriteBankData"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = ["${format(local.arn_prefix, "dynamodb")}:table/agentic-bank-*"]
  }
  statement {
    sid       = "InvokeBankLambdas"
    actions   = ["lambda:InvokeFunction"]
    resources = ["${format(local.arn_prefix, "lambda")}:function:agentic-bank-*"]
  }
  statement {
    sid       = "AgentCore" # the gateway role asks the policy engine for decisions
    actions   = ["bedrock-agentcore:*"]
    resources = ["*"]
  }
  # Disputes.
  statement {
    sid       = "DetectPii" # Comprehend has no resource for a stateless detection call
    actions   = ["comprehend:DetectPiiEntities"]
    resources = ["*"]
  }
  statement {
    sid       = "PiiKey" # the key policy narrows Decrypt further, to one role
    actions   = ["kms:Encrypt", "kms:Decrypt"]
    resources = [aws_kms_key.pii.arn]
  }
  statement {
    sid       = "Review"
    actions   = ["bedrock:InvokeModel", "bedrock:ApplyGuardrail"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/*", "${format(local.arn_prefix, "bedrock")}:guardrail/*"]
  }
  statement {
    sid       = "DisputeWorkflow"
    actions   = ["states:StartExecution"]
    resources = ["${format(local.arn_prefix, "states")}:stateMachine:agentic-bank-*"]
  }
  # Callback APIs have no resource-level support. Only the callback action we use is unscoped.
  statement {
    sid       = "DisputeCallback"
    actions   = ["states:SendTaskSuccess"]
    resources = ["*"]
  }
  statement {
    sid       = "AnalystUrl"
    actions   = ["lambda:InvokeFunctionUrl"]
    resources = ["${format(local.arn_prefix, "lambda")}:function:agentic-bank-*"]
  }
}

# --- PII key: here, not in the pipeline, because whoever edits a key policy can grant
# themselves Decrypt. Only the analyst function's role may decrypt; everyone else, including this
# account's admins, is denied by the key policy (an admin could still edit the policy: a bank
# separates key administrators from data access and alarms on PutKeyPolicy).
locals {
  analyst_fn_role = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/agentic-bank-app-analyst-fn"
}

data "aws_iam_policy_document" "pii_key" {
  statement {
    sid       = "AccountAdministersKeyAndIamGrantsUse"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
  statement {
    sid       = "OnlyTheAnalystFunctionDecrypts"
    effect    = "Deny"
    actions   = ["kms:Decrypt", "kms:ReEncrypt*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalArn"
      values   = [local.analyst_fn_role]
    }
  }
  # Every ciphertext is bound to one case and one token; anything else is refused.
  statement {
    sid       = "EncryptOnlyWithCaseContext"
    effect    = "Deny"
    actions   = ["kms:Encrypt", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "ForAnyValue:StringNotEquals"
      variable = "kms:EncryptionContextKeys"
      values   = ["case_id", "token"]
    }
  }
  statement {
    sid       = "NoContextNoUse"
    effect    = "Deny"
    actions   = ["kms:Encrypt", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Null"
      variable = "kms:EncryptionContextKeys"
      values   = ["true"]
    }
  }
}

resource "aws_kms_key" "pii" {
  description             = "agentic-bank PII vault"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.pii_key.json
}

resource "aws_kms_alias" "pii" {
  name          = "alias/agentic-bank-pii"
  target_key_id = aws_kms_key.pii.key_id
}

resource "aws_iam_policy" "boundary" {
  name   = "agentic-bank-workload-boundary"
  policy = data.aws_iam_policy_document.boundary.json
}

# --- CloudTrail: who called which AWS API, in every region ---
# Management events only (control-plane calls such as CreateRole or PutKeyPolicy); data events
# (each S3 GET or DynamoDB read) cost extra and are not recorded. Log-file validation writes signed
# hourly digests, so a deleted or edited log file is detectable. A bank would send this to a
# separate log-archive account with Object Lock and an organization trail, so that no one in this
# account could delete it; here the logs live in this account and expire after 90 days.
locals {
  trail_name   = "agentic-bank-trail"
  trail_bucket = "agentic-bank-cloudtrail-${data.aws_caller_identity.current.account_id}"
  trail_arn    = "arn:aws:cloudtrail:${var.region}:${data.aws_caller_identity.current.account_id}:trail/${local.trail_name}"
}

resource "aws_s3_bucket" "trail" {
  bucket = local.trail_bucket
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" } # SSE-S3: no key policy needed
  }
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    id     = "expire-90-days"
    status = "Enabled"
    filter {}
    expiration { days = 90 }
  }
}

data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.trail.arn, "${aws_s3_bucket.trail.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  # Only this trail may write, and only under this account's prefix (SourceArn stops a trail in
  # another account from using our bucket: the confused-deputy problem).
  statement {
    sid       = "CloudTrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
  statement {
    sid       = "CloudTrailWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket     = aws_s3_bucket.trail.id
  policy     = data.aws_iam_policy_document.trail_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.trail]
}

resource "aws_cloudtrail" "main" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.trail.id
  is_multi_region_trail         = true
  include_global_service_events = true # IAM and STS calls
  enable_log_file_validation    = true
  depends_on                    = [aws_s3_bucket_policy.trail]
}

output "state_bucket" {
  value = aws_s3_bucket.state.id
}

output "plan_role_arn" {
  value = aws_iam_role.plan.arn
}

output "boundary_arn" {
  value = aws_iam_policy.boundary.arn
}

output "apply_role_arn" {
  value = aws_iam_role.apply_v2.arn
}
