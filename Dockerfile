FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install-deps firefox && python -m camoufox fetch

COPY . .

EXPOSE 5001

CMD ["python", "app.py"]