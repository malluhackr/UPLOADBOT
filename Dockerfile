# Stage 1: Build the application and its dependencies
# MODIFIED: Updated Python version from 3.9 to 3.11
FROM python:3.11-slim as builder

# Set the working directory
WORKDIR /app

# Install OS dependencies needed for building certain Python packages
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file to leverage Docker's layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt


# Stage 2: Final, lightweight runtime image
# MODIFIED: Updated Python version from 3.9 to 3.11
FROM python:3.11-slim

# Install only the necessary runtime OS dependencies (e.g., ffmpeg)
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

# MODIFIED: Updated paths from python3.9 to python3.11
# Copy the installed Python packages from the builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Set environment variable to point imageio to the system-installed ffmpeg
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

# Set the application's working directory
WORKDIR /app

# Copy your application code into the final image
COPY . .

# Command to run your application
CMD ["python3", "main.py"]
