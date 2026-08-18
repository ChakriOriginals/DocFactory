# --- naming and placement ---------------------------------------------------
#
# These three must match the data layer's. `check "layers_agree"` in
# data_plane.tf fails the plan if they do not.

variable "aws_region" {
  description = "Region for every resource in this layer. Must match the data layer."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name. Must match the data layer."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Tag value for who to ask before deleting something."
  type        = string
  default     = "docfactory"
}

variable "name_prefix" {
  description = "Prefix for resource names. Must match the data layer."
  type        = string
  default     = "docfactory"
}

# --- compute ----------------------------------------------------------------

variable "api_desired_count" {
  description = "API tasks. One is enough for a demo stack; the ALB is the cost, not the task."
  type        = number
  default     = 1
}

variable "worker_min_count" {
  description = "Minimum worker tasks. 0 means an idle stack runs no workers at all."
  type        = number
  default     = 0
}

variable "worker_max_count" {
  description = "Ceiling on worker fan-out under backlog."
  type        = number
  default     = 6
}

variable "task_cpu" {
  description = "Fargate CPU units per task (256 = 0.25 vCPU)."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate memory (MiB) per task."
  type        = number
  default     = 1024
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Log groups are a silent forever-cost without it."
  type        = number
  default     = 7
}

variable "image_tag" {
  description = <<-EOT
    Image tag the services run.

    CI pushes the git SHA and updates the service; "latest" is the bootstrap
    value for the first apply, before any image exists.
  EOT
  type        = string
  default     = "latest"
}

variable "model_provider" {
  description = <<-EOT
    "mock" or "anthropic". THE DEPLOYED DEFAULT IS MOCK.

    A real-model run is a deliberate, temporary change — flip it, run a handful
    of documents, flip it back. Leaving a deployed stack on "anthropic" is how
    a demo becomes a bill.
  EOT
  type        = string
  default     = "mock"

  validation {
    condition     = contains(["mock", "anthropic"], var.model_provider)
    error_message = "model_provider must be \"mock\" or \"anthropic\"."
  }
}

variable "default_tenant_id" {
  description = "Tenant the batch drop path attributes unprefixed keys to."
  type        = string
  default     = "dev-tenant"
}

# --- local emulation --------------------------------------------------------

variable "localstack_endpoint" {
  description = <<-EOT
    LocalStack endpoint. Empty = real AWS.

    This layer is never *applied* against LocalStack — Fargate, the ALB and
    application autoscaling are not emulated by LocalStack Community. It is
    set only to run `terraform plan` against an applied LocalStack data layer,
    which proves the cross-layer references resolve.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.localstack_endpoint == "" || can(regex("^https?://", var.localstack_endpoint))
    error_message = "localstack_endpoint must be empty or an http(s) URL."
  }
}
