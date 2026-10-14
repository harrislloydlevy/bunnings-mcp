FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY bunnings_mcp ./bunnings_mcp

RUN pip install --no-cache-dir .

ENV BUNNINGS_GUEST_TOKEN_CACHE_SECONDS=3600

EXPOSE 8000

CMD ["bunnings-mcp"]
