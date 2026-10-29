#!/usr/bin/env bash
# gateway.sh: the maintainer's endpoints (DESIGN.md section 6, issue #13) on a running control plane whose Cilium has BGP on (BGP=yes cilium.sh).
#   gateway.sh bgp       the BGP peering and the LoadBalancer pool, both for own nodes only (gateway/bgp.yaml)
#   gateway.sh install   Envoy Gateway and cert-manager pinned to own nodes, a local CA, the Gateway with its HTTPS listener (gateway/gateway.yaml)
#   gateway.sh router    (test only, Incus) the FRR router container that plays the edge router: it accepts a session only from the own node
# Versions, ASNs, the router address and the pool come from stack.env. Idempotent: every subcommand can run again.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
OWN_SEL='{"node-restriction.kubernetes.io/trust":"own"}'
OWN_IP="${OWN_IP:-$CP_UNDERLAY}"   # the own node's address on the router's network (the PoC's own node is the control plane)
fill() { sed -e "s#@OWN_ASN@#$OWN_ASN#g; s#@ROUTER_ASN@#$ROUTER_ASN#g; s#@ROUTER_IP@#$ROUTER_IP#g; s#@OWN_IP@#$OWN_IP#g" \
  -e "s#@LB_POOL_CIDR@#$LB_POOL_CIDR#g; s#@GW_HOST@#$GW_HOST#g; s#@GW_BODY@#${GW_BODY:-lan-party-gateway-ok}#g" "$POC/gateway/$1"; }

case "${1:-}" in
  bgp) fill bgp.yaml | k apply -f - ;;
  install)
    # CRDs first. k3s (1.37) installs the Gateway API CRDs itself, standard channel (its bundled chart gateway-api-crd), together with Gateway
    # API's safe-upgrades policy, which refuses experimental CRDs on top of standard ones; Envoy Gateway's main chart carries the experimental
    # channel, so it is installed with --skip-crds and its own CRDs come from its CRD chart. Gateway API's come from there too, standard
    # channel, only on a cluster that has none yet. cert-manager's Gateway API support needs them when it starts, so they go first.
    # Rendered and applied server-side, as Envoy Gateway documents: as a Helm release they exceed the 1 MiB limit of the release Secret.
    gwapi=false; k get crd gateways.gateway.networking.k8s.io >/dev/null 2>&1 || gwapi=true
    helm_cp template eg-crds oci://docker.io/envoyproxy/gateway-crds-helm --version "$ENVOY_GATEWAY_VERSION" \
      --set crds.envoyGateway.enabled=true --set crds.gatewayAPI.enabled=$gwapi --set crds.gatewayAPI.channel=standard \
      | k apply --server-side --force-conflicts -f - >/dev/null
    k wait --for=condition=Established crd/envoyproxies.gateway.envoyproxy.io --timeout=60s >/dev/null
    helm_cp upgrade --install eg oci://docker.io/envoyproxy/gateway-helm --version "$ENVOY_GATEWAY_VERSION" -n envoy-gateway-system --create-namespace --skip-crds \
      --set-json "deployment.pod.nodeSelector=$OWN_SEL" --set-json "certgen.job.nodeSelector=$OWN_SEL" --wait --timeout "${HELM_TIMEOUT:-8m}"
    helm_cp upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager --version "$CERT_MANAGER_VERSION" -n cert-manager --create-namespace \
      --set crds.enabled=true --set-json "global.nodeSelector=$OWN_SEL" \
      --set config.apiVersion=controller.config.cert-manager.io/v1alpha1 --set config.kind=ControllerConfiguration --set config.enableGatewayAPI=true \
      --wait --timeout "${HELM_TIMEOUT:-8m}"
    fill gateway.yaml | k apply -f - ;;
  router)
    [ "$RUNTIME" = incus ] || { echo "router: the test router is an Incus container (RUNTIME=incus)"; exit 2; }
    r=emesh-rtr
    if ! incus info "$r" >/dev/null 2>&1; then
      incus init "$FRR_IMAGE" "$r" --network "$VMNET" -c limits.cpu=1 -c limits.memory=512MiB >/dev/null
      incus config device set "$r" eth0 ipv4.address="$ROUTER_IP" >/dev/null
      incus start "$r"
    fi
    wait_for 120 incus exec "$r" -- getent hosts deb.debian.org   # its address is up and it resolves (the image has no ping)
    incus exec "$r" -- sh -c 'command -v vtysh >/dev/null || { for i in 1 2 3 4 5; do apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq frr curl openssl iproute2 >/dev/null && break; sleep 5; done; }'
    fill frr.conf | incus exec "$r" -- sh -c 'cat >/etc/frr/frr.conf.new'
    incus exec "$r" -- sh -c 'sed -i "s/^bgpd=no/bgpd=yes/" /etc/frr/daemons; mkdir -p /var/log/frr; chown frr:frr /var/log/frr
      if ! cmp -s /etc/frr/frr.conf.new /etc/frr/frr.conf || ! pidof bgpd >/dev/null; then mv /etc/frr/frr.conf.new /etc/frr/frr.conf; systemctl restart frr; echo "changed: frr"; else rm /etc/frr/frr.conf.new; fi'
    ;;
  *) echo "usage: gateway.sh bgp | install | router"; exit 2 ;;
esac
