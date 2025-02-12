FROM python:3.12-slim

# Evita la creación de archivos __pycache__ y establece la salida en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instala ffmpeg
RUN apt-get update && apt-get install -y ffmpeg

# Define el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copia el archivo de requerimientos y luego instálalos
COPY pyproject.toml .
RUN pip install --upgrade pip && pip install .

# Copia todo el código de la aplicación al contenedor
COPY . .

# Comando para ejecutar la aplicación
CMD ["python", "main.py"]
