FROM python:3.10-alpine

WORKDIR /app

RUN apk update && apk add --no-cache \
    openssl \
    bash \
    curl \
    && rm -rf /var/cache/apk/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=3000
EXPOSE $PORT

CMD ["python3", "app.py"]
