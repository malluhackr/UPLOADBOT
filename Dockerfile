FROM python:3.9-slim as builder

RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.9-slim

COPY --from=builder /root/.local /root/.local
COPY --from=builder /usr/bin/ffmpeg /usr/bin/ffmpeg

ENV PATH=/root/.local/bin:$PATH
ENV IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg

WORKDIR /app
COPY . .

CMD ["python", "main.py"]
