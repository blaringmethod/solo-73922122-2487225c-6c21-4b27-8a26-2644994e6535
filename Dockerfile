FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OCR_DATABASE=/data/archive.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ocr_archive ./ocr_archive

# The SQLite file lives on a volume so releases survive container rebuilds.
RUN mkdir -p /data && chown -R nobody:nogroup /data
USER nobody

EXPOSE 8000
VOLUME ["/data"]

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", \
     "--preload", "--timeout", "60", "ocr_archive.wsgi:app"]
