terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # State lives locally by default so a first `apply` needs no chicken-and-egg
  # bootstrap bucket. Uncomment for a shared/CI state backend — CI deploys need
  # this, since a local state file in a runner is state you have already lost.
  # backend "s3" {
  #   bucket       = "docfactory-tfstate-<account-id>"
  #   key          = "docfactory/terraform.tfstate"
  #   region       = "us-east-1"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  # Every resource carries these. The teardown check greps on project=docfactory
  # to find anything a destroy left behind — an orphan without a tag is an
  # orphan you pay for and never notice.
  default_tags {
    tags = {
      project     = "docfactory"
      environment = var.environment
      managed_by  = "terraform"
      owner       = var.owner
    }
  }
}
