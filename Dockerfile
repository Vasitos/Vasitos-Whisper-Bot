FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y ffmpeg --no-install-recommends

WORKDIR /app

COPY pyproject.toml .
RUN pip install --upgrade pip && pip install --no-cache-dir .

COPY . .

CMD ["python", "main.py"]
