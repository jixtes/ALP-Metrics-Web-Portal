from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask_security import SQLAlchemyUserDatastore, hash_password

from backend.app import create_app
from backend.auth import db, Role, User


class RoleChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "ALP_INITIAL_ADMIN_EMAIL": "role-admin@example.com",
            "ALP_INITIAL_ADMIN_PASSWORD": "role-test-password",
        })
        self.env.start()
        self.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{self.root / 'auth.db'}",
            "DATABASE_PATH": str(self.root / "portal.db"),
        })
        self.csrf = patch("backend.auth._require_csrf")
        self.csrf.start()
        with self.app.app_context():
            store = SQLAlchemyUserDatastore(db, User, Role)
            role = store.create_role(name="restricted", project_scope="restricted", upload_scope="none")
            user = store.create_user(
                email="role-user@example.com", password=hash_password("role-test-password"),
                active=True, roles=[role], allowed_project_refs_json=json.dumps(["old-project"]),
            )
            db.session.commit()
            self.user_id, self.role_id = user.id, role.id
        self.administrator = self.app.test_client()
        self.user = self.app.test_client()
        self.powerbi_config = SimpleNamespace(
            workspace_id="workspace",
            default_effective_username="dashboard-admin@example.com",
            default_effective_roles=["AllProjects"],
        )
        self.login(self.administrator, "role-admin@example.com")
        self.login(self.user)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.csrf.stop()
        self.env.stop()
        self.temp.cleanup()

    def session_user(self):
        return self.user.get("/api/auth/session").get_json()["user"]

    def login(self, client, email="role-user@example.com", *, remember=False):
        response = client.post("/api/auth/login", json={
            "email": email, "password": "role-test-password", "remember": remember,
        })
        self.assertEqual(response.status_code, 200)

    def change_role(self, role):
        response = self.administrator.patch(f"/api/admin/users/{self.user_id}", json={
            "role": role, "allowedProjectRefs": ["old-project"],
        })
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_promotion_requires_fresh_login_then_gains_admin_access_and_embed_identity(self):
        before = self.session_user()
        self.assertEqual(self.user.get("/api/admin/users").status_code, 403)
        response = self.change_role("admin")
        self.assertTrue(response["sessionsReset"])
        self.assertIsNone(self.session_user())
        self.assertEqual(self.user.get("/api/powerbi/embed-configs", headers={"Accept": "application/json"}).status_code, 401)
        self.login(self.user)
        after = self.session_user()
        self.assertEqual(after["roles"], ["admin"])
        self.assertNotEqual(before["accessRevision"], after["accessRevision"])
        self.assertEqual(self.user.get("/api/admin/users").status_code, 200)
        selections = [{"report_id": "other-report", "project_scope": "restricted", "allowed_project_refs": ["other-project"]}]
        with patch("backend.app.fetch_powerbi_report_selections", return_value=selections):
            self.assertEqual(self.user.get("/api/powerbi/selections").get_json()["reports"], selections)
        with patch("backend.app.PowerBIConfig.from_env", return_value=self.powerbi_config), patch("backend.app.PowerBIClient") as factory:
            factory.return_value.build_embed_config.return_value = {"reportId": "report"}
            self.assertEqual(self.user.get("/api/powerbi/embed-config").status_code, 200)
            self.assertEqual(factory.return_value.build_embed_config.call_args.kwargs,
                             {"username": "dashboard-admin@example.com", "roles": ["AllProjects"]})

    def dashboard_identity(self, client, *, preview_user_id=None):
        selection = {
            "id": 1, "report_id": "report", "report_name": "Dashboard",
            "dataset_id": "dataset", "selected_at": "now",
        }
        path = "/api/powerbi/embed-configs"
        if preview_user_id is not None:
            path += f"?userPreviewId={preview_user_id}"
        with patch("backend.app.PowerBIConfig.from_env", return_value=self.powerbi_config), patch(
            "backend.app.PowerBIClient"
        ) as factory, patch("backend.app.fetch_powerbi_report_selections", return_value=[selection]):
            factory.return_value.build_embed_config.return_value = {"reportId": "report", "datasetId": "dataset"}
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.get_json()["reports"][0]["error"])
            return factory.return_value.build_embed_config.call_args.kwargs

    def test_all_admins_share_configured_dashboard_identity_after_promotion(self):
        original_admin = self.dashboard_identity(self.administrator)
        self.change_role("admin")
        self.login(self.user)
        promoted_admin = self.dashboard_identity(self.user)
        self.assertEqual(promoted_admin, original_admin)
        self.assertEqual(promoted_admin["username"], self.powerbi_config.default_effective_username)
        self.assertEqual(promoted_admin["roles"], ["AllProjects"])

    def test_non_admin_keeps_personal_identity_and_roles(self):
        identity = self.dashboard_identity(self.user)
        self.assertEqual(identity["username"], "role-user@example.com")
        self.assertEqual(identity["roles"], ["restricted"])

    def test_admin_preview_uses_previewed_users_identity_and_roles(self):
        identity = self.dashboard_identity(self.administrator, preview_user_id=self.user_id)
        self.assertEqual(identity["username"], "role-user@example.com")
        self.assertEqual(identity["roles"], ["restricted"])

    def test_demoted_admin_returns_to_personal_dashboard_identity(self):
        self.change_role("admin")
        self.login(self.user)
        self.assertEqual(self.dashboard_identity(self.user)["username"], "dashboard-admin@example.com")
        self.change_role("restricted")
        self.login(self.user)
        identity = self.dashboard_identity(self.user)
        self.assertEqual(identity["username"], "role-user@example.com")
        self.assertEqual(identity["roles"], ["restricted"])

    def test_admin_individual_report_still_uses_personal_identity(self):
        from backend.app import INDIVIDUAL_REPORT_NAME
        with patch("backend.app.PowerBIConfig.from_env", return_value=self.powerbi_config), patch("backend.app.PowerBIClient") as factory:
            factory.return_value.list_reports.return_value = [{
                "id": "individual-report", "name": INDIVIDUAL_REPORT_NAME, "datasetId": "individual-dataset",
            }]
            factory.return_value.build_embed_config.return_value = {"reportId": "individual-report"}
            factory.return_value.get_refresh_history.return_value = []
            self.assertEqual(self.administrator.get("/api/powerbi/individual-report").status_code, 200)
            identity = factory.return_value.build_embed_config.call_args.kwargs
            self.assertEqual(identity["username"], "role-admin@example.com")
            self.assertEqual(identity["roles"], ["IR Web Demo User RLS"])

    def test_demotion_changes_revision_and_removes_admin_access(self):
        self.change_role("admin")
        self.login(self.user)
        before = self.session_user()
        self.change_role("restricted")
        self.assertIsNone(self.session_user())
        self.assertEqual(self.user.get("/api/admin/users", headers={"Accept": "application/json"}).status_code, 401)
        self.login(self.user)
        after = self.session_user()
        self.assertNotEqual(before["accessRevision"], after["accessRevision"])
        self.assertEqual(self.user.get("/api/admin/users").status_code, 403)

    def test_role_change_invalidates_other_devices_and_remembered_logins(self):
        other_device = self.app.test_client()
        self.login(other_device, remember=True)
        self.assertIsNotNone(other_device.get_cookie("remember_token"))
        self.change_role("admin")
        self.assertIsNone(self.session_user())
        # Simulate a browser restart, with only the remember cookie remaining.
        other_device.delete_cookie("session")
        session = other_device.get("/api/auth/session").get_json()
        self.assertFalse(session["authenticated"])
        self.assertIsNone(session["user"])
        # The administrator who made the change keeps their own session.
        self.assertEqual(self.administrator.get("/api/admin/users").status_code, 200)

    def test_same_role_save_preserves_existing_sessions(self):
        before = self.session_user()
        response = self.change_role("restricted")
        self.assertFalse(response["sessionsReset"])
        self.assertEqual(self.session_user(), before)

    def test_display_name_edit_preserves_existing_sessions(self):
        response = self.administrator.patch(f"/api/admin/users/{self.user_id}", json={
            "fullName": "New display name", "role": "restricted", "allowedProjectRefs": ["old-project"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["sessionsReset"])
        self.assertEqual(self.session_user()["fullName"], "New display name")

    def test_invalid_role_update_preserves_role_and_session(self):
        before = self.session_user()
        response = self.administrator.patch(f"/api/admin/users/{self.user_id}", json={"role": "missing-role"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.session_user(), before)

    def test_self_demotion_invalidates_the_admins_session(self):
        administrator_id = self.administrator.get("/api/auth/session").get_json()["user"]["id"]
        response = self.administrator.patch(f"/api/admin/users/{administrator_id}", json={"role": "restricted"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["sessionsReset"])
        self.assertFalse(self.administrator.get("/api/auth/session").get_json()["authenticated"])
        self.assertEqual(self.administrator.get("/api/admin/users", headers={"Accept": "application/json"}).status_code, 401)

    def test_rule_changes_are_visible_without_changing_role_name(self):
        before = self.session_user()
        response = self.administrator.patch(f"/api/admin/roles/{self.role_id}", json={
            "projectScope": "all", "uploadScope": "all",
        })
        self.assertEqual(response.status_code, 200)
        after = self.session_user()
        self.assertEqual(before["roles"], after["roles"])
        self.assertNotEqual(before["accessRevision"], after["accessRevision"])

    def test_revision_is_stable_for_unchanged_access_and_display_name_edits(self):
        before = self.session_user()["accessRevision"]
        self.assertEqual(self.session_user()["accessRevision"], before)
        with self.app.app_context():
            User.query.filter_by(id=self.user_id).one().full_name = "New display name"
            db.session.commit()
        self.assertEqual(self.session_user()["accessRevision"], before)


if __name__ == "__main__":
    unittest.main()
