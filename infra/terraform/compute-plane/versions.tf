terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # backend "s3" {
  #   bucket       = "docfactory-tfstate-<account-id>"
  #   key          = "docfactory/compute-plane.tfstate"
  #   region       = "us-east-1"
  #   use_lockfile = true
  # }
}

# Same opt-in LocalStack mode as the data layer, and for the same reason: the
# two layers must be configured identically or a cross-layer reference resolves
# against the wrong account.
#
# This layer is NOT applied against LocalStack. Fargate, the ALB and
# application autoscaling are not emulated by LocalStack Community, so a
# "successful" apply here would be a lie. What IS run against LocalStack is
# `terraform plan`, which is worth doing: it proves every reference into the
# data layer's outputs resolves against a real, applied state file.
provider "aws" {
  region = var.aws_region

  access_key = local.localstack ? "test" : null
  secret_key = local.localstack ? "test" : null

  skip_credentials_validation = local.localstack
  skip_metadata_api_check     = local.localstack
  skip_requesting_account_id  = local.localstack

  dynamic "endpoints" {
    for_each = local.localstack ? [var.localstack_endpoint] : []

    content {
      cloudwatch             = endpoints.value
      ec2                    = endpoints.value
      ecs                    = endpoints.value
      elbv2                  = endpoints.value
      iam                    = endpoints.value
      logs                   = endpoints.value
      applicationautoscaling = endpoints.value
      sqs                    = endpoints.value
      sts                    = endpoints.value
    }
  }

  default_tags {
    tags = {
      project     = "docfactory"
      environment = var.environment
      managed_by  = "terraform"
      owner       = var.owner
      layer       = "compute-plane"
    }
  }
}
