# Pytest marker ledger

This ledger is the review record for the deliberately marked nodes. Durations
are the immutable wall-clock node durations from the clean #37 benchmark's
first sequential block (`--durations=20`); the marker-only diff did not change
their bodies, fixtures, parameters, or assertions. The
`tests/test_report_diff_coverage.py` rows (#12) are pytest's reported session
time for each node run alone through `-Route Targeted` on main @ f634d20
(2026-10-22), since that file was marked after the #37 benchmark.

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
| tests/test_report_diff_coverage.py::test_dirty_target_repo_at_start_fails_closed[tracked] | slow | 7.10 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_dirty_target_repo_at_start_fails_closed[untracked] | slow | 1.82 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_head_not_at_declared_t_fails_closed | slow | 1.71 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_repository_drift_during_run_fails_closed_with_no_completion_marker | slow | 4.31 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_two_runs_sharing_only_t_resolve_to_different_run_directories | slow | 9.34 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_existing_staging_directory_is_refused_not_reused | slow | 2.25 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_killed_run_leaves_no_completion_marker | slow | 2.61 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_valid_single_change_range_attributes_correctly | slow | 5.27 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_ten_commit_local_stack_reports_cumulative_evidence_label | slow | 7.46 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_declared_computed_mismatch_invalidates_attribution_without_failing | slow | 5.27 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_missing_declared_set_is_rejected | slow | 1.67 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_empty_production_diff_is_reported_as_empty_not_full_coverage | slow | 4.37 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_path_mismatch_is_surfaced_not_smoothed_into_everything_covered | slow | 4.87 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_partial_path_mismatch_when_diffcover_covers_a_subset_of_changed_files | slow | 4.49 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_matching_multi_file_path_set_does_not_trigger_path_mismatch | slow | 5.29 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_pure_deletion_is_not_flagged_as_path_mismatch | slow | 5.23 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_deletion_alongside_measurable_edit_is_not_flagged_as_path_mismatch | slow | 6.34 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_malformed_report_with_null_src_stats_is_rejected_even_when_diff_is_empty | slow | 3.84 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_malformed_report_missing_numeric_field_is_rejected | slow | 4.75 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_unsafe_artifact_root_is_rejected_before_any_io | slow | 2.34 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_collection_identity_mismatch_with_equal_totals_and_no_failures_is_rejected | slow | 3.90 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_collection_identity_match_with_identical_populations_still_succeeds | slow | 4.27 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_junit_reports_are_hashed_in_the_completed_manifest | slow | 5.24 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_full_route_receives_the_same_run_id_full_coverage_used | slow | 4.80 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_full_route_junit_lands_only_in_this_runs_staging_directory | slow | 4.39 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_missing_full_route_junit_in_staging_directory_fails_closed | slow | 4.25 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_launcher_capture_files_never_land_in_system_temp_when_killed_mid_run | slow | 2.23 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_completed_manifest_hashes_the_coverage_data_files | slow | 4.96 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_merge_stack_ambiguity_invalidates_attribution_but_keeps_cumulative_report | slow | 6.51 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_moved_symbolic_origin_ref_does_not_change_the_recorded_result | slow | 13.08 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_changed_partial_branches_are_parsed_from_xml_not_diffcover_percentage | slow | 4.90 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_operational_failures_exit_non_zero_and_leave_no_completion_marker[full-coverage-route-exit-nonzero] | slow | 3.55 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_operational_failures_exit_non_zero_and_leave_no_completion_marker[full-route-exit-nonzero] | slow | 3.62 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_operational_failures_exit_non_zero_and_leave_no_completion_marker[coverage-xml-missing] | slow | 3.20 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_operational_failures_exit_non_zero_and_leave_no_completion_marker[coverage-xml-malformed] | slow | 3.94 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_operational_failures_exit_non_zero_and_leave_no_completion_marker[instrumented-uninstrumented-mismatch] | slow | 4.01 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_operational_failures_exit_non_zero_and_leave_no_completion_marker[diff-cover-tool-failure] | slow | 3.98 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_low_coverage_percentage_alone_still_exits_success | slow | 5.06 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
| tests/test_report_diff_coverage.py::test_disposable_repos_disable_git_auto_maintenance_for_direct_and_nested_git | slow | 0.92 | Tooling integration: real pwsh subprocess against throwaway git repositories (#12). |
