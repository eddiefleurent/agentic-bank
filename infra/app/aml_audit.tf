# Detective controls for AML triage. Kept out of aml.tf so a concurrent edit to the reviewer's
# IAM role merges cleanly. Execution history is unchanged (AWS-owned key, payloads included).
# These logs name states and errors only: the state machine sets include_execution_data = false.

# The PII key (alias/agentic-bank-pii) cannot encrypt this group. Its policy denies Encrypt
# unless the encryption context keys are case_id and token, and it denies Decrypt to everyone
# but the analyst function. CloudWatch Logs calls KMS with context aws:logs:arn. The logs key
# is created in infra/bootstrap (a human applies that root; the pipeline cannot put a key policy).
data "aws_kms_key" "aml_logs" {
  key_id = "alias/agentic-bank-aml-logs"
}

# 90 days, same as CloudTrail in this account: long enough to investigate a failed proof run,
# not the BSA five-year record. A bank archives that to S3 Object Lock (docs/aml.md). The
# /aws/vendedlogs/ prefix is what lets delivery write here without a hand-written resource policy.
resource "aws_cloudwatch_log_group" "aml_triage" {
  name              = "/aws/vendedlogs/states/agentic-bank-aml-triage"
  retention_in_days = 90
  kms_key_id        = data.aws_kms_key.aml_logs.arn
}

# Log-delivery actions take no resource ARN (AWS requires "*"). The workload boundary allows the
# same actions; without that, this policy would not take effect. Only this role's policy grants them.
resource "aws_iam_role_policy" "aml_workflow_logs" {
  name = "aml-execution-logs"
  role = aws_iam_role.aml_workflow.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogDelivery", "logs:CreateLogStream", "logs:GetLogDelivery",
        "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries",
        "logs:PutLogEvents", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups",
      ]
      Resource = "*"
    }]
  })
}

# Console state only: no email was requested here. One failure in a five-minute window is enough.
locals {
  aml_alarm = {
    comparison_operator = "GreaterThanThreshold"
    evaluation_periods  = 1
    period              = 300
    statistic           = "Sum"
    threshold           = 0
    treat_missing_data  = "notBreaching"
  }
}

resource "aws_cloudwatch_metric_alarm" "aml_executions_failed" {
  alarm_name          = "agentic-bank-aml-executions-failed"
  alarm_description   = "AML triage executions failed. Visible in CloudWatch; nothing is paged."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  comparison_operator = local.aml_alarm.comparison_operator
  evaluation_periods  = local.aml_alarm.evaluation_periods
  period              = local.aml_alarm.period
  statistic           = local.aml_alarm.statistic
  threshold           = local.aml_alarm.threshold
  treat_missing_data  = local.aml_alarm.treat_missing_data
  dimensions          = { StateMachineArn = aws_sfn_state_machine.aml.arn }
}

resource "aws_cloudwatch_metric_alarm" "aml_executions_timed_out" {
  alarm_name          = "agentic-bank-aml-executions-timed-out"
  alarm_description   = "AML triage executions timed out. Visible in CloudWatch; nothing is paged."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  comparison_operator = local.aml_alarm.comparison_operator
  evaluation_periods  = local.aml_alarm.evaluation_periods
  period              = local.aml_alarm.period
  statistic           = local.aml_alarm.statistic
  threshold           = local.aml_alarm.threshold
  treat_missing_data  = local.aml_alarm.treat_missing_data
  dimensions          = { StateMachineArn = aws_sfn_state_machine.aml.arn }
}

resource "aws_cloudwatch_metric_alarm" "aml_review_errors" {
  alarm_name          = "agentic-bank-aml-review-errors"
  alarm_description   = "AML reviewer Lambda errors. Visible in CloudWatch; nothing is paged."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  comparison_operator = local.aml_alarm.comparison_operator
  evaluation_periods  = local.aml_alarm.evaluation_periods
  period              = local.aml_alarm.period
  statistic           = local.aml_alarm.statistic
  threshold           = local.aml_alarm.threshold
  treat_missing_data  = local.aml_alarm.treat_missing_data
  dimensions          = { FunctionName = aws_lambda_function.fn["aml-review"].function_name }
}
