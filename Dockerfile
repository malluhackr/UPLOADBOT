# Use a single stage to avoid any multi-stage copy issues
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Install all OS dependencies in one go
# (build-essential for building packages, ffmpeg/curl for runtime)
RUN apt-get update && apt-get install -y build-essential ffmpeg curl && rm -rf /var/lib/apt/lists/*

# Copy the requirements file
COPY requirements.txt .

# Install Python dependencies. This will now be in the final image directly.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Command to run your application
CMD ["python3", "main.py"]
