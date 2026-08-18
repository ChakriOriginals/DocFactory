# The load balancer — and the stack's main idle cost.
#
# An ALB is roughly $16/month whether or not a request ever reaches it. It is
# the price of having a URL, so it is worth knowing that destroying the stack
# between demos is what keeps this project in student-budget territory. There
# is no cheaper way to get a stable public endpoint in front of Fargate.
#
# HTTP only. A real deployment terminates TLS here with an ACM certificate,
# which needs a domain; this stack has none, so the listener is port 80 and the
# runbook says plainly that the demo URL is unencrypted.

resource "aws_lb" "api" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # Nothing here is precious, and a protected ALB is exactly the resource that
  # survives a destroy and keeps billing.
  enable_deletion_protection = false

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "api" {
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
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
