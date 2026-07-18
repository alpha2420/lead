"""Flask application factory for LeadFlow."""

import os

from dotenv import load_dotenv
from flask import Flask

import pipeline as pl  # noqa: F401  (importing triggers pipeline's own .env loading)

load_dotenv(override=True)

from .state import RunRegistry

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root


def create_app() -> Flask:
    app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

    # 50 MB cap on request bodies (CSV/JSON imports, CSV Mapper uploads) —
    # previously unbounded, so a large/malicious upload could buffer
    # arbitrarily large payloads into memory.
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

    app.extensions["run_registry"] = RunRegistry()

    from .blueprints.csv_mapper import bp as csv_mapper_bp
    from .blueprints.health import bp as health_bp
    from .blueprints.history import bp as history_bp
    from .blueprints.icp import bp as icp_bp
    from .blueprints.pages import bp as pages_bp
    from .blueprints.pipeline_runs import bp as pipeline_runs_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(health_bp)
    app.register_blueprint(icp_bp)
    app.register_blueprint(pipeline_runs_bp)
    app.register_blueprint(csv_mapper_bp)
    app.register_blueprint(history_bp)

    return app
