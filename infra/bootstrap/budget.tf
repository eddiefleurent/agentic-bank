# Account-wide monthly cost cap. This root, not infra/app: a budget is account-level, like the
# trail and the keys, and CI never applies this root.
#
# The apply role (agentic-bank-ci-apply-v2) cannot manage budgets. Its policy has no budgets
# actions. Those APIs take no resource ARN, so moving this resource into infra/app would need
# budgets:ViewBudget and budgets:ModifyBudget on "*" added to that role. The role's region deny
# also does not exempt budgets (it exempts iam, sts, organizations, route53, cloudfront and
# support). This account is us-east-1, where the Budgets API is served, so the missing actions
# are what would block an app-stack apply, not the region deny.

variable "budget_alert_email" {
  description = "Subscriber for the monthly cost budget. Set in terraform.tfvars (gitignored). No default, and do not commit an address. AWS emails a confirmation that must be accepted."
  type        = string

  validation {
    condition     = length(trimspace(var.budget_alert_email)) > 0
    error_message = "Set budget_alert_email in terraform.tfvars (gitignored)."
  }
}

resource "aws_budgets_budget" "monthly" {
  name         = "agentic-bank-monthly"
  budget_type  = "COST"
  limit_amount = "25"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  # Fixed start so later plans do not drift. Covers every month from January 2026 on.
  time_period_start = "2026-01-01_00:00"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
