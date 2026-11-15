FROM python:3.12-slim
WORKDIR /app
COPY main.py .
# Railway mounts volumes after image build; permissions must be fixed at runtime.
# gosu drops privileges before the webhook server starts.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /data \
 && chown -R nobody:nogroup /data /app
ENV PORT=8080 DATA_DIR=/data
EXPOSE 8080
CMD ["sh", "-c", "set -eu; chown -R nobody:nogroup /data; exec gosu nobody python /app/main.py"]
