FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/data/attsite.db \
    LOG_FILE=/app/data/attsite.log

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY attsite ./attsite
COPY bot.py .

VOLUME ["/app/data"]
CMD ["python", "bot.py"]
