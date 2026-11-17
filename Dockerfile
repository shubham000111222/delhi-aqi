FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY docs ./docs

RUN pip install --no-cache-dir .

RUN mkdir -p data reports logs

EXPOSE 8000

CMD ["python", "-m", "tradebot.main", "run"]
