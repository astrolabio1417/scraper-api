FROM python:3.12-slim

WORKDIR /app

# Prevent Python from writing .pyc files and buffer output for cleaner docker logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium --with-deps

# Copy the rest of your scraper source code
COPY . .

EXPOSE 5001

CMD ["python", "main.py"]