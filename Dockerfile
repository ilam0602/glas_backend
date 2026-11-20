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
COPY csam_screening.py .
COPY photodna_hash.py .
COPY economy_v2.py .
COPY payout_provider.py .
COPY AuthenSnap.json .
COPY Token.json .
COPY Glas.json .

# PhotoDNA EdgeHash SDK (licensed, NOT in git). In the cloudbuild.yaml flow
# the fetch-photodna-sdk step fills photodna_sdk/ from the private GCS bucket
# before this build; from a bare checkout the dir holds only its committed
# README, which still builds -- the photodna screening provider then reports
# "SDK not configured" instead of crashing (feature ships dark either way).
COPY photodna_sdk /app/photodna_sdk
ENV PHOTODNA_SDK_DIR=/app/photodna_sdk

EXPOSE 8080

CMD ["python", "server.py"]
