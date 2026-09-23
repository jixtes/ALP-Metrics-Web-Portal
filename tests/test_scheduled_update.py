from datetime import datetime, timedelta, timezone
import fcntl
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import scheduled_update as scheduled
from backend.database import (
    PipelineAlreadyRunning, PipelineRecentlyStarted, complete_pipeline_run,
    connect_database, fetch_pipeline_run, initialize_database, insert_pipeline_run,
)


class ScheduledUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'portal.db'
        initialize_database(self.db)
        self.repo = patch('backend.scheduled_update.get_pipeline_repo_status', return_value={'branch': 'main', 'commit': 'abc'})
        self.repo.start()
        self.addCleanup(self.repo.stop)
        self.output = io.StringIO()
        output = patch('sys.stdout', self.output)
        output.start()
        self.addCleanup(output.stop)

    def prior(self, *, minutes=30, version='V3', mode='surveycto', status='completed', actor='Portal user'):
        run_id = insert_pipeline_run(
            self.db, status='running', extract_mode=mode, pipeline_version=version,
            started_at=(datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(),
            triggered_by_email=None if actor == scheduled.SCHEDULE_ACTOR else 'user@example.com',
            triggered_by_name=actor,
        )
        if status != 'running':
            complete_pipeline_run(self.db, run_id=run_id, status=status, completed_at='now', message='done')
        return run_id

    def finish(self, db, **kwargs):
        # The same atomic reservation must block a simultaneous portal update.
        with self.assertRaises(PipelineAlreadyRunning):
            insert_pipeline_run(db, status='running', extract_mode='surveycto', started_at='now',
                                triggered_by_email=None, triggered_by_name=None, reject_if_running=True)
        complete_pipeline_run(db, run_id=kwargs['run_id'], status='completed', completed_at='now', message='done')
        return {'status': 'completed', 'uploads': [{'status': 'uploaded'}]}

    def test_scheduled_run_uses_live_v3_uploads_snapshots_and_shared_reservation(self):
        with patch('backend.scheduled_update.run_pipeline_and_snapshot', side_effect=self.finish) as run:
            self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
        options = run.call_args.kwargs
        self.assertEqual(options['pipeline_version'], 'V3')
        self.assertEqual(options['extract_mode'], 'surveycto')
        self.assertTrue(options['upload_to_sharepoint'])
        self.assertTrue(options['publish_snapshot'])
        saved = fetch_pipeline_run(self.db, options['run_id'])
        self.assertEqual(saved['triggered_by_name'], 'Automatic schedule')
        self.assertEqual(saved['status'], 'completed')
        # A second scheduled call also respects the one-hour cooldown.
        with patch('backend.scheduled_update.run_pipeline_and_snapshot') as run:
            self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
            run.assert_not_called()
        self.assertIn('last hour', self.output.getvalue())

    def test_recent_manual_trigger_skips_even_if_it_failed(self):
        for status in ('completed', 'failed'):
            with self.subTest(status=status), connect_database(self.db) as conn:
                conn.execute('DELETE FROM pipeline_runs')
            prior_id = self.prior(status=status)
            with patch('backend.scheduled_update.run_pipeline_and_snapshot') as run:
                self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
                run.assert_not_called()
            with connect_database(self.db) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM pipeline_runs').fetchone()[0], 1)
            self.assertEqual(fetch_pipeline_run(self.db, prior_id)['status'], status)

    def test_old_v3_or_recent_v2_or_test_webhook_do_not_prevent_v3(self):
        self.prior(minutes=61)
        self.prior(version='V2', mode='configured')
        self.prior(mode='surveycto_test')
        with patch('backend.scheduled_update.run_pipeline_and_snapshot', side_effect=self.finish) as run:
            self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
            run.assert_called_once()

    def test_manual_and_powerbi_work_in_progress_are_skipped(self):
        prior_id = self.prior(minutes=120, status='running')
        with patch('backend.scheduled_update.run_pipeline_and_snapshot') as run:
            self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
            run.assert_not_called()
        self.assertEqual(fetch_pipeline_run(self.db, prior_id)['status'], 'running')
        complete_pipeline_run(self.db, run_id=prior_id, status='completed', completed_at='now', message='done')
        with connect_database(self.db) as conn:
            conn.execute("INSERT INTO powerbi_refresh_jobs (status,value,updated_at) VALUES ('restoring','{}',0)")
        with patch('backend.scheduled_update.run_pipeline_and_snapshot') as run:
            self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
            run.assert_not_called()

    def test_another_scheduled_process_holds_lock_so_its_run_is_not_recovered(self):
        run_id = self.prior(minutes=120, status='running', actor=scheduled.SCHEDULE_ACTOR)
        with open(str(self.db) + '.scheduled-update.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('backend.scheduled_update.run_pipeline_and_snapshot') as run:
                self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
                run.assert_not_called()
        self.assertEqual(fetch_pipeline_run(self.db, run_id)['status'], 'running')

    def test_recover_interrupted_schedule_without_changing_manual_history(self):
        orphan = self.prior(minutes=120, status='running', actor=scheduled.SCHEDULE_ACTOR)
        manual = self.prior(minutes=120)
        with patch('backend.scheduled_update.run_pipeline_and_snapshot', side_effect=self.finish):
            self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
        self.assertEqual(fetch_pipeline_run(self.db, orphan)['status'], 'failed')
        self.assertEqual(fetch_pipeline_run(self.db, manual)['status'], 'completed')

    def test_interruption_releases_running_reservation_and_preserves_recent_trigger(self):
        with patch('backend.scheduled_update.run_pipeline_and_snapshot', side_effect=InterruptedError):
            self.assertEqual(scheduled.run_scheduled_update(self.db), 1)
        with connect_database(self.db) as conn:
            row = conn.execute('SELECT id,status FROM pipeline_runs').fetchone()
            self.assertEqual(row['status'], 'failed')
        with patch('backend.scheduled_update.run_pipeline_and_snapshot') as run:
            self.assertEqual(scheduled.run_scheduled_update(self.db), 0)
            run.assert_not_called()

    def test_incomplete_uploads_return_failure_for_service_monitoring(self):
        def upload_failed(db, **kwargs):
            result = self.finish(db, **kwargs)
            result['uploads'] = [{'status': 'failed'}]
            return result
        with patch('backend.scheduled_update.run_pipeline_and_snapshot', side_effect=upload_failed):
            self.assertEqual(scheduled.run_scheduled_update(self.db), 1)

    def test_cooldown_compares_timezone_aware_start_times_and_keeps_manual_runs_available(self):
        run_id = insert_pipeline_run(self.db, status='completed', extract_mode='surveycto',
                                    started_at='2026-09-24T13:30:00+05:30',
                                    triggered_by_email='user@example.com', triggered_by_name='User')
        options = dict(status='running', extract_mode='surveycto', started_at='2026-09-24T09:00:00Z',
                       triggered_by_email=None, triggered_by_name=scheduled.SCHEDULE_ACTOR,
                       reject_if_running=True, skip_if_started_since='2026-09-24T08:00:00Z')
        with self.assertRaises(PipelineRecentlyStarted) as context:
            insert_pipeline_run(self.db, **options)
        self.assertEqual(context.exception.run_id, run_id)
        options.pop('skip_if_started_since')
        self.assertGreater(insert_pipeline_run(self.db, **options), run_id)

    def test_missing_database_does_not_create_a_separate_portal(self):
        missing = self.db.parent / 'missing.db'
        with self.assertRaises(ValueError):
            scheduled.run_scheduled_update(missing)
        self.assertFalse(missing.exists())
