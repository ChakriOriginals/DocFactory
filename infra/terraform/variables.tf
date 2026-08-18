variable "aws_region" {
  description = "Region for every resource in this stack."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name; part of every resource name so two stacks can coexist."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Tag value for who to ask before deleting something."
  type        = string
  default     = "docfactory"
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "docfactory"
}

# --- data plane -------------------------------------------------------------

variable "documents_bucket_name" {
  description = <<-EOT
    Bucket for tenant documents. Empty generates a unique name.

    This bucket is deliberately OUTSIDE the destroy blast radius: it holds
    customer documents, and a `terraform destroy` meant to stop compute charges
    must not delete data. See `force_destroy_documents`.
  EOT
  type        = string
  default     = ""
}

variable "force_destroy_documents" {
  description = <<-EOT
    Allow `terraform destroy` to delete the documents bucket AND its contents.

    false (default) means destroy fails loudly if the bucket is non-empty,
    which is the intended behaviour: compute is disposable, documents are not.
    Set true only for a scratch stack you are deliberately erasing.
  EOT
  type        = bool
  default     = false
}

variable "neon_database_url_owner" {
  description = <<-EOT
    Neon connection string for the OWNER role, used by migrations only.

    Passed at apply time (TF_VAR_neon_database_url_owner) and stored in Secrets
    Manager; never written to a file in the repo. Neon rather than RDS is a
    deliberate choice — see infra/terraform/README.md.
  EOT
  type        = string
  sensitive   = true
}

variable "neon_database_url_app" {
  description = <<-EOT
    Neon connection string for the NON-SUPERUSER app role (docfactory_app).

    This is what the API and worker tasks connect as. It has no DDL rights and
    does not bypass RLS — the isolation guarantees from Phase 3a depend on that
    being true in the cloud exactly as it is locally.
  EOT
  type        = string
  sensitive   = true
}

variable "anthropic_api_key" {
  description = "Model API key. Empty is fine: the deployed default is mock mode."
  type        = string
  sensitive   = true
  default     = ""
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

# --- CI ---------------------------------------------------------------------

variable "github_repository" {
  description = <<-EOT
    "owner/repo" allowed to assume the CI deploy role via OIDC.

    Empty skips the OIDC role entirely, so the stack stands up before a GitHub
    repository exists.
  EOT
  type        = string
  default     = ""
}

variable "github_oidc_provider_arn" {
  description = <<-EOT
    Existing GitHub OIDC provider ARN, if the account already has one.

    An AWS account may only have one provider per URL, so creating a second is
    an error — set this when another stack already created it.
  EOT
  type        = string
  default     = ""
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
