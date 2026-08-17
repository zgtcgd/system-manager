#!/bin/bash

# ==================== 优雅退出：Ctrl+C 时清理所有子进程 ====================
_CLEANUP_DONE=false
cleanup() {
    $_CLEANUP_DONE && exit 0
    _CLEANUP_DONE=true
    echo ""
    echo "正在停止所有服务..."
    # 杀掉当前 shell 的所有直接子进程
    local child_pids=$(pgrep -P $$ 2>/dev/null)
    [ -n "$child_pids" ] && kill $child_pids 2>/dev/null
    # 只杀 cloudflared（用 -x 精确匹配进程名，避免误杀自身）
    pkill -x "cloudflared" 2>/dev/null
    # 等待子进程结束
    sleep 1
    echo "所有服务已停止"
    exit 0
}
trap cleanup SIGINT SIGTERM SIGHUP

export FILE_PATH=${FILE_PATH:-'/tmp'}
export BASE_DIR=${BASE_DIR:-'/data'}  # 面板数据根目录(文件管理上传/创建的文件),挂载到持久化卷
export ENABLE_ARGO=${ENABLE_ARGO:-'false'}  # true or false true为开启argo。
export KEEPALIVE=${KEEPALIVE:-'false'}
export PORT=${PORT:-'8080'}
export PANEL_PASSWORD=${PANEL_PASSWORD:-'123456'}
export ARGO_DOMAIN=${ARGO_DOMAIN:-''}
export ARGO_AUTH=${ARGO_AUTH:-''}

# 面板节点部分，不设为下面默认值。
export WS_ENABLE=${WS_ENABLE:-'false'}  # true为开启面板ws代理，订阅为/sub
export UUID=${UUID:-'7160b696-dd5e-42e3-a024-145e92cec916'}
export SUB_PATH=${SUB_PATH:-'sub'}
export CF_IP=${CF_IP:-'ip.sb'}
export SUB_NAME=${SUB_NAME:-'test'}
export MY_DOMAIN=${MY_DOMAIN:-''} # ENABLE_ARGO为false时，有值则使用cdn cf域名
export LOCAL_DOMAIN=${LOCAL_DOMAIN:-''}
export CLIENT_TYPE=${CLIENT_TYPE:-'v2'}

if [ ! -d "$FILE_PATH" ]; then
  mkdir -p "$FILE_PATH"
fi

# 确保持久化数据目录存在
if [ ! -d "$BASE_DIR" ]; then
  mkdir -p "$BASE_DIR"
fi

# Download Dependency Files
download_program() {
  local program_name="$1"
  local default_url="$2"
  local x64_url="$3"

  local download_url
  case "$(uname -m)" in
    x86_64|amd64|x64)
      download_url="${x64_url}"
      ;;
    *)
      download_url="${default_url}"
      ;;
  esac

  if [ ! -s "${program_name}" ]; then
    if [ -n "${download_url}" ]; then
      # 下载失败重试 3 次(每次间隔 5s):容器里网络抖动/上游限流常见
      local attempt=1
      while [ "$attempt" -le 3 ]; do
        echo "Downloading ${program_name} (attempt $attempt/3)..."
        if command -v curl &> /dev/null; then
          curl -sSL --retry 2 --connect-timeout 15 "${download_url}" -o "${program_name}"
        elif command -v wget &> /dev/null; then
          wget -qO "${program_name}" "${download_url}"
        fi
        if [ -s "${program_name}" ]; then
          echo "Downloaded ${program_name}"
          break
        fi
        echo "Download failed, retrying..."
        attempt=$((attempt+1))
        [ "$attempt" -le 3 ] && sleep 5
      done
      if [ ! -s "${program_name}" ]; then
        echo "ERROR: Failed to download ${program_name} after 3 attempts." >&2
        return 1
      fi
    else
      echo "Skipping download for ${program_name}"
    fi
  else
    echo "${program_name} already exists, skipping download"
  fi
  chmod +x "$program_name"
}

initialize_downloads() {
  if [ "${ENABLE_ARGO}" = "true" ]; then
    download_program "${FILE_PATH}/cloudflared" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" || return 1
  fi

  download_program "${FILE_PATH}/system-panel" "https://github.com/kahunama/myfile/releases/download/main/system-panel-arm" "https://github.com/kahunama/myfile/releases/download/main/system-panel" || return 1
}

# run
run_panel() {
  # 切到数据目录运行:面板 BASE_DIR 默认取当前工作目录,.env 也从这里读
  cd "$BASE_DIR" 2>/dev/null || true
  ${FILE_PATH}/system-panel >/dev/null 2>&1 &
  _PANEL_PID=$!
  echo "system-panel started (pid $_PANEL_PID)"
}

run_cloudflared() {
  ${FILE_PATH}/cloudflared $args >/dev/null 2>&1 &
}

Detect_process() {
  local process_name="$1"
  local pids=""
  if command -v pidof &> /dev/null; then
    pids=$(pidof "$process_name" 2>/dev/null)
  elif command -v pgrep &> /dev/null; then
    pids=$(pgrep -x "$process_name" 2>/dev/null)
  elif command -v ps &> /dev/null; then
    pids=$(ps -eo pid,comm | awk -v name="$process_name" '$2 == name {print $1}')
  fi
  [ -n "$pids" ] && echo "$pids"
}

keep_alive() {
  while true; do
    if [ -e "${FILE_PATH}/cloudflared" ] && [ "${ENABLE_ARGO}" = "true" ] && [ -z "$(Detect_process "cloudflared")" ]; then
      run_cloudflared
      sleep 5
    fi
    if [ -e "${FILE_PATH}/system-panel" ] && [ -z "$(Detect_process "system-panel")" ]; then
      run_panel
    fi
    sleep 55
  done
}

fetch_and_parse() {
    local url="$1"
    local json_key="$2"

    IP_INFO=$(curl -s "$url")
    if echo "$IP_INFO" | grep -q "\"${json_key}\""; then
        if [[ "$json_key" == "ip" ]]; then
            SERVER_IP=$(echo "$IP_INFO" | sed -n 's/.*"ip": *"\([^"]*\).*/\1/p')
        else
            SERVER_IP=$(echo "$IP_INFO" | sed -n 's/.*"query": *"\([^"]*\).*/\1/p')
        fi
        if [[ -n "$SERVER_IP" ]]; then
            export SERVER_IP
            return 0
        fi
    fi
    return 1
}

# get IP
get_ip_code() {
    local API_STATUS=1

    if fetch_and_parse "https://api.ip.sb/geoip" "ip"; then
        API_STATUS=0
    elif fetch_and_parse "http://ip-api.com/json" "query"; then
        API_STATUS=0
    elif [[ -n "$MYIP_URL" ]] && fetch_and_parse "${MYIP_URL}" "ip"; then
        API_STATUS=0
    fi

    if [[ "$API_STATUS" -eq 0 ]]; then
        if [[ ! "$SERVER_IP" =~ : ]]; then
            export MYIP="$SERVER_IP"
        else
            export MYIP="[$SERVER_IP]"
        fi
        return 0
    else
        export MYIP="1.1.1.1"
        return 1
    fi
}

# ==================== argo辅助函数 ====================
# argoconfig
argo_type() {
  if [ -e "${FILE_PATH}/cloudflared" ] && [ -z "${ARGO_AUTH}" ] && [ -z "${ARGO_DOMAIN}" ]; then
    echo "ARGO_AUTH or ARGO_DOMAIN is empty, use Quick Tunnels" > /dev/null
    return
  fi

  if [ -e "${FILE_PATH}/cloudflared" ] && [ -n "$(echo "${ARGO_AUTH}" | grep TunnelSecret)" ]; then
    echo ${ARGO_AUTH} > ${FILE_PATH}/tunnel.json
    cat > ${FILE_PATH}/tunnel.yml << EOF
tunnel=$(echo "${ARGO_AUTH}" | cut -d\" -f12)
credentials-file: ${FILE_PATH}/tunnel.json
protocol: http2

ingress:
  - hostname: ${ARGO_DOMAIN}
    service: http://localhost:${PORT}
    originRequest:
      noTLSVerify: true
  - service: http_status:404
EOF
  else
    echo "ARGO_AUTH Mismatch TunnelSecret" > /dev/null
  fi
}

args() {
  if [ -e "${FILE_PATH}/cloudflared" ]; then
    if [ -n "$(echo "${ARGO_AUTH}" | grep '^[A-Z0-9a-z=]\{120,250\}$')" ]; then
      args="tunnel --edge-ip-version auto --no-autoupdate --protocol http2 run --token ${ARGO_AUTH}"
    elif [ -n "$(echo "${ARGO_AUTH}" | grep TunnelSecret)" ]; then
      args="tunnel --edge-ip-version auto --config ${FILE_PATH}/tunnel.yml run"
    else
      args="tunnel --edge-ip-version auto --no-autoupdate --protocol http2 --logfile ${FILE_PATH}/argo.log --loglevel info --url http://localhost:${PORT}"
    fi
  fi
}

# 从argo.log获取临时隧道域名
get_argo_domain() {
  if [ -s "${FILE_PATH}/argo.log" ]; then
    export ARGO_DOMAIN=$(cat ${FILE_PATH}/argo.log | grep -o "info.*https://.*trycloudflare.com" | sed "s@.*https://@@g" | tail -n 1)
  fi
}

run_processes() {
  if [ "${ENABLE_ARGO}" = "true" ] && [ -e "${FILE_PATH}/cloudflared" ]; then
    argo_type
    args
    run_cloudflared
    sleep 5
    get_argo_domain && sleep 2
  fi

  # 当 WS_ENABLE 与 ENABLE_ARGO 同时开启时，订阅 /sub 内容使用 ARGO_DOMAIN 获取
  if [ "${WS_ENABLE}" = "true" ] && [ "${ENABLE_ARGO}" = "true" ] && [ -n "${ARGO_DOMAIN}" ] && [ -z "${MY_DOMAIN}" ]; then
    export MY_DOMAIN="${ARGO_DOMAIN}"
  fi

  if [ -e "${FILE_PATH}/system-panel" ]; then
    run_panel
  else
    echo "ERROR: ${FILE_PATH}/system-panel not found after download." >&2
    exit 1
  fi

  if [ "${ENABLE_ARGO}" = "true" ]; then
      echo "Panel started successfully: http://${ARGO_DOMAIN}"
  else
    if [ -n "${MY_DOMAIN}" ]; then
      echo "Panel started successfully: http://${MY_DOMAIN}"
    else
      get_ip_code && sleep 3
      echo "Panel started successfully: http://${MYIP}:${PORT}"
    fi
  fi

  if [ -n "${KEEPALIVE}" ] && [ "${KEEPALIVE}" = "true" ]; then
    keep_alive 2>&1 &
  fi
}

# main
main() {
  # 下载依赖;失败则明确报错退出,避免静默走到 wait 导致容器秒退
  initialize_downloads || {
    echo "ERROR: Dependency download failed. Container will exit." >&2
    exit 1
  }
  run_processes
  # KEEPALIVE=true 时 keep_alive 已在 run_processes 后台拉起,这里 wait 等后台进程
  # KEEPALIVE 非 true 时: 等面板进程;若面板秒退则给出提示(不静默)
  local panel_pid="${_PANEL_PID:-}"
  if [ -z "$panel_pid" ] || ! kill -0 "$panel_pid" 2>/dev/null; then
    echo "WARNING: system-panel process is not running; waiting for background jobs..." >&2
  fi
  wait
}
main
