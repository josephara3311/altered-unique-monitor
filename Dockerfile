# Image Playwright avec navigateurs déjà installés (Chromium/Firefox/WebKit)
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

# Dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code
COPY monitor.py .

# Logs immédiats
ENV PYTHONUNBUFFERED=1

# Lance le script
CMD ["python", "monitor.py"]

