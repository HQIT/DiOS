ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY failed_main.py /app/main.py
