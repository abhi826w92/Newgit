FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y gcc build-essential libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV NGROK_AUTHTOKEN="3IBGFjZrUBgDgqY1Hn3EIU20BXL_7DSo3P6LkVk1CrgNkg95Q"
EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
