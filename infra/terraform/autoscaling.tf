# Worker autoscaling on queue depth — the headline artifact of this phase.
#
# WHY STEP SCALING AND NOT TARGET TRACKING.
#
# Target tracking is the usual advice, but it cannot scale a service to zero on
# a raw queue metric: the target is a per-task average, and "messages per task"
# is undefined at zero tasks — the fleet parks at one task forever, and one
# always-on Fargate task is a permanent ~$9/month for a stack that is idle most
# of the time. AWS's documented workaround is to publish a custom
# backlog-per-task metric, which needs something running to publish it.
#
# Step scaling on the raw backlog has neither problem: the alarm fires on
# ApproximateNumberOfMessagesVisible whether or not anything is running, so
# 0 -> 1 works, and the steps give coarse but predictable fan-out. Scale-in is
# a separate alarm that only fires after a sustained *empty* queue, so a
# briefly-drained queue mid-batch does not kill the fleet that is draining it.
#
# NOTE ON THE METRIC: ApproximateNumberOfMessagesVisible excludes in-flight
# messages. A backlog being actively worked therefore reads lower than the true
# outstanding work, which biases toward under-scaling — the safe direction for
# cost, and the reason the scale-in alarm also requires zero *in-flight*
# messages before going to zero tasks.

resource "aws_appautoscaling_target" "worker" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.worker_min_count
  max_capacity       = var.worker_max_count
}

resource "aws_appautoscaling_policy" "worker_scale_out" {
  name               = "${local.name}-worker-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type = "ExactCapacity"
    # Short cooldown: a batch arriving is not a reason to wait five minutes.
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    # Bounds are offsets from the alarm threshold (1 message).
    step_adjustment {
      metric_interval_lower_bound = 0 # 1-20 messages
      metric_interval_upper_bound = 19
      scaling_adjustment          = 1
    }

    step_adjustment {
      metric_interval_lower_bound = 19 # 20-99
      metric_interval_upper_bound = 99
      scaling_adjustment          = 3
    }

    step_adjustment {
      metric_interval_lower_bound = 99 # 100+
      scaling_adjustment          = var.worker_max_count
    }
  }
}

resource "aws_appautoscaling_policy" "worker_scale_in" {
  name               = "${local.name}-worker-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type = "ExactCapacity"
    # Longer than scale-out: bringing the fleet back is cheap, killing a fleet
    # that is about to be needed again is not.
    cooldown                = 300
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = var.worker_min_count
    }
  }
}

# Backlog exists -> scale out. One datapoint so a batch starts draining within
# about a minute of arriving.
resource "aws_cloudwatch_metric_alarm" "backlog" {
  alarm_name          = "${local.name}-extract-backlog"
  alarm_description   = "Extract queue has work waiting; bring workers up."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.main["extract"].name }

  alarm_actions = [aws_appautoscaling_policy.worker_scale_out.arn]
}

# Queue empty AND nothing in flight, for five straight minutes -> back to zero.
# The composite alarm is what makes scale-to-zero safe: a queue can read empty
# while a worker is mid-document, and killing that worker would redeliver the
# message and waste the model call already paid for.
resource "aws_cloudwatch_metric_alarm" "idle_visible" {
  alarm_name          = "${local.name}-extract-idle-visible"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.main["extract"].name }
}

resource "aws_cloudwatch_metric_alarm" "idle_in_flight" {
  alarm_name          = "${local.name}-extract-idle-in-flight"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesNotVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = aws_sqs_queue.main["extract"].name }
}

resource "aws_cloudwatch_composite_alarm" "worker_idle" {
  alarm_name        = "${local.name}-worker-idle"
  alarm_description = "Nothing waiting and nothing in flight: release the fleet."

  alarm_rule = join(" AND ", [
    "ALARM(${aws_cloudwatch_metric_alarm.idle_visible.alarm_name})",
    "ALARM(${aws_cloudwatch_metric_alarm.idle_in_flight.alarm_name})",
  ])

  alarm_actions = [aws_appautoscaling_policy.worker_scale_in.arn]
}
