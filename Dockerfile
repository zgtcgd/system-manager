FROM alpine:latest

WORKDIR /app

ARG PORT=8080
ENV PORT=$PORT
EXPOSE $PORT

# 持久化目录:面板 BASE_DIR 数据根(文件管理上传/创建的文件)
# 运行时用 -v /宿主机目录:/data 挂载即可持久化
ENV BASE_DIR=/data

COPY start.sh /app/

RUN apk update && apk add --no-cache bash wget curl grep procps unzip git nodejs python uv &&\
    chmod -v 755 start.sh &&\
    mkdir -p /data && chmod 755 /data

VOLUME ["/data"]

# Health check(面板无 /healthcheck 端点,探根路径 / 返回 200 即健康)
HEALTHCHECK --interval=2m --timeout=30s CMD wget -qO- http://localhost:${PORT}/ >/dev/null 2>&1 || exit 1

ENTRYPOINT [ "./start.sh" ]
