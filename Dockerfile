# Production-образ для Railway (Railway собирает по Dockerfile автоматически, если он есть в корне).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg/ffprobe — для библиотеки видео-референсов (кадры, склейки, аудио)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY samples ./samples

# Данные (SQLite, видео, кадры) — на томе Railway, смонтированном в /data.
# Если том не подключён, приложение работает, но показывает предупреждение: данные потеряются при деплое.
ENV DATA_DIR=/data

# Railway задаёт PORT; app.serve читает его сам и включает доверие к заголовкам прокси.
CMD ["python", "-m", "app.serve"]
