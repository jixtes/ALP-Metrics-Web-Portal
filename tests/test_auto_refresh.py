import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend import auto_refresh as auto
from backend.database import (initialize_database, insert_pipeline_run, complete_pipeline_run,
                              connect_database, PipelineAlreadyRunning, fetch_powerbi_report_selections)
from powerbi.fabric import FabricClient, validate_resource_id

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
            self.history.append({'requestId': 'refresh-1', 'status': 'Unknown', 'refreshType': 'ViaApi'})
            return {'requestId': 'refresh-1'}
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

    def test_full_cycle_deduplicates_semantic_models_and_restores_f2(self):
        self.save(['r1', 'r2'])
        self.start_refresh()
        self.assertEqual(self.sku, 'F16')
        self.assertTrue(auto.latest_job(self.db)['active'])
        self.finish_refresh()
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'completed')
        self.assertEqual(self.client.refresh_dataset.call_count, 1)
        self.assertEqual([c.args[0] for c in self.fabric.resize.call_args_list], ['F16', 'F2'])

    def test_successful_watermark_skips_unchanged_but_same_count_edit_queues(self):
        self.start_refresh()
        self.finish_refresh()
        self.new_run()
        self.assertEqual(auto.latest_job(self.db)['status'], 'skipped')
        self.csv.write_text('id,value\n1,edited\n2,b\n')
        self.new_run()
        self.assertEqual(auto.latest_job(self.db)['status'], 'queued')

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
        self.assertEqual(self.sku, 'F16')
        self.finish_refresh('Cancelled')
        self.assertEqual(self.sku, 'F2')
        self.assertEqual(auto.latest_job(self.db)['status'], 'failed')

    def test_failed_or_skipped_upload_does_not_queue(self):
        for status in ['failed', 'skipped']:
            self.new_run(upload_status=status)
            self.assertIsNone(auto.latest_job(self.db))

    def test_powerbi_outage_cannot_leave_f16_indefinitely_when_azure_is_available(self):
        self.start_refresh()
        self.client.get_refresh_history.side_effect = RuntimeError('Power BI unavailable')
        self.now += 21601
        self.step()
        self.assertEqual(self.sku, 'F16')
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
        self.assertEqual(self.sku, 'F16')

    def test_resource_id_and_allowed_sizes_are_restricted(self):
        self.assertEqual(validate_resource_id(RESOURCE), RESOURCE)
        for invalid in ['https://evil.test', RESOURCE + '?x=1', '/subscriptions/abc']:
            with self.assertRaises(ValueError): validate_resource_id(invalid)
        with patch.object(FabricClient, '_request') as request:
            client = FabricClient(RESOURCE)
            with self.assertRaises(ValueError): client.resize('F64')
            request.assert_not_called()


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

    def test_disable_persists_and_malformed_settings_are_rejected(self):
        self.login()
        response = self.client.put('/api/powerbi/auto-refresh', json={'reportIds': [], 'capacityResourceId': RESOURCE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/powerbi/auto-refresh').json['capacityResourceId'], RESOURCE)
        for payload in [[], {'reportIds': 'bad'}, {'reportIds': ['r1'], 'capacityResourceId': 'https://evil.test'}]:
            self.assertEqual(self.client.put('/api/powerbi/auto-refresh', json=payload).status_code, 400)
