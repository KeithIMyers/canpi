#!/usr/bin/env python3
"""
Entry point for the CanPi Flask application.

This script creates the Flask app, configures logging, and starts the
Gunicorn server. It is used by Docker to run the application.
"""

import os
import sys
from app import create_app

# Ensure the app can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

app = create_app()

if __name__ == '__main__':
    # For local development
    app.run(host='0.0.0.0', port=5000, debug=True)