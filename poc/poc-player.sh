#!/usr/bin/env bash
# make poc-player: player console's local security and API checks (RFC PM01-PM05, issue #100).
set -u
POC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$POC/.." && pwd)"
PY="${PYTHON:-python3}"
LOG="${EMESH_STATE:-$POC/.state}/player-console-tests.log"
mkdir -p "$(dirname "$LOG")"
FAILS=0
step() {
  local id="$1" label="$2"; shift 2
  if "$PY" -m unittest "$@" >"$LOG" 2>&1; then echo "PASS $id $label"
  else cat "$LOG"; echo "FAIL $id $label"; FAILS=$((FAILS + 1)); fi
}
cd "$ROOT"
step PM01 "the listener binds loopback only" poc.test_console_player.PlayerTest.test_pm01_loopback_only_address_validation
step PM02 "status, events, parties and joinable games use only the player's namespace" poc.test_console_player.PlayerTest.test_pm02_countdown_and_node_connection_use_only_own_namespace poc.test_console_player.PlayerTest.test_events_parties_games_are_filtered_to_owned_namespace
step PM03 "outside-namespace reads are reported as 403 and API data is redacted" poc.test_console_player.PlayerTest.test_pm02_forbidden_read_is_named_as_outside_namespace poc.test_console_player.PlayerTest.test_pm03_api_redacts_token_pem_and_node_key_path
step PM04 "party consent actions are scoped and host actions are refused" poc.test_console_player.PlayerTest.test_pm04_party_mutations_are_validated_and_scoped poc.test_console_player.PlayerTest.test_pm04_handwritten_host_action_is_forbidden
step PM05 "Host/Origin validation blocks DNS rebinding" poc.test_console_player.PlayerTest.test_pm05_host_and_origin_checks_block_rebinding
step PM06 "join installs an atomic player-owned kubeconfig with mode 0600" poc.test_console_player.InstallKubeconfigTest.test_installs_atomic_user_owned_0600_file_and_refuses_symlink_dir
step PM07 "join user arguments must be unambiguous" poc.test_edge_mesh.KubectlWrapperTest.test_join_requires_user_value_and_rejects_duplicate_user
if [ "$FAILS" -eq 0 ]; then echo "RESULT PASS"; else echo "RESULT FAIL ($FAILS)"; fi
[ "$FAILS" -eq 0 ]
