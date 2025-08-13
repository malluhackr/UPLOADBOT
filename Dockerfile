FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for moviepy + ffmpeg
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6 git && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Force uninstall any twscrape before installing the exact pinned version
RUN pip uninstall -y twscrape || true

# Install dependencies from requirements.txt with force reinstall (no cache)
RUN pip install --no-cache-dir -r requirements.txt --upgrade --force-reinstall

# Copy the rest of the application code
COPY . .

# Print installed twscrape version for debugging
RUN python3 -c "import twscrape; print('Twscrape version installed in build:', twscrape.__version__)"

# At container startup, print the version again before running app
CMD python3 -c "import twscrape; print('Twscrape version at runtime:', twscrape.__version__)" && python3 main.py
