#!/bin/sh
# The match's server (issue #75 part B): Odamex with FreeDM (a small deathmatch map: MAP15, 8 starts), deathmatch (sv_gametype 1, GM_DM in Odamex's doomstat.h) no monsters, on 10666/UDP. It is NOT announced to the public master servers
# (sv_usemasters 0: the demo's address stays between the two friends). Its log goes to the pod's log and to /tmp/server.log, where the player
# who runs it reads it with `kubectl exec` (a player's kubeconfig can exec into their own pods, not read pod logs of another namespace).
# Issue #80, so the scripted robots score frags in the recording (Odamex 12.3.0 common/c_cvarlist.cpp and p_user.cpp, read in its source): they
# fire without pause and pick nothing up, so their 50 bullets were gone before the recording started (fists only, AMMO 0 in the first run of
# this change): sv_infiniteammo 1 keeps the pistol firing (sv_doubleammo only doubles pickups), and sv_weapondamage 3 makes each hit three times
# stronger, so the few that land kill; a dead robot never presses Use, the only way to respawn, so sv_forcerespawn 1 with sv_forcerespawntime 3 brings it
# back after 3 s. Frags are still never a PASS condition.
cd /opt/odamex || exit 1
exec ./odasrv -iwad /opt/wad/freedm.wad -waddir /opt/wad -port 10666 +sv_usemasters 0 +sv_maxplayers 4 \
  +sv_hostname "k8s-lan-party doom" +sv_gametype 1 +sv_nomonsters 1 +sv_warmup 0 \
  +sv_infiniteammo 1 +sv_weapondamage 3 +sv_forcerespawn 1 +sv_forcerespawntime 3 +map MAP15 2>&1 | tee /tmp/server.log
