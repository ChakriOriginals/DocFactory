# --- naming and placement ---------------------------------------------------

variable "aws_region" {
  description = "Region for every resource in this layer. Must match the compute layer AND Neon."
  type        = string
  default     = "us-east-2"
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
  description = "Prefix for resource names. The compute layer asserts it matches."
  type        = string
  default     = "docfactory"
}

# --- storage ----------------------------------------------------------------

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

# --- secrets ----------------------------------------------------------------

variable "neon_database_url_owner" {
  description = <<-EOT
    Neon connection string for the OWNER role, used by migrations only.

    Passed at apply time (TF_VAR_neon_database_url_owner) and stored as a
    SecureString in SSM Parameter Store at /docfactory-<env>/database-url-owner;
    never written to a file in the repo. Neon rather than RDS is a deliberate
    choice — see infra/terraform/README.md.
  EOT
  type        = string
  sensitive   = true

  validation {
    # parameters.tf rewrites sslmode to verify-full. A URL with no sslmode at
    # all would make that rewrite a silent no-op and ship an unverified
    # connection, so refuse it here instead. This one can also change the schema.
    condition     = can(regex("sslmode=[a-z-]+", var.neon_database_url_owner))
    error_message = "The connection string must carry an sslmode= parameter; it is rewritten to verify-full before being stored."
  }
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

  validation {
    # parameters.tf rewrites sslmode to verify-full. A URL with no sslmode at
    # all would make that rewrite a silent no-op and ship an unverified
    # connection, so refuse it here instead. This is the role every task connects as.
    condition     = can(regex("sslmode=[a-z-]+", var.neon_database_url_app))
    error_message = "The connection string must carry an sslmode= parameter; it is rewritten to verify-full before being stored."
  }
}

variable "anthropic_api_key" {
  description = "Model API key. Empty is fine: the deployed default is mock mode."
  type        = string
  sensitive   = true
  default     = ""
}

# --- cost guard rails --------------------------------------------------------

variable "monthly_budget_usd" {
  description = <<-EOT
    Monthly cost budget, in whole USD. Notifications fire at 50% and 100% of
    actual spend and at a 100% forecast.

    10 is chosen to be uncomfortable rather than generous: this stack's designed
    idle cost is the ALB at roughly $16/month if it is left standing all month,
    so a $10 budget breaches BEFORE a forgotten stack completes its first full
    month. A budget you never hit teaches you nothing.
  EOT
  type        = number
  default     = 10

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be positive."
  }
}

variable "credit_balance_usd" {
  description = <<-EOT
    Promotional credits on the account, for the runway figure in the outputs.

    Documentation only — Terraform cannot read a credit balance, and nothing
    enforces this. It exists so `terraform output cost_runway` can say
    something concrete instead of leaving the arithmetic to the reader.

    Two things credits do NOT cover, worth knowing before relying on the
    number: the Anthropic API is billed by Anthropic, not AWS, so a real-model
    run spends money that no AWS credit touches; and support plans and some
    Marketplace charges are excluded from most credit programmes.
  EOT
  type        = number
  default     = 100

  validation {
    condition     = var.credit_balance_usd >= 0
    error_message = "credit_balance_usd cannot be negative."
  }
}

variable "cost_alert_email" {
  description = <<-EOT
    Where budget and billing alarms go. Empty creates the SNS topic and the
    budget without a subscriber — the alarms still fire, nobody hears them.

    AWS sends a confirmation email; an unconfirmed subscription is a silent
    alarm. Confirm it, then verify with the command in the runbook's prereqs.
  EOT
  type        = string
  default     = ""
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

variable "github_owner_id" {
  description = <<-EOT
    Numeric account ID of the repository owner, as a string.

    GitHub's default OIDC subject claim embeds immutable numeric IDs:
      repo:OWNER@<owner_id>/REPO@<repo_id>:environment:dev
    not the "repo:OWNER/REPO:..." every guide shows. Without these, the trust
    policy does not match and the deploy fails with a bare "Not authorized to
    perform sts:AssumeRoleWithWebIdentity".

    Find them with:
      gh api repos/OWNER/REPO --jq '"owner=\(.owner.id) repo=\(.id)"'

    Or read the prefix GitHub will actually send:
      gh api repos/OWNER/REPO/actions/oidc/customization/sub

    Leave empty to match only the legacy name-only claim.
  EOT
  type        = string
  default     = ""
}

variable "github_repository_id" {
  description = "Numeric ID of the repository, as a string. See github_owner_id."
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

# --- local emulation --------------------------------------------------------

variable "localstack_endpoint" {
  description = <<-EOT
    LocalStack endpoint (e.g. "http://localhost:4566"). Empty = real AWS.

    Set ONLY by the local validation run (see infra/localstack/). When set, the
    provider also pins credentials to dummy values, so this layer cannot reach
    a real account by accident.

    What a green LocalStack apply proves: the resources, their ARNs, their
    dependency order, the S3 -> SQS notification configuration and the queues'
    RedrivePolicy. What it does NOT prove: that the IAM policies below are
    sufficient. LocalStack Community creates IAM objects but does not enforce
    them, so every call succeeds regardless of policy. See 4c.5c in
    docs/deploy_runbook.md for the static cross-check that covers the gap.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.localstack_endpoint == "" || can(regex("^https?://", var.localstack_endpoint))
    error_message = "localstack_endpoint must be empty or an http(s) URL."
  }
}

variable "ecr_image_retention_count" {
  description = <<-EOT
    How many images to keep per repository. Everything older is expired.

    Each image is about 260 MB, so this is roughly $0.026/month each, against
    an ECR free tier of 0.5 GB. Five per repository is ~2.6 GB, about $0.21/mo
    after the free allowance -- close to what the unbounded policy already
    costs today, with the difference that it stops growing.

    Five is chosen for rollback depth, not for storage: it leaves the running
    image plus four previous deploys to fall back to. Lower it to 3 to save
    about $0.10/month if you never roll back by hand; raise it if you want
    deeper history. Do not set it below 2 -- the ECS deployment circuit breaker
    rolls back to the previous task definition, and that image has to still
    exist for the rollback to pull.
  EOT
  type        = number
  default     = 5

  validation {
    condition     = var.ecr_image_retention_count >= 2
    error_message = "Keep at least 2 images: a rollback needs the previous image to still exist."
  }
}

variable "noncurrent_version_retention_days" {
  description = <<-EOT
    How long a superseded object version survives after being replaced.

    The bucket is versioned, so every overwrite leaves the old bytes behind and
    every delete leaves a delete marker over a version that still exists and
    still bills. Without this rule that history is unbounded and invisible —
    it does not appear in the console's object listing.

    30 days is long enough to undo a mistake and short enough that the tail
    does not grow forever. It cannot delete a current object.
  EOT
  type        = number
  default     = 30

  validation {
    condition     = var.noncurrent_version_retention_days >= 1
    error_message = "Keep at least a day of version history; the point is to be able to undo."
  }
}

variable "document_retention_days" {
  description = <<-EOT
    Days to keep a client's documents before deleting them. 0 = keep forever.

    Deliberately 0. Retention is a contract term and the safe default is to
    keep: a too-short guess destroys the documents a client paid to have
    processed, and no alarm would fire. Set it when a client's agreement says
    a number, not before.

    Note this expires objects in S3 only. Rows in `documents` and `extractions`
    are not touched, so a real deletion request needs both — there is no
    delete endpoint yet, which is tracked separately.
  EOT
  type        = number
  default     = 0

  validation {
    condition     = var.document_retention_days == 0 || var.document_retention_days >= 7
    error_message = "Use 0 to keep documents, or at least 7 days. Anything shorter is almost certainly a typo."
  }
}
