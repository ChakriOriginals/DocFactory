output "api_url" {
  description = "The live URL. HTTP only — this stack terminates no TLS (no domain)."
  value       = "http://${aws_lb.api.dns_name}"
}

output "documents_bucket" {
  description = "Drop a PDF under {tenant}/dropbox/{doc_type}/ to exercise the batch path."
  value       = aws_s3_bucket.documents.id
}

output "queue_urls" {
  description = "Stage queues."
  value       = { for key, queue in aws_sqs_queue.main : key => queue.url }
}

output "dlq_urls" {
  description = "Dead-letter queues; a message here is a poison document, by design."
  value       = { for key, queue in aws_sqs_queue.dlq : key => queue.url }
}

output "ecr_repositories" {
  description = "Push targets for CI."
  value = {
    api    = aws_ecr_repository.api.repository_url
    worker = aws_ecr_repository.worker.repository_url
  }
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "migrate_task_definition" {
  description = "Run once per deploy, as the owner role, before the services roll."
  value       = aws_ecs_task_definition.migrate.family
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable in GitHub."
  value       = local.enable_oidc ? aws_iam_role.github_deploy[0].arn : "(github_repository not set)"
}

output "teardown_reminder" {
  description = "What still costs money while this stack exists."
  value = join(" ", [
    "Idle cost is the ALB (~$16/mo) plus any running Fargate tasks;",
    "there is no NAT gateway by design.",
    "Run `terraform destroy`, then scripts/aws_orphan_check.sh to confirm nothing survived.",
  ])
}
