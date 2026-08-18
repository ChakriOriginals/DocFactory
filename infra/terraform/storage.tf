# Document storage.
#
# DESTROY BLAST RADIUS: this bucket is the one thing in the stack that holds
# customer data, and `terraform destroy` is a routine cost-control action here.
# So the bucket refuses to be destroyed while it has objects in it unless
# `force_destroy_documents` is explicitly set. Compute is disposable; documents
# are not, and the difference has to be enforced by the tooling rather than by
# remembering.

data "aws_caller_identity" "current" {}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "documents" {
  bucket = coalesce(
    var.documents_bucket_name,
    "${local.name}-documents-${random_id.bucket_suffix.hex}",
  )

  # Guard rail, not convenience: destroy fails on a non-empty bucket.
  force_destroy = var.force_destroy_documents

  tags = { Name = "${local.name}-documents" }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket = aws_s3_bucket.documents.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Batch ingestion. The filter matches the drop prefix the code already routes
# on ({tenant}/dropbox/...), so the pipeline's own artifacts — incoming
# uploads and parsed text — never re-trigger ingestion.
resource "aws_s3_bucket_notification" "documents" {
  bucket = aws_s3_bucket.documents.id

  queue {
    id            = "ingest"
    queue_arn     = aws_sqs_queue.main["ingest"].arn
    events        = ["s3:ObjectCreated:*"]
    filter_suffix = ".pdf"
  }

  depends_on = [aws_sqs_queue_policy.ingest_from_s3]
}
