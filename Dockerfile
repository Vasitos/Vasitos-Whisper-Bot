FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y ffmpeg

WORKDIR /app

COPY pyproject.toml .
RUN pip install --upgrade pip && pip install .

COPY . .

CMD ["python", "main.py"]
