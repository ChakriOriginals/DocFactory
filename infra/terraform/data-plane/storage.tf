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

# Versioning with no lifecycle keeps every version of every object forever.
#
# That is two problems wearing one hat. The cost one is mild and obvious:
# superseded versions and abandoned multipart uploads are invisible in the
# console's object list and bill anyway. The other is not about cost at all —
# a client who asks you to delete their documents cannot be told yes, because
# deleting an object under versioning writes a delete marker and keeps every
# prior version indefinitely.
#
# What this deliberately does NOT do is expire live documents. How long a
# client's data is kept is a contract term, not an infrastructure default, and
# guessing it wrong destroys the thing the client paid to have processed. The
# knob exists (document_retention_days) and defaults to 0, meaning keep.
resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  # Housekeeping only: this rule cannot touch a current object.
  rule {
    id     = "reclaim-superseded-and-abandoned"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Off unless a retention term is actually agreed. Deleting a client's
  # documents on a default would be worse than keeping them on one.
  dynamic "rule" {
    for_each = var.document_retention_days > 0 ? [1] : []

    content {
      id     = "expire-documents"
      status = "Enabled"

      filter {}

      expiration {
        days = var.document_retention_days
      }
    }
  }
}

# Batch ingestion.
#
# The comment here used to say the filter "matches the drop prefix the code
# already routes on ({tenant}/dropbox/...)". There is no prefix filter and
# never was — what actually keeps the pipeline's own artifacts from
# re-triggering ingestion is the suffix, because parsed text is written as
# `{tenant}/parsed/{id}.txt`, plus route_key rejecting any key whose second
# segment is not `dropbox`. Two real mechanisms, neither of them the one
# described.
#
# S3 SUFFIX FILTERS ARE CASE-SENSITIVE, which is the reason for the second
# rule. A client exporting `INVOICE.PDF` — and plenty of scanners and finance
# systems emit uppercase extensions — generates no event at all: not a message,
# not a row, not a log line. The object simply sits in the bucket while the
# client believes it was sent. That is a worse failure than a rejected file,
# because nothing anywhere records that anything arrived.
#
# Two rules rather than dropping the filter entirely: without any suffix
# filter, every parsed-text write would also raise an event, and the pipeline
# would spend an ingest receive rejecting its own output on every document.
resource "aws_s3_bucket_notification" "documents" {
  bucket = aws_s3_bucket.documents.id

  queue {
    id            = "ingest"
    queue_arn     = aws_sqs_queue.main["ingest"].arn
    events        = ["s3:ObjectCreated:*"]
    filter_suffix = ".pdf"
  }

  queue {
    id            = "ingest-uppercase"
    queue_arn     = aws_sqs_queue.main["ingest"].arn
    events        = ["s3:ObjectCreated:*"]
    filter_suffix = ".PDF"
  }

  depends_on = [aws_sqs_queue_policy.ingest_from_s3]
}
