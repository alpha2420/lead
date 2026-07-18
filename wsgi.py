"""WSGI entrypoint for LeadFlow.

Local dev:       python wsgi.py
Production:      point a WSGI server at wsgi:app, e.g. gunicorn wsgi:app
"""

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(debug=debug, threaded=True, port=int(os.getenv("PORT", 5001)))
