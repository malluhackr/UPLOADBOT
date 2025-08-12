# Stage 1: Build Python dependencies
# ✅ കൂടുതൽ സ്ഥിരതയുള്ള പുതിയ പൈത്തൺ വേർഷനിലേക്ക് (3.10) മാറി.
FROM python:3.10-slim as builder

WORKDIR /app
COPY requirements.txt .

# ✅ --user ഒഴിവാക്കി. ഇതാണ് ഏറ്റവും പ്രധാനപ്പെട്ട മാറ്റം!
# ഇത് പാക്കേജുകളെ സിസ്റ്റം മുഴുവൻ തിരിച്ചറിയുന്ന സ്ഥലത്തേക്ക് ഇൻസ്റ്റാൾ ചെയ്യും.
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Final runtime image with ffmpeg
FROM python:3.10-slim

# Install ffmpeg, a system dependency for video processing
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from the builder stage
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# Set environment variables
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

# Set working directory
WORKDIR /app

# Copy your bot's code into the image
COPY . .

# Run your bot when the container starts
CMD ["python3", "main.py"]
