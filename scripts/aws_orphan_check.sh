#!/usr/bin/env bash
# Post-destroy orphan check.
#
# `terraform destroy` reports success when it has deleted everything it knows
# about. That is not the same as "nothing is billing": resources created
# outside the state file, resources whose deletion silently failed, and
# resources Terraform never owned all survive a clean-looking destroy. The ones
# that cost money quietly are, in rough order of how much they hurt:
#
#   NAT gateways      ~$32/mo each   (this stack creates none, by design)
#   ALBs              ~$16/mo each
#   Elastic IPs        ~$3.6/mo each when unattached
#   Fargate tasks      per second, and a stuck service keeps replacing them
#   Log groups         storage, forever, with no retention set
#
# Run this after every destroy. It looks for anything tagged project=docfactory
# plus the untagged categories that bite hardest.
#
#   ./scripts/aws_orphan_check.sh [region]
#
# Exit 0 = clean. Exit 1 = something survived; the output says what.

set -uo pipefail

REGION="${1:-${AWS_REGION:-us-east-1}}"
PROJECT_TAG="docfactory"
FOUND=0

command -v aws >/dev/null 2>&1 || {
  echo "aws CLI not installed — cannot check for orphans." >&2
  exit 2
}

echo "Orphan check: project=${PROJECT_TAG} in ${REGION}"
echo

report() { # name, payload
  if [ -n "${2//[[:space:]]/}" ] && [ "$2" != "None" ]; then
    echo "  ORPHAN  $1:"
    echo "$2" | sed 's/^/          /'
    FOUND=1
  else
    echo "  clean   $1"
  fi
}

# Tagged resources — the broad sweep. Anything this stack created carries the
# project tag, so one API call covers most of it.
tagged=$(aws resourcegroupstaggingapi get-resources \
  --region "$REGION" \
  --tag-filters "Key=project,Values=${PROJECT_TAG}" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null)
report "tagged resources" "$tagged"

# The expensive untagged categories, checked by hand because a resource created
# before the tag policy — or by a half-failed destroy — will not appear above.
nat=$(aws ec2 describe-nat-gateways --region "$REGION" \
  --filter "Name=state,Values=available,pending" \
  --query 'NatGateways[].NatGatewayId' --output text 2>/dev/null)
report "NAT gateways (should ALWAYS be empty — this stack creates none)" "$nat"

albs=$(aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[?contains(LoadBalancerName, '${PROJECT_TAG}')].LoadBalancerArn" \
  --output text 2>/dev/null)
report "load balancers" "$albs"

eips=$(aws ec2 describe-addresses --region "$REGION" \
  --query 'Addresses[?AssociationId==null].PublicIp' --output text 2>/dev/null)
report "unattached elastic IPs" "$eips"

clusters=$(aws ecs list-clusters --region "$REGION" \
  --query "clusterArns[?contains(@, '${PROJECT_TAG}')]" --output text 2>/dev/null)
report "ECS clusters" "$clusters"

# A service left at desired_count > 0 keeps launching tasks even with no
# cluster capacity — the classic "I destroyed it but it is still billing".
running=""
for cluster in $clusters; do
  tasks=$(aws ecs list-tasks --region "$REGION" --cluster "$cluster" \
    --query 'taskArns' --output text 2>/dev/null)
  [ -n "${tasks//[[:space:]]/}" ] && [ "$tasks" != "None" ] && running="${running} ${tasks}"
done
report "running ECS tasks" "$running"

logs=$(aws logs describe-log-groups --region "$REGION" \
  --log-group-name-prefix "/ecs/${PROJECT_TAG}" \
  --query 'logGroups[].logGroupName' --output text 2>/dev/null)
report "log groups" "$logs"

queues=$(aws sqs list-queues --region "$REGION" \
  --queue-name-prefix "${PROJECT_TAG}" --query 'QueueUrls' --output text 2>/dev/null)
report "SQS queues" "$queues"

# Secrets are the one thing where "still there" is usually correct: a deleted
# secret with a recovery window keeps its NAME reserved, which breaks the next
# apply. This stack sets recovery_window_in_days = 0 so they go immediately.
pending=$(aws secretsmanager list-secrets --region "$REGION" \
  --include-planned-deletion \
  --query "SecretList[?contains(Name, '${PROJECT_TAG}') && DeletedDate!=null].Name" \
  --output text 2>/dev/null)
report "secrets pending deletion (would block the next apply)" "$pending"

# S3 is EXPECTED to survive: the documents bucket is deliberately outside the
# destroy blast radius. Reported for visibility, never counted as an orphan.
buckets=$(aws s3api list-buckets \
  --query "Buckets[?contains(Name, '${PROJECT_TAG}')].Name" --output text 2>/dev/null)
echo "  kept    S3 buckets (data survives destroy by design):"
echo "${buckets:-  (none)}" | sed 's/^/          /'

echo
if [ "$FOUND" -eq 0 ]; then
  echo "CLEAN — nothing billable survived the destroy."
else
  echo "ORPHANS FOUND — see above. Delete them, or re-run terraform destroy."
  echo "If Terraform no longer tracks them, delete by ARN in the console."
fi
exit "$FOUND"
