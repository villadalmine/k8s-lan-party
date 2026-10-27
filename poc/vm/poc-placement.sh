#!/usr/bin/env bash
# Phase C on real VMs: the same poc-placement.sh with RUNTIME=incus.
export RUNTIME=incus
exec "$(dirname "$0")/../poc-placement.sh" "$@"
