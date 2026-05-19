import os

from dotenv import load_dotenv
from flask import Flask
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"


def format_ghana_cedis(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"GH₵ {amount:,.2f}"


def create_app():
    load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(app.instance_path, 'chrisnat_payroll.db')}",
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
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

    from app.auth import auth_bp
    from app.finance import finance_bp
    from app.payroll import payroll_bp
    from app.proposals import proposals_bp
    from app.routes import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(payroll_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(proposals_bp)

    @app.context_processor
    def inject_sidebar_clients():
        from app.models import ClientCompany

        return {
            "sidebar_clients": ClientCompany.query.filter_by(status="Active")
            .order_by(ClientCompany.name)
            .all()
        }

    with app.app_context():
        db.create_all()
        from app.seed import seed_default_data

        seed_default_data()

    return app


@login_manager.user_loader
def load_user(user_id):
    from app.models import User

    return db.session.get(User, int(user_id))
