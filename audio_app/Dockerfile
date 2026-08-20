# Render's default Python buildpack does NOT include ffmpeg, which pydub
# needs for audio decoding (webm/mp3/etc). Deploying via Dockerfile
# guarantees ffmpeg is present.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT at runtime -- bind to it
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
