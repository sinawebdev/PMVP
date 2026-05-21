import os
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"


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
        "viewer": "Viewer",
    }
    return labels.get(str(value or "").lower(), str(value or "").replace("_", " ").title())


def create_app():
    load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    is_production = os.getenv("RENDER") == "true" or os.getenv("FLASK_ENV") == "production"
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri(
        os.path.join(app.instance_path, "chrisnat_payroll.db")
    )
    database_type = database_type_label(app.config["SQLALCHEMY_DATABASE_URI"])
    app.config["DATABASE_TYPE_LABEL"] = database_type
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

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["EXPORT_FOLDER"], exist_ok=True)
    os.makedirs(app.config["IMPORT_SESSION_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    app.jinja_env.filters["cedis"] = format_ghana_cedis
    app.jinja_env.filters["role_label"] = format_role_label

    from app.audit import audit_bp
    from app.auth import auth_bp
    from app.finance import finance_bp
    from app.payroll import payroll_bp
    from app.proposals import proposals_bp
    from app.reports import reports_bp
    from app.routes import main_bp

    app.register_blueprint(audit_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(proposals_bp)
    app.register_blueprint(reports_bp)

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
        db.create_all()
        from app.schema import ensure_phase2_schema

        ensure_phase2_schema()
        from app.seed import seed_default_data

        seed_default_data()


@login_manager.user_loader
def load_user(user_id):
    from app.models import User

    return db.session.get(User, int(user_id))
