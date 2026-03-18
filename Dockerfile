# Используем официальный легкий образ Python
FROM python:3.12-slim

# Устанавливаем рабочую папку
WORKDIR /app

# Системные настройки Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Собираем статику (картинки, стили)
RUN python manage.py collectstatic --noinput
