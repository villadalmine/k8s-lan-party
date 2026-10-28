#!/usr/bin/env bash
# leftovers.sh: after `terraform destroy`, prove that nothing of this demo is left in the account.
#   AWS_PROFILE=<profile> ./leftovers.sh <region> [name]      (name: the `name` variable, default k8slp-demo)
# Looks for everything the example creates, by its tag (k8s-lan-party=<name>) or its name: instances not yet terminated, volumes,
# elastic IPs, network interfaces, security groups, key pairs, VPCs, subnets, internet gateways, the IAM role and the schedule.
# Prints "OK: nothing left" and exits 0, or lists what is left and exits 1. An AWS call that fails is an error (exit 2), never "nothing left".
set -euo pipefail
region="${1:?usage: leftovers.sh <region> [name]}"; name="${2:-k8slp-demo}"
export AWS_REGION="$region" AWS_PAGER=""
tag=(--filters "Name=tag:k8s-lan-party,Values=$name")
q() { local what="$1"; shift; local out; out=$("$@" --output text) || { echo "ERROR: the AWS call for $what failed" >&2; exit 2; }; out=$(echo "$out" | tr -s '[:space:]' ' ' | sed 's/^ *//; s/ *$//; s/^None$//'); [ -z "$out" ] || echo "$what: $out"; }
left=$(
  q instances aws ec2 describe-instances "${tag[@]}" "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped" --query 'Reservations[].Instances[].InstanceId'
  q volumes aws ec2 describe-volumes "${tag[@]}" --query 'Volumes[].VolumeId'
  q elastic-ips aws ec2 describe-addresses "${tag[@]}" --query 'Addresses[].AllocationId'
  q security-groups aws ec2 describe-security-groups "${tag[@]}" --query 'SecurityGroups[].GroupId'
  q key-pairs aws ec2 describe-key-pairs "${tag[@]}" --query 'KeyPairs[].KeyName'
  q vpcs aws ec2 describe-vpcs "${tag[@]}" --query 'Vpcs[].VpcId'
  q subnets aws ec2 describe-subnets "${tag[@]}" --query 'Subnets[].SubnetId'
  q internet-gateways aws ec2 describe-internet-gateways "${tag[@]}" --query 'InternetGateways[].InternetGatewayId'
  q iam-role aws iam list-roles --query "Roles[?RoleName=='$name-terminate'].RoleName"
  q schedule aws scheduler list-schedules --name-prefix "$name-end" --query 'Schedules[].Name'
)
# network interfaces carry no tag of their own: the ones left in this demo's VPCs (if any VPC is left at all)
for v in $(aws ec2 describe-vpcs "${tag[@]}" --query 'Vpcs[].VpcId' --output text); do
  e=$(aws ec2 describe-network-interfaces --filters "Name=vpc-id,Values=$v" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)
  [ -z "$e" ] || left="$left
network-interfaces ($v): $e"
done
if [ -z "$left" ]; then echo "OK: nothing left of '$name' in $region"; else echo "LEFT in $region:"; echo "$left"; exit 1; fi
