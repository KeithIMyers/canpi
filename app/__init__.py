import os
import secrets
from flask import Flask
from .models import init_db


def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
    app.config['DATABASE'] = os.environ.get('DB_PATH', '/data/canpi.db')

    init_db()

    from . import routes
    app.register_blueprint(routes.bp)

    # Start PAMAS auto-detect watcher (scans for RS485 devices on boot)
    from .utils import pamas_manager
    pamas_manager.start_watcher()

    return app