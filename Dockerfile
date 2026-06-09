FROM python:3.10-alpine

WORKDIR /app

COPY . .

ARG PORT=3000
ENV PORT=$PORT
EXPOSE $PORT

RUN apk update && apk --no-cache add openssl bash curl && \
    chmod +x app.py && \
    pip install -r requirements.txt

CMD ["python3", "app.py"]
