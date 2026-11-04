#!/bin/sh
# The match's server (issue #75 part B): Odamex with Freedoom, deathmatch (sv_gametype 1, GM_DM in Odamex's doomstat.h) on MAP01, no monsters, on 10666/UDP. It is NOT announced to the public master servers
# (sv_usemasters 0: the demo's address stays between the two friends). Its log goes to the pod's log and to /tmp/server.log, where the player
# who runs it reads it with `kubectl exec` (a player's kubeconfig can exec into their own pods, not read pod logs of another namespace).
cd /opt/odamex || exit 1
exec ./odasrv -iwad /opt/wad/freedoom2.wad -waddir /opt/wad -port 10666 +sv_usemasters 0 +sv_maxplayers 4 \
  +sv_hostname "k8s-lan-party doom" +sv_gametype 1 +sv_nomonsters 1 +sv_warmup 0 +map MAP01 2>&1 | tee /tmp/server.log
