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

# Storage is the one cost here that only ever goes up.
#
# The original policy expired untagged images only. CI tags every image with the
# commit SHA, so nothing it pushed was ever untagged, and nothing it pushed was
# ever removed -- measured at 2.57 GB across both repositories, growing about
# $0.05/month with every merge to main, forever. On a stack whose whole standing
# cost is $13.67/month that is not nothing, and it is invisible: no alarm
# watches it and it never spikes.
#
# WHY THE COUNT RULE IS SAFE ONLY BECAUSE CI MOVES `latest`.
#
# Rule 2 keeps the newest images by push time and expires the rest. That would
# be actively dangerous on its own: Terraform's task definitions reference
# `:latest` (see compute-plane ecs.tf), and until today `latest` was a
# hand-pushed image CI never touched. It was already the third-newest tag and
# sinking with every deploy -- a count rule would eventually have expired the
# tag the migrate task definition points at, and the next `terraform apply`
# would have registered a task definition referencing an image that no longer
# exists.
#
# The deploy workflow now pushes `:latest` alongside `:$SHA` on every deploy, so
# `latest` always rides on the newest image and can never age out. Do not adopt
# a count rule in a repository where `latest` is not maintained that way.
resource "aws_ecr_lifecycle_policy" "expire_untagged" {
  for_each = {
    api    = aws_ecr_repository.api.name
    worker = aws_ecr_repository.worker.name
  }

  repository = each.value

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after a day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the newest ${var.ecr_image_retention_count} images; expire older deploys"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.ecr_image_retention_count
        }
        action = { type = "expire" }
      },
    ]
  })
}
