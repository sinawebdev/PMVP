import os
import threading
import time
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, render_template
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"

from flask_migrate import Migrate

migrate = Migrate()


def resolve_database_uri(local_sqlite_path):
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        return database_url
    return f"sqlite:///{local_sqlite_path}"


def database_type_label(database_uri):
    # Match both the bare "postgresql://" form and the driver-qualified
    # "postgresql+psycopg2://" form (the DATABASE_URL shape the pmvp-v1 pooler
    # uses). Without the "+driver" case this returns "Other", which silently
    # skips the Supabase connection-resilience engine options below.
    if str(database_uri or "").startswith(("postgresql://", "postgresql+")):
        return "PostgreSQL"
    if str(database_uri or "").startswith("sqlite"):
        return "SQLite"
    return "Other"


def assert_persistent_database_config(app):
    database_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    is_production = app.config.get("IS_PRODUCTION", False)
    persistence_required = (
        os.getenv("PERSISTENCE_REQUIRED", "true" if is_production else "false").lower()
        == "true"
    )
    if persistence_required and not os.getenv("DATABASE_URL"):
        raise RuntimeError(
            "DATABASE_URL is required for deployed Chrisnat Payroll MVP persistence. "
            "Do not run production on local SQLite."
        )
    if persistence_required and str(database_uri).startswith("sqlite"):
        raise RuntimeError(
            "Persistent PostgreSQL storage is required for deployment; SQLite files "
            "will not survive Render/Railway restarts."
        )


def format_ghana_cedis(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"GH₵ {amount:,.2f}"


def format_role_label(value):
    labels = {
        "admin": "Admin",
        "md": "MD",
        "accounts_officer": "Accounts Officer",
        "payroll_officer": "Payroll Officer",
        "operations_supervisor": "Operations Supervisor",
        "client_user": "Client User",
        "viewer": "Viewer",
    }
    return labels.get(str(value or "").lower(), str(value or "").replace("_", " ").title())


def create_app():
    if os.getenv("SKIP_DOTENV", "false").lower() != "true":
        load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    is_production = (
        os.getenv("RENDER") == "true"
        or os.getenv("RAILWAY_ENVIRONMENT") is not None
        or os.getenv("FLASK_ENV") == "production"
    )
    app.config["IS_PRODUCTION"] = is_production
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
    # --- Product identity (single config seam) ---
    # De-hardcodes the product name so a rebrand is a config change, not a
    # cross-template sweep. Defaults reproduce today's chrome byte-for-byte.
    app.config["APP_NAME"] = os.getenv("APP_NAME", "Chrisnat Payroll MVP")
    app.config["APP_BRAND_NAME"] = os.getenv("APP_BRAND_NAME", "Chrisnat")
    app.config["APP_SHORT_NAME"] = os.getenv("APP_SHORT_NAME", "Payroll MVP")
    app.config["APP_BRAND_MARK"] = os.getenv("APP_BRAND_MARK", "CN")
    app.config["SERVICE_SLUG"] = os.getenv("SERVICE_SLUG", "chrisnat-payroll-mvp")
    app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri(
        os.path.join(app.instance_path, "chrisnat_payroll.db")
    )
    assert_persistent_database_config(app)
    database_type = database_type_label(app.config["SQLALCHEMY_DATABASE_URI"])
    app.config["DATABASE_TYPE_LABEL"] = database_type

    # Connection resilience for the Supabase-hosted Postgres. Without these,
    # SQLAlchemy's default pool eventually hands out a connection the pooler has
    # already dropped after an idle period, and the first query on that dead
    # socket fails with 'SSL error: decryption failed or bad record mac' (the
    # Flask-Login user-loader SELECT was the visible casualty). pool_pre_ping
    # tests and transparently replaces a stale connection before use;
    # pool_recycle retires one before Supabase's idle timeout; TCP keepalives
    # keep an otherwise-idle connection from being reaped. Postgres only — these
    # connect_args are invalid for the local SQLite fallback.
    if database_type == "PostgreSQL":
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "pool_recycle": 280,
            "connect_args": {
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        }
    startup_message = (
        "Using PostgreSQL database"
        if database_type == "PostgreSQL"
        else "Using local SQLite database"
    )
    print(startup_message)
    app.logger.info(startup_message)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = (
        os.getenv("SESSION_COOKIE_SECURE", "true" if is_production else "false").lower()
        == "true"
    )
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))
    app.config["UPLOAD_FOLDER"] = os.path.abspath(
        os.path.join(app.root_path, "..", "uploads")
    )
    app.config["EXPORT_FOLDER"] = os.path.abspath(
        os.path.join(app.root_path, "..", "exports")
    )
    app.config["IMPORT_SESSION_FOLDER"] = os.path.join(
        app.instance_path, "import_sessions"
    )

    # Raw-hours engine bank whitelist (config, never hardcoded in a formula —
    # the DZ workbook's nested-IF listed 10 banks inline). A net-pay worker whose
    # bank is on this list is routed to the bank schedule; everyone else goes to
    # cash/PV. Override with a comma-separated RAW_BANK_WHITELIST env var.
    _default_banks = (
        "ADB,SG-GH,GCB,GCB BANK,FBN,FIRST BANK,ECOBANK,ACCESS,ACCESS BANK,"
        "NIB,CBG,CONSOLIDATED BANK,ZENITH,GT BANK,GTBANK,STANBIC,ABSA,"
        "FIDELITY,CAL BANK,CALBANK,REPUBLIC,PRUDENTIAL,UBA,BANK OF AFRICA,"
        "SOCIETE GENERALE,STANDARD CHARTERED,ADB BANK,NATIONAL INVESTMENT BANK"
    )
    app.config["RAW_BANK_WHITELIST"] = [
        b.strip() for b in os.getenv("RAW_BANK_WHITELIST", _default_banks).split(",")
        if b.strip()
    ]

    # --- Payslip distribution channels ---
    # Each channel defaults to a console backend (logs only, no credentials, no network).
    # Set the matching *_BACKEND + credentials to go live per channel.
    app.config["SMS_BACKEND"] = os.getenv("SMS_BACKEND", "console")          # console|hubtel
    app.config["SMS_SENDER_ID"] = os.getenv("SMS_SENDER_ID")
    app.config["SMS_HUBTEL_CLIENT_ID"] = os.getenv("SMS_HUBTEL_CLIENT_ID")
    app.config["SMS_HUBTEL_CLIENT_SECRET"] = os.getenv("SMS_HUBTEL_CLIENT_SECRET")
    app.config["SMS_HUBTEL_BASE_URL"] = os.getenv(
        "SMS_HUBTEL_BASE_URL", "https://sms.hubtel.com/v1/messages/send"
    )
    app.config["WHATSAPP_BACKEND"] = os.getenv("WHATSAPP_BACKEND", "console")  # console|cloud
    app.config["WHATSAPP_TOKEN"] = os.getenv("WHATSAPP_TOKEN")
    app.config["WHATSAPP_PHONE_NUMBER_ID"] = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    app.config["WHATSAPP_API_VERSION"] = os.getenv("WHATSAPP_API_VERSION", "v21.0")
    app.config["WHATSAPP_BASE_URL"] = os.getenv("WHATSAPP_BASE_URL", "https://graph.facebook.com")
    app.config["EMAIL_BACKEND"] = os.getenv("EMAIL_BACKEND", "console")        # console|smtp
    app.config["DEFAULT_FROM_EMAIL"] = os.getenv("DEFAULT_FROM_EMAIL", "payroll@chrisnat.local")
    app.config["SMTP_HOST"] = os.getenv("SMTP_HOST")
    app.config["SMTP_PORT"] = int(os.getenv("SMTP_PORT", "587"))
    app.config["SMTP_USERNAME"] = os.getenv("SMTP_USERNAME")
    app.config["SMTP_PASSWORD"] = os.getenv("SMTP_PASSWORD")
    app.config["SMTP_USE_TLS"] = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

    # No-login payslip links: PUBLIC_BASE_URL is the public host used in the link we send
    # (falls back to the request host when unset); PAYSLIP_LINK_MAX_AGE is the link lifetime.
    app.config["PUBLIC_BASE_URL"] = os.getenv("PUBLIC_BASE_URL")
    app.config["PAYSLIP_LINK_MAX_AGE"] = int(
        os.getenv("PAYSLIP_LINK_MAX_AGE", str(60 * 60 * 24 * 30))
    )

    # --- Distribution queue worker (Phase 3, Slice 1) ---
    # No separate worker dyno/service exists yet (Render's plan is a single web
    # process), so the default is an in-process polling thread inside the web
    # process itself — durable because the queue lives in Postgres, not memory.
    # A real `flask distribution-worker` process (see the CLI command below) can
    # take over later just by setting DISTRIBUTION_WORKER_INLINE=false once a
    # dedicated worker dyno exists; claiming a batch is row-locked either way, so
    # both can safely run at once during a migration between the two.
    app.config["DISTRIBUTION_WORKER_POLL_INTERVAL"] = int(
        os.getenv("DISTRIBUTION_WORKER_POLL_INTERVAL", "3")
    )
    app.config["DISTRIBUTION_WORKER_INLINE"] = (
        os.getenv("DISTRIBUTION_WORKER_INLINE", "true" if is_production else "false").lower()
        == "true"
    )
    # Retry policy (Phase 3, Slice 3). MAX_ATTEMPTS caps *automatic* retries of a
    # failed delivery (the first send counts as attempt 1); once reached the
    # delivery is a final failure. Automatic retries back off exponentially:
    # BACKOFF_SECONDS * 2**(attempts-1). A manual "resend failed" is the operator
    # override and is not bounded by MAX_ATTEMPTS.
    app.config["DISTRIBUTION_MAX_ATTEMPTS"] = int(
        os.getenv("DISTRIBUTION_MAX_ATTEMPTS", "3")
    )
    app.config["DISTRIBUTION_RETRY_BACKOFF_SECONDS"] = int(
        os.getenv("DISTRIBUTION_RETRY_BACKOFF_SECONDS", "60")
    )

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["EXPORT_FOLDER"], exist_ok=True)
    os.makedirs(app.config["IMPORT_SESSION_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    app.jinja_env.filters["cedis"] = format_ghana_cedis
    app.jinja_env.filters["role_label"] = format_role_label

    # Operator capability predicates as template globals — one source of truth
    # (app/permissions.py) for nav/action gating, replacing inline role lists.
    from app.permissions import (
        can_approve_run,
        can_bulk_approve_reject,
        can_calculate_run,
        can_delete_run,
        can_distribute_run,
        can_edit_run_figures,
        can_manage_statutory,
        can_maintain_roster,
        can_mark_run_processed,
        can_operate_payroll,
        can_reject_run,
        can_submit_run_for_approval,
        can_view_audit,
    )

    from app.payroll_status import run_progress, status_badge_class
    from app.distribution.service import retry_state as delivery_retry_state

    app.jinja_env.globals.update(
        # Per-delivery retry position (attempts, retries remaining, final-failure)
        # for the distribution status tables — one source of truth, Phase 3 Slice 3.
        delivery_retry_state=delivery_retry_state,
        # Lifecycle progress (presentation) — a status-derived stepper + status
        # pill, reused across the operator dashboard, runs list, and run detail.
        run_progress=run_progress,
        status_badge_class=status_badge_class,
        can_operate_payroll=can_operate_payroll,
        can_maintain_roster=can_maintain_roster,
        can_view_audit=can_view_audit,
        can_manage_statutory=can_manage_statutory,
        # Payroll-run lifecycle gates (role x run-status), used by
        # payroll_detail.html in place of inline role/status expressions.
        can_calculate_run=can_calculate_run,
        can_edit_run_figures=can_edit_run_figures,
        can_submit_run_for_approval=can_submit_run_for_approval,
        can_approve_run=can_approve_run,
        can_reject_run=can_reject_run,
        can_bulk_approve_reject=can_bulk_approve_reject,
        can_mark_run_processed=can_mark_run_processed,
        can_distribute_run=can_distribute_run,
        can_delete_run=can_delete_run,
    )

    from app.audit import audit_bp
    from app.auth import auth_bp
    from app.client import client_bp
    from app.distribution import distribution_bp, payslip_link_bp
    from app.employees import employees_bp
    from app.notifications import notifications_bp
    from app.oversight import oversight_bp
    from app.payroll import payroll_bp
    from app.payslip import payslip_bp
    from app.raw_engine.web import raw_engine_bp
    from app.routes import main_bp
    from app.statutory import statutory_bp

    app.register_blueprint(audit_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(client_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(payslip_bp)
    app.register_blueprint(distribution_bp)
    app.register_blueprint(payslip_link_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(statutory_bp)
    app.register_blueprint(raw_engine_bp)
    app.register_blueprint(oversight_bp)
    app.register_blueprint(notifications_bp)

    @app.context_processor
    def inject_app_identity():
        # Product-name strings for templates (title, brand). Config-sourced so a
        # rebrand never requires touching a template.
        return {
            "app_name": app.config["APP_NAME"],
            "app_brand_name": app.config["APP_BRAND_NAME"],
            "app_short_name": app.config["APP_SHORT_NAME"],
            "app_brand_mark": app.config["APP_BRAND_MARK"],
        }

    @app.context_processor
    def inject_notification_count():
        # Unread badge for both plane navbars. Never raise from a context
        # processor (it renders on the error page too) — fail soft to 0.
        try:
            from app.notifications import unread_count

            return {"notif_unread": unread_count()}
        except Exception:  # noqa: BLE001
            db.session.rollback()
            return {"notif_unread": 0}

    @app.context_processor
    def inject_sidebar_clients():
        from app.models import ClientCompany
        from app.tenancy import active_tenant_id

        # Rendered on every authenticated page — including the branded 500 page.
        # If the DB connection is the very thing that failed, this query would
        # raise again and turn the friendly error page into a raw crash, so fail
        # soft to an empty sidebar rather than let the error handler re-error.
        #
        # Tenant-scoped: a client user must never see other tenants' company
        # names in the sidebar, so a tenant user's list is limited to their own
        # company; platform (Chrisnat) users see all active clients.
        try:
            query = ClientCompany.query.filter_by(status="Active")
            tenant_id = active_tenant_id()
            if tenant_id is not None:
                query = query.filter(ClientCompany.id == tenant_id)
            clients = query.order_by(ClientCompany.name).all()
        except Exception:  # noqa: BLE001 - context processors must never raise
            db.session.rollback()
            clients = []
        return {"sidebar_clients": clients}

    # Branded error pages (A4). DEBUG is False under gunicorn in production (it
    # imports run:app, so app.run(debug=...) never executes), which is what lets
    # these handlers run instead of leaking a stack trace.
    @app.errorhandler(404)
    def handle_not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def handle_server_error(error):
        # A failed request may have left the session mid-transaction; roll it back
        # so rendering the error page (and the next request) starts clean.
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.cli.command("init-db")
    def init_db_command():
        """Create/upgrade tables and seed starter users/clients without wiping data."""
        initialize_database(app)
        print("Database initialized.")

    @app.cli.command("distribution-worker")
    def distribution_worker_command():
        """Run the payslip distribution queue worker in the foreground (Ctrl+C to stop).

        For a deployment with a dedicated worker dyno/service — run this instead of
        (or alongside) the in-process thread, and set DISTRIBUTION_WORKER_INLINE=false
        on the web dyno so only this process sends.
        """
        from app.distribution.queue import run_worker_loop

        print("Distribution worker started — polling for queued batches.")
        try:
            run_worker_loop(poll_interval=app.config["DISTRIBUTION_WORKER_POLL_INTERVAL"])
        except KeyboardInterrupt:
            print("Distribution worker stopped.")

    if os.getenv("AUTO_INIT_DB", "true").lower() == "true":
        initialize_database(app)

    if app.config["DISTRIBUTION_WORKER_INLINE"] and (
        not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    ):
        _start_inline_distribution_worker(app)

    return app


def _start_inline_distribution_worker(app):
    """Poll the distribution queue on a background thread inside the web process.

    Guarded by the caller against Werkzeug's debug-reloader parent process, so this
    starts exactly once per running app instance.
    """
    from app.distribution.queue import run_worker_loop

    def _target():
        with app.app_context():
            run_worker_loop(poll_interval=app.config["DISTRIBUTION_WORKER_POLL_INTERVAL"])

    threading.Thread(target=_target, name="distribution-worker", daemon=True).start()


def initialize_database(app):
    with app.app_context():
        for attempt in range(1, 6):
            try:
                db.create_all()
                break
            except Exception as exc:
                if attempt == 5:
                    app.logger.exception("Database initialization failed after 5 attempts.")
                    raise
                app.logger.warning(
                    "Database not ready yet during startup attempt %s/5: %s",
                    attempt,
                    exc,
                )
                time.sleep(2)
        from app.seed import seed_default_data

        seed_default_data()


@login_manager.user_loader
def load_user(user_id):
    from app.models import User

    return db.session.get(User, int(user_id))
