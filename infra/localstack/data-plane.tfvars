# Variable values for the LocalStack validation run ONLY.
#
# Passed explicitly with `-var-file`, never named *.auto.tfvars, so it cannot
# be picked up by accident during a real apply. Every credential here is a
# deliberate dummy: if any of these values ever reached AWS, they would fail.

localstack_endpoint = "http://localhost:4566"

environment = "dev"
owner       = "localstack"
aws_region  = "us-east-1"

# Empty exercises the random-suffix naming path, the same as a real apply.
documents_bucket_name = ""

# A scratch emulator is exactly the case force_destroy exists for: the destroy
# half of this run must be able to remove a bucket with test objects in it.
# This is the opposite of the real-AWS default, and deliberately so.
force_destroy_documents = true

neon_database_url_owner = "postgresql+psycopg://owner:not-a-real-password@localstack.invalid/docfactory"
neon_database_url_app   = "postgresql+psycopg://docfactory_app:not-a-real-password@localstack.invalid/docfactory"
anthropic_api_key       = "sk-ant-not-a-real-key"

# Exercises the OIDC provider + deploy role path too.
github_repository = "docfactory/localstack-validation"
