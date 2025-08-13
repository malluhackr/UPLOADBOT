FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for moviepy + ffmpeg
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6 git && rm -rf /var/lib/apt/lists/*

# Copy requirements file to the working directory
COPY requirements.txt .

# Remove any pre-installed twscrape (if present) and reinstall everything fresh
RUN pip uninstall -y twscrape || true
RUN pip install --no-cache-dir -r requirements.txt --upgrade --force-reinstall

# Print installed twscrape version for debugging (safe method)
RUN pip show twscrape

# Copy the rest of the application code
COPY . .

# At container startup, show twscrape version again for runtime debugging
CMD python3 -c "import importlib.metadata; print('twscrape version at runtime:', importlib.metadata.version('twscrape'));" && python3 main.py
