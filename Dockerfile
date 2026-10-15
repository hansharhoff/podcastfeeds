FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/podcastfeeds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user. Default UID/GID match the host owner of the bind-mounted
# /data and /config so writes work; set APP_UID/APP_GID in .env on another host.
ARG UID=1002
ARG GID=1002
# -o: the host IDs may collide with a group or user the base image already
# has (GID 20 is "dialout" in Debian, "staff" on macOS).
RUN groupadd -o -g "$GID" app && useradd -o -u "$UID" -g "$GID" -m app
USER app

ENV DATA_DIR=/data CONFIG_DIR=/config PORT=8080
VOLUME ["/data", "/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"]

CMD ["python", "-m", "app.main"]
