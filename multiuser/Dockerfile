FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config
RUN groupadd -g 10001 job-searcher && useradd -u 10001 -g 10001 -M job-searcher && mkdir -p /app/data && chown 10001:10001 /app/data
USER 10001:10001

CMD ["qa-job-bot"]
