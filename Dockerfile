FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Unbuffered stdout: without this, print()ed errors sit in a block buffer and
# never reach Cloud Logging in time to debug (werkzeug's request lines go to
# stderr and show up; the app's own error prints didn't).
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY economy_v2.py .
COPY payout_provider.py .
COPY AuthenSnap.json .
COPY Token.json .
COPY Glas.json .

EXPOSE 8080

CMD ["python", "server.py"]
