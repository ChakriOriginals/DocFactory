terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # State lives locally by default so a first `apply` needs no chicken-and-egg
  # bootstrap bucket. The compute layer reads THIS state file directly
  # (compute-plane/data_plane.tf), so if you move it to S3, move both and
  # update the remote-state config there to match.
  # backend "s3" {
  #   bucket       = "docfactory-tfstate-<account-id>"
  #   key          = "docfactory/data-plane.tfstate"
  #   region       = "us-east-1"
  #   use_lockfile = true
  # }
}

# Provider, with an opt-in LocalStack mode.
#
# `localstack_endpoint = ""` (the default, and the only value a real deploy
# ever uses) leaves every endpoint at its AWS default and lets the ordinary
# credential chain apply. Setting it flips the whole provider at once:
# endpoints move to the emulator, credentials become the literal string "test",
# and the metadata/credential checks that would reach out to AWS are skipped.
#
# The two modes cannot be half-applied: the credentials are overridden in the
# same conditional as the endpoints, so a LocalStack run physically cannot
# authenticate against real AWS even if a profile is exported.
provider "aws" {
  region = var.aws_region

  access_key = local.localstack ? "test" : null
  secret_key = local.localstack ? "test" : null

  skip_credentials_validation = local.localstack
  skip_metadata_api_check     = local.localstack
  skip_requesting_account_id  = local.localstack
  s3_use_path_style           = local.localstack

  dynamic "endpoints" {
    for_each = local.localstack ? [var.localstack_endpoint] : []

    content {
      ecr            = endpoints.value
      iam            = endpoints.value
      s3             = endpoints.value
      secretsmanager = endpoints.value
      sqs            = endpoints.value
      sts            = endpoints.value
    }
  }

  # Every resource carries these. The teardown check greps on project=docfactory
  # to find anything a destroy left behind — an orphan without a tag is an
  # orphan you pay for and never notice.
  default_tags {
    tags = {
      project     = "docfactory"
      environment = var.environment
      managed_by  = "terraform"
      owner       = var.owner
      layer       = "data-plane"
    }
  }
}
