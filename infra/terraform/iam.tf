# IAM.
#
# Two task roles, deliberately different. The execution role is what Fargate
# itself uses to start a task (pull the image, read the secrets, write logs);
# the task roles are what the *application* gets, and they are scoped to what
# each service actually does:
#
#   api     — read/write the documents bucket, send to the ingest/parse queues
#   worker  — read/write the documents bucket, consume from all three queues
#
# Neither can create a queue, delete a bucket, read another stack's secrets, or
# touch IAM. The worker cannot send to the ALB's target group; the API cannot
# delete messages. If a task is compromised, its blast radius is this stack's
# data plane and nothing else.

data "aws_iam_policy_document" "ecs_task_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- execution role (Fargate's own) -----------------------------------------

resource "aws_iam_role" "task_execution" {
  name               = "${local.name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    # Exactly this stack's secrets, by ARN. Not secretsmanager:* on "*".
    resources = [
      aws_secretsmanager_secret.database_url_app.arn,
      aws_secretsmanager_secret.database_url_owner.arn,
      aws_secretsmanager_secret.anthropic_api_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "read-stack-secrets"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# --- application task roles -------------------------------------------------

data "aws_iam_policy_document" "documents_bucket" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/*"]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.documents.arn]
  }
}

resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
}

data "aws_iam_policy_document" "api_task" {
  source_policy_documents = [data.aws_iam_policy_document.documents_bucket.json]

  statement {
    sid    = "EnqueueWork"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes", # the depth signal on GET /usage
    ]
    resources = [
      aws_sqs_queue.main["parse"].arn,
      aws_sqs_queue.main["ingest"].arn,
      aws_sqs_queue.main["extract"].arn,
    ]
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "api-data-plane"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_task_assume.json
}

data "aws_iam_policy_document" "worker_task" {
  source_policy_documents = [data.aws_iam_policy_document.documents_bucket.json]

  statement {
    sid    = "ConsumeAndEnqueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:SendMessage", # stage hand-off, and 4b's deferral
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [for q in aws_sqs_queue.main : q.arn]
  }
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "worker-data-plane"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task.json
}

# --- CI deploy role (OIDC, no long-lived keys) ------------------------------
#
# GitHub Actions assumes this role with a short-lived token minted from its own
# OIDC identity. There is no AWS access key in the repository's secrets, which
# is the point: a leaked repo secret cannot be replayed because there is none.

locals {
  enable_oidc = var.github_repository != ""

  oidc_provider_arn = local.enable_oidc ? (
    var.github_oidc_provider_arn != ""
    ? var.github_oidc_provider_arn
    : aws_iam_openid_connect_provider.github[0].arn
  ) : ""
}

resource "aws_iam_openid_connect_provider" "github" {
  count = local.enable_oidc && var.github_oidc_provider_arn == "" ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  count = local.enable_oidc ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Scoped to this repository. Without this condition ANY GitHub repository
    # in the world could assume the role — the classic OIDC misconfiguration.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:*"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  count = local.enable_oidc ? 1 : 0

  name               = "${local.name}-github-deploy"
  description        = "Assumed by GitHub Actions to push images and roll ECS services."
  assume_role_policy = data.aws_iam_policy_document.github_assume[0].json
}

data "aws_iam_policy_document" "github_deploy" {
  count = local.enable_oidc ? 1 : 0

  statement {
    sid       = "PushImages"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action does not accept a resource
  }

  statement {
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
    ]
    resources = [aws_ecr_repository.api.arn, aws_ecr_repository.worker.arn]
  }

  statement {
    sid    = "RollServices"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:UpdateService",
      "ecs:RunTask",
      "ecs:DescribeTasks",
    ]
    resources = ["*"]
  }

  # RegisterTaskDefinition/RunTask need to hand the task roles to ECS, and
  # that is a privilege escalation path if left open: without the condition,
  # this role could pass ANY role in the account to a task it starts.
  statement {
    sid       = "PassOnlyThisStacksRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.task_execution.arn, aws_iam_role.api_task.arn, aws_iam_role.worker_task.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  count = local.enable_oidc ? 1 : 0

  name   = "deploy"
  role   = aws_iam_role.github_deploy[0].id
  policy = data.aws_iam_policy_document.github_deploy[0].json
}
