# Stage 1: Build Python dependencies
# ✅ മാറ്റം 1: കൂടുതൽ സ്ഥിരതയുള്ള പുതിയ പൈത്തൺ വേർഷനിലേക്ക് മാറി.
FROM python:3.10-slim as builder

WORKDIR /app
COPY requirements.txt .

# ✅ മാറ്റം 2: ഇവിടെയുണ്ടായിരുന്ന --user ഒഴിവാക്കി. ഇതാണ് ഏറ്റവും പ്രധാനപ്പെട്ട മാറ്റം!
# ഇത് പാക്കേജുകളെ എല്ലാവർക്കും കാണാൻ കഴിയുന്ന പ്രധാന ഹാളിലേക്ക് ഇൻസ്റ്റാൾ ചെയ്യും.
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: Final runtime image with ffmpeg
FROM python:3.10-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages

# ✅ മാറ്റം 3: ആവശ്യമില്ലാത്തതുകൊണ്ട് രണ്ടാമത്തെ pip install ഒഴിവാക്കി.

# Set environment variables
# ✅ ഈ ENV PATH ഇനി ആവശ്യമില്ല, കാരണം പാക്കേജുകൾ ശരിയായ സ്ഥലത്താണ്.
# ENV PATH="/root/.local/bin:${PATH}"
ENV IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg"

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Run your bot
CMD ["python3", "main.py"]
