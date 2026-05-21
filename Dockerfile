FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    CLAUDE_DATA_DIR=/data/claude \
    CODEX_DATA_DIR=/data/codex \
    CURSOR_DATA_DIR=/data/cursor \
    COPILOT_DATA_DIR=/data/copilot

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
