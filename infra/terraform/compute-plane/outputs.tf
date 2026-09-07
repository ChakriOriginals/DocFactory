output "api_url" {
  description = <<-EOT
    How to reach the API.

    With a load balancer, a stable hostname. Without one — the default, because
    an ALB is $16.43/month for a hostname — the task's public IP, which changes
    whenever the task is replaced, so this output is the command that finds it
    rather than a value that goes stale in the state file.
  EOT
  value = (
    var.enable_alb
    ? "http://${aws_lb.api[0].dns_name}"
    : "run: make api-url   (no ALB — the task's public IP changes on replacement)"
  )
}

output "alb_enabled" {
  description = "false means there is no hourly load-balancer charge on this stack."
  value       = var.enable_alb
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
    var.enable_alb
    ? "An ALB is enabled: ~$23.73/mo on top of the API task — $16.43 for the load balancer plus $7.30 for the two public IPv4 addresses it places, one per subnet. Turn it off when the demo is over."
    : "Standing cost is ~$13.67/mo: the always-on API task (~$9.01), its public IPv4 address ($3.65), four billable CloudWatch alarm metrics ($0.40), and the data plane ($0.11).",
    "Workers are $0 idle and run on Spot; there is no NAT gateway by design.",
    "See docs/cost_model.md.",
    "`terraform destroy` HERE removes both and keeps the bucket, queues,",
    "secrets and images intact — that is the overnight park.",
    "Then run scripts/aws_orphan_check.sh to confirm nothing survived.",
  ])
}
