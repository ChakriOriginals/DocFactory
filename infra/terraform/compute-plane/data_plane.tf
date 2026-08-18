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

data "terraform_remote_state" "data_plane" {
  backend = "local"

  config = {
    path = "${path.module}/../data-plane/terraform.tfstate"
  }
}

# A cross-layer contract worth failing on early. The two layers derive resource
# names from the same prefix + environment; if they disagree, this layer would
# happily build an ALB in front of nothing and wire tasks to another stack's
# queues. Better to stop at plan time.
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
