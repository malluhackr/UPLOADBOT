FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for moviepy + ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsm6 \
    libxext6 \
    git \
&& rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies, including twscrape from git
RUN pip install --no-cache-dir -r requirements.txt

# Copy all your project files
COPY . .

# Run the bot
CMD ["python3", "main.py"]
