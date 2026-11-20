FROM ubuntu:22.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 ffmpeg libimage-exiftool-perl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
ENV PORT=10000
EXPOSE 10000
CMD ["python3", "app.py"]
