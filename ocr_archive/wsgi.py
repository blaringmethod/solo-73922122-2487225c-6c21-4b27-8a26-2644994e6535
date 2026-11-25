"""WSGI entry point.

Run locally with Flask's dev server:
    OCR_DATABASE=./archive.sqlite3 flask --app ocr_archive.wsgi run

Or with a production server:
    gunicorn --bind 0.0.0.0:8000 ocr_archive.wsgi:app
"""

import os

from .app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
