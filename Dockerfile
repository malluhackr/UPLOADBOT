# Stage 1: Build stage for Python dependencies
FROM python:3.9-slim as builder

WORKDIR /app

# Install Python dependencies first
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final runtime image
FROM python:3.9-slim

# Install ffmpeg directly in the final stage
# This ensures all necessary dependencies for ffmpeg are also installed.
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy the installed Python packages from the builder stage
COPY --from=builder /root/.local /root/.local

# Add /root/.local/bin to PATH for Python executables
ENV PATH="/root/.local/bin:${PATH}"
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg" # Keep this, it's good practice for Python libs

WORKDIR /app
COPY . .

# Command to run your bot
CMD ["python", "main.py"]
