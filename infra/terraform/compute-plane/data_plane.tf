# The dependency, made explicit.
#
# DIRECTION: compute-plane depends on data-plane. Never the reverse.
#
# The data layer owns the bucket, the queues, the secrets, the registries and
# the IAM roles; this layer owns the VPC, the ALB, the ECS services and the
# autoscaling. Every cross-layer reference in this directory goes through
# `local.data_plane.*` and therefore through the data layer's declared outputs
# — there is no data source here that reaches around them and looks a resource
# up by name.
#
# WHY THE DIRECTION MATTERS OPERATIONALLY: because nothing in the data layer
# refers to anything here, `terraform destroy` in this directory is a complete,
# safe operation on its own. It removes the ALB (~$16/month, the stack's whole
# idle cost) and every Fargate task, and leaves the documents bucket, the
# queues and their contents, the secrets and the pushed images untouched. Bring
# the compute layer back up the next morning and the pipeline resumes against
# the same data. The reverse — destroying the data layer while compute is up —
# is not a supported operation and Terraform will not warn you about it,
# because the reference does not exist in that direction. Order is: apply data,
# apply compute; destroy compute, destroy data.

# Reads the data layer's state from S3, matching that layer's backend.
#
# This used to read a LOCAL path, ../data-plane/terraform.tfstate. Moving the
# data layer to a remote backend broke it immediately and loudly:
#
#   Error: Unable to find remote state
#   Plan: 15 to add   <- against an empty data layer
#
# It fails closed, which is the only reason this was a five-minute fix rather
# than an outage: the plan errors instead of quietly resolving every
# `local.data_plane.*` to null and building a compute layer wired to nothing.
# If this ever reads a stale local file again, that is the failure mode to
# expect — a plan that wants to create everything.
data "terraform_remote_state" "data_plane" {
  backend = "s3"

  config = {
    bucket = "docfactory-tfstate-215472107457"
    key    = "data-plane/terraform.tfstate"
    region = "us-east-2"
  }
}

# A cross-layer contract worth failing on early. The two layers derive resource
# names from the same prefix + environment; if they disagree, this layer would
# happily build an ALB in front of nothing and wire tasks to another stack's
# queues.
#
# THIS IS ENFORCED TWICE, ON PURPOSE. A `check` block assertion is a WARNING:
# it does not fail plan and does not fail apply. This file used to claim it
# stopped the plan, and so did variables.tf and terraform.tfvars.example. All
# three were wrong -- a mismatched name would have printed a warning nobody
# reads in a wall of plan output, and then built the wrong stack.
#
# The check block stays because it reports BOTH mismatches in one run, which is
# the better operator experience. The precondition below is what actually
# stops the apply.
check "layers_agree" {
  assert {
    condition     = local.name == local.data_plane.name
    error_message = "compute-plane name '${local.name}' != data-plane name '${local.data_plane.name}'. name_prefix/environment must match between the two layers."
  }

  assert {
    condition     = var.aws_region == local.data_plane.aws_region
    error_message = "compute-plane region '${var.aws_region}' != data-plane region '${local.data_plane.aws_region}'."
  }
}

# The gate that actually holds. A null_resource precondition is evaluated
# during plan and FAILS it -- unlike a check block, which only warns. Nothing
# is created; this exists solely to refuse to proceed against the wrong layer.
resource "terraform_data" "layers_agree" {
  lifecycle {
    precondition {
      condition = local.name == local.data_plane.name
      error_message = join(" ", [
        "compute-plane name '${local.name}' != data-plane name '${local.data_plane.name}'.",
        "name_prefix/environment must match between the two layers.",
      ])
    }

    precondition {
      condition = var.aws_region == local.data_plane.aws_region
      error_message = join(" ", [
        "compute-plane region '${var.aws_region}' != data-plane region",
        "'${local.data_plane.aws_region}'.",
      ])
    }
  }
}
