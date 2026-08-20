# The dead man's switch.
#
# The queue-depth autoscaling in autoscaling.tf is the normal mechanism: a
# composite alarm sees an empty queue and nothing in flight for five minutes
# and releases the fleet. This is the backstop for when that mechanism is the
# thing that is broken — a misconfigured alarm, a scaling policy that was
# edited, an SQS metric that stopped publishing — and the fleet sits at N tasks
# with nothing to do, billing per second, until somebody notices.
#
# WHY IT KILLS ONLY IDLE FLEETS, AND WHY THAT IS AUTOMATIC.
#
# It parks the workers whenever ANY worker task has been running for
# `idle_park_hours` consecutive hours, without consulting the queue at all —
# because consulting the queue is what the primary mechanism does, and a
# backstop that shares a dependency with the thing it backs up is not a
# backstop.
#
# That sounds like it would kill a legitimate long backlog, and for about sixty
# seconds it does. Then the backlog alarm sees work still waiting and scales the
# fleet straight back out. No work is lost: an interrupted worker's message was
# never deleted, so SQS redelivers it after the visibility timeout, which is the
# same recovery path as a crashed task. So the switch is a blip on a busy stack
# and a hard stop on a stuck one, with no configuration deciding which.
#
# WHY NOT A LAMBDA. An EventBridge rule plus a Lambda plus its role plus its log
# group plus a packaging step is five more resources and a build artifact, to
# express "turn it off if it has been on too long" — which two CloudWatch
# resources already express. The Lambda earns its place when the condition gets
# interesting (idle detection across several signals, a graceful drain); it does
# not earn it here. `make aws-park` covers the on-demand case, and
# `terraform destroy` in this directory covers the real one.

resource "aws_appautoscaling_policy" "worker_park" {
  name               = "${local.name}-worker-park"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    # Deliberately its own policy rather than reusing worker_scale_in. Step
    # bounds are offsets from the triggering alarm's threshold, and scale-in's
    # single step covers (-inf, 0] — which never matches this alarm, whose
    # metric sits at or above its threshold by construction. Reusing it would
    # have produced a switch that fires and does nothing.
    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 0
    }
  }
}

# CPUUtilization, not RunningTaskCount, and the reason matters: RunningTaskCount
# lives in the ECS/ContainerInsights namespace, and Container Insights is off in
# this stack because it bills per metric. AWS/ECS CPUUtilization is free, is
# published per service, and — the property this depends on — is published ONLY
# while tasks exist. Zero tasks means no datapoints at all, which with
# notBreaching means the alarm cannot fire on an already-parked fleet.
resource "aws_cloudwatch_metric_alarm" "worker_running_too_long" {
  alarm_name = "${local.name}-worker-running-too-long"
  alarm_description = join(" ", [
    "Worker tasks have been running for ${var.idle_park_hours}h straight.",
    "Parking the fleet; the backlog alarm brings it back if there is real work.",
  ])

  namespace   = "AWS/ECS"
  metric_name = "CPUUtilization"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.worker.name
  }

  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = var.idle_park_hours
  threshold           = 0
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # No datapoints means no tasks means nothing to park.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_appautoscaling_policy.worker_park.arn]
}

# The API has no equivalent and deliberately so: one API task is the stack's
# reason to exist, and a backstop that turns off the service being demonstrated
# is a fault, not a safeguard. The API's cost is bounded by `api_desired_count`
# and stopped by destroying this layer — see docs/cost_model.md.
