locals {
  name = "${var.name_prefix}-${var.environment}"

  localstack = var.localstack_endpoint != ""

  # Everything this layer borrows from the layer below, named once.
  data_plane = data.terraform_remote_state.data_plane.outputs
}
