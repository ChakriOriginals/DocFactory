# Queues.
#
# The same three logical queues the code has used since Phase 1, each with a
# DLQ and the same redrive policy: after `max_receive_count` failed receives the
# queue service moves the message aside. That policy came from ElasticMQ
# locally and carries here unchanged, which is the whole point of having coded
# against the SQS API from the start.

locals {
  max_receive_count = 3

  queues = {
    ingest  = { name = "${local.name}-ingest", visibility = 30 }
    parse   = { name = "${local.name}-parse", visibility = 10 }
    extract = { name = "${local.name}-extract", visibility = 90 }
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each = local.queues

  name                      = "${each.value.name}-dlq"
  message_retention_seconds = 1209600 # 14 days: long enough to actually look

  tags = { Name = "${each.value.name}-dlq" }
}

resource "aws_sqs_queue" "main" {
  for_each = local.queues

  name                       = each.value.name
  visibility_timeout_seconds = each.value.visibility
  receive_wait_time_seconds  = 10 # long polling: fewer empty receives, lower cost

  # 14 days, matching the DLQs, because the default is 4 and the park destroys
  # the workers.
  #
  # The DLQs got a deliberate 14 ("long enough to actually look") and the live
  # queues were left unset, which means the AWS default of 4 days. So the
  # FAILURE path was preserved for a fortnight while the path carrying real
  # work was preserved for a long weekend. Park on a Thursday, come back the
  # following Wednesday, and any document queued at park time is gone — no
  # error, no DLQ entry, no alarm. SQS bills per request, not per message-day,
  # so the shorter window bought nothing.
  message_retention_seconds = 1209600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = local.max_receive_count
  })

  tags = { Name = each.value.name }
}

# The batch path from 4b, with the local bridge deleted: S3 publishes the
# object-created event straight to SQS. The event body and the worker handler
# that consumes it are identical to the MinIO path, which is why this is a
# configuration change rather than new code.
resource "aws_sqs_queue_policy" "ingest_from_s3" {
  queue_url = aws_sqs_queue.main["ingest"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowS3ObjectCreatedNotifications"
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.main["ingest"].arn
      Condition = {
        ArnEquals    = { "aws:SourceArn" = aws_s3_bucket.documents.arn }
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}
