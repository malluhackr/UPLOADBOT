# Stage 1
FROM python:3.10-slim as builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2
FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy both site-packages & bin scripts
COPY --from=builder /usr/local /usr/local

COPY . .
CMD ["python3", "main.py"]
