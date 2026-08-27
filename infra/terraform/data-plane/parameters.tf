# Secret material, in SSM Parameter Store.
#
# Nothing sensitive is an environment variable in a task definition: those are
# visible to anyone who can call DescribeTaskDefinition. The task pulls them at
# runtime, and the execution role's permission to read them is scoped to
# exactly these three parameters.
#
# WHY NOT SECRETS MANAGER, WHICH THIS USED TO BE. $0.40 per secret per month,
# so $1.20 — which sounds trivial until you notice it was 92% of what this
# stack costs at rest once the compute layer is destroyed. SSM Parameter Store
# Standard parameters are free, hold SecureString values encrypted with KMS
# exactly as Secrets Manager does, and ECS reads them through the same
# `valueFrom` field. The task definitions did not change shape at all.
#
# What Secrets Manager offers that this does not: automatic rotation,
# cross-account sharing, and versioned staging labels. This project uses none
# of the three — the Neon URLs change when a human changes them, and there is
# one account. Paying $14/year for features nobody calls is not a security
# posture, it is a subscription.
#
# ONE GENUINE IMPROVEMENT FELL OUT OF THIS. Secrets Manager keeps a deleted
# secret's NAME reserved for a recovery window, so a destroy followed by an
# apply used to fail with "a secret with this name is scheduled for deletion" —
# which is why the old resources carried `recovery_window_in_days = 0` and why
# the orphan check probed for pending deletions. Parameters delete immediately.
# That whole failure mode is gone rather than worked around.

locals {
  parameter_prefix = "/${local.name}"
}

# Standard tier explicitly, never Advanced. Advanced parameters are $0.05 each
# per month and allow 8KB instead of 4KB; a connection string is a few hundred
# bytes, and the point of this file is that the number is zero.
resource "aws_ssm_parameter" "database_url_app" {
  name        = "${local.parameter_prefix}/database-url-app"
  description = "Neon connection string for the non-superuser app role."
  type        = "SecureString"
  tier        = "Standard"
  value       = var.neon_database_url_app
}

resource "aws_ssm_parameter" "database_url_owner" {
  name        = "${local.parameter_prefix}/database-url-owner"
  description = "Neon owner connection string. Migrations only - never the app tasks."
  type        = "SecureString"
  tier        = "Standard"
  value       = var.neon_database_url_owner
}

resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = "${local.parameter_prefix}/anthropic-api-key"
  description = "Model API key. Unused while MODEL_PROVIDER=mock."
  type        = "SecureString"
  tier        = "Standard"
  # A placeholder keeps the parameter readable when the stack runs in mock
  # mode, which is the default. SSM rejects an empty value outright.
  value = coalesce(var.anthropic_api_key, "unset-mock-mode")
}
