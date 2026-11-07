# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home app \
    && mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8000
CMD ["uvicorn", "quilr_assessment.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
