FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /root/.mystore_native && \
    g++ -O3 -shared -fPIC cpp_core/engine.cpp -o /root/.mystore_native/libengine.so 2>/dev/null || true

CMD ["python", "main.py"]
