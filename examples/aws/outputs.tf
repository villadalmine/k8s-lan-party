output "lighthouse_public_ip" {
  description = "The lighthouse's public address: the <public-host> of poc/lighthouse-vps.sh."
  value       = aws_instance.m["lighthouse"].public_ip
}

output "lighthouse_private_ip" {
  description = "How the control plane host reaches the lighthouse over SSH (same VPC)."
  value       = aws_instance.m["lighthouse"].private_ip
}

output "host_public_ip" {
  description = "The control plane host: ssh ubuntu@<this>."
  value       = aws_instance.m["host"].public_ip
}

output "joiners" {
  description = "Each participant: public address and architecture."
  value       = { for k in keys(var.joiners) : k => { public_ip = aws_instance.m[k].public_ip, arch = local.arch[var.joiners[k]] } }
}

output "deadline_utc" {
  description = "When every machine is terminated, whatever happens."
  value       = local.deadline
}

output "ssh" {
  description = "Ready-made SSH commands (user ubuntu)."
  value       = { for k, m in aws_instance.m : k => "ssh ubuntu@${m.public_ip}" }
}
