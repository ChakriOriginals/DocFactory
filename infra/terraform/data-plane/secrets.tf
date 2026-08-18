# Secrets.
#
# Nothing sensitive is an environment variable in a task definition: those are
# visible to anyone who can call DescribeTaskDefinition. The task pulls them at
# runtime from Secrets Manager, and the execution role's permission to read
# them is scoped to exactly these ARNs.
#
# recovery_window_in_days = 0 matters for a stack that gets destroyed and
# rebuilt: the default 30-day recovery window keeps the *name* reserved, so the
# next `apply` fails with "a secret with this name is scheduled for deletion".
# It also means these are genuinely gone on destroy, which is the intent.

resource "aws_secretsmanager_secret" "database_url_app" {
  name                    = "${local.name}/database-url-app"
  description             = "Neon connection string for the non-superuser app role."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url_app" {
  secret_id     = aws_secretsmanager_secret.database_url_app.id
  secret_string = var.neon_database_url_app
}

resource "aws_secretsmanager_secret" "database_url_owner" {
  name                    = "${local.name}/database-url-owner"
  description             = "Neon owner connection string. Migrations only — never the app tasks."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url_owner" {
  secret_id     = aws_secretsmanager_secret.database_url_owner.id
  secret_string = var.neon_database_url_owner
}

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name                    = "${local.name}/anthropic-api-key"
  description             = "Model API key. Unused while MODEL_PROVIDER=mock."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "anthropic_api_key" {
  secret_id = aws_secretsmanager_secret.anthropic_api_key.id
  # Placeholder keeps the secret readable when the stack runs in mock mode,
  # which is the default. An empty string is not a valid secret value.
  secret_string = coalesce(var.anthropic_api_key, "unset-mock-mode")
}
