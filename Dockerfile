FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for moviepy + ffmpeg
RUN apt-get update && apt-get install -y ffmpeg libsm6 libxext6 git && rm -rf /var/lib/apt/lists/*

# Copy requirements file to the working directory
COPY requirements.txt .

# Install Python dependencies from the requirements file
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set the command to run the application
CMD ["python3", "main.py"]
