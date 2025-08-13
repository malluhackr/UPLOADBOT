FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for moviepy + ffmpeg
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6 git && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Force uninstall twscrape first to avoid cached/wrong versions
RUN pip uninstall -y twscrape || true

# Install dependencies exactly as pinned in requirements.txt (no cache)
RUN pip install --no-cache-dir -r requirements.txt --upgrade --force-reinstall

# Copy the rest of the application code
COPY . .

# Print twscrape version at startup for debugging
RUN python3 -c "import twscrape; print('Twscrape version installed:', twscrape.__version__)"

# Set the command to run the application
CMD ["python3", "main.py"]
