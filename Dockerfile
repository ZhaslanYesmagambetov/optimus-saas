FROM python:3.10-slim

WORKDIR /app

# Устанавливаем системные пакеты
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libpq-dev \
    libasound2 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости и устанавливаем их (БЕЗ --no-deps)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY . .

CMD ["python", "ai_bot.py"]