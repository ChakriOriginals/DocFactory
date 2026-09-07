# --- naming and placement ---------------------------------------------------
#
# These three must match the data layer's. `check "layers_agree"` in
# data_plane.tf fails the plan if they do not.

variable "aws_region" {
  description = "Region for every resource in this layer. Must match the data layer AND Neon."
  type        = string
  default     = "us-east-2"
}

variable "environment" {
  description = "Environment name. Must match the data layer."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Tag value for who to ask before deleting something."
  type        = string
  default     = "docfactory"
}

variable "name_prefix" {
  description = "Prefix for resource names. Must match the data layer."
  type        = string
  default     = "docfactory"
}

# --- compute ----------------------------------------------------------------

variable "api_desired_count" {
  description = "API tasks. One is enough for a demo stack; the ALB is the cost, not the task."
  type        = number
  default     = 1
}

variable "worker_min_count" {
  description = "Minimum worker tasks. 0 means an idle stack runs no workers at all."
  type        = number
  default     = 0
}

variable "worker_max_count" {
  description = "Ceiling on worker fan-out under backlog."
  type        = number
  default     = 6
}

variable "enable_alb" {
  description = <<-EOT
    Put an Application Load Balancer in front of the API.

    DEFAULT false, and that is a cost decision. An ALB is $0.0225/hour — about
    $16.43/month — whether or not a single request reaches it, which on a
    standing stack was more than everything else combined except the API task
    itself. With it off, the API task keeps its public IP (there is no NAT
    gateway, so it already had one) and is reached directly on port 8000.

    What you give up, stated plainly: the address changes every time the task
    is replaced, there is no health-check-driven replacement in front of it,
    and there is no TLS. For a stack that is brought up for a demo and
    destroyed the same evening, that is the right trade. Set it true for a
    stable URL when you actually need one — an interview, a shared link — and
    it costs about $0.02 for the afternoon.

    `terraform output api_url` tells you the right thing either way.
  EOT
  type        = bool
  default     = false
}

variable "api_ingress_cidrs" {
  description = <<-EOT
    Who may reach the API on port 8000 when there is no load balancer.

    Only consulted when `enable_alb = false`; with an ALB the task accepts
    traffic from the load balancer's security group and from nothing else.

    Defaults to the whole internet because a demo URL you cannot reach is not a
    demo. Narrowing it to your own address costs nothing, and is the right
    setting for anything but a live demo:
      api_ingress_cidrs = ["203.0.113.4/32"]

    This used to claim "every route except /healthz requires an API key, so
    this is exposure rather than access". That was false. Six paths are exempt
    in apps/api/docfactory_api/main.py, and one of them --
    /internal/storage-events -- puts its request body onto the ingest queue. It
    carries its own shared secret, which now defaults to empty so the route
    fails closed, but the general point stands: an open CIDR is only as safe as
    the auth on the least-protected route behind it, and that list changes as
    the app grows. Check it against _UNAUTHENTICATED_PATHS before widening.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "api_cpu" {
  description = <<-EOT
    Fargate CPU units for the API task (256 = 0.25 vCPU).

    256 is the Fargate minimum and is what the API needs: it hashes an upload,
    writes it to S3, inserts a row and enqueues a message. It does no parsing
    and no extraction — that is the worker's job. Measured resident memory for
    the whole API process is well under 200 MiB.

    This is the only task that runs when the stack is idle, so it is the only
    task whose size shows up on a monthly bill. Halving it from 512/1024 halves
    the idle floor: $18.02/month to $9.01.
  EOT
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the API task. 512 is the minimum for 256 CPU units."
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = <<-EOT
    Fargate CPU units for worker tasks.

    Deliberately NOT reduced with the API. Workers run at zero when idle, so
    their size costs nothing on a parked stack — it only affects how fast a
    burst drains, and pdfplumber is the pipeline's one genuinely CPU-hungry
    stage (96.7% of non-model CPU; see docs/performance.md). Making these
    smaller would slow the demo without saving a cent.
  EOT
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for worker tasks. Parsing a large PDF is the peak."
  type        = number
  default     = 1024
}

variable "worker_use_spot" {
  description = <<-EOT
    Run workers on Fargate Spot (~70% cheaper), interruptions and all.

    This pipeline is already built for interruption: a worker that dies
    mid-document never deleted its message, so SQS redelivers it after the
    visibility timeout into a handler that is idempotent by status guard. That
    is exactly the precondition Spot asks for, and it is why this defaults to
    true for workers and is not offered for the API — an interrupted API task
    is a demo going dark mid-sentence.
  EOT
  type        = bool
  default     = true
}

variable "idle_park_hours" {
  description = <<-EOT
    Hours of continuously-running worker tasks before the dead man's switch
    parks the fleet.

    3 is chosen against the shape of this stack's work: a demo batch drains in
    minutes, and the 4e experiment's 125 documents take well under an hour, so
    three hours of continuous running means something is wrong rather than
    something is busy. Raise it if a real backlog ever legitimately runs longer
    — the cost of it firing early is a sixty-second blip, not lost work.
  EOT
  type        = number
  default     = 3

  validation {
    condition     = var.idle_park_hours >= 1
    error_message = "idle_park_hours must be at least 1 (the alarm period is one hour)."
  }
}

variable "enable_task_exec" {
  description = <<-EOT
    Allow `aws ecs execute-command` to open a shell in a running task.

    Free, and it is the difference between diagnosing an incident and guessing
    at one from log lines — you can check what the container actually resolved
    for an env var, whether it can reach Neon, what the queue client sees.

    It is also a shell inside a container holding live credentials. Defaults on
    because on this stack the operator and the developer are the same person
    and every invocation is written to CloudTrail; turn it off for a deployment
    where those are different people.

    Requires ssmmessages:* on the TASK role (granted in the data layer), not
    the execution role — a distinction that costs an afternoon if you get it
    the wrong way round.
  EOT
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Log groups are a silent forever-cost without it."
  type        = number
  default     = 7
}

variable "image_tag" {
  description = <<-EOT
    Image tag the services run.

    CI pushes the git SHA and updates the service; "latest" is the bootstrap
    value for the first apply, before any image exists.
  EOT
  type        = string
  default     = "latest"
}

variable "model_provider" {
  description = <<-EOT
    "mock" or "anthropic". THE DEPLOYED DEFAULT IS MOCK.

    A real-model run is a deliberate, temporary change — flip it, run a handful
    of documents, flip it back. Leaving a deployed stack on "anthropic" is how
    a demo becomes a bill.
  EOT
  type        = string
  default     = "mock"

  validation {
    condition     = contains(["mock", "anthropic"], var.model_provider)
    error_message = "model_provider must be \"mock\" or \"anthropic\"."
  }
}

variable "default_tenant_id" {
  description = "Tenant the batch drop path attributes unprefixed keys to."
  type        = string
  default     = "dev-tenant"
}

# --- local emulation --------------------------------------------------------

variable "localstack_endpoint" {
  description = <<-EOT
    LocalStack endpoint. Empty = real AWS.

    This layer is never *applied* against LocalStack — Fargate, the ALB and
    application autoscaling are not emulated by LocalStack Community. It is
    set only to run `terraform plan` against an applied LocalStack data layer,
    which proves the cross-layer references resolve.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.localstack_endpoint == "" || can(regex("^https?://", var.localstack_endpoint))
    error_message = "localstack_endpoint must be empty or an http(s) URL."
  }
}
