ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
COPY wheels /opt/wheels
RUN pip install --no-cache-dir --no-index --find-links=/opt/wheels reference-workflows && rm -r /opt/wheels
WORKDIR /work
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["dios-reference-workflow"]
