# k8s-lan-party on AWS: a lighthouse, a control plane host and N participants (docs/DEMO-AWS.md).
#
#   VPC "host"     lighthouse (UDP 4242 open to the internet: the only public port of the game)
#                  host       (the control plane: Docker + mesh host init; nothing inbound but SSH)
#   VPC "players"  one machine per entry of var.joiners (amd64 and arm64; nothing inbound but SSH)
#
# Two VPCs so the participants reach the game the way strangers do, over the internet, never over a shared private network.
# Their own CIDRs, not the default VPC: the default VPC is 172.31.0.0/16, and the PoC's Docker network is 172.31.0.0/24.
# Every security group is stateful: a participant opens nothing for Nebula, its own outbound packets open the way back.
# Hard lifetime, twice: each machine powers itself off at the deadline (systemd timer; shutdown behaviour = terminate) and an
# EventBridge Scheduler schedule terminates them all from the AWS side at the same time, whatever happens inside the machines.

locals {
  machines = merge(
    { lighthouse = { type = var.lighthouse_type, vpc = "host", sg = "lighthouse" } },
    { host = { type = var.host_type, vpc = "host", sg = "host" } },
    { for k, t in var.joiners : k => { type = t, vpc = "players", sg = "players" } },
  )
  types = toset([for m in values(local.machines) : m.type])
  arch  = { for t in local.types : t => contains(data.aws_ec2_instance_type.t[t].supported_architectures, "arm64") ? "arm64" : "amd64" }
  cidrs = { host = "10.250.0.0/24", players = "10.251.0.0/24" } # clear of Nebula 10.77/24, pods 10.42/16, services 10.43/16, Docker 172.16/12
  # one availability zone per VPC that offers every type that VPC runs
  zones    = { for v in keys(local.cidrs) : v => sort(setintersection([for m in values(local.machines) : toset(data.aws_ec2_instance_type_offerings.t[m.type].locations) if m.vpc == v]...)) }
  deadline = timeadd(time_static.created.rfc3339, "${var.lifetime_minutes}m")
}

check "joiner_names" {
  assert {
    condition     = !contains(keys(var.joiners), "lighthouse") && !contains(keys(var.joiners), "host")
    error_message = "A joiner cannot be called lighthouse or host."
  }
}

data "aws_ec2_instance_type" "t" {
  for_each      = local.types
  instance_type = each.key
}

data "aws_ec2_instance_type_offerings" "t" {
  for_each      = local.types
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [each.key]
  }
}

data "aws_ami" "ubuntu" {
  for_each    = toset(["amd64", "arm64"])
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-${each.key}-server-*"]
  }
}

resource "time_static" "created" {}

# ---- network: two VPCs with a public subnet each ----
resource "aws_vpc" "v" {
  for_each             = local.cidrs
  cidr_block           = each.value
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name}-${each.key}" }
}

resource "aws_internet_gateway" "v" {
  for_each = aws_vpc.v
  vpc_id   = each.value.id
  tags     = { Name = "${var.name}-${each.key}" }
}

resource "aws_route" "internet" {
  for_each               = aws_vpc.v
  route_table_id         = each.value.main_route_table_id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.v[each.key].id
}

resource "aws_subnet" "v" {
  for_each                = aws_vpc.v
  vpc_id                  = each.value.id
  cidr_block              = each.value.cidr_block
  availability_zone       = local.zones[each.key][0]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-${each.key}" }
}

# ---- security groups: SSH only from var.ssh_cidr; UDP 4242 only on the lighthouse ----
resource "aws_security_group" "lighthouse" {
  name        = "${var.name}-lighthouse"
  description = "k8s-lan-party lighthouse: Nebula UDP 4242 from anywhere, SSH from the operator and the host"
  vpc_id      = aws_vpc.v["host"].id
  ingress {
    description = "Nebula"
    from_port   = 4242
    to_port     = 4242
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "SSH from the operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  ingress {
    description     = "SSH from the control plane host (lighthouse-vps.sh deploy and sync)"
    from_port       = 22
    to_port         = 22
    protocol        = "tcp"
    security_groups = [aws_security_group.host.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "host" {
  name        = "${var.name}-host"
  description = "k8s-lan-party control plane host: SSH from the operator only; Nebula leaves by egress"
  vpc_id      = aws_vpc.v["host"].id
  ingress {
    description = "SSH from the operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "players" {
  name        = "${var.name}-players"
  description = "k8s-lan-party participants: SSH from the operator only; Nebula leaves by egress"
  vpc_id      = aws_vpc.v["players"].id
  ingress {
    description = "SSH from the operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

locals {
  sg = {
    lighthouse = aws_security_group.lighthouse.id
    host       = aws_security_group.host.id
    players    = aws_security_group.players.id
  }
}

resource "aws_key_pair" "k" {
  key_name   = var.name
  public_key = trimspace(file(pathexpand(var.ssh_public_key_file)))
}

# ---- the machines ----
resource "aws_instance" "m" {
  for_each                             = local.machines
  ami                                  = data.aws_ami.ubuntu[local.arch[each.value.type]].id
  instance_type                        = each.value.type
  subnet_id                            = aws_subnet.v[each.value.vpc].id
  vpc_security_group_ids               = [local.sg[each.value.sg]]
  key_name                             = aws_key_pair.k.key_name
  instance_initiated_shutdown_behavior = "terminate" # powering off ends it: nothing is left stopped (and its disk billing)
  user_data = templatefile("${path.module}/lifetime.sh.tftpl", {
    hostname = "${var.name}-${each.key}"
    deadline = formatdate("YYYY-MM-DD hh:mm:ss", local.deadline)
  })
  root_block_device {
    volume_size           = var.disk_gb
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }
  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  tags = { Name = "${var.name}-${each.key}", role = each.value.sg }
  lifecycle {
    ignore_changes = [ami] # a newer Ubuntu image must not replace a running game
  }
}

# ---- the AWS side of the hard lifetime ----
resource "aws_iam_role" "terminate" {
  name = "${var.name}-terminate"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "terminate" {
  name = "terminate-tagged-instances"
  role = aws_iam_role.terminate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "ec2:TerminateInstances"
      Resource  = "*"
      Condition = { StringEquals = { "aws:ResourceTag/k8s-lan-party" = var.name } } # only this demo's machines
    }]
  })
}

resource "aws_scheduler_schedule" "end" {
  name                         = "${var.name}-end"
  schedule_expression          = "at(${formatdate("YYYY-MM-DD'T'hh:mm:ss", local.deadline)})"
  schedule_expression_timezone = "UTC"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:terminateInstances"
    role_arn = aws_iam_role.terminate.arn
    input    = jsonencode({ InstanceIds = [for m in aws_instance.m : m.id] })
    retry_policy {
      maximum_retry_attempts = 10
    }
  }
}
