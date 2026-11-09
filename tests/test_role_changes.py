from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
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
        for client, email in [(self.administrator, "role-admin@example.com"), (self.user, "role-user@example.com")]:
            response = client.post("/api/auth/login", json={"email": email, "password": "role-test-password"})
            self.assertEqual(response.status_code, 200)

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.csrf.stop()
        self.env.stop()
        self.temp.cleanup()

    def session_user(self):
        return self.user.get("/api/auth/session").get_json()["user"]

    def change_role(self, role):
        response = self.administrator.patch(f"/api/admin/users/{self.user_id}", json={
            "role": role, "allowedProjectRefs": ["old-project"],
        })
        self.assertEqual(response.status_code, 200)

    def test_existing_session_gains_admin_access_and_fresh_embed_identity(self):
        before = self.session_user()
        self.assertEqual(self.user.get("/api/admin/users").status_code, 403)
        self.change_role("admin")
        after = self.session_user()
        self.assertEqual(after["roles"], ["admin"])
        self.assertNotEqual(before["accessRevision"], after["accessRevision"])
        self.assertEqual(self.user.get("/api/admin/users").status_code, 200)
        selections = [{"report_id": "other-report", "project_scope": "restricted", "allowed_project_refs": ["other-project"]}]
        with patch("backend.app.fetch_powerbi_report_selections", return_value=selections):
            self.assertEqual(self.user.get("/api/powerbi/selections").get_json()["reports"], selections)
        with patch("backend.app.PowerBIConfig.from_env"), patch("backend.app.PowerBIClient") as factory:
            factory.return_value.build_embed_config.return_value = {"reportId": "report"}
            self.assertEqual(self.user.get("/api/powerbi/embed-config").status_code, 200)
            self.assertEqual(factory.return_value.build_embed_config.call_args.kwargs,
                             {"username": "role-user@example.com", "roles": ["admin"]})

    def test_demotion_changes_revision_and_removes_admin_access(self):
        self.change_role("admin")
        before = self.session_user()
        self.change_role("restricted")
        after = self.session_user()
        self.assertNotEqual(before["accessRevision"], after["accessRevision"])
        self.assertEqual(self.user.get("/api/admin/users").status_code, 403)

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
