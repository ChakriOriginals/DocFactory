#!/usr/bin/env bash
# Create the Terraform state bucket. Run once per account, by hand.
#
# This bucket is deliberately NOT managed by Terraform. A state bucket
# described by the configuration whose state it holds is circular: destroying
# the layer would destroy the record of what was being destroyed, part-way
# through doing it. So it is bootstrapped here and left alone.
#
# Safe to re-run: every call is idempotent.
set -euo pipefail

ACCOUNT="${1:-$(aws sts get-caller-identity --query Account --output text)}"
REGION="${2:-us-east-2}"
BUCKET="docfactory-tfstate-${ACCOUNT}"

echo "state bucket: s3://${BUCKET} (${REGION})"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "  exists already"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  echo "  created"
fi

# Versioning is the one non-negotiable setting. A corrupted or truncated state
# is recovered by rolling back to the previous version; without versioning the
# only copy is the broken one.
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
echo "  versioning enabled"

aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
echo "  public access blocked"

# State holds database passwords in cleartext. Encrypt at rest.
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
echo "  encryption enabled"

aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration \
  '{"Rules":[{"ID":"reclaim-old-state-versions","Status":"Enabled","Filter":{},"NoncurrentVersionExpiration":{"NoncurrentDays":90},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}' >/dev/null
echo "  90 days of state history kept"

echo
echo "Now migrate each layer:"
echo "  cd infra/terraform/data-plane    && terraform init -migrate-state"
echo "  cd infra/terraform/compute-plane && terraform init -migrate-state"
