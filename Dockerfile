FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    CLAUDE_DATA_DIR=/data/claude \
    CODEX_DATA_DIR=/data/codex

EXPOSE 8000

# The bound on a request head and on connections lives here, in the process that holds both
# tokens and does the parsing, rather than only in the `ingress` relay (#43): `app/server.py`
# names the values and `uvicorn`'s own defaults arm none of them.
CMD ["python", "-m", "app.server", "--bind", "0.0.0.0", "--port", "8000"]
