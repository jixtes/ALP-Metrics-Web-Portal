from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import Flask, current_app, jsonify, request
from flask_login import login_user, logout_user
from flask_security import Security, SQLAlchemyUserDatastore, auth_required, current_user, hash_password, roles_required
from flask_security.models import fsqla_v3 as fsqla
from flask_security.utils import verify_and_update_password
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf, validate_csrf
from sqlalchemy import text

db = SQLAlchemy()
csrf = CSRFProtect()
fsqla.FsModels.set_db_info(db)


class Role(db.Model, fsqla.FsRoleMixin):
    project_scope = db.Column(db.String(32), nullable=False, default="all")
    report_scope = db.Column(db.String(32), nullable=False, default="all")
    upload_scope = db.Column(db.String(32), nullable=False, default="all")
    allowed_project_refs_json = db.Column(db.Text, nullable=False, default="[]")
    allowed_report_ids_json = db.Column(db.Text, nullable=False, default="[]")


class User(db.Model, fsqla.FsUserMixin):
    full_name = db.Column(db.String(255), nullable=True)
    allowed_project_refs_json = db.Column(db.Text, nullable=False, default="[]")


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id])


def init_auth(app: Flask) -> None:
    auth_db_path = Path(app.instance_path) / "auth.db"
    auth_db_path.parent.mkdir(parents=True, exist_ok=True)

    secret_key = os.getenv("SECRET_KEY", "alp-metrics-dev-secret-key")
    password_salt = os.getenv("SECURITY_PASSWORD_SALT", "alp-metrics-dev-password-salt")

    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = secret_key
    if not app.secret_key:
        app.secret_key = secret_key
    if not app.config.get("SECURITY_PASSWORD_SALT"):
        app.config["SECURITY_PASSWORD_SALT"] = password_salt
    app.config.setdefault("SQLALCHEMY_DATABASE_URI", f"sqlite:///{auth_db_path}")
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("SECURITY_PASSWORD_HASH", "pbkdf2_sha512")
    app.config.setdefault("SECURITY_REGISTERABLE", False)
    app.config.setdefault("SECURITY_RECOVERABLE", False)
    app.config.setdefault("SECURITY_SEND_REGISTER_EMAIL", False)
    app.config.setdefault("SECURITY_EMAIL_VALIDATOR_ARGS", {"check_deliverability": False})
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_SECURE", os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true")
    app.config.setdefault("REMEMBER_COOKIE_DURATION", timedelta(days=7))
    app.config.setdefault("REMEMBER_COOKIE_HTTPONLY", True)
    app.config.setdefault("REMEMBER_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("REMEMBER_COOKIE_SECURE", app.config["SESSION_COOKIE_SECURE"])
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)
    app.config.setdefault("ALP_RESET_LINK_HOURS", int(os.getenv("ALP_RESET_LINK_HOURS", "168")))

    db.init_app(app)
    csrf.init_app(app)

    user_datastore = SQLAlchemyUserDatastore(db, User, Role)
    Security(app, user_datastore)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error: CSRFError):
        return jsonify({"error": error.description or "CSRF validation failed."}), 400

    with app.app_context():
        db.create_all()
        _ensure_auth_schema()
        _bootstrap_roles_and_admin(user_datastore)

    register_auth_routes(app, user_datastore)


def register_auth_routes(app: Flask, user_datastore: SQLAlchemyUserDatastore) -> None:
    @app.get("/api/auth/session")
    def auth_session():
        return jsonify(
            {
                "authenticated": bool(current_user.is_authenticated),
                "user": _serialize_user(current_user if current_user.is_authenticated else None),
                "csrfToken": generate_csrf(),
            }
        )

    @app.post("/api/auth/login")
    def auth_login():
        _require_csrf()
        payload = request.get_json(silent=True) or {}
        email = str(payload.get("email", "")).strip().lower()
        password = str(payload.get("password", ""))
        remember = bool(payload.get("remember"))

        if not email or not password:
            return jsonify({"error": "Email and password are required."}), 400

        user = user_datastore.find_user(email=email)
        if not user or not user.active:
            return jsonify({"error": "Invalid email or password."}), 401

        if not verify_and_update_password(password, user):
            db.session.rollback()
            return jsonify({"error": "Invalid email or password."}), 401

        login_user(user, remember=remember)
        db.session.commit()
        return jsonify(
            {
                "authenticated": True,
                "user": _serialize_user(user),
                "csrfToken": generate_csrf(),
            }
        )

    @app.post("/api/auth/logout")
    @auth_required("session")
    def auth_logout():
        _require_csrf()
        logout_user()
        return jsonify(
            {
                "authenticated": False,
                "user": None,
                "csrfToken": generate_csrf(),
            }
        )

    @app.get("/api/auth/profile")
    @auth_required("session")
    def auth_profile():
        return jsonify({"user": _serialize_user(current_user)})

    @app.patch("/api/auth/profile")
    @auth_required("session")
    def update_auth_profile():
        _require_csrf()
        payload = request.get_json(silent=True) or {}
        email = str(payload.get("email", "")).strip().lower()
        full_name = str(payload.get("fullName", "")).strip() or None

        if not email:
            return jsonify({"error": "Email is required."}), 400
        if "@" not in email:
            return jsonify({"error": "Enter a valid email address."}), 400

        existing_user = user_datastore.find_user(email=email)
        if existing_user and existing_user.id != current_user.id:
            return jsonify({"error": "A user with that email already exists."}), 409

        current_user.email = email
        current_user.full_name = full_name
        db.session.commit()
        return jsonify({"user": _serialize_user(current_user)})

    @app.post("/api/auth/change-password")
    @auth_required("session")
    def change_auth_password():
        _require_csrf()
        payload = request.get_json(silent=True) or {}
        current_password = str(payload.get("currentPassword", ""))
        new_password = str(payload.get("newPassword", ""))

        if not verify_and_update_password(current_password, current_user):
            db.session.rollback()
            return jsonify({"error": "Current password is incorrect."}), 400

        password_error = _validate_password(new_password)
        if password_error:
            db.session.rollback()
            return jsonify({"error": password_error}), 400

        current_user.password = hash_password(new_password)
        current_user.fs_uniquifier = uuid.uuid4().hex
        _revoke_other_reset_tokens(current_user.id)
        db.session.commit()

        login_user(current_user, remember=False)
        return jsonify(
            {
                "message": "Password updated successfully.",
                "user": _serialize_user(current_user),
                "csrfToken": generate_csrf(),
            }
        )

    @app.get("/api/auth/reset-password/validate")
    def validate_reset_password():
        token = str(request.args.get("token", "")).strip()
        reset_row = _lookup_active_reset_token(token)
        if not reset_row:
            return jsonify({"valid": False, "error": "This reset link is invalid or has expired."}), 400
        return jsonify({"valid": True, "email": reset_row.user.email, "expiresAt": _to_iso(reset_row.expires_at)})

    @app.post("/api/auth/reset-password")
    def reset_password():
        _require_csrf()
        payload = request.get_json(silent=True) or {}
        token = str(payload.get("token", "")).strip()
        password = str(payload.get("password", ""))

        password_error = _validate_password(password)
        if password_error:
            return jsonify({"error": password_error}), 400

        reset_row = _lookup_active_reset_token(token)
        if not reset_row:
            return jsonify({"error": "This reset link is invalid or has expired."}), 400

        reset_row.user.password = hash_password(password)
        reset_row.user.fs_uniquifier = uuid.uuid4().hex
        reset_row.used_at = _utcnow()
        _revoke_other_reset_tokens(reset_row.user_id, except_id=reset_row.id)
        db.session.commit()

        return jsonify({"message": "Password updated. You can now sign in with the new password."})

    @app.get("/api/admin/users")
    @auth_required("session")
    @roles_required("admin")
    def list_users():
        users = User.query.order_by(User.email.asc()).all()
        return jsonify({"users": [_serialize_user(user) for user in users]})

    @app.get("/api/admin/roles")
    @auth_required("session")
    @roles_required("admin")
    def list_roles():
        roles = Role.query.order_by(Role.name.asc()).all()
        return jsonify({"roles": [_serialize_role(role) for role in roles]})

    @app.post("/api/admin/roles")
    @auth_required("session")
    @roles_required("admin")
    def create_role():
        _require_csrf()
        payload = request.get_json(silent=True) or {}
        role_name = str(payload.get("name", "")).strip().lower()
        description = str(payload.get("description", "")).strip() or None

        if not role_name:
            return jsonify({"error": "Role name is required."}), 400
        if role_name == "admin":
            return jsonify({"error": "The admin role is reserved."}), 400
        if Role.query.filter_by(name=role_name).first():
            return jsonify({"error": "A role with that name already exists."}), 409

        role = Role(name=role_name, description=description)
        try:
            _apply_role_access_payload(role, payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        db.session.add(role)
        db.session.commit()
        return jsonify({"role": _serialize_role(role)}), 201

    @app.patch("/api/admin/roles/<int:role_id>")
    @auth_required("session")
    @roles_required("admin")
    def update_role(role_id: int):
        _require_csrf()
        role = Role.query.get(role_id)
        if not role:
            return jsonify({"error": "Role not found."}), 404
        if role.name == "admin":
            return jsonify({"error": "The admin role cannot be updated."}), 400

        payload = request.get_json(silent=True) or {}
        role_name = str(payload.get("name", role.name)).strip().lower()
        description = str(payload.get("description", role.description or "")).strip() or None

        if not role_name:
            return jsonify({"error": "Role name is required."}), 400
        if role_name == "admin":
            return jsonify({"error": "The admin role is reserved."}), 400

        existing_role = Role.query.filter_by(name=role_name).first()
        if existing_role and existing_role.id != role.id:
            return jsonify({"error": "A role with that name already exists."}), 409

        role.name = role_name
        role.description = description
        try:
            _apply_role_access_payload(role, payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        db.session.commit()
        return jsonify({"role": _serialize_role(role)})

    @app.delete("/api/admin/roles/<int:role_id>")
    @auth_required("session")
    @roles_required("admin")
    def delete_role(role_id: int):
        _require_csrf()
        role = Role.query.get(role_id)
        if not role:
            return jsonify({"error": "Role not found."}), 404
        if role.name == "admin":
            return jsonify({"error": "The admin role cannot be deleted."}), 400
        if _role_user_count(role) > 0:
            return jsonify({"error": "Reassign users before deleting this role."}), 400

        db.session.delete(role)
        db.session.commit()
        return jsonify({"deleted": True, "roleId": role_id})

    @app.post("/api/admin/users")
    @auth_required("session")
    @roles_required("admin")
    def create_user():
        _require_csrf()
        payload = request.get_json(silent=True) or {}
        email = str(payload.get("email", "")).strip().lower()
        full_name = str(payload.get("fullName", "")).strip() or None
        role_name = str(payload.get("role", "viewer")).strip().lower() or "viewer"
        password = str(payload.get("password", ""))

        if not email:
            return jsonify({"error": "Email is required."}), 400
        if "@" not in email:
            return jsonify({"error": "Enter a valid email address."}), 400
        if user_datastore.find_user(email=email):
            return jsonify({"error": "A user with that email already exists."}), 409

        password_error = _validate_password(password)
        if password_error:
            return jsonify({"error": password_error}), 400

        role = user_datastore.find_role(role_name)
        if role is None:
            return jsonify({"error": "Select a valid role."}), 400

        user = user_datastore.create_user(
            email=email,
            password=hash_password(password),
            active=True,
            roles=[role],
            full_name=full_name,
            allowed_project_refs_json=json.dumps(_clean_string_list(payload.get("allowedProjectRefs"))),
        )
        db.session.commit()
        return jsonify({"user": _serialize_user(user)}), 201

    @app.patch("/api/admin/users/<int:user_id>")
    @auth_required("session")
    @roles_required("admin")
    def update_user(user_id: int):
        _require_csrf()
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found."}), 404

        payload = request.get_json(silent=True) or {}
        email = str(payload.get("email", user.email or "")).strip().lower()
        full_name = str(payload.get("fullName", user.full_name or "")).strip() or None
        role_name = str(payload.get("role", _primary_role(user).name if _primary_role(user) else "")).strip().lower()

        if not email:
            return jsonify({"error": "Email is required."}), 400
        if "@" not in email:
            return jsonify({"error": "Enter a valid email address."}), 400

        existing_user = user_datastore.find_user(email=email)
        if existing_user and existing_user.id != user.id:
            return jsonify({"error": "A user with that email already exists."}), 409

        role = user_datastore.find_role(role_name)
        if role is None:
            return jsonify({"error": "Select a valid role."}), 400

        user.email = email
        user.full_name = full_name
        user.roles = [role]
        user.allowed_project_refs_json = json.dumps(_clean_string_list(payload.get("allowedProjectRefs")))
        db.session.commit()
        return jsonify({"user": _serialize_user(user)})

    @app.delete("/api/admin/users/<int:user_id>")
    @auth_required("session")
    @roles_required("admin")
    def delete_user(user_id: int):
        _require_csrf()
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found."}), 404
        if user.id == current_user.id:
            return jsonify({"error": "You cannot delete your own account."}), 400

        role_names = {role.name for role in user.roles}
        if "admin" in role_names:
            other_admin_count = User.query.filter(User.id != user.id, User.roles.any(name="admin")).count()
            if other_admin_count == 0:
                return jsonify({"error": "Create another admin before deleting this admin user."}), 400

        PasswordResetToken.query.filter(
            (PasswordResetToken.user_id == user.id) | (PasswordResetToken.created_by_user_id == user.id)
        ).delete(synchronize_session=False)
        user.roles = []
        db.session.delete(user)
        db.session.commit()
        return jsonify({"deleted": True, "userId": user_id})

    @app.post("/api/admin/users/<int:user_id>/reset-link")
    @auth_required("session")
    @roles_required("admin")
    def issue_reset_link(user_id: int):
        _require_csrf()
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found."}), 404

        plaintext_token = secrets.token_urlsafe(32)
        now = _utcnow()
        expires_at = now + timedelta(hours=int(current_app.config["ALP_RESET_LINK_HOURS"]))

        _revoke_other_reset_tokens(user.id)
        reset_row = PasswordResetToken(
            token_hash=_hash_token(plaintext_token),
            user_id=user.id,
            created_by_user_id=current_user.id,
            created_at=now,
            expires_at=expires_at,
        )
        db.session.add(reset_row)
        db.session.commit()

        return jsonify(
            {
                "resetUrl": _build_reset_url(plaintext_token),
                "expiresAt": _to_iso(expires_at),
                "user": _serialize_user(user),
            }
        )


def _bootstrap_roles_and_admin(user_datastore: SQLAlchemyUserDatastore) -> None:
    default_roles = [
        ("admin", "Full access to ALP Metrics administration."),
        ("operator", "Can run the pipeline and review workspace data."),
        ("viewer", "Read-only access to workspace data."),
    ]
    roles_exist = Role.query.first() is not None
    roles_to_ensure = default_roles if not roles_exist else default_roles[:1]
    for role_name, description in roles_to_ensure:
        if not user_datastore.find_role(role_name):
            user_datastore.create_role(name=role_name, description=description)

    for role in Role.query.all():
        if role.name == "admin":
            role.project_scope = "all"
            role.report_scope = "all"
            role.upload_scope = "all"
            role.allowed_project_refs_json = "[]"
            role.allowed_report_ids_json = "[]"

    admin_email = os.getenv("ALP_INITIAL_ADMIN_EMAIL", "admin@example.com").strip().lower()
    admin_password = os.getenv("ALP_INITIAL_ADMIN_PASSWORD", "admin 497")
    admin_name = os.getenv("ALP_INITIAL_ADMIN_NAME", "Admin")

    admin_user = user_datastore.find_user(email=admin_email)
    admin_role = user_datastore.find_role("admin")

    if not admin_user:
        user_datastore.create_user(
            email=admin_email,
            password=hash_password(admin_password),
            active=True,
            roles=[admin_role],
            full_name=admin_name,
        )
    elif admin_role not in admin_user.roles:
        admin_user.roles.append(admin_role)

    db.session.commit()


def _serialize_user(user: User | None) -> dict | None:
    if not user:
        return None
    primary_role = _primary_role(user)
    return {
        "id": user.id,
        "email": user.email,
        "fullName": user.full_name,
        "roles": sorted(role.name for role in user.roles),
        "primaryRole": primary_role.name if primary_role else None,
        "uploadScope": primary_role.upload_scope if primary_role else "all",
        "allowedProjectRefs": _load_json_list(user.allowed_project_refs_json),
    }


def _serialize_role(role: Role) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "projectScope": role.project_scope or "all",
        "reportScope": role.report_scope or "all",
        "uploadScope": role.upload_scope or "all",
        "allowedProjectRefs": _load_json_list(role.allowed_project_refs_json),
        "allowedReportIds": _load_json_list(role.allowed_report_ids_json),
        "isSystem": role.name == "admin",
        "userCount": _role_user_count(role),
    }


def _role_user_count(role: Role) -> int:
    return role.users.count() if hasattr(role.users, "count") else len(role.users)


def _require_csrf() -> None:
    validate_csrf(request.headers.get("X-CSRF-Token", ""))


def _validate_password(password: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters."
    return None


def _ensure_auth_schema() -> None:
    role_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(role)")).fetchall()
    }
    for column_name, column_type, default_value in [
        ("project_scope", "TEXT NOT NULL DEFAULT 'all'", "all"),
        ("report_scope", "TEXT NOT NULL DEFAULT 'all'", "all"),
        ("upload_scope", "TEXT NOT NULL DEFAULT 'all'", "all"),
        ("allowed_project_refs_json", "TEXT NOT NULL DEFAULT '[]'", "[]"),
        ("allowed_report_ids_json", "TEXT NOT NULL DEFAULT '[]'", "[]"),
    ]:
        if column_name not in role_columns:
            db.session.execute(text(f"ALTER TABLE role ADD COLUMN {column_name} {column_type}"))
            db.session.execute(text(f"UPDATE role SET {column_name} = :value WHERE {column_name} IS NULL"), {"value": default_value})

    user_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(user)")).fetchall()
    }
    if "allowed_project_refs_json" not in user_columns:
        db.session.execute(text("ALTER TABLE user ADD COLUMN allowed_project_refs_json TEXT NOT NULL DEFAULT '[]'"))
        db.session.execute(text("UPDATE user SET allowed_project_refs_json = '[]' WHERE allowed_project_refs_json IS NULL"))
    db.session.commit()


def _apply_role_access_payload(role: Role, payload: dict) -> None:
    project_scope = str(payload.get("projectScope", role.project_scope or "all")).strip().lower() or "all"
    report_scope = str(payload.get("reportScope", role.report_scope or "all")).strip().lower() or "all"
    if project_scope not in {"all", "restricted"}:
        raise ValueError("projectScope must be 'all' or 'restricted'.")
    if report_scope not in {"all", "restricted"}:
        raise ValueError("reportScope must be 'all' or 'restricted'.")
    upload_scope = str(payload.get("uploadScope", role.upload_scope or "all")).strip().lower() or "all"
    if upload_scope not in {"all", "project_files", "none"}:
        raise ValueError("uploadScope must be 'all', 'project_files', or 'none'.")

    role.project_scope = project_scope
    role.report_scope = report_scope
    role.upload_scope = upload_scope
    role.allowed_project_refs_json = json.dumps(_clean_string_list(payload.get("allowedProjectRefs")))
    role.allowed_report_ids_json = json.dumps(_clean_string_list(payload.get("allowedReportIds")))


def _clean_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _load_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item).strip() for item in loaded if str(item).strip()]


def _primary_role(user: User | None) -> Role | None:
    if not user or not user.roles:
        return None
    return sorted(user.roles, key=lambda role: (role.name != "admin", role.name))[0]


def current_user_project_access() -> tuple[str, set[str]]:
    user = current_user if current_user.is_authenticated else None
    role = _primary_role(user)
    if not role or role.name == "admin":
        return "all", set()
    if (role.project_scope or "all") != "restricted":
        return "all", set()
    return "restricted", set(_load_json_list(user.allowed_project_refs_json))


def current_user_report_access() -> tuple[str, set[str]]:
    role = _primary_role(current_user if current_user.is_authenticated else None)
    if not role or role.name == "admin":
        return "all", set()
    return role.report_scope or "all", set(_load_json_list(role.allowed_report_ids_json))


def current_user_upload_access() -> str:
    role = _primary_role(current_user if current_user.is_authenticated else None)
    if not role or role.name == "admin":
        return "all"
    return role.upload_scope or "all"


def current_user_is_admin() -> bool:
    if not current_user.is_authenticated:
        return False
    return any(role.name == "admin" for role in current_user.roles)


def user_access_preview(user_id: int | None) -> dict | None:
    if not user_id or not current_user_is_admin():
        return None

    user = User.query.get(user_id)
    role = _primary_role(user)
    if not user or not role or role.name == "admin":
        return None

    project_scope = role.project_scope or "all"
    return {
        "role": _serialize_role(role),
        "user": _serialize_user(user),
        "project_scope": project_scope,
        "allowed_project_refs": set(_load_json_list(user.allowed_project_refs_json)) if project_scope == "restricted" else set(),
        "report_scope": role.report_scope or "all",
        "allowed_report_ids": set(_load_json_list(role.allowed_report_ids_json)),
        "upload_scope": role.upload_scope or "all",
    }


def _lookup_active_reset_token(plaintext_token: str) -> PasswordResetToken | None:
    if not plaintext_token:
        return None

    reset_row = PasswordResetToken.query.filter_by(token_hash=_hash_token(plaintext_token)).first()
    if not reset_row:
        return None

    now = _utcnow()
    expires_at = _as_utc(reset_row.expires_at)
    if reset_row.used_at or reset_row.revoked_at or expires_at < now:
        return None
    if not reset_row.user.active:
        return None
    return reset_row


def _revoke_other_reset_tokens(user_id: int, *, except_id: int | None = None) -> None:
    now = _utcnow()
    query = PasswordResetToken.query.filter(
        PasswordResetToken.user_id == user_id,
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.revoked_at.is_(None),
    )
    if except_id is not None:
        query = query.filter(PasswordResetToken.id != except_id)

    for row in query.all():
        row.revoked_at = now


def _build_reset_url(plaintext_token: str) -> str:
    origin = request.headers.get("Origin", "").strip() or request.url_root.rstrip("/")
    return f"{origin}/reset-password?token={quote(plaintext_token)}"


def _hash_token(plaintext_token: str) -> str:
    return hashlib.sha256(plaintext_token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
