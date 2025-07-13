# Base image with Python 3.9 and essential build tools
FROM python:3.9-slim as builder

# Install system dependencies
RUN apt-get update && \
    apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create and set working directory
WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# --- Runtime stage ---
FROM python:3.9-slim

# Copy only necessary files from builder
COPY --from=builder /root/.local /root/.local
COPY --from=builder /usr/bin/ffmpeg /usr/bin/ffmpeg

# Ensure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH
ENV IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg

WORKDIR /app
COPY . .

# Health check endpoint (required for Koyeb)
HEALTHCHECK --interval=30s --timeout=3s \
  CMD curl -f http://localhost:8080/ || exit 1

# Run the application (use either CMD format)
CMD ["python", "main.py"]
# Alternative if you need start.sh:
# RUN echo '#!/bin/bash\npython main.py' > start.sh && chmod +x start.sh
# CMD ["./start.sh"]
