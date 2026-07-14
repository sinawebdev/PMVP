import os
import time
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask
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
    if str(database_uri or "").startswith("postgresql://"):
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

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["EXPORT_FOLDER"], exist_ok=True)
    os.makedirs(app.config["IMPORT_SESSION_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    app.jinja_env.filters["cedis"] = format_ghana_cedis
    app.jinja_env.filters["role_label"] = format_role_label

    from app.audit import audit_bp
    from app.auth import auth_bp
    from app.distribution import distribution_bp, payslip_link_bp
    from app.employees import employees_bp
    from app.payroll import payroll_bp
    from app.payslip import payslip_bp
    from app.raw_engine.web import raw_engine_bp
    from app.routes import main_bp
    from app.statutory import statutory_bp

    app.register_blueprint(audit_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(payslip_bp)
    app.register_blueprint(distribution_bp)
    app.register_blueprint(payslip_link_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(statutory_bp)
    app.register_blueprint(raw_engine_bp)

    @app.context_processor
    def inject_sidebar_clients():
        from app.models import ClientCompany

        return {
            "sidebar_clients": ClientCompany.query.filter_by(status="Active")
            .order_by(ClientCompany.name)
            .all()
        }

    @app.cli.command("init-db")
    def init_db_command():
        """Create/upgrade tables and seed starter users/clients without wiping data."""
        initialize_database(app)
        print("Database initialized.")

    if os.getenv("AUTO_INIT_DB", "true").lower() == "true":
        initialize_database(app)

    return app


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
