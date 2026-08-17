FROM alpine:latest

WORKDIR /app

ARG PORT=8080
ENV PORT=$PORT
EXPOSE $PORT

ENV BASE_DIR=/data

COPY start.sh /app/

RUN apk update && apk add --no-cache \
      --repository https://dl-cdn.alpinelinux.org/alpine/latest-stable/community \
      bash wget curl grep procps unzip git nodejs python3 uv &&\
    chmod -v 755 start.sh &&\
    mkdir -p /data && chmod 755 /data

VOLUME ["/data"]

HEALTHCHECK --interval=2m --timeout=30s CMD wget -qO- http://localhost:${PORT}/ >/dev/null 2>&1 || exit 1

ENTRYPOINT [ "./start.sh" ]
