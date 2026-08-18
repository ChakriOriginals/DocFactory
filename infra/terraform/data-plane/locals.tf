locals {
  name = "${var.name_prefix}-${var.environment}"

  localstack = var.localstack_endpoint != ""
}
