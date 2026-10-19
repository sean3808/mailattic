# Pytest marker ledger

This ledger is the review record for the deliberately marked nodes. Durations
are the immutable wall-clock node durations from the clean #37 benchmark's
first sequential block (`--durations=20`); the marker-only diff did not change
their bodies, fixtures, parameters, or assertions.

| Node ID | Marker | Duration (s) | Rationale |
| --- | --- | ---: | --- |
| tests/test_mcp_server.py::McpServerTests::test_budget_reduced_delivery_is_itself_within_budget | slow | 91.63 | Pathological large MCP response-budget payload. |
| tests/test_mcp_server.py::McpServerTests::test_mail_show_ladder_reduces_body_when_result_exceeds_budget | slow | 77.34 | Pathological large MCP response-budget payload. |
| tests/test_mcp_server.py::McpServerTests::test_mail_thread_ladder_drops_oldest_messages_when_result_exceeds_budget | slow | 12.11 | Pathological large MCP response-budget payload. |
| tests/test_mcp_server.py::McpServerTests::test_mail_thread_ladder_reduces_the_remaining_message_once_only_one_is_left | slow | 77.68 | Pathological large MCP response-budget payload. |
| tests/test_mcp_server.py::MessagePayloadShapingTests::test_body_budget_boundary_table | slow | 10.92 | Pathological large MCP response-budget payload. |
| tests/test_tag_based_release.py::test_release_preconditions_accept_clean_main_and_return_full_commit | release | 2.38 | Temporary repository and release precondition subprocess flow. |
| tests/test_tag_based_release.py::test_release_preconditions_reject_existing_remote_tag | release | 2.44 | Temporary repository, remote, and release-tag subprocess flow. |
| tests/test_tag_based_release.py::test_release_orchestration_orders_stages_and_pushes_annotated_tag | release | 4.12 | Full release orchestration with reinstall, tag, remote, and Git subprocesses. |
| tests/test_tag_based_release.py::test_test_failure_short_circuits_without_tag_or_new_evidence | release | 2.88 | Release failure orchestration and fail-closed evidence assertions. |
| tests/test_tag_based_release.py::test_tag_creation_failure_restores_prior_evidence_state[False] | release | 3.11 | Temporary repository and tag-creation failure subprocess flow. |
| tests/test_tag_based_release.py::test_tag_creation_failure_restores_prior_evidence_state[True] | release | 3.05 | Temporary repository and tag-creation failure subprocess flow. |
| tests/test_tag_based_release.py::test_push_failure_retains_local_tag_and_evidence_with_retry | release | 3.44 | Atomic remote push failure and local release evidence subprocess flow. |
| tests/test_tag_based_release.py::test_global_reinstall_invokes_repo_script_by_absolute_path | release | 2.24 | Global installation subprocess orchestration. |
| tests/test_tag_based_release.py::test_installer_composes_pre_commit_wrapper_and_raw_stdin_companion | release | 7.44 | Git-hook installer and temporary remote end-to-end flow. |
| tests/test_tag_based_release.py::test_installer_preserves_explicit_default_core_hooks_path | release | 3.18 | Git-hook installer and temporary repository end-to-end flow. |
| tests/test_tag_based_release.py::test_release_stops_before_reinstall_on_any_nonzero_launcher_exit[1-failed with exit code 1] | release | 4.93 | Release orchestration launcher-failure stop-before-reinstall subprocess flow. |
| tests/test_tag_based_release.py::test_release_stops_before_reinstall_on_any_nonzero_launcher_exit[17-failed with exit code 17] | release | 4.06 | Release orchestration launcher-failure stop-before-reinstall subprocess flow. |
| tests/test_tag_based_release.py::test_release_stops_before_reinstall_on_any_nonzero_launcher_exit[18-failed with exit code 18] | release | 4.19 | Release orchestration launcher-failure stop-before-reinstall subprocess flow. |
