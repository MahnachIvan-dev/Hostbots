FROM python:3.11-slim

WORKDIR /app

# Установим системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements и устанавливаем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY bot.py .

# Создаём папку для данных
RUN mkdir -p /app/data

# Запускаем бота
CMD ["python", "-u", "bot.py"]
