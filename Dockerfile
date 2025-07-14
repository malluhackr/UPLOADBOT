# Stage 1: Build Python dependencies
FROM python:3.9-slim as builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime + ffmpeg
FROM python:3.9-slim

RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH="/root/.local/bin:${PATH}"
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

WORKDIR /app
COPY . .

CMD ["python", "main.py"]
