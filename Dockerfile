# Stage 1: Build Python dependencies
FROM python:3.9-slim as builder

WORKDIR /app
COPY requirements.txt .

# Install build dependencies first (needed for some Python packages)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    pip install --user --no-cache-dir -r requirements.txt && \
    apt-get remove -y gcc python3-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Stage 2: Final runtime image
FROM python:3.9-slim

# Install runtime dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    libnss3 \
    libgconf-2-4 \
    libfontconfig1 \
    libxss1 \
    libasound2 \
    libxtst6 \
    libgtk-3-0 \
    # Additional dependencies that pysnap might need
    libssl-dev \
    libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /root/.local /root/.local

# Ensure scripts in .local are executable
RUN chmod -R 755 /root/.local

# Set environment variables
ENV PATH="/root/.local/bin:${PATH}"
ENV PYTHONPATH="/root/.local/lib/python3.9/site-packages"
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Verify Python can import pysnap (debugging step)
RUN python3 -c "from pysnap import PySnap; print('PySnap imported successfully')" || \
    echo "Warning: PySnap import failed - Snapchat features may not work"

# Run your bot
CMD ["python3", "main.py"]
