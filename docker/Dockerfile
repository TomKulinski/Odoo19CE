FROM odoo:19

USER root

# WeasyPrint-Systemabhängigkeiten (Pango, Cairo, GDK-Pixbuf fürs Rendern;
# Schriften für konsistente PDF-Typografie). Kein Browser nötig — die
# nativen Layouts werden über WeasyPrint gerendert.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    fonts-liberation \
    fonts-noto-core \
    fonts-noto-mono \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --break-system-packages weasyprint

USER odoo
