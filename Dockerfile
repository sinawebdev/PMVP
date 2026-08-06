FROM python:3.11-slim

# System deps. psycopg2-binary ships wheels, but libpq + build tools keep the
# image resilient if a source build is ever needed. libxml2/libxslt cover
# openpyxl/lxml-style parsing. The app renders PDFs with reportlab, so the
# WeasyPrint cairo/pango/gdk-pixbuf stack is intentionally NOT installed
# (those packages were also renamed on Debian trixie and broke the build).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libffi-dev \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# This image is a production artefact, so it says so. Without this the app could
# not identify itself as production, which meant the SECRET_KEY guard never fired
# and sessions were signed with a fallback committed to git. It also makes
# SESSION_COOKIE_SECURE default on and keeps the demo login hints off.
# Overridden to "development" by docker-compose for the local stack.
ENV FLASK_ENV=production

# AUTO_INIT_DB (default true) creates tables + runs ensure_phase2_schema on boot.
# gunicorn, not `flask run`: the Werkzeug development server is single-threaded,
# unhardened and explicitly not for production use.
CMD ["gunicorn", "run:app", "--bind", "0.0.0.0:5000", "--timeout", "300", "--threads", "4"]
