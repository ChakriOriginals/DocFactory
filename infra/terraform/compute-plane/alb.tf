# The load balancer — OPTIONAL, and off by default.
#
# An ALB is $0.0225/hour, about $16.43/month, whether or not a request ever
# reaches it. On a standing stack that was more than everything else combined
# except the API task, for the single feature of a stable hostname.
#
# So it is now `enable_alb`, default false. Without it the API task keeps the
# public IP it already had — this stack has no NAT gateway, so tasks run in
# public subnets by design — and is reached directly on port 8000. The address
# changes when the task is replaced, there is no health-check-driven
# replacement, and there is no TLS; for a stack brought up for a demo and
# destroyed the same evening, none of those is worth $16 a month. Turn it on
# for the afternoon you need a link someone else can open and it costs about
# two cents.
#
# Counted rather than extracted into a module: the whole file disappears from
# the plan when the flag is false, and turning it back on is a one-variable
# change rather than an edit.
#
# HTTP only. A real deployment terminates TLS here with an ACM certificate,
# which needs a domain; this stack has none, so the listener is port 80 and the
# runbook says plainly that the demo URL is unencrypted.

resource "aws_lb" "api" {
  count = var.enable_alb ? 1 : 0

  name               = "${local.name}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb[0].id]
  subnets            = aws_subnet.public[*].id

  # Nothing here is precious, and a protected ALB is exactly the resource that
  # survives a destroy and keeps billing.
  enable_deletion_protection = false

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "api" {
  count = var.enable_alb ? 1 : 0

  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # Fargate awsvpc tasks register by IP, not instance

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # The API is stateless; drain fast so a deploy or destroy is not slow.
  deregistration_delay = 10
}

resource "aws_lb_listener" "http" {
  count = var.enable_alb ? 1 : 0

  load_balancer_arn = aws_lb.api[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }
}
