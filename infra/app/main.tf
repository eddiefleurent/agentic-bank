terraform {
  required_version = ">= 1.11"

  # Partial configuration: bucket, key and region come from backend.hcl (see the example).
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = { project = "agentic-bank" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  arn     = "arn:aws:%s:us-east-1:${local.account}" # format(local.arn, "lambda")
  # Created by infra/bootstrap. The apply role may only create roles that carry it.
  boundary_arn = "arn:aws:iam::${local.account}:policy/agentic-bank-workload-boundary"
}

# --- Two-pass first deploy ---
# The Cognito resource server's identifier must be the gateway URL, but the gateway needs the
# client IDs whose scopes come from that resource server: a cycle. So the gateway URL is written to
# SSM and read back on the NEXT run. Empty on the first run: the URL-dependent pieces wait (count 0).
data "aws_ssm_parameters_by_path" "app" {
  path = "/agentic-bank"
}

locals {
  stored_gateway_url = try(
    data.aws_ssm_parameters_by_path.app.values[index(data.aws_ssm_parameters_by_path.app.names, "/agentic-bank/gateway-url")],
    "",
  )
  second_pass = local.stored_gateway_url != ""
  scopes      = { for s in ["read", "propose", "disputes"] : s => "${local.stored_gateway_url}/${s}" }
}

resource "aws_ssm_parameter" "gateway_url" {
  name  = "/agentic-bank/gateway-url"
  type  = "String"
  value = aws_bedrockagentcore_gateway.bank.gateway_url
}

output "gateway_url" {
  value = aws_bedrockagentcore_gateway.bank.gateway_url
}

output "approvals_url" {
  value = aws_apigatewayv2_api.app.api_endpoint
}
