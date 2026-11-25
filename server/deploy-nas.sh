#!/bin/bash
# Motion Gamepad NAS 一键部署（裸 Python 路线，无 Docker 依赖）
# 用法:
#   AUTH_PASS=你的网页登录密码 bash deploy-nas.sh <NAS内网IP> [start|stop|restart|status]
# 示例:
#   AUTH_PASS=YOUR_PASSWORD bash deploy-nas.sh 192.168.1.100 start
# 登录密码通过环境变量 AUTH_PASS 显式传入，脚本内不保存任何密码。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$HERE")"          # 项目根目录
LOG_FILE="$APP_DIR/logs/server.log"
PID_FILE="$APP_DIR/logs/server.pid"
AUTH_PASS="${AUTH_PASS:-}"
HTTP_PORT=8443

log()  { echo "[deploy] $*"; }
fail() { echo "[deploy][错误] $*" >&2; exit 1; }

find_python() {
    for c in python3 python3.13 python3.12 python3.11 python3.10 python3.9 /usr/local/bin/python3; do
        if command -v "$c" >/dev/null 2>&1; then
            v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
            major=${v%%.*}; minor=${v#*.}
            [ "$major" -eq 3 ] && [ "$minor" -ge 9 ] && { echo "$c"; return; }
        fi
    done
    return 1
}

install_deps() {
    local PY="$1"
    # 优先 venv（隔离、不动系统包）；DSM python 可能缺 ensurepip，则退回 --user
    if "$PY" -m venv "$APP_DIR/.venv" 2>/dev/null && "$APP_DIR/.venv/bin/pip" -q install "websockets>=13" 2>/dev/null; then
        PY="$APP_DIR/.venv/bin/python"; log "依赖已装入 venv"
    elif "$PY" -m pip install --user -q "websockets>=13" 2>/dev/null; then
        log "依赖已装入用户目录（venv 不可用）"
    else
        fail "websockets 安装失败：请在套件中心装 Python 3.9+，或手动 pip3 install 'websockets>=13'"
    fi
    echo "$PY"
}

do_stop() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE" && log "服务已停止"
    else
        log "没有运行中的服务"
    fi
}

do_start() {
    local NAS_IP="${1:-}"
    mkdir -p "$APP_DIR/logs"
    do_stop 2>/dev/null || true

    if [ -z "$AUTH_PASS" ]; then
        fail "未设置登录密码：请用 AUTH_PASS=你的密码 bash deploy-nas.sh ... 方式启动"
    fi

    local PY; PY=$(find_python) || fail "需要 Python 3.9+：请在群晖套件中心安装（Package Center → Python 3.9）"
    log "使用 Python: $PY ($($PY -V 2>&1))"
    PY=$(install_deps "$PY") || exit 1
    # 本服务只跑明文 HTTP+WS；如需公网 HTTPS，请在反向代理（nginx/frp 等）侧终结 TLS

    nohup "$PY" -u "$HERE/server.py" --auth "$AUTH_PASS" >>"$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2

    local code; code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/" 2>/dev/null || echo 000)
    [ "$code" = "200" ] || fail "页面健康检查未通过（HTTP $code），日志见 $LOG_FILE"
    grep -q "auth] 网页登录已启用" "$LOG_FILE" || fail "未检测到登录鉴权启用日志，请检查 $LOG_FILE"

    log "部署完成 ✔  页面 http://${NAS_IP:-127.0.0.1}:$HTTP_PORT/"
    log "ESP32 固件 SERVER_IP 需改为 NAS IP（改 CIRCUITPY 盘上的 code.py 即可，无需重新烧录）"
}

do_status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        log "运行中 PID=$(cat "$PID_FILE")；$(curl -s -o /dev/null -w '页面 HTTP %{http_code}' "http://127.0.0.1:$HTTP_PORT/" 2>/dev/null)"
    else
        log "未运行"
    fi
}

case "${2:-start}" in
    start)   do_start "$1" ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 1; do_start "$1" ;;
    status)  do_status ;;
    *) echo "用法: bash deploy-nas.sh <NAS内网IP> [start|stop|restart|status]"; exit 1 ;;
esac
