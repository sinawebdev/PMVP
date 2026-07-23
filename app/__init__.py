import os
import socket
import threading
import time
from datetime import timedelta

import click

from dotenv import load_dotenv
from flask import Flask, render_template
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
csrf = CSRFProtect()

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
            "DATABASE_URL is required for deployed Payrolla persistence. "
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


def format_duration(seconds):
    """Human duration for the monitoring dashboard: 45 -> '45s', 125 -> '2m 5s',
    3700 -> '1h 1m'. None/negative -> '—'."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


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
    _INSECURE_SECRET_KEY = "dev-secret-change-me"
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", _INSECURE_SECRET_KEY)
    # --- Product identity (single config seam) ---
    # De-hardcodes the product/company names so a rebrand or white-label is a
    # config change, not a cross-template sweep. Every user-facing surface reads
    # these (title, sidebar, login, emails, payslip PDF) via inject_app_identity.
    #   APP_NAME       — full product name (browser title, marketing)
    #   APP_BRAND_NAME — wordmark shown in-app (sidebar, email header)
    #   APP_SHORT_NAME — subtitle under the wordmark
    #   APP_BRAND_MARK — the single-glyph logo tile (the Payrolla "P")
    #   APP_TAGLINE    — one-line product descriptor for login/landing/footer
    #   COMPANY_NAME   — the company behind the product; legal/footer attribution
    #                    only, never in nav/app chrome (Sinaforte stays quiet)
    app.config["APP_NAME"] = os.getenv("APP_NAME", "Payrolla")
    app.config["APP_BRAND_NAME"] = os.getenv("APP_BRAND_NAME", "Payrolla")
    app.config["APP_SHORT_NAME"] = os.getenv("APP_SHORT_NAME", "Payroll & HR")
    app.config["APP_BRAND_MARK"] = os.getenv("APP_BRAND_MARK", "P")
    app.config["APP_TAGLINE"] = os.getenv(
        "APP_TAGLINE", "Modern payroll, compliance & workforce management"
    )
    app.config["COMPANY_NAME"] = os.getenv("COMPANY_NAME", "Sinaforte Technologies")
    app.config["SERVICE_SLUG"] = os.getenv("SERVICE_SLUG", "payrolla")
    app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri(
        os.path.join(app.instance_path, "chrisnat_payroll.db")
    )
    assert_persistent_database_config(app)
    # Never sign production sessions with the shared dev fallback: anyone who knows
    # it could forge a session cookie for any user. Refuse to boot in production
    # without a real SECRET_KEY (the fallback stays for local/tests). Ordered after
    # the persistence check so the DATABASE_URL failure still surfaces first.
    if is_production and app.config["SECRET_KEY"] == _INSECURE_SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY must be set to a strong random value in production — "
            "refusing to start with the insecure development fallback."
        )
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
    # Standard uploads stream through tempfile and raw uploads stage in
    # IMPORT_SESSION_FOLDER; there is no reader for a persistent UPLOAD_FOLDER, so
    # it is not configured (removed dead config in Phase 5).
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
    # Delivery-receipt webhooks (Phase 4, Slice 4). Endpoints stay disabled (404)
    # until their secret/token is set, so an unconfigured deployment can't be
    # spoofed. Meta: verify token for the subscription handshake + optional app
    # secret for X-Hub-Signature-256. Hubtel: a shared secret on the callback URL.
    app.config["WHATSAPP_VERIFY_TOKEN"] = os.getenv("WHATSAPP_VERIFY_TOKEN")
    app.config["WHATSAPP_APP_SECRET"] = os.getenv("WHATSAPP_APP_SECRET")
    app.config["HUBTEL_WEBHOOK_SECRET"] = os.getenv("HUBTEL_WEBHOOK_SECRET")
    app.config["EMAIL_BACKEND"] = os.getenv("EMAIL_BACKEND", "console")        # console|smtp
    app.config["DEFAULT_FROM_EMAIL"] = os.getenv("DEFAULT_FROM_EMAIL", "payroll@payrolla.app")
    # Optional sender display name and reply-to (Phase 3, Slice 9). Both optional
    # so existing config keeps working: with neither set, From is the bare
    # DEFAULT_FROM_EMAIL and no Reply-To header is added.
    app.config["EMAIL_SENDER_NAME"] = os.getenv("EMAIL_SENDER_NAME", app.config["APP_NAME"])
    app.config["EMAIL_REPLY_TO"] = os.getenv("EMAIL_REPLY_TO")
    # Accent colour for the branded email header/button — Payrolla Deep Teal.
    app.config["EMAIL_BRAND_COLOR"] = os.getenv("EMAIL_BRAND_COLOR", "#0D4D4D")
    # Optionally attach the payslip PDF to the email (off by default — v1 sends a
    # tokenized link). When on, the attachment is validated (exists, non-empty,
    # under the size cap) and silently skipped if it fails, so a bad attachment
    # never blocks the email.
    app.config["EMAIL_ATTACH_PAYSLIP_PDF"] = (
        os.getenv("EMAIL_ATTACH_PAYSLIP_PDF", "false").lower() == "true"
    )
    app.config["EMAIL_MAX_ATTACHMENT_BYTES"] = int(
        os.getenv("EMAIL_MAX_ATTACHMENT_BYTES", str(5 * 1024 * 1024))
    )
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
    # Stuck-batch recovery (Phase 5). A batch left in `running` because its worker
    # died mid-send is requeued once its started_at is older than STALE_SECONDS
    # (set comfortably above the longest plausible batch runtime so a live, busy
    # worker is never falsely reclaimed). A batch reclaimed more than MAX_RECLAIMS
    # times is failed instead of looping forever.
    app.config["DISTRIBUTION_BATCH_STALE_SECONDS"] = int(
        os.getenv("DISTRIBUTION_BATCH_STALE_SECONDS", "900")
    )
    app.config["DISTRIBUTION_BATCH_MAX_RECLAIMS"] = int(
        os.getenv("DISTRIBUTION_BATCH_MAX_RECLAIMS", "3")
    )
    # Above this delivery failure rate (0..1) a completed batch also alerts
    # platform admins, not just the initiator (Phase 3, Slice 8).
    app.config["DISTRIBUTION_FAILURE_ALERT_RATE"] = float(
        os.getenv("DISTRIBUTION_FAILURE_ALERT_RATE", "0.5")
    )
    # Per-channel send-rate ceilings (sends per second; 0 = unlimited). Paces
    # outbound sends to stay within provider quotas (Phase 4, Slice 2). Set these
    # at (provider limit / number of worker processes).
    app.config["RATE_LIMIT_SMS_PER_SEC"] = float(os.getenv("RATE_LIMIT_SMS_PER_SEC", "0"))
    app.config["RATE_LIMIT_WHATSAPP_PER_SEC"] = float(
        os.getenv("RATE_LIMIT_WHATSAPP_PER_SEC", "0")
    )
    app.config["RATE_LIMIT_EMAIL_PER_SEC"] = float(
        os.getenv("RATE_LIMIT_EMAIL_PER_SEC", "0")
    )
    # SLA thresholds + alerting (Phase 4, Slice 6). A batch not finished within
    # SLA_BATCH_MINUTES of when it should run, or a failure rate over
    # SLA_FAILURE_RATE across the recent window (with at least SLA_MIN_VOLUME
    # deliveries), is a breach. SLA_DELIVERY_CONFIRM_HOURS (0 = off) breaches on
    # sent messages with no delivery receipt after that long. The worker
    # re-checks every SLA_CHECK_INTERVAL_SECONDS and alerts platform admins at
    # most once per SLA_ALERT_COOLDOWN_SECONDS per breach type.
    app.config["SLA_BATCH_MINUTES"] = int(os.getenv("SLA_BATCH_MINUTES", "30"))
    app.config["SLA_FAILURE_RATE"] = float(os.getenv("SLA_FAILURE_RATE", "0.2"))
    app.config["SLA_MIN_VOLUME"] = int(os.getenv("SLA_MIN_VOLUME", "20"))
    app.config["SLA_WINDOW_HOURS"] = int(os.getenv("SLA_WINDOW_HOURS", "24"))
    app.config["SLA_DELIVERY_CONFIRM_HOURS"] = int(
        os.getenv("SLA_DELIVERY_CONFIRM_HOURS", "0")
    )
    app.config["SLA_CHECK_INTERVAL_SECONDS"] = int(
        os.getenv("SLA_CHECK_INTERVAL_SECONDS", "300")
    )
    app.config["SLA_ALERT_COOLDOWN_SECONDS"] = int(
        os.getenv("SLA_ALERT_COOLDOWN_SECONDS", "3600")
    )

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["EXPORT_FOLDER"], exist_ok=True)
    os.makedirs(app.config["IMPORT_SESSION_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    # Global CSRF protection (Flask-WTF). Every mutating request must carry a
    # session-bound token — in a hidden form field (auto-injected client-side) or
    # the X-CSRFToken header (htmx / fetch). Disabled only for the test suite
    # (WTF_CSRF_ENABLED=false, set in tests/__init__.py); the provider webhooks are
    # exempted below because they authenticate via HMAC/shared secret, not a
    # browser session.
    app.config["WTF_CSRF_ENABLED"] = (
        os.getenv("WTF_CSRF_ENABLED", "true").lower() == "true"
    )
    csrf.init_app(app)
    app.jinja_env.filters["cedis"] = format_ghana_cedis
    app.jinja_env.filters["role_label"] = format_role_label
    app.jinja_env.filters["duration"] = format_duration

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
    from app.distribution.webhooks import distribution_webhooks_bp
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
    app.register_blueprint(distribution_webhooks_bp)
    # Provider callbacks authenticate via HMAC signature / shared secret and carry
    # no browser session, so CSRF does not apply (and providers can't send a token).
    csrf.exempt(distribution_webhooks_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(statutory_bp)
    app.register_blueprint(raw_engine_bp)
    app.register_blueprint(oversight_bp)
    app.register_blueprint(notifications_bp)

    @app.context_processor
    def inject_app_identity():
        # Product-name strings for templates (title, brand, footer attribution).
        # Config-sourced so a rebrand or white-label never touches a template.
        return {
            "app_name": app.config["APP_NAME"],
            "app_brand_name": app.config["APP_BRAND_NAME"],
            "app_short_name": app.config["APP_SHORT_NAME"],
            "app_brand_mark": app.config["APP_BRAND_MARK"],
            "app_tagline": app.config["APP_TAGLINE"],
            "company_name": app.config["COMPANY_NAME"],
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
    @click.option("--once", is_flag=True, help="Process the queue once and exit (cron mode).")
    def distribution_worker_command(once):
        """Run the payslip distribution queue worker as a dedicated process.

        For a deployment with a dedicated worker service — run this instead of
        (or alongside) the in-process thread, and set DISTRIBUTION_WORKER_INLINE=false
        on the web service so only this process sends. Handles SIGTERM/SIGINT for a
        graceful shutdown (finishes the current poll, then exits), so a deploy
        restart never interrupts a send mid-flight. With --once it does a single
        drain and exits (for a cron/scheduled-job platform).
        """
        import signal
        import threading

        from app.distribution.queue import default_worker_name, drain_once, run_worker

        if once:
            processed = drain_once()
            print(f"Distribution worker drained once ({'work done' if processed else 'idle'}).")
            return

        stop_event = threading.Event()

        def _graceful(signum, _frame):
            print(f"Received signal {signum} — shutting down after the current poll…")
            stop_event.set()

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)

        name = default_worker_name()
        print(f"Distribution worker '{name}' started — polling for queued batches.")
        try:
            run_worker(
                poll_interval=app.config["DISTRIBUTION_WORKER_POLL_INTERVAL"],
                stop_event=stop_event,
                worker_name=name,
            )
        except KeyboardInterrupt:
            pass
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
    from app.distribution.queue import run_worker

    # A distinct heartbeat name so an inline (web) worker and a separate worker
    # process on the same host don't overwrite each other's heartbeat row.
    inline_name = f"{socket.gethostname()}-web-inline"

    def _target():
        with app.app_context():
            run_worker(
                poll_interval=app.config["DISTRIBUTION_WORKER_POLL_INTERVAL"],
                worker_name=inline_name,
            )

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
