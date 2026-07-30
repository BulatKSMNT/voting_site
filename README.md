# 🗳️ Voting Site

> Сайт для голосования с использованием фреймворка Django и Telegram-бота.

Этот проект представляет собой веб-приложение для проведения голосований, разработанное на базе **Django**. Проект включает в себя интеграцию с **Telegram-ботом** и полностью подготовлен к быстрому развертыванию с помощью Docker.

## 🛠 Технологии

- **Backend:** Python, Django
- **Telegram Bot:** (модуль `tg_bot`)
- **Frontend:** HTML/CSS (встроенные шаблоны Django)
- **Инфраструктура:** Docker, Docker Compose

## 📁 Структура проекта

- `core/` — главная конфигурация проекта (настройки, маршруты).
- `voting/` — основное Django-приложение, содержащее логику проведения голосований.
- `tg_bot/` — директория с логикой работы Telegram-бота.
- `templates/` — HTML-шаблоны для отображения страниц веб-сайта.
- `static/` — статические файлы приложения (CSS, изображения и т.д.).
- `Dockerfile` и `docker-compose.yml` — конфигурация для контейнеризации приложения.
- `requirements.txt` — список Python-зависимостей.
- `manage.py` — стандартная утилита управления Django.

## 🚀 Запуск проекта

У вас есть два варианта запуска проекта: с использованием Docker (рекомендуется) или классический локальный запуск. Перед запуском убедитесь, что вы задали необходимые токены (например, для бота) в переменных окружения, если этого требует конфигурация.

### Способ 1: Использование Docker (Рекомендуемый)

Для этого потребуется установленный [Docker](https://docs.docker.com/get-docker/) и Docker Compose.

1. Склонируйте репозиторий:
   ```bash
   git clone https://github.com/BulatKSMNT/voting_site.git
   cd voting_site
   ```

2. Соберите и запустите контейнеры:
   ```bash
   docker-compose up -d --build
   ```

### Способ 2: Локальный запуск (без Docker)

1. Склонируйте репозиторий и перейдите в папку:
   ```bash
   git clone https://github.com/BulatKSMNT/voting_site.git
   cd voting_site
   ```

2. Создайте и активируйте виртуальное окружение:
   ```bash
   python -m venv venv
   
   # Для Windows:
   venv\Scripts\activate
   # Для Linux/macOS:
   source venv/bin/activate
   ```

3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```

4. Примените миграции:
   ```bash
   python manage.py migrate
   ```

5. Запустите сервер разработки:
   ```bash
   python manage.py runserver
   ```
```
