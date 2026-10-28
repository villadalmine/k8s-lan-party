#!/usr/bin/env bash
# Step 1 on real VMs: the same poc-join, poc-host and poc-weakness scripts, with RUNTIME=incus (flannel, as in step 1).
# Run on a Linux host with Incus. Never more than 4 VMs at a time; every script cleans up after itself.
set -u
export RUNTIME=incus CNI=flannel
POC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rc=0
for s in poc-join poc-host poc-weakness poc-ca poc-revoke poc-placement; do echo "### $s"; "$POC/$s.sh" || rc=1; done
exit $rc
