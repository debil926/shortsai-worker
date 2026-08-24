FROM python:3.11-slim

# ffmpeg for trimming, ca-certificates for yt-dlp/urllib HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend_lite/ backend_lite/
COPY handler.py .

CMD ["python", "-u", "handler.py"]
