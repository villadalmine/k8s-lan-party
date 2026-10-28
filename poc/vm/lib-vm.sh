#!/usr/bin/env bash
# Shared by vm/up.sh and lib.sh (new_container in the VM flow). Needs lib.sh already sourced with RUNTIME=incus.
VMNET="${VMNET:-emeshbr}"   # the Incus network (a bridge) the machines live on; a second game on the same host needs its own name and UNDERLAY_NET
IMG="${VM_IMAGE:-images:ubuntu/24.04/cloud}"
declare -A VMIP=([lh]=$UNDERLAY_NET.2 [cp]=$UNDERLAY_NET.3 [n1]=$UNDERLAY_NET.4 [n2]=$UNDERLAY_NET.5)

fetch() { # pinned binaries, cached next to the scripts
  [ -x "$CACHE/nebula" ] || curl -sL "https://github.com/slackhq/nebula/releases/download/v$NEBULA_VERSION/nebula-linux-amd64.tar.gz" | tar xz -C "$CACHE"
  [ -x "$CACHE/k3s" ] || { curl -sL -o "$CACHE/k3s" "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION/+/%2B}/k3s" && chmod +x "$CACHE/k3s"; }
  [ -x "$CACHE/helm" ] || { curl -sL "https://get.helm.sh/helm-v$HELM_VERSION-linux-amd64.tar.gz" | tar xz -C "$CACHE" --strip-components=1 linux-amd64/helm; }
}
vm_up() { # vm_up <name> <cpus> <mem> <ip>
  local n="emesh-$1"
  incus info "$n" >/dev/null 2>&1 && incus delete -f "$n"
  incus init "$IMG" "$n" --vm -c limits.cpu="$2" -c limits.memory="$3" -d root,size=12GiB --network "$VMNET" >/dev/null
  incus config device set "$n" eth0 ipv4.address="$4" >/dev/null
  incus start "$n"
  wait_for 240 incus exec "$n" -- true
  wait_for 120 incus exec "$n" -- sh -c "ping -c1 -W1 $UNDERLAY_NET.1"
  incus exec "$n" -- sh -c 'cloud-init status --wait >/dev/null 2>&1 || true; for i in 1 2 3 4 5; do apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tcpdump wireguard-tools iproute2 iputils-ping curl ca-certificates >/dev/null && break; sleep 5; done'
  vm_tools "$1"
}
vm_tools() { # the three binaries every machine carries: nebula, k3s, edge-mesh
  local n="emesh-$1"
  incus exec "$n" -- mkdir -p /opt/nebula /bundle
  incus file push -q "$CACHE/nebula" "$n/opt/nebula/nebula"; incus file push -q "$CACHE/k3s" "$n/usr/local/bin/k3s"
  incus file push -q "$POC/edge-mesh" "$n/usr/local/bin/edge-mesh"
  incus exec "$n" -- chmod +x /opt/nebula/nebula /usr/local/bin/k3s /usr/local/bin/edge-mesh
}
push_bundle() { # push_bundle <bundle-name> [vm-role]
  # a certificate is valid from "now" on the host; a VM clock a second behind would call it "not yet valid": set it first
  incus exec "emesh-${2:-$1}" -- date -s "@$(date +%s)" >/dev/null
  tar cf - -C "$STATE/bundles/$1" . | incus exec "emesh-${2:-$1}" -- tar xf - -C /bundle
}
vm_new() { # vm_new <player>: a machine for a joining player. At most 4 VMs exist: with 4 up, the n1 VM is wiped and plays this player.
  local p="$1" ip last vm
  last=$(grep ^NODE_IP= "$STATE/bundles/$p/join.env" | cut -d= -f2); last=${last##*.}; ip="$UNDERLAY_NET.$((100 + last))"
  mkdir -p "$STATE/alias"
  if [ "$(incus list emesh- -c n -f csv | wc -l)" -lt "${MAX_VMS:-4}" ]; then
    vm_up "$p" 2 3GiB "$ip"; vm="$p"
  elif ! incus info emesh-n1 >/dev/null 2>&1; then
    echo "VM limit reached (MAX_VMS=${MAX_VMS:-4}): kick a player first (mesh host kick <name>) or raise MAX_VMS"; return 1
  else
    vm=n1; incus restart -f "emesh-$vm"; wait_for 240 incus exec "emesh-$vm" -- true
    incus exec "emesh-$vm" -- sh -c 'rm -rf /var/lib/rancher /var/lib/kubelet /etc/rancher /etc/nebula /var/log/nebula.log /var/log/k3s-agent.log /bundle/* /run/k3s'
    echo "$vm" >"$STATE/alias/$p"
  fi
  push_bundle "$p" "$vm"
}

# nebula and k3s on a machine: `nohup` for the disposable PoC (gone at a reboot); PERSIST=1 makes them systemd services that come back after one,
# and the control plane VM starts with the host (a game people keep joining).
unit_push() { # unit_push <vm-name> <unit-name> <file>
  incus file push -q "$3" "emesh-$1/etc/systemd/system/$2.service"; rm -f "$3"
}
start_nebula() { # start_nebula <vm-name>
  if [ "${PERSIST:-0}" != 1 ]; then
    incus exec "emesh-$1" -- sh -c 'mkdir -p /etc/nebula && cp /bundle/* /etc/nebula/ && nohup /opt/nebula/nebula -config /etc/nebula/config.yml >/var/log/nebula.log 2>&1 &'; return
  fi
  local u; u=$(mktemp)
  cat >"$u" <<'U'
[Unit]
Description=Nebula (edge-mesh)
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/opt/nebula/nebula -config /etc/nebula/config.yml
ExecReload=/bin/kill -HUP $MAINPID
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
U
  unit_push "$1" nebula "$u"
  incus exec "emesh-$1" -- sh -c 'mkdir -p /etc/nebula && cp /bundle/* /etc/nebula/ && systemctl daemon-reload && systemctl enable --now nebula'
}
start_k3s_server() { # start_k3s_server <flags>: on the control plane VM, after Nebula brought its address up
  if [ "${PERSIST:-0}" != 1 ]; then   # the start command is kept in the machine for `mesh host refresh cp` (lib.sh cp_k3s_start)
    echo "exec k3s server $1 >>/var/log/k3s.log 2>&1" | incus exec emesh-cp -- sh -c 'mkdir -p /etc/emesh && cat >/etc/emesh/k3s-server.sh'
    incus exec emesh-cp -- sh -c 'nohup sh /etc/emesh/k3s-server.sh >/dev/null 2>&1 &'; return; fi
  local u; u=$(mktemp)
  cat >"$u" <<U
[Unit]
Description=k3s server (edge-mesh control plane, API on the Nebula IP only)
After=nebula.service
Wants=nebula.service
[Service]
ExecStartPre=/bin/sh -c 'until ip -4 addr show dev nebula1 | grep -q "inet $CP_NEB/"; do sleep 1; done'
ExecStart=/usr/local/bin/k3s server $1
KillMode=process
Restart=always
RestartSec=5
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
U
  unit_push cp k3s "$u"
  incus exec emesh-cp -- sh -c 'systemctl daemon-reload && systemctl enable --now k3s'
}
