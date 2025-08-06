# Stage 1: Build Python dependencies
FROM python:3.9-slim as builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final runtime image with ffmpeg and Playwright dependencies
FROM python:3.9-slim

# ✅ Install required OS dependencies
RUN apt-get update && \
    apt-get install -y ffmpeg curl \
    libglib2.0-0 libnss3 libgconf-2-4 libfontconfig1 libxss1 libasound2 libxtst6 libgtk-3-0 && \
    rm -rf /var/lib/apt/lists/*

# ✅ Install Playwright browsers
RUN pip install --no-cache-dir playwright==1.44.0 && \
    playwright install --with-deps

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
