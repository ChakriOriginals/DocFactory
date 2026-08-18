# Compute.
#
# Two services from the same codebase: the API behind the ALB, and the worker
# fleet that autoscales on queue depth (see autoscaling.tf). Plus a one-off
# migration task definition that is registered but never run as a service —
# CI runs it once per deploy, as the OWNER role, before the app tasks roll.
#
# The app tasks connect as docfactory_app, which is not a superuser and holds
# no DDL rights. That split is what makes the Phase 3a RLS guarantees true in
# the cloud rather than just locally: even a compromised task cannot drop a
# policy, because it cannot issue DDL at all.

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # per-metric charges; Phase 5 turns this on for the load test
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}/api"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}/worker"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/ecs/${local.name}/migrate"
  retention_in_days = var.log_retention_days
}

locals {
  image_api    = "${local.data_plane.ecr_repositories.api}:${var.image_tag}"
  image_worker = "${local.data_plane.ecr_repositories.worker}:${var.image_tag}"

  # Endpoints are the only thing that changes between compose and AWS: the code
  # has taken them from the environment since Phase 0. Unset S3/SQS endpoints
  # mean "use real AWS", which is exactly what these tasks want.
  common_environment = [
    { name = "AWS_REGION", value = var.aws_region },
    { name = "S3_BUCKET", value = local.data_plane.documents_bucket },
    { name = "INGEST_QUEUE", value = local.data_plane.queue_names.ingest },
    { name = "PARSE_QUEUE", value = local.data_plane.queue_names.parse },
    { name = "EXTRACT_QUEUE", value = local.data_plane.queue_names.extract },
    { name = "MAX_RECEIVE_COUNT", value = tostring(local.data_plane.max_receive_count) },
    # Terraform owns the bucket and the queues here, and the task roles have no
    # rights to create either. The app verifies and fails loudly instead.
    { name = "INFRA_MODE", value = "assert" },
    # Mock is the deployed default. A real-model run is a deliberate,
    # temporary variable change — never the state the stack sits in.
    { name = "MODEL_PROVIDER", value = var.model_provider },
    { name = "DEFAULT_TENANT_ID", value = var.default_tenant_id },
    # On AWS, S3 publishes events straight to SQS: the local webhook bridge
    # has no counterpart here, and the notification target is the queue.
    { name = "INGEST_NOTIFY_TARGET", value = "" },
  ]

  common_secrets = [
    { name = "DATABASE_URL", valueFrom = local.data_plane.secret_arns.database_url_app },
    { name = "ANTHROPIC_API_KEY", valueFrom = local.data_plane.secret_arns.anthropic_api_key },
  ]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = local.data_plane.task_execution_role_arn
  task_role_arn            = local.data_plane.api_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "api"
    image       = local.image_api
    essential   = true
    environment = local.common_environment
    secrets     = local.common_secrets

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')\" || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = local.data_plane.task_execution_role_arn
  task_role_arn            = local.data_plane.worker_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "worker"
    image       = local.image_worker
    essential   = true
    environment = local.common_environment
    secrets     = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

# Migrations. Registered here, run by CI as a one-off task before the services
# roll — and it is the ONLY task definition wired to the owner credentials.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = local.data_plane.task_execution_role_arn
  task_role_arn            = local.data_plane.api_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.image_api
    essential = true
    command   = ["uv", "run", "alembic", "upgrade", "head"]

    environment = local.common_environment
    secrets = [
      # Alembic connects as the owner; the app role must never hold DDL rights.
      { name = "DATABASE_ADMIN_URL", valueFrom = local.data_plane.secret_arns.database_url_owner },
      { name = "DATABASE_URL", valueFrom = local.data_plane.secret_arns.database_url_app },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.migrate.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets = aws_subnet.public[*].id
    # No NAT gateway in this stack, so the task needs a public IP to reach ECR,
    # Secrets Manager, SQS and Neon. Inbound is closed by the security group.
    assign_public_ip = true
    security_groups  = [aws_security_group.api.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # Give the task time to pull secrets and warm up before the ALB judges it.
  health_check_grace_period_seconds = 60

  depends_on = [aws_lb_listener.http]

  lifecycle {
    # CI deploys a new task definition revision; Terraform must not roll it
    # back to the revision it happens to know about on the next plan.
    ignore_changes = [task_definition, desired_count]
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_min_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    assign_public_ip = true
    security_groups  = [aws_security_group.worker.id]
  }

  lifecycle {
    # desired_count belongs to the autoscaler, not to Terraform: without this
    # every plan would try to scale the fleet back to the minimum.
    ignore_changes = [task_definition, desired_count]
  }
}
