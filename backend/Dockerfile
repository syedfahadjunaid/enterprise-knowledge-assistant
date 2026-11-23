# syntax=docker/dockerfile:1
# One image, three roles (see docker-compose.yml):
#   migrate:  alembic upgrade head
#   api:      uvicorn (the default command)
#   worker:   python -m app.ingestion.worker
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app

# The embedding model is downloaded at build time, in its own layer, so it is cached across
# code changes and the running container never needs the internet.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    EMBEDDING_CACHE_DIR=/opt/models
COPY docker/download_models.py /tmp/download_models.py
RUN pip install "fastembed>=0.9,<1" \
 && python /tmp/download_models.py \
 && chmod -R a+rX /opt/models   # readable (not writable) by the non-root app user

# Dependencies first (cached while only the code changes), then the code.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app
COPY app/ app/
COPY migrations/ migrations/
COPY alembic.ini ./
RUN pip install --no-deps .

# Never reach out to Hugging Face at runtime: use only the baked-in model files.
ENV HF_HUB_OFFLINE=1
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
