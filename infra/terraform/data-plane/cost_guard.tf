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
#
# THIS IS THE BURN-RATE GAUGE, and `include_credit = false` is what makes it
# one. The flag does not mean what its name suggests, and this file used to have
# it exactly backwards on both budgets.
#
# Measured on the live account, same month, same moment:
#
#   gross usage                       $0.3654
#   credits applied                  -$0.3654
#   net                               $0.0000
#
#   budget with include_credit=false  $0.3650   <- tracks GROSS
#   budget with include_credit=true   $0.0000   <- tracks NET
#
# So `include_credit = false` EXCLUDES the credit line items from the sum and
# shows spend before credits: the burn rate against the balance. It does not
# show "the spend credits failed to cover", which is what the comment here used
# to claim and what the $1 out-of-pocket budget below was built on.
#
# Percentages of monthly_budget_usd, so this answers "am I consuming credits
# faster than planned" — the question worth asking while the balance holds.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    # Exclude credits: this budget is about consumption, not about the bill.
    include_credit   = false
    include_refund   = false
    include_upfront  = true
    include_tax      = true
    include_support  = true
    include_discount = true
    use_amortized    = false
  }

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

# --- tripwire 1b: am I spending REAL money? ---------------------------------
#
# The question a promotional-credit account actually needs answered, and it
# needs `include_credit = true` to answer it — the opposite of what this budget
# used to set.
#
# The original reasoning was sound and the flag was inverted: credits should be
# INCLUDED so their negative line items cancel the usage, leaving only what the
# balance did not absorb. That reads $0.00 while credits last, and the moment it
# moves, real money is leaving. See the measurement in the monthly budget above.
#
# The bug was not theoretical. With include_credit = false this budget tracked
# gross usage, so it fired at one cent of ANY activity — $0.30 of usage that
# credits had already covered in full, reported as though the account were being
# charged. An alarm that fires every month regardless of the thing it is
# watching gets muted, and then the real signal has nowhere to arrive.
#
# The limit stays $1 with an alert at 1% — one cent — because the useful signal
# is not "how much" but "at all". Now it means it.
#
# It is a notification, not a brake. Nothing in AWS stops a resource billing on
# your behalf; what this buys is finding out on the first cent instead of at
# the end of the month.
resource "aws_budgets_budget" "out_of_pocket" {
  name         = "${local.name}-out-of-pocket"
  budget_type  = "COST"
  limit_amount = "1"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    # The whole point: credits included, so they cancel the usage they cover and
    # what remains is money actually owed. Everything else is the AWS default,
    # restated so a future reader can see which flag carries the meaning.
    include_credit   = true
    include_refund   = false
    include_upfront  = true
    include_tax      = true
    include_support  = true
    include_discount = true
    use_amortized    = false
    use_blended      = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 1 # 1% of $1 — one cent of real money
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
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
#     stack runs in — and this stack runs in us-east-2, so that is not a
#     hypothetical. The alias below exists for that alone, and it is why
#     `terraform destroy` can leave a billing alarm behind in a region the rest
#     of the stack never touched. The orphan check sweeps us-east-1 separately
#     for exactly this reason.
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

# A SECOND topic, in us-east-1, for the one alarm that has to live there.
#
# CloudWatch alarm actions must target an SNS topic in the SAME region as the
# alarm. The billing alarm is pinned to us-east-1 because AWS/Billing publishes
# nowhere else; the rest of this stack — and the cost_alerts topic above — is
# in us-east-2. The first real apply rejected the alarm with "Invalid region
# us-east-2 specified", which reads like a provider misconfiguration and is
# actually the alarm_actions ARN being cross-region.
#
# Two topics is the correct answer rather than a workaround: the constraint is
# AWS's, and the alternative — moving the whole stack to us-east-1 for the sake
# of one alarm — is a worse trade than one extra free topic. Both are free, and
# email notifications are free to 1,000/month, so this costs nothing but a
# second confirmation email.
resource "aws_sns_topic" "cost_alerts_billing" {
  provider = aws.billing

  name = "${local.name}-cost-alerts-billing"
}

resource "aws_sns_topic_subscription" "cost_alerts_billing_email" {
  provider = aws.billing
  count    = var.cost_alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.cost_alerts_billing.arn
  protocol  = "email"
  endpoint  = var.cost_alert_email
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

  # The us-east-1 topic, not the us-east-2 one — see above.
  alarm_actions = [aws_sns_topic.cost_alerts_billing.arn]
  ok_actions    = [aws_sns_topic.cost_alerts_billing.arn]
}
