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
# Exit 3 = one or more checks could not run, so the answer is UNKNOWN.
#
# THE THIRD EXIT CODE IS THE POINT. The first version of this script piped
# every call's stderr to /dev/null and reported a failed call as "clean" — an
# expired SSO session, a missing permission or a wrong region would have
# printed CLEAN over a running ALB. Found by running the script against
# LocalStack in Phase 4c.5b, where several services are simply not implemented
# and every one of them came back "clean". A check that cannot distinguish
# "nothing there" from "I could not look" is worse than no check, because it
# is trusted.

set -uo pipefail

REGION="${1:-${AWS_REGION:-us-east-1}}"
PROJECT_TAG="docfactory"
FOUND=0
UNKNOWN=0

command -v aws >/dev/null 2>&1 || {
  echo "aws CLI not installed — cannot check for orphans." >&2
  exit 2
}

# probe NAME -- aws args...
#
# Three outcomes, never two: clean, orphan, or "the check itself failed".
probe() {
  local name="$1"
  shift
  [ "$1" = "--" ] && shift

  local stderr_file output status
  stderr_file="$(mktemp)"
  output="$("$@" 2>"$stderr_file")"
  status=$?

  if [ "$status" -ne 0 ]; then
    echo "  UNKNOWN $name — the check could not run (exit $status):"
    tail -2 "$stderr_file" | sed 's/^/          /'
    UNKNOWN=1
    rm -f "$stderr_file"
    return 2
  fi
  rm -f "$stderr_file"

  report "$name" "$output"
}

report() { # name, payload
  if [ -n "${2//[[:space:]]/}" ] && [ "$2" != "None" ]; then
    echo "  ORPHAN  $1:"
    echo "$2" | sed 's/^/          /'
    FOUND=1
  else
    echo "  clean   $1"
  fi
}

echo "Orphan check: project=${PROJECT_TAG} in ${REGION}"
echo

# Tagged resources — the broad sweep. Anything this stack created carries the
# project tag, so one API call covers most of it.
probe "tagged resources" -- \
  aws resourcegroupstaggingapi get-resources \
  --region "$REGION" \
  --tag-filters "Key=project,Values=${PROJECT_TAG}" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text

# The expensive untagged categories, checked by hand because a resource created
# before the tag policy — or by a half-failed destroy — will not appear above.
probe "NAT gateways (should ALWAYS be empty — this stack creates none)" -- \
  aws ec2 describe-nat-gateways --region "$REGION" \
  --filter "Name=state,Values=available,pending" \
  --query 'NatGateways[].NatGatewayId' --output text

probe "load balancers" -- \
  aws elbv2 describe-load-balancers --region "$REGION" \
  --query "LoadBalancers[?contains(LoadBalancerName, '${PROJECT_TAG}')].LoadBalancerArn" \
  --output text

probe "unattached elastic IPs" -- \
  aws ec2 describe-addresses --region "$REGION" \
  --query 'Addresses[?AssociationId==null].PublicIp' --output text

clusters="$(aws ecs list-clusters --region "$REGION" \
  --query "clusterArns[?contains(@, '${PROJECT_TAG}')]" --output text 2>/dev/null)"
cluster_status=$?
if [ "$cluster_status" -ne 0 ]; then
  echo "  UNKNOWN ECS clusters — the check could not run (exit $cluster_status)"
  echo "  UNKNOWN running ECS tasks — skipped, no cluster list"
  UNKNOWN=1
else
  report "ECS clusters" "$clusters"
  # A service left at desired_count > 0 keeps launching tasks even with no
  # cluster capacity — the classic "I destroyed it but it is still billing".
  running=""
  task_error=0
  for cluster in $clusters; do
    tasks="$(aws ecs list-tasks --region "$REGION" --cluster "$cluster" \
      --query 'taskArns' --output text 2>/dev/null)" || task_error=1
    [ -n "${tasks//[[:space:]]/}" ] && [ "$tasks" != "None" ] && running="${running} ${tasks}"
  done
  if [ "$task_error" -ne 0 ]; then
    echo "  UNKNOWN running ECS tasks — at least one list-tasks call failed"
    UNKNOWN=1
  else
    report "running ECS tasks" "$running"
  fi
fi

probe "log groups" -- \
  aws logs describe-log-groups --region "$REGION" \
  --log-group-name-prefix "/ecs/${PROJECT_TAG}" \
  --query 'logGroups[].logGroupName' --output text

probe "SQS queues" -- \
  aws sqs list-queues --region "$REGION" \
  --queue-name-prefix "${PROJECT_TAG}" --query 'QueueUrls' --output text

probe "ECR repositories" -- \
  aws ecr describe-repositories --region "$REGION" \
  --query "repositories[?contains(repositoryName, '${PROJECT_TAG}')].repositoryName" \
  --output text

# Secrets are the one thing where "still there" is usually correct: a deleted
# secret with a recovery window keeps its NAME reserved, which breaks the next
# apply. This stack sets recovery_window_in_days = 0 so they go immediately.
probe "secrets pending deletion (would block the next apply)" -- \
  aws secretsmanager list-secrets --region "$REGION" \
  --include-planned-deletion \
  --query "SecretList[?contains(Name, '${PROJECT_TAG}') && DeletedDate!=null].Name" \
  --output text

# S3 is EXPECTED to survive: the documents bucket is deliberately outside the
# destroy blast radius. Reported for visibility, never counted as an orphan.
if buckets="$(aws s3api list-buckets \
  --query "Buckets[?contains(Name, '${PROJECT_TAG}')].Name" --output text 2>/dev/null)"; then
  echo "  kept    S3 buckets (data survives destroy by design):"
  echo "${buckets:-  (none)}" | sed 's/^/          /'
else
  echo "  UNKNOWN S3 buckets — the check could not run"
  UNKNOWN=1
fi

echo
if [ "$UNKNOWN" -ne 0 ] && [ "$FOUND" -eq 0 ]; then
  echo "UNKNOWN — some checks did not run, so this is NOT a clean bill of health."
  echo "Fix the failing calls (credentials? region? permissions?) and re-run."
  exit 3
fi
if [ "$FOUND" -eq 0 ]; then
  echo "CLEAN — nothing billable survived the destroy."
  exit 0
fi
echo "ORPHANS FOUND — see above. Delete them, or re-run terraform destroy."
echo "If Terraform no longer tracks them, delete by ARN in the console."
[ "$UNKNOWN" -ne 0 ] && echo "Some checks also failed to run; the list above may be incomplete."
exit 1
