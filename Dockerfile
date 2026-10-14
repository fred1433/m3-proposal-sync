# syntax=docker/dockerfile:1
# Agent image: one process, no listening port, state in /data (persistent volume).
FROM python:3.12.7-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home m3sync && mkdir /data && chown m3sync /data

# Test stage: runs the whole harness on Linux inside the image.
#   docker build --target test -t m3sync-test . && docker run --rm m3sync-test
FROM base AS test
RUN apt-get update && apt-get install -y --no-install-recommends lsof && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest>=8"
COPY sim ./sim
COPY tests ./tests
RUN chown -R m3sync /app
USER m3sync
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS agent
USER m3sync
VOLUME ["/data"]
# No EXPOSE: the agent opens outbound connections only (M3 endpoint, receiver).
HEALTHCHECK --interval=5m --timeout=20s CMD ["m3sync", "health", "--config", "/config/agent.toml"]
ENTRYPOINT ["m3sync"]
CMD ["run", "--config", "/config/agent.toml"]
