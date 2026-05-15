FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY AuthenSnap.json .
COPY Token.json .
COPY Glas.json .

EXPOSE 8765

CMD ["python", "server.py"]
