# Stage 1: Build Python dependencies
FROM python:3.10-slim as builder

WORKDIR /app
COPY requirements.txt .

# Install packages system-wide (NO --user flag)
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Final runtime image with ffmpeg
FROM python:3.10-slim

# Install ffmpeg, a system dependency for video processing
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from the builder stage
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Set environment variable for ffmpeg
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

# Set working directory
WORKDIR /app

# Copy your bot's code into the image
COPY . .

# Run your bot when the container starts
CMD ["python3", "main.py"]
