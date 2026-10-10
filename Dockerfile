# RetailDwell (Flask)
# Build:  docker build -t dwell-mapper .
# Run:    docker run --rm -p 5000:5000 --env-file .env dwell-mapper   -> http://localhost:5000
#
# Video pipeline deps (OpenCV + Ultralytics) are large, so they are opt-in:
#   docker build --build-arg WITH_VIDEO=1 -t dwell-mapper-video .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt requirements-video.txt ./
RUN pip install -r requirements.txt

ARG WITH_VIDEO=0
RUN if [ "$WITH_VIDEO" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
      && rm -rf /var/lib/apt/lists/* \
      && pip install -r requirements-video.txt; \
    fi

COPY . .

# Keys come from the environment (docker run --env-file .env), never from the image.
# Without Supabase keys the app falls back to a local SQLite store under data/local (development only).
RUN mkdir -p data/local
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 5000
# The demo analysis is warmed in the background after startup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/healthz')" || exit 1

# One worker with threads: the analysis cache lives in memory, so extra workers would repeat it.
CMD ["gunicorn", "--preload", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "--timeout", "120", "app:create_app()"]
