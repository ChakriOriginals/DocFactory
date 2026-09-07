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

  dimensions = { QueueName = local.data_plane.queue_names.extract }

  alarm_actions = [aws_appautoscaling_policy.worker_scale_out.arn]
}

# Queue empty AND nothing in flight, for five straight minutes -> back to zero.
# Both halves matter: a queue can read empty while a worker is mid-document, and
# killing that worker redelivers the message and wastes a model call already
# paid for.
#
# One metric-math alarm rather than two alarms behind a composite, for two
# independent reasons -- the first fails the apply, the second would have failed
# silently afterwards:
#
#   1. PutCompositeAlarm does not accept an Application Auto Scaling policy ARN.
#      Composite alarms may notify SNS, invoke Lambda, or open an OpsItem; only
#      PutMetricAlarm takes a scalingPolicy action. Same class as the us-east-1
#      alarm pointed at a us-east-2 topic: an action AWS will not honour, that
#      survives validate and plan and dies at apply.
#
#   2. Step bounds are offsets from the TRIGGERING ALARM'S THRESHOLD, and a
#      composite alarm has no metric and no threshold to offset from. Even if
#      the ARN were accepted, Application Auto Scaling would have no value to
#      place against scale-in's single (-inf, 0] step and nothing would happen.
#      dead_mans_switch.tf:45 already reasons this out for its own policy; the
#      same reasoning was never applied here.
#
# The arithmetic is unchanged. total = visible + inflight; with threshold 1 and
# total 0 the offset is -1, which lands in (-inf, 0] and selects
# worker_min_count -- exactly what the composite was meant to express. It is
# also $0.50/month cheaper, which is the same trade dlq_alarms.tf already made.
resource "aws_cloudwatch_metric_alarm" "worker_idle" {
  alarm_name        = "${local.name}-worker-idle"
  alarm_description = "Nothing waiting and nothing in flight: release the fleet."

  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 5
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "total"
    expression  = "visible + inflight"
    label       = "Extract queue: waiting plus in flight"
    return_data = true
  }

  metric_query {
    id = "visible"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      period      = 60
      stat        = "Maximum"
      dimensions  = { QueueName = local.data_plane.queue_names.extract }
    }
  }

  metric_query {
    id = "inflight"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      period      = 60
      stat        = "Maximum"
      dimensions  = { QueueName = local.data_plane.queue_names.extract }
    }
  }

  alarm_actions = [aws_appautoscaling_policy.worker_scale_in.arn]
}
