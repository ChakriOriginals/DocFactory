output "api_url" {
  description = "The live URL. HTTP only — this stack terminates no TLS (no domain)."
  value       = "http://${aws_lb.api.dns_name}"
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "migrate_task_definition" {
  description = "Run once per deploy, as the owner role, before the services roll."
  value       = aws_ecs_task_definition.migrate.family
}

output "subnet_ids" {
  description = "Needed by `aws ecs run-task` for the one-off migration."
  value       = aws_subnet.public[*].id
}

output "worker_security_group_id" {
  description = "Needed by `aws ecs run-task` for the one-off migration."
  value       = aws_security_group.worker.id
}

# Repeated from the data layer so `terraform output` in the directory you are
# standing in tells you what you need for the smoke test, without a second
# `cd`. These are reads of the layer below, not resources this layer owns.
output "documents_bucket" {
  value = local.data_plane.documents_bucket
}

output "queue_urls" {
  value = local.data_plane.queue_urls
}

output "teardown_reminder" {
  description = "What still costs money, and what destroying THIS layer does."
  value = join(" ", [
    "Standing cost is ~$36/mo: the ALB (~$16.43) plus the always-on API task",
    "(~$18.02) plus ~$1.90 of secrets, alarms and images. Workers are $0 idle,",
    "and there is no NAT gateway by design. See docs/cost_model.md.",
    "`terraform destroy` HERE removes both and keeps the bucket, queues,",
    "secrets and images intact — that is the overnight park.",
    "Then run scripts/aws_orphan_check.sh to confirm nothing survived.",
  ])
}
