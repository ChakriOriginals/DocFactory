# Network.
#
# THE COST DECISION: there is no NAT gateway in this stack.
#
# A NAT gateway is ~$32/month before a byte moves, and it is the single most
# common way a "small" AWS project quietly costs real money. Fargate tasks
# therefore run in PUBLIC subnets with public IPs, and are kept private by
# security groups instead of by network topology: nothing may reach a task
# except the ALB, on one port.
#
# The alternative — private subnets plus interface endpoints for ECR, SQS,
# Secrets Manager and CloudWatch Logs — is the more conventional answer and is
# stronger isolation, but it is ~$7/month per endpoint per AZ and would cost
# more than the NAT it replaces. For a stack that is cycled up and down, public
# subnets with tight security groups is the honest trade. The S3 *gateway*
# endpoint below is free, so bucket traffic never leaves the VPC regardless.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Two AZs: the ALB requires two subnets, and one AZ is not a deployment.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Free, and keeps S3 traffic off the public path even though the subnets are
# public. There is no gateway endpoint for SQS, so queue traffic does traverse
# the internet gateway — noted rather than hidden.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = { Name = "${local.name}-s3-endpoint" }
}

# --- security groups --------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public entry point: HTTP from anywhere to the load balancer."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from the internet"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "To the API tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-alb" }
}

resource "aws_security_group" "api" {
  name        = "${local.name}-api"
  description = "API tasks. Reachable ONLY from the load balancer, on one port."
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "API port, from the ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Outbound is open because the task must reach ECR, Secrets Manager, SQS,
  # Neon and (in anthropic mode) the model API. This is the trade the
  # no-NAT decision makes explicit: the task has a public IP, and nothing can
  # reach it inbound except the ALB.
  egress {
    description = "Outbound to AWS services, Neon and the model API"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-api" }
}

resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "Worker tasks. No inbound at all — they poll, nothing calls them."
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Outbound to AWS services, Neon and the model API"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-worker" }
}
