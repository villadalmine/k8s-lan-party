variable "region" {
  description = "AWS region, for example eu-west-1. Required: nothing here assumes an account or a region."
  type        = string
}

variable "aws_profile" {
  description = "Named profile from ~/.aws/config. null uses the usual chain (AWS_PROFILE, environment variables)."
  type        = string
  default     = null
}

variable "name" {
  description = "Prefix of every resource and the value of the k8s-lan-party tag. Two demos in one account need two names."
  type        = string
  default     = "k8slp-demo"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name))
    error_message = "3 to 21 characters: a-z, 0-9 and '-', starting with a letter."
  }
}

variable "ssh_cidr" {
  description = "The only source allowed to SSH in, as a CIDR (your address: curl -s https://checkip.amazonaws.com, plus /32)."
  type        = string
  validation {
    condition     = can(cidrhost(var.ssh_cidr, 0)) && !contains(["0.0.0.0/0", "::/0"], var.ssh_cidr)
    error_message = "A valid CIDR, and not the whole internet."
  }
}

variable "ssh_public_key_file" {
  description = "Path of the SSH public key that goes on every machine (user ubuntu)."
  type        = string
}

variable "lighthouse_type" {
  description = "The lighthouse only introduces hosts: the smallest free-tier type is enough (t3.micro, t4g.micro)."
  type        = string
  default     = "t3.micro"
}

variable "host_type" {
  description = "The control plane machine runs Docker with the k3s server, Cilium and Kyverno: 8 GiB is comfortable (m7i-flex.large), 4 GiB is tight (c7i-flex.large)."
  type        = string
  default     = "m7i-flex.large"
}

variable "joiners" {
  description = "The participants: name => instance type. The architecture follows the type (t4g = arm64, the others amd64)."
  type        = map(string)
  default = {
    amd64 = "t3.small"
    arm64 = "t4g.small"
  }
  validation {
    condition     = alltrue([for k in keys(var.joiners) : can(regex("^[a-z][a-z0-9-]{0,15}$", k))])
    error_message = "Joiner names: a-z, 0-9 and '-', starting with a letter, at most 16 characters."
  }
  validation {
    condition     = !contains(keys(var.joiners), "lighthouse") && !contains(keys(var.joiners), "host")
    error_message = "A joiner cannot be called lighthouse or host."
  }
}

variable "lifetime_minutes" {
  description = "Hard lifetime. At the deadline every machine powers itself off (and AWS terminates it), and an EventBridge schedule terminates them from the AWS side too."
  type        = number
  default     = 120
  validation {
    condition     = var.lifetime_minutes >= 15 && var.lifetime_minutes <= 720
    error_message = "Between 15 minutes and 12 hours."
  }
}

variable "disk_gb" {
  description = "Root disk of every machine (gp3, deleted with the machine)."
  type        = number
  default     = 16
}
