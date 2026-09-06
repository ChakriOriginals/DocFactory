# A document in a dead-letter queue should tell somebody.
#
# The self-healing built in 4f-C covers the recoverable case: a provider outage
# dead-letters a batch, the outage clears, and the bounded redrive brings them
# home. What it deliberately does not cover is the other case — a document that
# is genuinely un-processable, or one that used both of its redrives. Those
# stay in the DLQ, correctly, and until now nothing said so. A queue quietly
# accumulating poison documents is the failure mode that gets discovered a
# fortnight later, by a customer.
#
# WHY THESE LIVE IN THE DATA PLANE. The queues do, and so does the SNS topic —
# but the real reason is that a stuck document is still stuck when the compute
# layer is destroyed. Put these next to the ECS alarms and parking the stack
# overnight would take the alarm down with it, which is precisely backwards.
#
# COST: metric alarms are free up to ten per month and this stack now uses
# eight. Deliberately three separate alarms rather than one composite —
# composite alarms are $0.50/month each and are not in the free tier, and there
# is nothing to compose here. Which queue is dead-lettering is the first thing
# you want to know.

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  for_each = aws_sqs_queue.dlq

  alarm_name = "${each.value.name}-not-empty"
  alarm_description = join(" ", [
    "Messages are sitting in ${each.value.name}.",
    "The bounded redrive has either not run yet or has given up on them.",
    "Inspect with: aws sqs receive-message --queue-url ${each.value.url}",
  ])

  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions  = { QueueName = each.value.name }

  # Fifteen minutes of a message still being there, rather than one datapoint.
  # The redrive sweep runs every five, so a shorter window would page on
  # messages that are about to heal themselves — which trains you to ignore it.
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # An empty DLQ publishes no datapoints at all. Missing data here means
  # nothing is dead, which is the good case.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.cost_alerts.arn]
  # The recovery is worth hearing about too: it is how you learn the redrive
  # worked without going to look.
  ok_actions = [aws_sns_topic.cost_alerts.arn]
}
