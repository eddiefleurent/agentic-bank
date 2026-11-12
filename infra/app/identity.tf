# Customer sign-in: Cognito issues the token that IS the customer for every later check.

resource "aws_cognito_user_pool" "customers" {
  name                = "agentic-bank-customers"
  user_pool_tier      = "ESSENTIALS" # custom scopes and Managed Login need Essentials or Plus
  deletion_protection = "INACTIVE"
  mfa_configuration   = "ON" # a stolen password alone cannot sign in

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true # no self sign-up: the bank creates every customer
  }

  password_policy {
    minimum_length                   = 16
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  # No email/SMS reset: a hijacked inbox cannot take over the account.
  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }
}

# Managed Login v2 page. Domain prefixes are global; a hash keeps the account ID out of the URL.
resource "aws_cognito_user_pool_domain" "customers" {
  domain                = "agentic-bank-${substr(sha256(local.account), 0, 8)}"
  user_pool_id          = aws_cognito_user_pool.customers.id
  managed_login_version = 2
}

# Identifier = gateway URL: MCP clients send resource=<gateway URL> (RFC 8707) and Cognito issues only
# scopes of the resource server with exactly that identifier. Second pass only.
resource "aws_cognito_resource_server" "bank" {
  count        = local.second_pass ? 1 : 0
  identifier   = local.stored_gateway_url
  name         = "Agentic Bank"
  user_pool_id = aws_cognito_user_pool.customers.id

  scope {
    scope_name        = "read"
    scope_description = "Read the customer's accounts and transactions"
  }
  scope {
    scope_name        = "propose"
    scope_description = "Propose a transfer that only the customer can approve"
  }
  scope {
    scope_name        = "disputes"
    scope_description = "File and answer disputes about the customer's card charges"
  }
}

# The customer's agent (Claude.ai custom connector). Confidential client: the owner hands the
# secret to the connector; it is never in Git or a Terraform output.
resource "aws_cognito_user_pool_client" "agent" {
  count        = local.second_pass ? 1 : 0
  name         = "agent"
  user_pool_id = aws_cognito_user_pool.customers.id

  generate_secret                      = true
  allowed_oauth_flows                  = ["code"] # code + PKCE; no implicit flow
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = concat(["openid"], [for s in values(local.scopes) : s])
  callback_urls                        = ["https://claude.ai/api/mcp/auth_callback"]
  supported_identity_providers         = ["COGNITO"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  # A stolen access token works for 15 minutes; the refresh token spares an MFA sign-in each time.
  access_token_validity  = 15
  id_token_validity      = 15
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # The scopes must exist on the resource server before a client may list them.
  depends_on = [aws_cognito_resource_server.bank]
}

# Without a branding record Managed Login v2 does not render for the client.
resource "aws_cognito_managed_login_branding" "agent" {
  count                       = local.second_pass ? 1 : 0
  client_id                   = aws_cognito_user_pool_client.agent[0].id
  user_pool_id                = aws_cognito_user_pool.customers.id
  use_cognito_provided_values = true
  depends_on                  = [aws_cognito_user_pool_domain.customers]
}

# Proof scripts: admin password sign-in, no OAuth, so its tokens carry NO read scope. A real
# customer token that Cedar must refuse.
resource "aws_cognito_user_pool_client" "proof" {
  name                          = "proof"
  user_pool_id                  = aws_cognito_user_pool.customers.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  access_token_validity         = 15
  id_token_validity             = 15
  refresh_token_validity        = 60
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "minutes"
  }
}

# The bank app on the customer's phone, played by scripts/bank_app.py. Public client (an app holds
# no secret); password + TOTP sign-in, no OAuth flow. Only its tokens pass the approvals API's
# authorizer, and the MCP gateway does not list it, so the agent's credential cannot approve.
resource "aws_cognito_user_pool_client" "app" {
  name                          = "app"
  user_pool_id                  = aws_cognito_user_pool.customers.id
  generate_secret               = false
  explicit_auth_flows           = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  access_token_validity         = 15
  id_token_validity             = 15
  refresh_token_validity        = 60
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "minutes"
  }
}

# No password here (it would sit in state). scripts/setup_customer.py sets it and enrols TOTP.
resource "aws_cognito_user" "customer" {
  for_each       = toset(["alex", "sam", "jordan"])
  user_pool_id   = aws_cognito_user_pool.customers.id
  username       = each.key
  message_action = "SUPPRESS"
}
