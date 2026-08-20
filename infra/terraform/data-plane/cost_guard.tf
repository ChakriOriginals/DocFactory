# Cost guard rails.
#
# THESE LIVE IN THE DATA PLANE ON PURPOSE. The compute layer is the one that is
# destroyed and rebuilt — that is the whole point of the split — and a budget
# alarm that disappears every time you park the stack is a budget alarm that is
# missing on the night you needed it. These exist from the first `apply` and
# survive every teardown of the layer that actually spends money.
#
# WHAT THESE DO AND DO NOT DO. They are tripwires, not brakes. AWS Budgets and
# CloudWatch billing alarms *notify*; neither can stop a resource billing. The
# only thing that stops the bill is destroying the resource, which is what the
# runbook's deploy-demo-destroy discipline and the scheduled park in the
# compute layer are for. Anyone reading this file should leave it believing
# they will be told quickly, not that they are protected automatically.

resource "aws_sns_topic" "cost_alerts" {
  name = "${local.name}-cost-alerts"
}

# Email rather than anything cleverer: it reaches a phone, it needs no
# infrastructure, and the confirmation step is a feature — an unconfirmed
# subscription is a silent alarm, and you find that out on day one instead of
# on the day it matters.
resource "aws_sns_topic_subscription" "cost_alerts_email" {
  count = var.cost_alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "email"
  endpoint  = var.cost_alert_email
}

# --- tripwire 1: AWS Budgets ------------------------------------------------
#
# In code, not a console click, so it exists the moment the account is used.
#
# Two thresholds on ACTUAL spend and one on FORECAST. The forecast one is the
# useful one: actual-spend alerts arrive after the money is gone, while a
# forecast breach fires while there is still time to destroy something. AWS
# needs a few days of history before it will forecast at all, so it is a
# supplement to the actual thresholds rather than a replacement.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    # Percentages of the limit, so a raised limit moves them together. At the
    # $10 default these are $5 and $10.
    for_each = [50, 100]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_sns_topic_arns  = [aws_sns_topic.cost_alerts.arn]
      subscriber_email_addresses = var.cost_alert_email != "" ? [var.cost_alert_email] : []
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.cost_alerts.arn]
    subscriber_email_addresses = var.cost_alert_email != "" ? [var.cost_alert_email] : []
  }
}

# --- tripwire 2: CloudWatch estimated charges -------------------------------
#
# Deliberately a SECOND, INDEPENDENT mechanism rather than a duplicate. Budgets
# and CloudWatch billing metrics are different services with different data
# paths and different failure modes; a guard rail with one implementation is a
# guard rail with one way to be silently broken.
#
# TWO THINGS ABOUT THIS ALARM THAT BITE:
#
#  1. AWS/Billing metrics are published ONLY to us-east-1, whatever region the
#     stack runs in. The alias below exists for that alone.
#  2. They are not published at all until "Receive Billing Alerts" is enabled
#     in Billing → Billing preferences. That is a console setting with no API
#     and no Terraform resource. Until it is ticked, this alarm sits in
#     INSUFFICIENT_DATA and protects nothing. The runbook's prereq checklist
#     says so; `treat_missing_data = "breaching"` makes the silence loud rather
#     than reassuring.
provider "aws" {
  alias  = "billing"
  region = "us-east-1"

  access_key = local.localstack ? "test" : null
  secret_key = local.localstack ? "test" : null

  skip_credentials_validation = local.localstack
  skip_metadata_api_check     = local.localstack
  skip_requesting_account_id  = local.localstack

  dynamic "endpoints" {
    for_each = local.localstack ? [var.localstack_endpoint] : []

    content {
      cloudwatch = endpoints.value
      sns        = endpoints.value
      sts        = endpoints.value
    }
  }

  default_tags {
    tags = {
      project     = "docfactory"
      environment = var.environment
      managed_by  = "terraform"
      owner       = var.owner
      layer       = "data-plane"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "estimated_charges" {
  provider = aws.billing

  alarm_name        = "${local.name}-estimated-charges"
  alarm_description = "Estimated month-to-date charges crossed the budget. Destroy the compute layer."

  namespace   = "AWS/Billing"
  metric_name = "EstimatedCharges"
  dimensions  = { Currency = "USD" }

  statistic           = "Maximum"
  period              = 21600 # billing metrics update roughly every 6 hours
  evaluation_periods  = 1
  threshold           = var.monthly_budget_usd
  comparison_operator = "GreaterThanThreshold"

  # Missing data means the billing metric is not being published, which is the
  # failure mode this alarm cannot otherwise report. Better a false alarm that
  # sends you to check the setting than a quiet alarm that never fires.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.cost_alerts.arn]
  ok_actions    = [aws_sns_topic.cost_alerts.arn]
}
