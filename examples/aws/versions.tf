terraform {
  required_version = ">= 1.6"
  required_providers {
    # newest stable releases on 2026-10-28 (registry.terraform.io); bump them on purpose, never by memory
    aws  = { source = "hashicorp/aws", version = "~> 6.70" }
    time = { source = "hashicorp/time", version = "~> 0.14" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile # null: the usual chain (AWS_PROFILE, environment variables, SSO)
  default_tags {
    tags = {
      "k8s-lan-party" = var.name # leftovers.sh finds everything by this tag
      "managed-by"    = "terraform"
      "ephemeral"     = "true"
    }
  }
}
