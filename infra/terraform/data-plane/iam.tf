# IAM.
#
# Two task roles, deliberately different. The execution role is what Fargate
# itself uses to start a task (pull the image, read the secrets, write logs);
# the task roles are what the *application* gets, and they are scoped to what
# each service actually does:
#
#   api     — read/write the documents bucket, send to the ingest/parse queues,
#             read the depth of all three
#   worker  — read/write the documents bucket, consume from all three queues
#
# Both may additionally resolve the DLQ URLs, because the startup assertion
# checks that the DLQs exist. The worker alone may also receive and delete from
# them, for the bounded redrive that recovers documents dead-lettered by a
# provider outage — see `drain_dlqs` below for why that widening is safe.
#
# Neither can create a queue, delete a bucket, read another stack's secrets, or
# touch IAM. The worker cannot send to the ALB's target group; the API cannot
# delete messages, and cannot enqueue onto `extract`. If a task is compromised,
# its blast radius is this stack's data plane and nothing else.
#
# EVERY GRANT BELOW HAS A CALL SITE, with one flagged exception
# (s3:GetBucketLocation). The mapping from call site to grant is the pre-flight
# table in docs/deploy_runbook.md; it was built by reading the application
# rather than by waiting for an AccessDenied, and it is the reason this file
# changed in 4c.5c.

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
    sid = "ReadStackParameters"
    # ECS calls the PLURAL form when resolving a task definition's `secrets`
    # block, even for a single parameter. Granting only ssm:GetParameter is a
    # task that starts and dies with AccessDeniedException on GetParameters,
    # which is a confusing thing to debug because the singular is the one you
    # reach for by hand.
    effect  = "Allow"
    actions = ["ssm:GetParameters"]
    # Exactly this stack's three parameters, by ARN. Not ssm:* on "*".
    resources = [
      aws_ssm_parameter.database_url_app.arn,
      aws_ssm_parameter.database_url_owner.arn,
      aws_ssm_parameter.anthropic_api_key.arn,
    ]
  }

  # SecureString parameters are encrypted with the AWS-managed key
  # `alias/aws/ssm`, and reading one needs kms:Decrypt as well as
  # ssm:GetParameters. Without this the task fails at startup with a KMS error
  # rather than an SSM one, which sends you looking in the wrong service.
  #
  # The resource is "*" and the scope comes from the condition instead, for a
  # reason rather than laziness: the AWS-managed key's ARN is not known until
  # the account's first SecureString parameter creates it, so a data-source
  # lookup would be a chicken-and-egg on a fresh account. `kms:ViaService`
  # pins this to decryption performed by SSM in this region — the key cannot be
  # used for anything else, by anyone holding this role.
  #
  # OPEN QUESTION, DELIBERATELY LEFT OVER-GRANTED. Whether this statement is
  # needed at all is genuinely unsettled when the parameter uses the
  # AWS-MANAGED key (alias/aws/ssm) rather than a customer-managed one: that
  # key's own policy already grants the account access via SSM, and AWS's ECS
  # documentation shows kms:Decrypt for the customer-managed case. An
  # adversarial review was asked to settle it and its verifier crashed, so the
  # question is open rather than answered, and saying so is better than
  # implying it was checked.
  #
  # Keeping it is the asymmetric bet. If it is unnecessary, the cost is one
  # redundant statement that ViaService confines to SSM decryption in one
  # region. If it is necessary and missing, every task dies at startup with a
  # KMS error pointing at the wrong service. After the first successful apply,
  # remove this statement and re-apply: if tasks still start, it was redundant.
  statement {
    sid       = "DecryptParametersViaSSMOnly"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.aws_region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name   = "read-stack-parameters"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# --- application task roles -------------------------------------------------

# Both task roles need this, and neither needs anything else on the DLQs.
#
# THE BUG THIS FIXES. Every service calls `ensure_infra()` at startup, and with
# INFRA_MODE=assert that runs `QueueBroker().assert_queues()`, which resolves a
# URL for each logical queue AND for its DLQ — `f"{queue}-dlq"`. Until this
# statement existed, neither role held any grant on a DLQ ARN, so the very
# first sqs:GetQueueUrl against `docfactory-dev-parse-dlq` would have returned
# AccessDenied and BOTH the API and the worker would have crash-looped on their
# first task start. Nothing local catches this: ElasticMQ authorizes nothing,
# and the LocalStack run in 4c.5b does not enforce IAM either. It was found by
# reading every AWS call the services make against the grants (4c.5c).
#
# GetQueueUrl only. The application never receives from, sends to, or reads
# attributes of a dead-letter queue — draining a DLQ is a human action taken
# with human credentials, deliberately.
data "aws_iam_policy_document" "resolve_dlq_urls" {
  statement {
    sid       = "ResolveDeadLetterQueueUrls"
    effect    = "Allow"
    actions   = ["sqs:GetQueueUrl"]
    resources = [for queue in aws_sqs_queue.dlq : queue.arn]
  }
}

# ECS Exec: the channel `aws ecs execute-command` opens into a running task.
#
# On the TASK role, not the execution role. The execution role is what Fargate
# uses to START a container; the exec session is held by the process INSIDE it,
# which runs as the task role. Getting that backwards produces a
# TargetNotConnectedException and sends you looking at the wrong policy.
#
# These four actions do not accept a resource, so "*" is the only expressible
# scope — the gate is `enable_execute_command` on the service, which is a
# per-service switch, plus the caller needing ecs:ExecuteCommand of their own.
# Holding these grants alone opens nothing.
#
# Both roles get it. An incident is exactly when you do not want to discover
# that the one you can shell into is not the one that is broken.
data "aws_iam_policy_document" "task_exec_channel" {
  statement {
    sid    = "EcsExecSessionChannel"
    effect = "Allow"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

# The worker, and ONLY the worker, may drain a DLQ back into its own queue.
#
# 4d deliberately gave neither role anything but GetQueueUrl on the DLQs, on
# the grounds that draining one is a human action taken with human
# credentials. 4f-C changes that on purpose and narrowly: a provider outage
# lasting longer than three receives dead-letters every in-flight document, and
# those are not poison — they are ordinary documents that arrived at a bad
# moment. Waiting for a human to notice and drain them is not self-healing.
#
# The widening is bounded in three ways worth stating. It is receive+delete
# only, so the worker still cannot create, purge or reconfigure a DLQ. It is
# scoped to this stack's DLQ ARNs. And it crosses no tenant boundary: a DLQ
# holds messages for documents this same role already processes, so the blast
# radius of a compromised worker is unchanged.
#
# The API does NOT get this. It never consumes from any queue.
data "aws_iam_policy_document" "drain_dlqs" {
  statement {
    sid       = "BoundedDeadLetterRedrive"
    effect    = "Allow"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage"]
    resources = [for queue in aws_sqs_queue.dlq : queue.arn]
  }
}

data "aws_iam_policy_document" "documents_bucket" {
  statement {
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/*"]
  }

  # s3:ListBucket is what authorizes HeadBucket, which is the startup
  # assertion's whole implementation — an easy one to miss, because the call is
  # spelled nothing like the permission.
  #
  # s3:GetBucketLocation has NO call site in this codebase. It is kept
  # deliberately: boto3 issues it when a client is redirected to a bucket's
  # home region, and losing object access to a region redirect would be a
  # confusing outage to debug for the sake of one read-only grant. Flagged
  # here rather than quietly left in.
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
  source_policy_documents = [
    data.aws_iam_policy_document.documents_bucket.json,
    data.aws_iam_policy_document.resolve_dlq_urls.json,
    data.aws_iam_policy_document.task_exec_channel.json,
  ]

  # The API enqueues onto exactly two queues: `parse`, from the upload
  # endpoint, and `ingest`, from the local storage-event bridge. It never
  # writes to `extract` — that hand-off belongs to the worker — so the send
  # grant stops short of it. Splitting this out of the read statement below is
  # the difference between "the API can start work" and "the API can inject a
  # message into the middle of the pipeline".
  statement {
    sid       = "EnqueueWork"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main["parse"].arn, aws_sqs_queue.main["ingest"].arn]
  }

  # Read-only, and on all three: GET /usage reports the depth of every stage,
  # and the startup assertion resolves every queue's URL.
  statement {
    sid       = "ReadQueueState"
    effect    = "Allow"
    actions   = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
    resources = [for queue in aws_sqs_queue.main : queue.arn]
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
  source_policy_documents = [
    data.aws_iam_policy_document.documents_bucket.json,
    data.aws_iam_policy_document.resolve_dlq_urls.json,
    data.aws_iam_policy_document.drain_dlqs.json,
    data.aws_iam_policy_document.task_exec_channel.json,
  ]

  # All three main queues, because the worker consumes from all three and hands
  # off between them.
  #
  # sqs:ChangeMessageVisibility is deliberately NOT here. It was, and nothing
  # called it: a failed handler returns the message by simply not deleting it,
  # and 4b's backpressure defers work by re-sending with DelaySeconds rather
  # than by extending a visibility timeout. A grant with no call site is a
  # grant that will be there the day someone needs it for something else.
  statement {
    sid    = "ConsumeAndEnqueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:SendMessage", # stage hand-off, and 4b's deferral
      "sqs:GetQueueUrl",
      "sqs:GetQueueAttributes",
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

  # THE SUBJECT CLAIM IS NOT WHAT THE GUIDES SAY IT IS.
  #
  # Every OIDC walkthrough, including AWS's own, tells you to match
  # "repo:OWNER/REPO:*". GitHub no longer sends that. The default subject claim
  # now embeds immutable numeric IDs for the owner and the repository:
  #
  #   repo:ChakriOriginals@70372465/DocFactory@1359728448:environment:dev
  #
  # Confirmed two ways: the sub_claim_prefix reported by
  # /repos/{owner}/{repo}/actions/oidc/customization/sub, and the principalId
  # CloudTrail recorded for the AccessDenied. The name-only pattern does not
  # match it, and the failure is a bare "Not authorized to perform
  # sts:AssumeRoleWithWebIdentity" with nothing pointing at the cause -- the
  # trust policy reads correctly, the provider is right, id-token: write is set.
  # CloudTrail's principalId is the only place the real claim appears.
  #
  # Both forms are allowed. The ID form is what GitHub sends today and is
  # strictly stronger -- renaming the repo or the account cannot forge it. The
  # name form is kept so this does not break if GitHub serves the older claim,
  # and it is still pinned to this one repository.
  github_owner = local.enable_oidc ? split("/", var.github_repository)[0] : ""
  github_repo  = local.enable_oidc ? split("/", var.github_repository)[1] : ""

  github_sub_patterns = compact([
    "repo:${var.github_repository}:*",
    var.github_owner_id != "" && var.github_repository_id != ""
    ? "repo:${local.github_owner}@${var.github_owner_id}/${local.github_repo}@${var.github_repository_id}:*"
    : "",
  ])
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
    # See the comment on github_sub_patterns above for why there are two.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.github_sub_patterns
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
      # The workflow asks whether the compute plane is parked before it builds
      # anything. Without this the call is AccessDenied, and the gate — which
      # could not tell a denied call from an absent cluster — concluded
      # "parked" and skipped every deploy, silently, including with the stack
      # running. Both halves are fixed: the grant here, and the gate no longer
      # treating a failed call as an answer.
      "ecs:DescribeClusters",
    ]
    resources = ["*"]
  }

  # The migrate step in .github/workflows/deploy.yml finds the subnets and the
  # security group by tag before it can call `ecs run-task` — the workflow has
  # no Terraform state to read them from. Without this the deploy fails at the
  # migration, after the images are already pushed: a half-done deploy, which
  # is the worst kind. Found in the 4c.5c pre-flight by reading the workflow's
  # AWS calls alongside the application's.
  #
  # resources = ["*"] is not laziness here: the ec2:Describe* actions do not
  # support resource-level permissions at all, so "*" is the only expressible
  # scope. They are read-only and return no secrets.
  statement {
    sid       = "FindTheNetworkForRunTask"
    effect    = "Allow"
    actions   = ["ec2:DescribeSubnets", "ec2:DescribeSecurityGroups"]
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
