FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 mivta \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin mivta \
    && mkdir -p /app/data \
    && chown -R mivta:mivta /app/data \
    && chmod 0700 /app/data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY mivta ./mivta
COPY pyproject.toml LICENSE README.md ./
RUN pip install --no-cache-dir --no-deps .

USER mivta

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "mivta", "--check"]

CMD ["python", "-m", "mivta"]
