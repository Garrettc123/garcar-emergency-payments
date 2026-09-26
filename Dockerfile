FROM python:3.12-slim
WORKDIR /app
COPY main.py .
RUN mkdir -p /data && chown -R nobody:nogroup /data /app
USER nobody
ENV PORT=8080 DATA_DIR=/data
EXPOSE 8080
CMD ["python", "main.py"]
