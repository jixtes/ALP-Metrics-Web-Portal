import json
from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend import auto_refresh as auto
from backend.database import (initialize_database, insert_pipeline_run, complete_pipeline_run,
                              connect_database, PipelineAlreadyRunning, fetch_powerbi_report_selections)
from powerbi.fabric import FabricAPIError, FabricClient, validate_resource_id

RESOURCE = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/test/providers/Microsoft.Fabric/capacities/testcapacity'


class AutoRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'portal.db'
        initialize_database(self.db)
        self.now = 1000000
        self.clock = patch('backend.auto_refresh.time.time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.sku = 'F2'
        self.fabric = Mock()
        self.fabric.get.side_effect = lambda: {'sku': {'name': self.sku}, 'properties': {'state': 'Active', 'provisioningState': 'Succeeded'}}
        self.fabric.resize.side_effect = lambda sku: setattr(self, 'sku', sku)
        self.client = Mock(config=SimpleNamespace(workspace_id='workspace'))
        self.client.get_workspace.return_value = {'isOnDedicatedCapacity': True, 'capacityId': 'capacity'}
        self.client.list_reports.return_value = [
            {'id': 'r1', 'name': 'Fieldwork Monitoring Dashboard', 'datasetId': 'dataset'},
            {'id': 'r2', 'name': 'Other dashboard', 'datasetId': 'dataset'},
        ]
        self.history = []
        self.client.get_refresh_history.side_effect = lambda *a, **kw: list(self.history)
        def refresh(*args):
            request_id = f'refresh-{len(self.history) + 1}'
            self.history.append({'requestId': request_id, 'status': 'Unknown', 'refreshType': 'ViaApi'})
            return {'requestId': request_id}
        self.client.refresh_dataset.side_effect = refresh
        self.csv = self.root / 'data.csv'
        self.csv.write_text('id,value\n1,a\n2,b\n')
        self.save(['r1'])

    def save(self, ids):
        return auto.save_settings(self.db, {'reportIds': ids, 'capacityResourceId': RESOURCE},
                                  client=self.client, fabric_factory=lambda _: self.fabric)

    def new_run(self, version='V3', status='completed', upload_status='uploaded'):
        run_id = insert_pipeline_run(self.db, status='running', extract_mode='surveycto', started_at='now',
                                    triggered_by_email=None, triggered_by_name=None, pipeline_version=version)
        auto.record_data_update(self.db, run_id, version, [{'local_path': str(self.csv), 'status': upload_status,
            'relative_path': 'Project/Survey/data/data.csv', 'is_project_data': True}], exports_root=self.root)
        complete_pipeline_run(self.db, run_id=run_id, status=status, completed_at='now', message='done')
        return run_id

    def step(self):
        self.now += 31
        job = auto.active_job(self.db)
        if job:
            auto.advance_job(self.db, job, fabric=self.fabric, client=self.client)
        return auto.latest_job(self.db)

    def start_refresh(self):
        self.new_run()
        for _ in range(4): self.step()
        self.assertEqual(self.client.refresh_dataset.call_count, 1)

    def finish_refresh(self, status='Completed'):
        self.history[-1]['status'] = status
        for _ in range(4): self.step()

    def manual(self, dataset='dataset'):
        return auto.queue_manual_refresh(self.db, dataset, client=self.client,
                                         fabric_factory=lambda _: self.fabric)

    def test_manual_refresh_without_auto_selection_or_pipeline_run(self):
        self.save([])
        job = self.manual()
        self.assertEqual(job['trigger'], 'manual')
        self.assertIsNone(job['runId'])
        self.client.refresh_dataset.assert_not_called()
        self.fabric.resize.assert_not_called()
        for _ in range(4): self.step()
        self.assertEqual(self.sku, 'F32')
        self.history[-1]['endTime'] = '2026-09-23T09:12:00Z'
        self.finish_refresh()
        self.assertEqual(auto.latest_job(self.db)['status'], 'completed')
        self.assertEqual(self.sku, 'F2')
        self.assertEqual([c.args[0] for c in self.fabric.resize.call_args_list], ['F32', 'F2'])
        with connect_database(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_runs').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM powerbi_refresh_watermarks').fetchone()[0], 0)
        # NULL pipeline IDs allow another manual refresh, even without new data.
        self.assertEqual(self.manual()['status'], 'queued')

    def test_manual_forces_refresh_when_automatic_would_skip(self):
        self.start_refresh(); self.finish_refresh(); self.new_run()
        self.assertEqual(auto.latest_job(self.db)['status'], 'skipped')
        self.manual()
        for _ in range(4): self.step()
        self.assertEqual(self.client.refresh_dataset.call_count, 2)
        self.assertEqual(self.sku, 'F32')

    def test_manual_failure_resumes_restoration_after_restart(self):
        self.manual()
        for _ in range(4): self.step()
        self.history[-1]['status'] = 'Failed'
        self.step(); self.step()
        self.fabric.resize.side_effect = RuntimeError('Azure unavailable')
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'restoring')
        initialize_database(self.db)
        self.fabric.resize.side_effect = lambda sku: setattr(self, 'sku', sku)
        self.step(); self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(self.client.refresh_dataset.call_count, 1)

    def test_manual_and_pipeline_reservations_are_mutually_exclusive(self):
        self.manual()
        with self.assertRaises(auto.RefreshBusy): self.manual()
        with self.assertRaises(PipelineAlreadyRunning):
            insert_pipeline_run(self.db, status='running', extract_mode='surveycto', started_at='now',
                                triggered_by_email=None, triggered_by_name=None, reject_if_running=True)
        for _ in range(4): self.step()
        self.finish_refresh()
        insert_pipeline_run(self.db, status='running', extract_mode='surveycto', started_at='now',
                            triggered_by_email=None, triggered_by_name=None, reject_if_running=True)
        with self.assertRaises(auto.RefreshBusy): self.manual()

    def test_manual_reservation_rechecks_after_remote_validation(self):
        # A pipeline starts while the manual request is reading Fabric metadata.
        original_get = self.fabric.get.side_effect
        def capacity():
            insert_pipeline_run(self.db, status='running', extract_mode='surveycto', started_at='now',
                                triggered_by_email=None, triggered_by_name=None, reject_if_running=True)
            return original_get()
        self.fabric.get.side_effect = capacity
        with self.assertRaises(auto.RefreshBusy): self.manual()
        self.assertIsNone(auto.active_job(self.db))
        self.fabric.resize.assert_not_called()

    def test_manual_rejects_unknown_dataset_and_missing_capacity(self):
        with self.assertRaises(ValueError): self.manual('outside-workspace')
        with connect_database(self.db) as conn:
            conn.execute('DELETE FROM powerbi_auto_settings')
        with patch.dict(os.environ, {'FABRIC_CAPACITY_RESOURCE_ID': ''}):
            with self.assertRaisesRegex(ValueError, 'FABRIC_CAPACITY_RESOURCE_ID'): self.manual()
        self.assertIsNone(auto.active_job(self.db))
        self.fabric.resize.assert_not_called()

    def test_migration_preserves_active_job_and_accepts_manual_runs(self):
        self.new_run()
        before = auto.active_job(self.db)
        with connect_database(self.db) as conn:
            conn.execute('ALTER TABLE powerbi_refresh_jobs RENAME TO jobs_copy')
            conn.execute("""CREATE TABLE powerbi_refresh_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL)""")
            conn.execute('INSERT INTO powerbi_refresh_jobs SELECT * FROM jobs_copy')
            conn.execute('DROP TABLE jobs_copy')
        initialize_database(self.db)
        self.assertEqual(auto.active_job(self.db), before)
        for _ in range(4): self.step()
        self.finish_refresh()
        job = self.manual()
        self.assertGreater(job['id'], before['id'])
        self.assertIsNone(job['runId'])

    def test_selection_is_separate_and_validated(self):
        saved = self.save(['r1', 'r2', 'r1'])
        self.assertEqual(saved['reportIds'], ['r1', 'r2'])
        self.assertEqual(fetch_powerbi_report_selections(self.db), [])
        with self.assertRaises(ValueError): self.save(['missing'])
        self.assertEqual(auto.settings(self.db)['reportIds'], ['r1', 'r2'])
        self.save([])
        self.new_run()
        self.assertIsNone(auto.latest_job(self.db))

    def test_fingerprint_ignores_row_column_order_but_detects_edits_and_duplicates(self):
        before = auto.csv_fingerprint([('data', self.csv)])
        self.csv.write_text('value,id\nb,2\na,1\n')
        self.assertEqual(before, auto.csv_fingerprint([('data', self.csv)]))
        self.csv.write_text('value,id\nc,2\na,1\n')
        self.assertNotEqual(before, auto.csv_fingerprint([('data', self.csv)]))
        self.csv.write_text('id,value\n1,a\n2,b\n2,b\n')
        self.assertNotEqual(before, auto.csv_fingerprint([('data', self.csv)]))

    def test_checkbox_save_retains_capacity_without_a_visible_capacity_field(self):
        saved = auto.save_settings(self.db, {'reportIds': ['r2']}, client=self.client,
                                   fabric_factory=lambda _: self.fabric)
        self.assertEqual(saved['capacityResourceId'], RESOURCE)
        self.assertEqual(saved['reportIds'], ['r2'])
        self.assertEqual(auto.save_settings(self.db, {'reportIds': []})['capacityResourceId'], RESOURCE)

    def test_capacity_can_be_configured_on_server_when_saved_value_is_empty(self):
        with connect_database(self.db) as conn:
            conn.execute('UPDATE powerbi_auto_settings SET value=? WHERE id=1', (json.dumps({'reportIds': [], 'capacityResourceId': ''}),))
        with patch.dict(os.environ, {'FABRIC_CAPACITY_RESOURCE_ID': RESOURCE}):
            self.assertEqual(auto.settings(self.db)['capacityResourceId'], RESOURCE)

    def test_full_cycle_deduplicates_semantic_models_and_restores_f2(self):
        self.save(['r1', 'r2'])
        self.start_refresh()
        self.assertEqual(self.sku, 'F32')
        self.assertTrue(auto.latest_job(self.db)['active'])
        self.finish_refresh()
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'completed')
        self.assertEqual(self.client.refresh_dataset.call_count, 1)
        self.assertEqual([c.args[0] for c in self.fabric.resize.call_args_list], ['F32', 'F2'])

    def test_successful_watermark_skips_unchanged_but_same_count_edit_queues(self):
        self.start_refresh()
        self.finish_refresh()
        self.new_run()
        self.assertEqual(auto.latest_job(self.db)['status'], 'skipped')
        self.csv.write_text('id,value\n1,edited\n2,b\n')
        self.new_run()
        self.assertEqual(auto.latest_job(self.db)['status'], 'queued')

    def test_status_uses_time_portal_received_successful_completion(self):
        self.start_refresh()
        self.assertEqual(auto.latest_job(self.db)['datasets'], [{'datasetId': 'dataset', 'completedAt': None}])
        self.history[-1]['endTime'] = '2026-09-23T09:12:00Z'
        confirmed_at = datetime.fromtimestamp(self.now + 31, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        self.finish_refresh()
        self.assertEqual(auto.latest_job(self.db)['datasets'], [
            {'datasetId': 'dataset', 'completedAt': confirmed_at}])
        self.assertEqual(auto.successful_refreshes(self.db, 'workspace')['dataset']['endTime'], confirmed_at)

    def test_failed_attempt_preserves_success_across_restart_and_models(self):
        self.start_refresh(); self.finish_refresh()
        previous = auto.successful_refreshes(self.db, 'workspace')
        self.manual()
        for _ in range(4): self.step()
        self.history[-1]['endTime'] = '2030-01-01T00:00:00Z'
        self.finish_refresh('Failed')
        initialize_database(self.db)
        self.assertEqual(auto.successful_refreshes(self.db, 'workspace'), previous)
        self.assertEqual(auto.latest_job(self.db)['datasets'][0]['completedAt'], None)
        self.assertEqual(auto.successful_refreshes(self.db, 'another-workspace'), {})
        self.client.list_reports.return_value.append({'id': 'r3', 'name': 'Third report', 'datasetId': 'another-model'})
        self.manual('another-model')
        for _ in range(4): self.step()
        self.finish_refresh()
        successes = auto.successful_refreshes(self.db, 'workspace')
        self.assertEqual(successes['dataset'], previous['dataset'])
        self.assertIn('another-model', successes)

    def test_failed_first_attempt_has_no_last_success(self):
        self.start_refresh(); self.finish_refresh('Failed')
        self.assertEqual(auto.successful_refreshes(self.db, 'workspace'), {})

    def test_confirmation_is_saved_before_restoration_even_without_api_end_time(self):
        self.start_refresh()
        self.history[-1]['status'] = 'Completed'
        self.step()
        previous = auto.successful_refreshes(self.db, 'workspace')
        self.assertIn('dataset', previous)
        self.assertEqual(self.sku, 'F32')
        self.step()
        self.fabric.resize.side_effect = RuntimeError('Azure unavailable')
        self.step()
        initialize_database(self.db)
        self.assertEqual(auto.successful_refreshes(self.db, 'workspace'), previous)

    def test_success_timestamp_migration_ignores_newer_failed_attempt(self):
        self.start_refresh(); self.finish_refresh()
        with connect_database(self.db) as conn:
            row = conn.execute('SELECT id,value FROM powerbi_refresh_jobs').fetchone()
            value = json.loads(row['value'])
            value['targets'][0].pop('confirmedAt')
            value['targets'][0]['completedAt'] = '2026-09-23T09:12:00Z'
            conn.execute('UPDATE powerbi_refresh_jobs SET value=? WHERE id=?', (json.dumps(value), row['id']))
            value['targets'][0].update(state='failed', completedAt='2026-09-24T09:12:00Z')
            conn.execute("INSERT INTO powerbi_refresh_jobs (status,value,updated_at) VALUES ('failed',?,?)", (json.dumps(value), self.now))
            conn.execute('DROP TABLE powerbi_refresh_successes')
        initialize_database(self.db)
        self.assertEqual(auto.successful_refreshes(self.db, 'workspace'), {
            'dataset': {'status': 'Completed', 'endTime': '2026-09-23T09:12:00.000000Z'}})
        self.assertIsNone(auto.latest_job(self.db)['datasets'][0]['completedAt'])

    def test_failure_restores_and_remains_pending(self):
        self.start_refresh()
        self.finish_refresh('Failed')
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.assertEqual(self.sku, 'F2')
        self.new_run()
        self.assertEqual(auto.latest_job(self.db)['status'], 'queued')

    def test_restoration_retries_after_restart_and_does_not_ack_early(self):
        self.start_refresh()
        self.history[-1]['status'] = 'Completed'
        self.step(); self.step()
        self.fabric.resize.side_effect = RuntimeError('Azure unavailable')
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'restoring')
        self.assertIn('retrying', auto.latest_job(self.db)['message'])
        with connect_database(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM powerbi_refresh_watermarks').fetchone()[0], 0)
        initialize_database(self.db)
        self.fabric.resize.side_effect = lambda sku: setattr(self, 'sku', sku)
        self.step(); self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'completed')

    def test_lost_post_response_never_duplicates_or_adopts_another_refresh(self):
        self.client.refresh_dataset.side_effect = TimeoutError('Response lost')
        self.start_refresh()
        self.assertEqual(json.loads(auto.active_job(self.db)['value'])['targets'][0]['state'], 'submitting')
        self.history = [{'requestId': 'someone-elses-refresh', 'status': 'Unknown', 'refreshType': 'ViaApi'}]
        self.now += 301
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'refreshing')
        self.client.cancel_refresh.assert_not_called()
        self.history[0]['status'] = 'Completed'
        for _ in range(3): self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(self.client.refresh_dataset.call_count, 1)
        with connect_database(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM powerbi_refresh_watermarks').fetchone()[0], 0)

    def test_timeout_cancels_own_refresh_and_waits_before_downscaling(self):
        self.start_refresh()
        self.now += 21601
        self.step()
        self.client.cancel_refresh.assert_called_once_with('dataset', 'refresh-1')
        self.assertEqual(self.sku, 'F32')
        self.finish_refresh('Cancelled')
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')

    def test_failed_or_skipped_upload_does_not_queue(self):
        for status in ['failed', 'skipped']:
            self.new_run(upload_status=status)
            self.assertIsNone(auto.latest_job(self.db))

    def test_powerbi_outage_cannot_leave_f32_indefinitely_when_azure_is_available(self):
        self.start_refresh()
        self.client.get_refresh_history.side_effect = RuntimeError('Power BI unavailable')
        self.now += 21601
        self.step()
        self.assertEqual(self.sku, 'F32')
        self.now += 301
        for _ in range(3): self.step()
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')

    def test_partial_pipeline_never_scales(self):
        self.new_run(status='partial')
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.fabric.resize.assert_not_called()

    def test_settings_and_new_pipeline_blocked_until_restoration_finishes(self):
        self.start_refresh()
        with self.assertRaises(ValueError): self.save([])
        with self.assertRaises(PipelineAlreadyRunning):
            insert_pipeline_run(self.db, status='running', extract_mode='configured', started_at='now',
                triggered_by_email=None, triggered_by_name=None, pipeline_version='V2', reject_if_running=True)

    def test_versions_have_independent_watermarks(self):
        self.start_refresh(); self.finish_refresh()
        self.new_run('V2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'queued')

    def test_changed_workspace_or_dataset_fails_before_scale(self):
        self.new_run()
        self.client.get_workspace.return_value = {'capacityId': 'different'}
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.fabric.resize.assert_not_called()

    def test_scaling_intent_survives_restart(self):
        self.new_run(); self.step()
        self.assertTrue(json.loads(auto.active_job(self.db)['value'])['ownsCapacity'])
        self.fabric.resize.assert_not_called()
        initialize_database(self.db)
        self.step()
        self.assertEqual(self.sku, 'F32')

    def test_scale_rejection_surfaces_azure_error_and_verifies_f2_without_waiting(self):
        self.new_run(); self.step()
        response = Mock(status_code=400, reason='Bad Request')
        response.json.return_value = {'error': {'code': 'QuotaExceeded', 'message': 'Requested 32 CUs exceeds quota.'}}
        self.fabric.resize.side_effect = FabricAPIError(response)
        self.step()
        job = auto.latest_job(self.db)
        self.assertEqual(job['status'], 'restoring')
        self.assertIn('QuotaExceeded', job['error'])
        self.assertIn('Requested 32 CUs', job['error'])
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.assertEqual(self.sku, 'F2')
        self.client.refresh_dataset.assert_not_called()
        self.fabric.resize.assert_called_once_with('F32')

    def test_transient_azure_scale_error_retries_and_can_complete(self):
        self.new_run(); self.step()
        response = Mock(status_code=503, reason='Service Unavailable')
        response.json.return_value = {'error': {'code': 'ServiceUnavailable', 'message': 'Try again later.'}}
        self.fabric.resize.side_effect = FabricAPIError(response)
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'scaling_up')
        self.fabric.resize.side_effect = lambda sku: setattr(self, 'sku', sku)
        for _ in range(3): self.step()
        self.finish_refresh()
        self.assertEqual(auto.latest_job(self.db)['status'], 'completed')
        self.assertEqual(auto.latest_job(self.db)['error'], '')
        self.assertEqual(self.sku, 'F2')

    def test_scale_timeout_preserves_previous_error_after_restart(self):
        self.new_run(); self.step()
        self.fabric.resize.side_effect = RuntimeError('Azure rejected the scale request')
        self.step()
        initialize_database(self.db)
        self.now += 901
        self.step()
        self.assertIn('Azure rejected the scale request', auto.latest_job(self.db)['error'])
        self.assertIn('Timed out while scaling to F32', auto.latest_job(self.db)['error'])
        self.step()
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.client.refresh_dataset.assert_not_called()

    def test_scale_timeout_includes_observed_state_if_no_http_error(self):
        self.new_run(); self.step()
        self.fabric.get.return_value = None
        self.fabric.get.side_effect = lambda: {'sku': {'name': 'F2'}, 'properties': {'state': 'Scaling', 'provisioningState': 'Updating'}}
        self.now += 901
        self.step()
        self.assertIn('Scaling', auto.latest_job(self.db)['error'])
        self.assertIn('Updating', auto.latest_job(self.db)['error'])
        self.fabric.resize.assert_not_called()
        self.client.refresh_dataset.assert_not_called()

    def test_fabric_client_preserves_azure_error_details_without_dumping_response(self):
        client = FabricClient(RESOURCE)
        client.token = 'test-token'
        client.expires = self.now + 10000
        response = Mock(ok=False, status_code=403, reason='Forbidden')
        response.json.return_value = {'error': {'code': 'AuthorizationFailed', 'message': 'Capacity write denied.',
                                               'details': [{'message': 'Missing write permission.'}]}, 'unrelated': 'not shown'}
        with patch('powerbi.fabric.requests.request', return_value=response):
            with self.assertRaises(FabricAPIError) as caught:
                client.resize('F32')
        self.assertIn('AuthorizationFailed', str(caught.exception))
        self.assertIn('Missing write permission', str(caught.exception))
        self.assertNotIn('test-token', str(caught.exception))
        self.assertNotIn('not shown', str(caught.exception))
        response.json.side_effect = ValueError('not JSON')
        with patch('powerbi.fabric.requests.request', return_value=response):
            with self.assertRaisesRegex(FabricAPIError, 'HTTP 403.*Forbidden'):
                client.resize('F32')

    def test_new_boost_target_is_persisted_before_azure_patch(self):
        self.new_run(); self.step()
        self.assertEqual(json.loads(auto.active_job(self.db)['value'])['boostSku'], 'F32')
        self.fabric.resize.assert_not_called()
        initialize_database(self.db)
        self.step()
        self.fabric.resize.assert_called_once_with('F32')

    def test_legacy_queued_job_uses_f32_when_it_starts(self):
        self.new_run()
        self.assertNotIn('boostSku', json.loads(auto.active_job(self.db)['value']))
        self.step(); self.step()
        self.assertEqual(self.sku, 'F32')

    def test_legacy_f16_scale_intent_resumes_and_restores_after_upgrade(self):
        self.new_run(); self.step()
        job = auto.active_job(self.db)
        value = json.loads(job['value'])
        value.pop('boostSku')  # Old worker persisted ownership but no explicit SKU.
        auto._save(self.db, job, value)
        initialize_database(self.db)
        for _ in range(3): self.step()
        self.assertEqual(self.sku, 'F16')
        self.client.refresh_dataset.assert_called_once()
        self.finish_refresh()
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'completed')
        self.assertEqual([c.args[0] for c in self.fabric.resize.call_args_list], ['F16', 'F2'])

    def test_legacy_f16_restoration_is_not_stranded_by_upgrade(self):
        self.start_refresh()
        self.history[-1]['status'] = 'Failed'
        self.step(); self.step()
        job = auto.active_job(self.db)
        self.assertEqual(job['status'], 'restoring')
        value = json.loads(job['value'])
        value.pop('boostSku')
        auto._save(self.db, job, value)
        self.sku = 'F16'
        self.fabric.resize.reset_mock()
        initialize_database(self.db)
        self.step(); self.step()
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')
        self.fabric.resize.assert_called_once_with('F2')

    def test_new_job_does_not_overwrite_unexpected_external_capacity_change(self):
        self.new_run(); self.step()
        self.sku = 'F16'
        self.step()
        self.fabric.resize.assert_not_called()
        self.assertEqual(auto.latest_job(self.db)['status'], 'scaling_up')

    def test_resource_id_and_allowed_sizes_are_restricted(self):
        self.assertEqual(validate_resource_id(RESOURCE), RESOURCE)
        for invalid in ['https://evil.test', RESOURCE + '?x=1', '/subscriptions/abc']:
            with self.assertRaises(ValueError): validate_resource_id(invalid)
        with patch.object(FabricClient, '_request') as request:
            client = FabricClient(RESOURCE)
            with self.assertRaises(ValueError): client.resize('F64')
            request.assert_not_called()
            client.resize('F32')
            request.assert_called_once_with('PATCH', json={'sku': {'name': 'F32', 'tier': 'Fabric'}})


class AutoRefreshAPITests(unittest.TestCase):
    def setUp(self):
        from backend.app import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'ALP_INITIAL_ADMIN_EMAIL': 'auto@example.com',
                                          'ALP_INITIAL_ADMIN_PASSWORD': 'test-password-123'})
        self.env.start()
        self.app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False,
                               'SQLALCHEMY_DATABASE_URI': f"sqlite:///{self.root / 'auth.db'}",
                               'DATABASE_PATH': str(self.root / 'portal.db')})
        self.client = self.app.test_client()

    def tearDown(self):
        from backend.auth import db
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.env.stop()
        self.temp.cleanup()

    def login(self):
        with patch('backend.auth._require_csrf'):
            response = self.client.post('/api/auth/login', json={'email': 'auto@example.com', 'password': 'test-password-123'})
        self.assertEqual(response.status_code, 200)

    def test_settings_require_authentication_and_admin(self):
        self.assertNotEqual(self.client.get('/api/powerbi/auto-refresh').status_code, 200)
        self.login()
        self.assertEqual(self.client.get('/api/powerbi/auto-refresh').status_code, 200)
        from backend.auth import db, User
        with self.app.app_context():
            User.query.filter_by(email='auto@example.com').one().roles = []
            db.session.commit()
        self.assertEqual(self.client.get('/api/powerbi/auto-refresh').status_code, 403)
        self.assertEqual(self.client.put('/api/powerbi/auto-refresh', json={'reportIds': []}).status_code, 403)
        self.assertEqual(self.client.get('/api/powerbi/auto-refresh/status').status_code, 200)

    def test_manual_endpoint_queues_shared_workflow_and_returns_errors(self):
        self.assertNotEqual(self.client.post('/api/powerbi/refresh', json={'datasetId': 'dataset'}).status_code, 202)
        self.login()
        job = {'id': 1, 'trigger': 'manual', 'active': True, 'status': 'queued', 'message': 'Queued'}
        with patch('backend.app.PowerBIConfig.from_env'), patch('backend.app.PowerBIClient') as client, patch(
                'backend.app.auto_refresh.queue_manual_refresh', return_value=job) as queue:
            result = self.client.post('/api/powerbi/refresh', json={'datasetId': 'dataset'})
            self.assertEqual(result.status_code, 202)
            self.assertEqual(result.json['job'], job)
            self.assertEqual(queue.call_args.args[1], 'dataset')
            client.return_value.refresh_dataset.assert_not_called()
            queue.side_effect = auto.RefreshBusy('Busy')
            self.assertEqual(self.client.post('/api/powerbi/refresh', json={'datasetId': 'dataset'}).status_code, 409)
            queue.side_effect = ValueError('Configure capacity')
            self.assertEqual(self.client.post('/api/powerbi/refresh', json={'datasetId': 'dataset'}).status_code, 400)
        for payload in [[], 'bad']:
            self.assertEqual(self.client.post('/api/powerbi/refresh', json=payload).status_code, 400)
        from backend.auth import db, User
        with self.app.app_context():
            User.query.filter_by(email='auto@example.com').one().roles = []
            db.session.commit()
        self.assertEqual(self.client.post('/api/powerbi/refresh', json={'datasetId': 'dataset'}).status_code, 403)

    def test_disable_persists_and_malformed_settings_are_rejected(self):
        self.login()
        response = self.client.put('/api/powerbi/auto-refresh', json={'reportIds': [], 'capacityResourceId': RESOURCE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/powerbi/auto-refresh').json['capacityResourceId'], RESOURCE)
        for payload in [[], {'reportIds': 'bad'}, {'reportIds': ['r1'], 'capacityResourceId': 'https://evil.test'}]:
            self.assertEqual(self.client.put('/api/powerbi/auto-refresh', json=payload).status_code, 400)

    def test_report_and_embed_metadata_use_saved_success_not_latest_attempt(self):
        self.login()
        db_path = self.root / 'portal.db'
        with connect_database(db_path) as conn:
            conn.execute('INSERT INTO powerbi_refresh_successes VALUES (?,?,?)',
                         ('workspace', 'dataset', '2026-09-23T09:12:00.000000Z'))
        reports = [{'id': 'r1', 'name': 'Fieldwork', 'datasetId': 'dataset'},
                   {'id': 'r2', 'name': 'Never completed', 'datasetId': 'new-dataset'}]
        selection = {'id': 1, 'report_id': 'r1', 'report_name': 'Fieldwork', 'dataset_id': 'dataset',
                     'selected_at': 'now', 'embed_url': 'https://example.test'}
        with patch('backend.app.PowerBIConfig.from_env', return_value=SimpleNamespace(workspace_id='workspace')), patch(
                'backend.app.PowerBIClient') as factory, patch(
                'backend.app.fetch_powerbi_report_selections', return_value=[selection]):
            client = factory.return_value
            client.list_reports.return_value = reports
            client.get_dataset.return_value = {}
            client.get_refresh_history.return_value = [{'status': 'Failed', 'endTime': '2026-09-24T09:12:00Z'}]
            client.build_embed_config.return_value = {'reportId': 'r1', 'datasetId': 'dataset'}
            report_response = self.client.get('/api/powerbi/reports')
            embed_response = self.client.get('/api/powerbi/embed-configs')
            self.assertEqual(report_response.status_code, 200)
            self.assertEqual(embed_response.status_code, 200)
            for response in (report_response, embed_response):
                self.assertEqual(response.json['reports'][0]['latestRefresh'],
                                 {'status': 'Completed', 'endTime': '2026-09-23T09:12:00.000000Z'})
            self.assertIsNone(report_response.json['reports'][1]['latestRefresh'])
            client.get_refresh_history.assert_not_called()

    def test_status_hides_models_that_are_not_available_to_the_user(self):
        self.login()
        from backend.auth import db, User
        with self.app.app_context():
            User.query.filter_by(email='auto@example.com').one().roles = []
            db.session.commit()
        job = {'id': 1, 'active': True, 'status': 'refreshing', 'message': 'Refreshing private dashboard',
               'error': 'private details', 'dashboards': ['private dashboard'],
               'datasets': [{'datasetId': 'allowed', 'completedAt': None}, {'datasetId': 'private', 'completedAt': None}]}
        with patch('backend.app.auto_refresh.latest_job', return_value=job), patch(
            'backend.app.fetch_powerbi_report_selections', return_value=[{'dataset_id': 'allowed'}]):
            response = self.client.get('/api/powerbi/auto-refresh/status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['job']['datasets'], [{'datasetId': 'allowed', 'completedAt': None}])
        self.assertNotIn('private', json.dumps(response.json))
