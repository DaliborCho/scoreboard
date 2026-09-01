FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Trust any locally supplied root CA before reaching the network. Machines
# running TLS-inspecting antivirus or a corporate proxy need this; elsewhere
# the directory is empty and the step is a no-op. See certs/README.md.
COPY certs /usr/local/share/ca-certificates/local/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY pyproject.toml ./
RUN mkdir -p src/scoreboard && touch src/scoreboard/__init__.py \
    && pip install --no-cache-dir -e ".[dev]"

COPY src ./src
COPY tests ./tests

EXPOSE 8000
CMD ["uvicorn", "scoreboard.main:app", "--host", "0.0.0.0", "--port", "8000"]
