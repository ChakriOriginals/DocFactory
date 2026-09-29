#!/usr/bin/env bash
# Where is the API right now? Run as `make api-url`.
#
# With no load balancer, the API task's public IP is the endpoint, and it
# changes whenever the task is replaced. So this asks ECS rather than reading a
# value that goes stale.
#
# "No address" has four causes with four different fixes, so this names the
# one it found instead of guessing:
#
#   no cluster          the stack is parked; terraform apply brings it back
#   desired count 0     scaled to zero by `make aws-park`; `make aws-unpark`
#   no task / no IP     still starting. ECS places the task about 15s after
#                       `terraform apply` returns, and it takes another ~30s to
#                       get an address. This used to answer "is the stack up?"
#                       about a stack that was up, and point at a fix
#                       (`make aws-unpark`) that fails after a destroy-park.
#   the lookup failed   the AWS error, verbatim
#
# The region is printed with every failure because a CLI pointed at the wrong
# region also finds no cluster, and that must not read as "parked".
#
# Usage: api_url.sh [cluster] [service]   (service defaults to <cluster>-api)
set -uo pipefail

cluster="${1:-docfactory-dev}"
service="${2:-$cluster-api}"
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null)}}"
region="${region:-no default region}"

fail() {
  echo "$*" >&2
  exit 1
}

if ! task=$(aws ecs list-tasks --cluster "$cluster" --service-name "$service" \
  --desired-status RUNNING --query 'taskArns[0]' --output text 2>&1); then
  case "$task" in
    *ClusterNotFound*) fail "No $cluster cluster in $region. If that is the right region," \
      "the stack is parked: bring it back with (cd infra/terraform/compute-plane && terraform apply)" ;;
    *ServiceNotFound*) fail "No $service service in the $cluster cluster ($region)." \
      "Run terraform apply in infra/terraform/compute-plane." ;;
    *) fail "Could not look up the API task in $region: $task" ;;
  esac
fi

if [ "$task" = "None" ] || [ -z "$task" ]; then
  desired=$(aws ecs describe-services --cluster "$cluster" --services "$service" \
    --query 'services[0].desiredCount' --output text 2>&1) ||
    fail "Could not read the $service service in $region: $desired"
  case "$desired" in
    0) fail "The API is scaled to zero (make aws-park). make aws-unpark brings it back." ;;
    None) fail "No $service service in the $cluster cluster ($region)." \
      "Run terraform apply in infra/terraform/compute-plane." ;;
    *) fail "The API task is still starting: ECS places it about 15s after terraform apply." \
      "Try again in a minute." ;;
  esac
fi

eni=$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task" \
  --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value | [0]" \
  --output text 2>&1) || fail "Could not describe the API task in $region: $eni"
if [ "$eni" = "None" ] || [ -z "$eni" ]; then
  fail "The API task is still starting (no network interface yet). Try again in a minute."
fi

ip=$(aws ec2 describe-network-interfaces --network-interface-ids "$eni" \
  --query 'NetworkInterfaces[0].Association.PublicIp' --output text 2>&1) ||
  fail "Could not read the API task's network interface in $region: $ip"
if [ "$ip" = "None" ] || [ -z "$ip" ]; then
  fail "The API task is still starting (no public IP yet). Try again in a minute."
fi

echo "http://$ip:8000"
