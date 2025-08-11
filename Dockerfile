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

# Copy installed Python packages from builder
COPY --from=builder /root/.local /root/.local

# ✅ Force reinstall moviepy + deps to make sure they're present
RUN pip install --no-cache-dir --upgrade moviepy imageio decorator tqdm numpy

# Set environment variables
ENV PATH="/root/.local/bin:${PATH}"
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# ✅ Run your bot
CMD ["python3", "main.py"]
