# PMB image — one image, two commands (paper loop + dashboard), same as the
# systemd setup. Build context is the repo root.
FROM python:3.12-slim

# No .pyc files, unbuffered stdout so `docker logs` is live, no pip cache layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# --- Layer 1: dependencies -------------------------------------------------
# Only pyproject.toml can invalidate this, so editing pmbot/ no longer forces
# a full dependency resolve + download. pandas/pydantic-core ship manylinux
# wheels, so the slim image needs no compiler toolchain. README.md is required
# because pyproject's `readme = "README.md"` is read at build time.
#
# The stub package exists so setuptools has something to build: `pip install
# .[dashboard]` resolves the extras from the metadata, and the real source
# replaces it in layer 2. Copying pmbot/ here instead is what broke the cache.
COPY pyproject.toml README.md ./
RUN mkdir -p pmbot \
    && touch pmbot/__init__.py \
    && pip install --upgrade pip \
    && pip install ".[dashboard]" \
    && rm -rf pmbot

# --- Layer 2: the application ----------------------------------------------
# Rebuilt on every source edit, but --no-deps keeps it to a seconds-long
# reinstall of one pure-Python wheel. --force-reinstall because the version is
# unchanged from layer 1's stub, which pip would otherwise consider satisfied.
COPY pmbot ./pmbot
RUN pip install --no-deps --force-reinstall .

# Unprivileged runtime user. /data is created here and owned by appuser so that
# a FRESH named volume mounted at /data inherits this ownership — Docker seeds a
# new named volume from the image directory's contents AND permissions, so the
# non-root user can write pmbot.db without an entrypoint chown dance.
RUN useradd --system --create-home appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# DB lives on the shared volume, not in the image layer. Overridable in compose.
ENV PMBOT_DB_PATH=/data/pmbot.db

# Default = the paper loop. docker-compose overrides this for the dashboard.
CMD ["pmbot", "paper"]
