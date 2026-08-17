FROM node:alpine

WORKDIR /app

RUN apk add --no-cache bash wget curl git unzip nano openssl procps python3 python3-pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PORT=8080
EXPOSE $PORT

CMD ["python3", "app.py"]
