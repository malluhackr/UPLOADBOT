# Stage 1: Build Python dependencies
FROM python:3.9-slim as builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final runtime image with ffmpeg
FROM python:3.9-slim

# ✅ Install ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages
COPY --from=builder /root/.local /root/.local

# Set environment variables
ENV PATH="/root/.local/bin:${PATH}"
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# ✅ Run your bot
CMD ["python3", "main.py"]
