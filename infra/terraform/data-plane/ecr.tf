# Image registries.
#
# force_delete = true is deliberate: without it, `terraform destroy` fails on a
# repository that still holds images, and you are left with a repository you
# forgot about, paying storage. Images are rebuilt by CI in a minute; keeping
# them is not worth an orphan.

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "worker" {
  name                 = "${local.name}-worker"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Untagged layers accumulate on every CI push and cost storage forever.
resource "aws_ecr_lifecycle_policy" "expire_untagged" {
  for_each = {
    api    = aws_ecr_repository.api.name
    worker = aws_ecr_repository.worker.name
  }

  repository = each.value

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after a day"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 1
      }
      action = { type = "expire" }
    }]
  })
}
