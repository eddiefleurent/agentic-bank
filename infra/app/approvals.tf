# The bank app's approvals API: the only door to moving money, and the agent's token does not open it.
# API Gateway's JWT authorizer accepts only the `app` client's tokens and answers 401 before our
# code runs. The handler re-checks client_id and token_use, then acts through the approver role.

resource "aws_apigatewayv2_api" "app" {
  name          = "agentic-bank-app"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_authorizer" "app" {
  api_id           = aws_apigatewayv2_api.app.id
  name             = "cognito-app-client"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/${aws_cognito_user_pool.customers.id}"
    # Cognito access tokens carry no aud; API Gateway then checks client_id against this list.
    audience = [aws_cognito_user_pool_client.app.id]
  }
}

resource "aws_apigatewayv2_integration" "approvals" {
  api_id                 = aws_apigatewayv2_api.app.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn["approvals"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "app" {
  for_each = toset([
    "GET /transfers",
    "GET /transfers/{id}",
    "POST /transfers/{id}/approve",
    "POST /transfers/{id}/reject",
    "POST /agent/revoke",
  ])
  api_id             = aws_apigatewayv2_api.app.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.approvals.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.app.id
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.app.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
  # One line per request: who (JWT subject), which route, status. No bodies, no tokens.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId", ip = "$context.identity.sourceIp",
      time      = "$context.requestTime", routeKey = "$context.routeKey",
      status    = "$context.status", subject = "$context.authorizer.claims.sub",
      error     = "$context.authorizer.error", latency = "$context.responseLatency",
    })
  }
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/agentic-bank-app"
  retention_in_days = 90
}

# A resource policy on the function: only this API may invoke it (no PassRole needed).
resource "aws_lambda_permission" "approvals" {
  statement_id  = "AllowApprovalsApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["approvals"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.app.execution_arn}/*/*"
}

# --- approver role: the only role that can move money, trusted only by the approvals function ---
# The handler adds a session policy pinning LeadingKeys to the signed-in customer on both tables.
# TransactWriteItems is authorized per item as its Update/Put/ConditionCheck action.
data "aws_iam_policy_document" "approver_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.lambda["approvals"].arn]
    }
  }
}

resource "aws_iam_role" "approver" {
  name                 = "agentic-bank-app-approver"
  assume_role_policy   = data.aws_iam_policy_document.approver_trust.json
  permissions_boundary = local.boundary_arn
}

resource "aws_iam_role_policy" "approver" {
  name = "approve-and-revoke"
  role = aws_iam_role.approver.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.data.arn
      },
      {
        Effect   = "Allow"
        Action   = "dynamodb:PutItem"
        Resource = aws_dynamodb_table.revocations.arn
      },
    ]
  })
}
