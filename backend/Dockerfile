# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# 国内服务器构建可传: --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install -i "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install -r requirements.txt; \
    fi

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
