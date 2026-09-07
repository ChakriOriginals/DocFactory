# Outputs.
#
# These are the layer's public contract. Everything the compute layer needs it
# reads from here via `terraform_remote_state` — the dependency runs one way
# only, data plane -> compute plane, which is what makes `terraform destroy` in
# compute-plane/ safe to run on its own.
#
# Adding a field here is additive. Renaming or removing one breaks the compute
# layer's next plan, which is the intended failure mode: loud, at plan time,
# before anything is applied.

output "name" {
  description = "The shared name prefix. The compute layer asserts its own matches."
  value       = local.name
}

output "aws_region" {
  value = var.aws_region
}

output "documents_bucket" {
  description = "Drop a PDF under {tenant}/dropbox/{doc_type}/ to exercise the batch path."
  value       = aws_s3_bucket.documents.id
}

output "documents_bucket_arn" {
  value = aws_s3_bucket.documents.arn
}

output "queue_names" {
  description = "Stage queue names, by logical stage. The app takes these as env vars."
  value       = { for key, queue in aws_sqs_queue.main : key => queue.name }
}

output "queue_urls" {
  value = { for key, queue in aws_sqs_queue.main : key => queue.url }
}

output "queue_arns" {
  value = { for key, queue in aws_sqs_queue.main : key => queue.arn }
}

output "dlq_names" {
  value = { for key, queue in aws_sqs_queue.dlq : key => queue.name }
}

output "dlq_urls" {
  description = "Dead-letter queues; a message here is a poison document, by design."
  value       = { for key, queue in aws_sqs_queue.dlq : key => queue.url }
}

output "max_receive_count" {
  description = "Receives before the redrive policy quarantines a message."
  value       = local.max_receive_count
}

output "ecr_repositories" {
  description = "Push targets for CI."
  value = {
    api    = aws_ecr_repository.api.repository_url
    worker = aws_ecr_repository.worker.repository_url
  }
}

output "task_execution_role_arn" {
  description = "What Fargate itself uses: pull the image, read the secrets, write logs."
  value       = aws_iam_role.task_execution.arn
}

output "api_task_role_arn" {
  description = "What the API process gets. Scoped to this stack's bucket and queues."
  value       = aws_iam_role.api_task.arn
}

output "worker_task_role_arn" {
  description = "What the worker process gets."
  value       = aws_iam_role.worker_task.arn
}

output "parameter_arns" {
  description = <<-EOT
    SSM parameter ARNs, referenced by the task definitions' `secrets` blocks.
    The values themselves never enter a task definition.

    Renamed from `secret_arns` when the stack moved off Secrets Manager. The
    rename is deliberate rather than a compatibility shim: an output called
    `secret_arns` holding SSM ARNs is the kind of small lie that costs someone
    an hour in two years.
  EOT
  value = {
    database_url_app   = aws_ssm_parameter.database_url_app.arn
    database_url_owner = aws_ssm_parameter.database_url_owner.arn
    anthropic_api_key  = aws_ssm_parameter.anthropic_api_key.arn
  }
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable in GitHub."
  value       = local.enable_oidc ? aws_iam_role.github_deploy[0].arn : "(github_repository not set)"
}

output "cost_alerts_topic_arn" {
  description = "Budget and billing-alarm notifications. Confirm the email subscription."
  value       = aws_sns_topic.cost_alerts.arn
}

output "cost_runway" {
  description = "How long the credit balance lasts, at this stack's three resting states."
  value = {
    credits_usd = var.credit_balance_usd
    # Figures from docs/cost_model.md, recomputed here so the output cannot
    # quietly disagree with the document.
    months_if_running_24x7      = format("%.1f", var.credit_balance_usd / 13.67)
    months_if_parked            = format("%.0f", var.credit_balance_usd / 0.61)
    months_if_compute_destroyed = format("%.0f", var.credit_balance_usd / 0.11)
    note = join(" ", [
      "Running 24/7 assumes enable_alb = false and a 256/512 API task.",
      "Turning the ALB on adds ~$23.73/mo all-in (balancer plus its two public",
      "IPv4 addresses) and cuts the first figure to about",
      format("%.1f", var.credit_balance_usd / 37.40),
      "months. Anthropic API usage is billed by Anthropic and no AWS credit covers it.",
    ])
  }
}
