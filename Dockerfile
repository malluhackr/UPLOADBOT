# Stage 1: Build Python dependencies
FROM python:3.10-slim as builder

WORKDIR /app
COPY requirements.txt .

# Install packages system-wide so they can be easily found
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Final runtime image
FROM python:3.10-slim

# Set the working directory
WORKDIR /app

# Install system dependencies like ffmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Copy the installed Python packages from the first stage
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Copy the rest of your application code
COPY . .

# Run your bot
CMD ["python3", "main.py"]
