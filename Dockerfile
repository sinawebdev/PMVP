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

# AUTO_INIT_DB (default true) creates tables + runs ensure_phase2_schema on boot.
CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]
