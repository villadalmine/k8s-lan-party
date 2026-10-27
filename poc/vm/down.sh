#!/usr/bin/env bash
# Delete every emesh-* VM, the network and the state the VM flow created (nothing else on the host is touched).
. "$(dirname "$0")/../lib.sh"
for n in $(incus list emesh- -c n -f csv); do incus delete -f "$n" 2>/dev/null; done
incus network delete "${VMNET:-emeshbr}" 2>/dev/null
rm -rf "$STATE"
echo "VM flow removed"
