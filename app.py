#!/usr/bin/env python3

import os
import sys
import json
import logging
import uuid
import time
import shutil
import asyncio
import subprocess
import pty
import select
import signal
import struct
import fcntl
import termios
import threading
from pathlib import Path
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, jsonify, send_file,
    redirect, make_response, send_from_directory
)
from flask_cors import CORS
import psutil
from werkzeug.utils import secure_filename
from flask_sock import Sock

# ==================== 配置 ====================
PORT = int(os.environ.get('SERVER_PORT') or os.environ.get('PORT') or 3000)
PANEL_PASSWORD = os.environ.get('PANEL_PASSWORD', '123456')
SESSION_TIMEOUT = 604800  # 7天，单位秒

# 工作目录
BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_TEMP_DIR = BASE_DIR / 'uploads'

# 会话存储（简单内存存储，生产环境建议使用Redis）
sessions = {}

# Flask应用
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', str(uuid.uuid4()))
# 禁用自动重定向末尾斜杠，便于调试
app.url_map.strict_slashes = False

# 简单跨域支持
CORS(app, supports_credentials=True)

# WebSocket 支持
sock = Sock(app)

# ==================== 工具函数 ====================

def ensure_dir(path: Path):
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)

def get_client_ip():
    """获取客户端真实IP（代理友好）"""
    if request.headers.get('X-Forwarded-For'):
        return request.headers['X-Forwarded-For'].split(',')[0].strip()
    return request.remote_addr or 'unknown'

def is_logged_in():
    """检查是否已登录"""
    token = request.cookies.get('panel_auth')
    if token and token in sessions:
        # 更新会话最后访问时间
        sessions[token]['last_access'] = datetime.now().timestamp()
        return True
    return False

def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.path in ['/login', '/api/login']:
            return f(*args, **kwargs)

        # API请求返回JSON，页面请求重定向
        if is_logged_in():
            return f(*args, **kwargs)

        if request.path.startswith('/api/'):
            return jsonify({'success': False, 'message': '未登录'}), 401
        return redirect('/login')
    return decorated_function

def async_run(cmd, cwd=None):
    """同步执行shell命令（包装为同步调用）"""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return '', '命令执行超时（30秒）', -1
    except Exception as e:
        return '', str(e), -1

def path_exists(p: Path) -> bool:
    """检查路径是否存在"""
    return p.exists()

def command_exists(cmd: str) -> bool:
    """检查系统命令是否存在"""
    return shutil.which(cmd) is not None

def parse_cookies():
    """解析Cookie"""
    cookies = {}
    cookie_header = request.headers.get('Cookie', '')
    for item in cookie_header.split(';'):
        item = item.strip()
        if not item:
            continue
        if '=' in item:
            name, value = item.split('=', 1)
            cookies[name.strip()] = value.strip()
    return cookies

# ==================== 初始化 ====================

@app.before_request
def before_request():
    """请求前处理，清理过期的会话"""
    # 清理超过SESSION_TIMEOUT未活动的会话
    now = datetime.now().timestamp()
    expired = []
    for token, data in sessions.items():
        if now - data.get('last_access', 0) > SESSION_TIMEOUT:
            expired.append(token)
    for token in expired:
        sessions.pop(token, None)

# 创建上传目录并清理旧文件
if UPLOAD_TEMP_DIR.exists():
    for f in UPLOAD_TEMP_DIR.iterdir():
        if f.is_file():
            try:
                f.unlink()
            except Exception:
                pass
else:
    ensure_dir(UPLOAD_TEMP_DIR)

# ==================== 认证路由 ====================

@app.route('/login')
def login_page():
    """登录页面"""
    if is_logged_in():
        return redirect('/')
    return LOGIN_PAGE

@app.route('/api/login', methods=['POST'])
def api_login():
    """登录接口"""
    data = request.get_json() or {}
    password = data.get('password', '')
    if password != PANEL_PASSWORD:
        return jsonify({'success': False}), 401

    token = str(uuid.uuid4())
    sessions[token] = {
        'created_at': datetime.now().timestamp(),
        'last_access': datetime.now().timestamp(),
        'ip': get_client_ip()
    }
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('panel_auth', token, max_age=SESSION_TIMEOUT, httponly=True, samesite='Lax')
    return resp

@app.route('/api/logout', methods=['POST'])
def api_logout():
    """登出接口"""
    token = request.cookies.get('panel_auth')
    if token and token in sessions:
        sessions.pop(token, None)
    resp = make_response(jsonify({'success': True}))
    resp.set_cookie('panel_auth', '', max_age=0, httponly=True)
    return resp

# ==================== 文件管理 API ====================

@app.route('/api/local/files/list')
@login_required
def list_files():
    """获取目录文件列表"""
    requested_path = request.args.get('directory', str(BASE_DIR))
    try:
        root_dir = Path(requested_path).resolve()
        files = []
        try:
            items = list(root_dir.iterdir())
        except PermissionError:
            return jsonify({'success': False, 'message': '权限不足，无法读取目录内容'}), 403

        for item in items:
            try:
                stat = item.stat()
                files.append({
                    'name': item.name,
                    'size': stat.st_size,
                    'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'is_file': item.is_file()
                })
            except (PermissionError, OSError):
                continue

        # 按名称排序
        files.sort(key=lambda x: (x['is_file'], x['name'].lower()))

        return jsonify({
            'success': True,
            'files': files,
            'currentDir': str(root_dir)
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/local/files/create-folder', methods=['POST'])
@login_required
def create_folder():
    """创建文件夹"""
    data = request.get_json() or {}
    name = data.get('name', '')
    directory = data.get('directory', str(BASE_DIR))

    if not name:
        return jsonify({'success': False, 'message': '文件夹名称不能为空'}), 400

    try:
        target_dir = Path(directory).resolve() / name
        ensure_dir(target_dir)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/local/files/delete', methods=['POST'])
@login_required
def delete_files():
    """批量删除文件/文件夹"""
    data = request.get_json() or {}
    root = data.get('root', str(BASE_DIR))
    files = data.get('files', [])

    if not files:
        return jsonify({'success': False, 'message': '请选择要删除的项目'}), 400

    try:
        root_dir = Path(root).resolve()
        for name in files:
            target = root_dir / name
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/local/files/rename', methods=['POST'])
@login_required
def rename_files():
    """重命名文件/文件夹"""
    data = request.get_json() or {}
    root = data.get('root', str(BASE_DIR))
    files = data.get('files', [])

    if not files:
        return jsonify({'success': False, 'message': '请提供重命名信息'}), 400

    try:
        root_dir = Path(root).resolve()
        for item in files:
            old_path = root_dir / item['from']
            new_path = root_dir / item['to']
            old_path.rename(new_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/local/files/download')
@login_required
def download_file():
    """下载文件"""
    file_path = request.args.get('file', '')
    if not file_path:
        return '文件路径不能为空', 400

    try:
        target = Path(file_path).resolve()
        # 安全检查
        if not target.is_file():
            return '文件不存在', 404

        return send_file(target, as_attachment=True)
    except Exception as e:
        return str(e), 500

@app.route('/api/local/files/content')
@login_required
def get_file_content():
    """获取文件内容（用于编辑）"""
    file_path = request.args.get('file', '')
    if not file_path:
        return jsonify({'success': False, 'message': '文件路径不能为空'}), 400

    try:
        target = Path(file_path).resolve()
        if not target.is_file():
            return jsonify({'success': False, 'message': '文件不存在'}), 404

        # 限制只能编辑文本文件（小于10MB）
        if target.stat().st_size > 10 * 1024 * 1024:
            return jsonify({'success': False, 'message': '文件过大，无法编辑'}), 400

        content = target.read_text(encoding='utf-8')
        return jsonify({'success': True, 'content': content})
    except UnicodeDecodeError:
        return jsonify({'success': False, 'message': '文件不是文本文件'}), 400
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/local/files/save', methods=['POST'])
@login_required
def save_file_content():
    """保存文件内容"""
    data = request.get_json() or {}
    file_path = data.get('filePath', '')
    content = data.get('content', '')

    if not file_path:
        return jsonify({'success': False, 'message': '文件路径不能为空'}), 400

    try:
        target = Path(file_path).resolve()
        # 如果文件不存在则创建（但要确保目录存在）
        ensure_dir(target.parent)
        target.write_text(content, encoding='utf-8')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# ==================== 文件上传和运行 ====================

from werkzeug.utils import secure_filename

@app.route('/api/extensions/upload', methods=['POST'])
@login_required
def upload_file_api():
    """上传文件"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '没有文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '文件名为空'}), 400

    directory = request.form.get('directory', '')

    # 安全处理文件名
    filename = secure_filename(file.filename)
    if not filename:
        filename = str(uuid.uuid4())[:8]

    try:
        # 确定目标路径
        if directory:
            target_dir = Path(directory).resolve()
        else:
            target_dir = UPLOAD_TEMP_DIR

        ensure_dir(target_dir)
        target_path = target_dir / filename

        # 保存文件
        file.save(str(target_path))

        return jsonify({
            'success': True,
            'filename': str(target_path)
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/extensions/run', methods=['POST'])
@login_required
def run_extension():
    """运行可执行文件"""
    data = request.get_json() or {}
    filename = data.get('filename', '')

    if not filename:
        return jsonify({'success': False, 'message': '请提供文件路径'}), 400

    try:
        filepath = Path(filename).resolve()
        if not filepath.is_file():
            return jsonify({'success': False, 'message': '文件不存在'}), 404

        # 添加执行权限
        filepath.chmod(filepath.stat().st_mode | 0o111)

        # 执行文件
        stdout, stderr, code = async_run(str(filepath))

        if code != 0:
            return jsonify({'success': False, 'output': stderr or f'退出码: {code}'})

        return jsonify({'success': True, 'output': stdout})
    except Exception as e:
        return jsonify({'success': False, 'output': str(e)}), 500

# ==================== WebSocket 终端处理 ====================

@sock.route('/api/terminal/ws')
def terminal_ws(ws):
    """WebSocket终端"""
    token = request.cookies.get('panel_auth')
    if not token or token not in sessions:
        try:
            ws.close()
        except:
            pass
        return

    cwd = request.args.get('cwd')
    if not cwd or cwd == '/': cwd = str(Path.home())

    try:
        work_dir = Path(cwd).resolve()
    except Exception:
        work_dir = Path.home()

    if not work_dir.exists():
        work_dir = Path.home()

    shell = os.environ.get('SHELL', '/bin/bash')
    child_pid, fd = pty.fork()

    if child_pid == 0:
        try:
            os.chdir(str(work_dir))
            os.environ['TERM'] = 'xterm-256color'
            os.environ['COLORTERM'] = 'truecolor'
            os.execvp(shell, [shell])
        except Exception:
            os._exit(1)

    session_closed = threading.Event()

    def set_winsize(rows, cols):
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except Exception as e:
            print(f"resize error: {e}", file=sys.stderr)

    set_winsize(24, 80)

    def read_pty():
        while not session_closed.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
                if not r:
                    continue
                data = os.read(fd, 4096)
                if not data:
                    break

                try:
                    ws.send(data.decode(errors='ignore'))
                except Exception as e:
                    print(
                        f"Error sending data to websocket: {e}",
                        file=sys.stderr
                    )
                    break
            except OSError as e:
                print(f"PTY read error: {e}", file=sys.stderr)
                break

            except Exception as e:
                print(f"Unexpected PTY error: {e}", file=sys.stderr)
                break
        session_closed.set()

    reader = threading.Thread(
        target=read_pty,
        daemon=True
    )
    reader.start()

    try:
        while not session_closed.is_set():
            try:
                message = ws.receive()
                if message is None:
                    break
                if (
                    isinstance(message, str)
                    and message.startswith('{')
                ):
                    try:
                        data = json.loads(message)
                        if data.get('type') == 'resize':
                            set_winsize(
                                int(data.get('rows', 24)),
                                int(data.get('cols', 80))
                            )
                            continue

                    except Exception:
                        pass
                if isinstance(message, str):
                    message = message.encode()
                os.write(fd, message)
            except Exception as e:
                print(
                    # f"WebSocket receive error: {e}",
                    file=sys.stderr
                )
                break
    finally:
        session_closed.set()
        try:
            reader.join(timeout=1)
        except:
            pass

        try:
            os.close(fd)
        except:
            pass

        try:
            os.kill(child_pid, signal.SIGTERM)
            time.sleep(0.2)
        except:
            pass

        try:
            os.kill(child_pid, signal.SIGKILL)
        except:
            pass

        try:
            os.waitpid(child_pid, 0)
        except:
            pass


@app.route('/api/processes')
@login_required
def list_processes():
    """获取服务器进程列表"""
    try:
        # 使用 ps 命令
        if command_exists('ps'):
            result = subprocess.run(['ps', '-eo', 'pid,comm'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                processes = []
                lines = result.stdout.strip().split('\n')
                for line in lines[1:]:
                    if not line.strip():
                        continue
                    parts = line.strip().split(None, 1)
                    if len(parts) >= 2:
                        processes.append({
                            'pid': parts[0],
                            'name': parts[1]
                        })
                    elif len(parts) == 1:
                        processes.append({
                            'pid': parts[0],
                            'name': ''
                        })
                return jsonify({
                    'success': True,
                    'processes': processes
                })

        # 没有 ps 命令，返回空白（与 Node.js 的 supported: false 对应）
        return jsonify({
            'success': True,
            'processes': []
        })
    except Exception:
        return jsonify({
            'success': True,
            'processes': []
        })

@app.route('/api/processes/find')
@login_required
def find_process():
    """按名称查找进程，优先级: ps > pidof > pgrep -x"""
    name = request.args.get('name', '').strip()

    if not name:
        return jsonify({'found': False})

    # 第一优选：使用 ps 命令
    if command_exists('ps'):
        try:
            # 使用 ps 命令查找进程
            result = subprocess.run(['ps', '-eo', 'pid,comm'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines[1:]:  # 跳过标题行
                    if not line.strip():
                        continue
                    parts = line.strip().split(None, 1)
                    if len(parts) >= 2:
                        proc_name = parts[1]
                        # 匹配进程名（支持完整路径匹配）
                        if proc_name == name or proc_name.endswith('/' + name):
                            return jsonify({
                                'found': True,
                                'pid': parts[0],
                                'name': name
                            })
        except (subprocess.TimeoutExpired, Exception):
            pass

    # 第二优选：使用 pidof 命令
    if command_exists('pidof'):
        try:
            result = subprocess.run(['pidof', name],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().split()[0]
                return jsonify({
                    'found': True,
                    'pid': pid,
                    'name': name
                })
        except (subprocess.TimeoutExpired, Exception):
            pass

    # 第三优选：使用 pgrep -x 命令
    if command_exists('pgrep'):
        try:
            result = subprocess.run(['pgrep', '-x', name],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                pid = result.stdout.strip().split()[0]
                return jsonify({
                    'found': True,
                    'pid': pid,
                    'name': name
                })
        except (subprocess.TimeoutExpired, Exception):
            pass

    return jsonify({'found': False})


@app.route('/api/processes/kill', methods=['POST'])
@login_required
def kill_process():
    """杀死进程（使用 kill -9）"""
    data = request.get_json() or {}
    pid = str(data.get('pid', '')).strip()

    # 验证 PID 格式，防止命令注入（与 Node.js 版本一致）
    if not pid or not pid.isdigit():
        return jsonify({
            'success': False,
            'message': '非法PID'
        }), 400

    try:
        # 使用 kill -9 命令强制杀死进程
        result = subprocess.run(['kill', '-9', pid],
                                capture_output=True, text=True, timeout=5)

        if result.returncode != 0:
            return jsonify({
                'success': False,
                'message': result.stderr.strip() or 'Failed to kill process'
            }), 500

        return jsonify({'success': True})
    except subprocess.TimeoutExpired:
        return jsonify({
            'success': False,
            'message': 'Kill command timeout'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

# ==================== 系统监控 API ====================

@app.route('/api/system/stats')
@login_required
def system_stats():
    """获取系统状态"""
    try:
        # 内存信息
        mem = psutil.virtual_memory()
        # 磁盘信息（根目录）
        disk = psutil.disk_usage('/')
        # 负载
        load_avg = psutil.getloadavg() if hasattr(psutil, 'getloadavg') else (0, 0, 0)
        # 启动时间
        boot_time = psutil.boot_time()
        uptime = datetime.now().timestamp() - boot_time

        return jsonify({
            'success': True,
            'uptime': uptime,
            'load': list(load_avg),
            'memory': {
                'total': f"{mem.total / (1024**3):.2f} GB",
                'used': f"{(mem.total - mem.available) / (1024**3):.2f} GB",
                'percent': mem.percent
            },
            'disk': {
                'total': f"{disk.total / (1024**3):.2f} GB",
                'used': f"{disk.used / (1024**3):.2f} GB",
                'percent': disk.percent
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# ==================== Cron 管理（计划任务） ====================

CRONTAB_PATH = Path.home() / '.crontab_backup'  # 存储自定义任务的备份文件

def _get_crontab():
    """获取当前用户的crontab内容"""
    try:
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
        return ''
    except Exception:
        return ''

def _set_crontab(content):
    """设置crontab内容"""
    if not content.strip():
        # 删除所有crontab
        subprocess.run(['crontab', '-r'], capture_output=True)
        return True, ''

    try:
        proc = subprocess.run(['crontab', '-'], input=content, text=True, capture_output=True)
        if proc.returncode == 0:
            return True, ''
        return False, proc.stderr
    except Exception as e:
        return False, str(e)

@app.route('/api/cron/list')
@login_required
def cron_list():
    """获取计划任务列表"""
    content = _get_crontab()
    tasks = [line.strip() for line in content.split('\n') if line.strip() and not line.startswith('#')]
    return jsonify({'success': True, 'tasks': tasks})

@app.route('/api/cron/add', methods=['POST'])
@login_required
def cron_add():
    """添加计划任务"""
    data = request.get_json() or {}
    task = data.get('task', '').strip()
    if not task:
        return jsonify({'success': False, 'message': '任务内容不能为空'}), 400

    current = _get_crontab()
    lines = [l for l in current.split('\n') if l.strip() and not l.startswith('#')]
    if task in lines:
        return jsonify({'success': False, 'message': '任务已存在'}), 400

    new_content = current.rstrip('\n') + '\n' + task + '\n' if current else task + '\n'
    success, err = _set_crontab(new_content)
    if success:
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': err}), 500

@app.route('/api/cron/delete', methods=['POST'])
@login_required
def cron_delete():
    """删除计划任务"""
    data = request.get_json() or {}
    task = data.get('task', '').strip()
    if not task:
        return jsonify({'success': False, 'message': '任务内容不能为空'}), 400

    current = _get_crontab()
    lines = [l for l in current.split('\n') if l.strip() and not l.startswith('#')]

    if task not in lines:
        return jsonify({'success': True})  # 不存在也算成功

    new_lines = [l for l in lines if l != task]
    new_content = '\n'.join(new_lines) + '\n' if new_lines else ''
    success, err = _set_crontab(new_content)
    if success:
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': err}), 500

# ==================== 前端页面 ====================

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>login - System Management Panel</title>
<style>
body { font-family: Arial, sans-serif; background: #111827; color: white; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
.login-card { background: #1f2937; padding: 30px; border-radius: 8px; border: 1px solid #374151; width: 320px; text-align: center; }
input { width: 100%; padding: 10px; margin: 15px 0; background: #374151; border: 1px solid #4b5563; color: white; border-radius: 4px; }
button { width: 100%; padding: 10px; background: #3b82f6; border: none; color: white; border-radius: 4px; cursor: pointer; font-weight: bold; }
button:hover { background: #2563eb; }
</style>
</head>
<body>
<div class="login-card">
<h2>System Management Panel</h2>
<input type="password" id="password" placeholder="Please enter your password" onkeydown="if(event.key==='Enter') login()">
<button onclick="login()">log in</button>
</div>
<script>
async function login() {
    const password = document.getElementById('password').value.trim();
    try {
        const res = await fetch('/api/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password })
        });
        const data = await res.json();
        if (data.success) {
            location.href = '/';
        } else {
            alert('Incorrect password');
        }
    } catch (e) {
        alert('Login failed; please check the server connection.');
    }
}
// 检查是否已登录
window.onload = async () => {
    const res = await fetch('/api/system/stats', { method: 'GET' }).catch(() => ({ status: 401 }));
    if (res.status !== 401) location.href = '/';
};
</script>
</body>
</html>
"""

INDEX_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>System Management Panel</title>
<!-- 添加 xterm.js 样式和脚本 -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm/css/xterm.css" />
<script src="https://cdn.jsdelivr.net/npm/xterm/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit/lib/xterm-addon-fit.js"></script>
<style>
*{
    box-sizing: border-box;
}
body{
    font-family:Arial,sans-serif;
    background:#111827;
    color:white;
    margin:0;
    padding:0;
}
h1{
    text-align:center;
    padding:15px 0;
    margin:0;
    background:#1f2937;
    font-size:24px;
}
.tabs{
    display:flex;
    background:#1f2937;
    font-size:14px;
}
.tabs button{
    flex:1;
    padding:8px 10px;
    background:#1f2937;
    border:none;
    color:white;
    cursor:pointer;
}
.tabs button.active{
    background:#374151;
}
.container{
    padding:15px;
}
input,button,select{
    padding:6px 8px;
    margin:5px 0;
    font-size:14px;
}
input, select {
    background: #374151;
    border: 1px solid #4b5563;
    color: white;
}
#header{
position:relative;
background:#1f2937;
}

#header h1{
margin:0;
padding:15px 0;
text-align:center;
}

#header button{
position:absolute;
right:20px;
top:50%;
transform:translateY(-50%);
padding:6px 12px;
cursor:pointer;
}
#botTable,
#remoteTable{
width:100%;
border-collapse:collapse;
margin-top:10px;
font-size:14px;
}

#botTable th,
#botTable td,
#remoteTable th,
#remoteTable td{
border:1px solid #374151;
padding:6px;
text-align:center;
}
.table-container {
    width: 100%;
    overflow-x: auto;
}
#output{
background:#111827;
border:1px solid #374151;
padding:10px;
white-space:pre-wrap;
margin-top:10px;
max-height:300px;
overflow-y:auto;
}
.terminal-box {
    background: #000;
    color: #00ff00;
    font-family: 'Courier New', Courier, monospace;
    padding: 10px;
    height: 300px;
    overflow-y: auto;
    border: 1px solid #374151;
}
.file-manager {
    background: #1f2937;
    border: 1px solid #374151;
    padding: 15px;
    margin-top: 15px;
    border-radius: 8px;
}
/* 刷新按钮专属样式 - 适配黑白灰风格 */
.refresh-btn {
    background: #374151;
    color: #9ca3af; /* 灰白色 */
    border: 1px solid #4b5563;
    border-radius: 4px;
    padding: 4px 8px;
    cursor: pointer;
    font-size: 14px;
    transition: all 0.2s ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
    vertical-align: middle;
}
.refresh-btn:hover {
    background: #4b5563;
    color: #ffffff; /* 悬停变白 */
    border-color: #6b7280;
}

/* 移动端适配 */
@media (max-width: 600px) {
    #header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 15px;
    }
    #header h1 {
    text-align: left;
    font-size: 18px;
    }
    #header button {
    position: static;
    transform: none;
    }
    h1 { font-size: 20px; }
    .container { padding: 10px; }
    .tabs button { padding: 12px 5px; font-size: 13px; }

    input, select, .container button:not(.refresh-btn) {
        width: 100%;
        display: block;
        padding: 10px;
        margin-bottom: 10px;
    }
    .refresh-btn {
        width: auto;
        padding: 8px 12px;
        font-size: 18px;
    }
}
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
.stat-card { background: #1f2937; padding: 15px; border-radius: 8px; border: 1px solid #374151; text-align: center; }
.stat-card label { display: block; color: #9ca3af; font-size: 12px; margin-bottom: 5px; }
.stat-card span { font-size: 20px; font-weight: bold; color: #3b82f6; }
.progress-bar { background: #374151; height: 8px; border-radius: 4px; margin-top: 10px; overflow: hidden; }
.progress-fill { background: #3b82f6; height: 100%; width: 0%; transition: width 0.5s ease; }
/* 编辑器模态框样式 */
.editor-modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.85); z-index:1000; padding:20px; }
.editor-container { background:#1f2937; height:100%; border-radius:8px; display:flex; flex-direction:column; padding:15px; border:1px solid #4b5563; }
#editorArea { flex:1; background:#111827; color:#e5e7eb; font-family:monospace; padding:15px; border:1px solid #374151; resize:none; outline:none; font-size:14px; line-height:1.5; }
/* 文件卡片模式样式 */
.file-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 15px; margin-top: 10px; }
.file-card { background: #1f2937; border: 1px solid #374151; border-radius: 8px; padding: 12px; display: flex; flex-direction: column; align-items: center; text-align: center; position: relative; transition: all 0.2s; }
.file-card:hover { border-color: #4b5563; background: #374151; transform: translateY(-2px); }
.file-card .icon { font-size: 40px; margin-bottom: 8px; cursor: pointer; user-select: none; }
.file-card .name { font-size: 14px; font-weight: bold; word-break: break-all; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; min-height: 2.8em; }
.file-card .info { font-size: 11px; color: #9ca3af; margin-bottom: 10px; }
.file-card .actions { display: flex; flex-wrap: wrap; gap: 4px; justify-content: center; margin-top: auto; width: 100%; }
.file-card .actions button { padding: 4px 8px; font-size: 11px; flex: 1; min-width: 60px; }
.file-card .checkbox-wrapper { position: absolute; top: 8px; left: 8px; z-index: 2; }
/* 大图标模式 */
.file-grid.big-icon { grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
.file-grid.big-icon .icon { font-size: 64px; }
/* 小图标模式 */
.file-grid.small-icon { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; }
.file-grid.small-icon .icon { font-size: 32px; }
.file-grid.small-icon .name { font-size: 12px; }
/* 列表模式 */
.file-grid.list-mode { display: flex; flex-direction: column; gap: 5px; }
.file-grid.list-mode .file-card { flex-direction: row; text-align: left; padding: 6px 12px; min-height: auto; align-items: center; }
.file-grid.list-mode .checkbox-wrapper { position: static; margin-right: 10px; }
.file-grid.list-mode .icon { font-size: 24px; margin-bottom: 0; margin-right: 12px; }
.file-grid.list-mode .name { flex: 1; min-height: auto; margin-bottom: 0; white-space: nowrap; -webkit-line-clamp: 1; }
.file-grid.list-mode .info { margin-bottom: 0; margin-left: 10px; margin-right: 15px; min-width: 140px; text-align: right; }
.file-grid.list-mode .actions { width: auto; margin-top: 0; }
</style>
</head>
<body>
<div id="header">
<h1>System Management Panel</h1>
<button onclick="logout()">登出</button>
</div>

<div class="tabs">
<button id="localTabBtn" class="active" onclick="showTab('ext')">系统管理</button>
<button id="advTabBtn" onclick="showTab('adv')">扩展功能</button>
</div>

<div class="container">
<!-- 系统管理标签页 -->
<div id="localTab">
<div class="stats-grid">
<div class="stat-card">
<label>CPU 负载 (1/5/15 min)</label>
<span id="stat-cpu">-</span>
</div>
<div class="stat-card">
<label>内存使用率</label>
<span id="stat-mem">-</span>
<div class="progress-bar"><div id="mem-fill" class="progress-fill"></div></div>
</div>
<div class="stat-card">
<label>磁盘使用率 (/)</label>
<span id="stat-disk">-</span>
<div class="progress-bar"><div id="disk-fill" class="progress-fill" style="background: #10b981;"></div></div>
</div>
<div class="stat-card">
<label>系统运行时间</label>
<span id="stat-uptime">-</span>
</div>
</div>

<h2>本地文件管理</h2>
<input type="file" id="extensionFile">
<button onclick="uploadFile()">上传文件</button>
<button onclick="runFile()">运行</button>

<div style="margin: 10px 0;">
<label style="cursor: pointer; font-size: 18px; font-weight: bold; display: inline-flex; align-items: center; gap: 5px;">
<input type="checkbox" id="showLocalFM" onchange="updateVisibility('showLocalFM', 'localFileManagerSection', 'mc_show_local_fm')"> 本地文件列表
</label>
</div>

<div id="localFileManagerSection" class="file-manager" style="display: none;">
<h3>本地文件管理 - <span id="currentLocalPath">/</span>
<button class="refresh-btn" onclick="loadLocalFiles()" title="刷新列表">
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>
</button>
</h3>
<div style="margin-bottom:10px; display: flex; align-items: center; flex-wrap: wrap; gap: 8px;">
<button onclick="goBackLocal()">返回上一级</button>
<button onclick="createLocalFolder()">新建文件夹</button>
<button onclick="deleteSelectedLocalItems()">全部删除</button>
<button onclick="toggleSelectAllLocalBtn()">全选</button>

<!-- 模式切换按钮 -->
<div style="display: flex; gap: 2px; margin-left: 10px;">
<button class="refresh-btn" onclick="changeViewMode('local', 'big-icon')" title="大图标">大</button>
<button class="refresh-btn" onclick="changeViewMode('local', 'small-icon')" title="小图标">小</button>
<button class="refresh-btn" onclick="changeViewMode('local', 'list-mode')" title="列表模式">列</button>
</div>

<!-- 本地翻页控制 -->
<div id="localScrollControls" style="display:none; align-items: center; gap: 8px; margin-left: 20px;">
<span id="localPageInfo" style="font-size:12px; color:#9ca3af;"></span>
<button class="refresh-btn" onclick="scrollLocal(-1)">▲ 向上</button>
<button class="refresh-btn" onclick="scrollLocal(1)">▼ 向下</button>
</div>
</div>
<div id="localFileTable" class="file-grid"></div>
</div>

<!-- 终端模拟器选择框 -->
<div style="margin: 10px 0;">
<label style="cursor: pointer; font-size: 18px; font-weight: bold; display: inline-flex; align-items: center; gap: 5px;">
<input type="checkbox" id="showTerminal" onchange="toggleTerminal()"> 终端模拟器
</label>
</div>

<!-- 终端模拟器（默认隐藏） -->
<div id="terminalSection" class="file-manager" style="display: none;">
<h3>终端模拟器</h3>
<div id="terminalContainer" style="height: 400px; background: #000;"></div>
</div>

<h2>本地进程管理</h2>
<input id="processName" placeholder="输入进程名">
<button class="refresh-btn" onclick="loadProcesses()" title="刷新进程列表">
<svg width="1em" height="1em" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>
</button>
<button onclick="findProcess()">查找进程</button>
<button onclick="killProcess()">杀死进程</button>

<!-- 本地进程管理选择框 -->
<div style="margin: 10px 0;">
<label style="cursor: pointer; font-size: 18px; font-weight: bold; display: inline-flex; align-items: center; gap: 5px;">
<input type="checkbox" id="showLocalPM" onchange="updateVisibility('showLocalPM', 'processDisplayArea', 'mc_show_local_pm')"> 本地进程列表
</label>
</div>

<!-- 进程列表联动区域 -->
<div id="processDisplayArea" style="display: none;">
<select id="processList" size="15" style="width:100%;"></select>
<pre id="processOutput"></pre>
</div>

<div style="margin: 10px 0;">
<label style="cursor: pointer; font-size: 18px; font-weight: bold; display: inline-flex; align-items: center; gap: 5px;">
<input type="checkbox" id="showCron" onchange="updateVisibility('showCron', 'cronSection', 'sys_show_cron')"> 计划任务管理
</label>
</div>

<div id="cronSection" class="file-manager" style="display: none;">
<h3>计划任务区域
<button class="refresh-btn" onclick="loadCronTasks()" title="刷新任务"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg></button>
</h3>
<div style="margin-bottom: 15px; display: flex; gap: 10px;">
<input id="newCronTask" style="flex: 1; padding: 8px;" placeholder="输入 Cron 表达式和命令 (例: * * * * * /usr/bin/python3 /script.py)">
<button onclick="addCronTask()" style="background: #3b82f6; white-space: nowrap;">添加任务</button>
</div>
<div id="cronListContainer" style="background:#000; border-radius: 4px; max-height: 400px; overflow-y: auto;"></div>
</div>
</div>

<!-- 扩展功能标签页 -->
<div id="advTab" style="display:none">
<p style="color: #9ca3af; text-align: center; margin-top: 50px;">即将推出更多实用功能，敬请期待...</p>
</div>
</div>

<!-- 文件编辑器 -->
<div id="editorModal" class="editor-modal">
<div class="editor-container">
<h3 id="editorTitle" style="margin-top:0;">编辑文件: <span></span></h3>
<textarea id="editorArea" spellcheck="false"></textarea>
<div style="margin-top:15px; display:flex; gap:10px; justify-content: flex-end;">
<button style="background:#4b5563;" onclick="closeEditor()">取消</button>
<button style="background:#3b82f6; padding: 6px 20px;" onclick="saveFileContent()">保存</button>
</div>
</div>
</div>

<script>
// 通用 Fetch 处理函数，自动处理 401 越权
async function apiFetch(url, options = {}) {
    try {
        const res = await fetch(url, options);
        if (res.status === 401) {
            location.href = '/login';
            return null;
        }
        return await res.json();
    } catch (e) {
        console.error('API请求失败:', e);
        return null;
    }
}

// ---------- 登出函数 ----------
async function logout(){
    await apiFetch('/api/logout', { method:'POST' });
    location.href='/login';
}

let localViewMode = localStorage.getItem('mc_local_view_mode') || 'small-icon';
function changeViewMode(type, mode) {
    localViewMode = mode;
    localStorage.setItem('mc_local_view_mode', mode);
    renderLocalFiles();
}

let editingFile = { type: 'local', path: '', name: '' };
async function editLocalFile(name) {
    const fullPath = currentLocalPath.endsWith('/') ? `${currentLocalPath}${name}` : `${currentLocalPath}/${name}`;
    editingFile = { type: 'local', path: fullPath, name: name };
    const data = await apiFetch(`/api/local/files/content?file=${encodeURIComponent(fullPath)}`);
    if (data && data.success) {
        document.querySelector('#editorTitle span').textContent = `${name} (本地)`;
        document.getElementById('editorArea').value = data.content;
        document.getElementById('editorModal').style.display = 'block';
    } else if (data) {
        alert('读取失败: ' + data.message);
    }
}

function closeEditor() { document.getElementById('editorModal').style.display = 'none'; }

async function saveFileContent() {
    const content = document.getElementById('editorArea').value;
    try {
        const data = await apiFetch('/api/local/files/save', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ filePath: editingFile.path, content })
        });
        if (data && data.success) {
            alert('保存成功');
            closeEditor();
            loadLocalFiles();
        } else if (data) { alert('保存失败: ' + data.message); }
    } catch (err) { console.error(err); }
}

// ---------- 本地文件管理逻辑 ----------
let allLocalFiles = [];
const PAGE_SIZE = 10;
let localOffset = 0;

let currentLocalPath = '';
let terminalCurrentPath = ''; // 终端专用独立路径
async function loadLocalFiles(targetDir) {
    const path = targetDir !== undefined ? targetDir : currentLocalPath;
    const query = path ? `directory=${encodeURIComponent(path)}` : '';
    const data = await apiFetch(`/api/local/files/list?${query}`);
    if (!data) return;
    if (!data.success) {
        alert(data.message || '加载目录失败');
        return;
    }

    // 首次加载时初始化终端路径
    if (!terminalCurrentPath) terminalCurrentPath = data.currentDir;

    currentLocalPath = data.currentDir;
    allLocalFiles = data.files;
    localOffset = 0;
    renderLocalFiles();
}

function renderLocalFiles() {
    document.getElementById('currentLocalPath').textContent = currentLocalPath;
    const grid = document.getElementById('localFileTable');
    grid.className = 'file-grid ' + localViewMode;
    const controls = document.getElementById('localScrollControls');
    const pageInfo = document.getElementById('localPageInfo');

    grid.innerHTML = '';
    const visibleFiles = allLocalFiles.slice(localOffset, localOffset + PAGE_SIZE);

    visibleFiles.forEach(f => {
        const isDir = !f.is_file;
        const size = f.is_file ? `${(f.size / 1024).toFixed(2)} KB` : '-';
        const card = document.createElement('div');
        card.className = 'file-card';
        card.innerHTML = `
            <div class="checkbox-wrapper"><input type="checkbox" class="local-file-checkbox" value="${f.name}" data-is-dir="${isDir}"></div>
            <div class="icon" onclick="${isDir ? `enterLocalDir('${f.name}')` : ''}">${isDir ? '📁' : '📄'}</div>
            <div class="name" title="${f.name}">${f.name}</div>
            <div class="info">${size} | ${new Date(f.modified_at).toLocaleDateString()}</div>
            <div class="actions">
                ${f.is_file ? `<button onclick="downloadLocalFile('${f.name}')">下载</button>` : ''}
                ${f.is_file ? `<button style="background: #059669;" onclick="editLocalFile('${f.name}')">编辑</button>` : ''}
                <button onclick="renameLocalItem('${f.name}')">重命名</button>
                <button onclick="deleteLocalItem('${f.name}')">删除</button>
            </div>`;
        grid.appendChild(card);
    });

    if (allLocalFiles.length > PAGE_SIZE) {
        controls.style.display = 'flex';
        pageInfo.textContent = `第 ${localOffset + 1} - ${Math.min(localOffset + PAGE_SIZE, allLocalFiles.length)} / 共 ${allLocalFiles.length} 项`;
    } else {
        controls.style.display = 'none';
    }
}

function scrollLocal(direction) {
    const newOffset = localOffset + (direction * PAGE_SIZE);
    if (newOffset >= 0 && newOffset < allLocalFiles.length) {
        localOffset = newOffset;
        renderLocalFiles();
    }
}

function enterLocalDir(name) {
    const nextPath = currentLocalPath.endsWith('/') ? `${currentLocalPath}${name}` : `${currentLocalPath}/${name}`;
    loadLocalFiles(nextPath);
}

function goBackLocal() {
    loadLocalFiles(`${currentLocalPath}/..`);
}

async function createLocalFolder() {
    const name = prompt('请输入本地文件夹名称:');
    if (!name) return;
    const data = await apiFetch('/api/local/files/create-folder', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name, directory: currentLocalPath })
    });
    if (data && data.success) loadLocalFiles();
    else alert('创建失败: ' + data.message);
}

async function deleteLocalItem(name) {
    if (!confirm(`确定要删除本地项目 ${name} 吗？`)) return;
    const data = await apiFetch('/api/local/files/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ root: currentLocalPath, files: [name] })
    });
    if (data && data.success) loadLocalFiles();
    else alert('删除失败: ' + data.message);
}

async function deleteSelectedLocalItems() {
    const selected = Array.from(document.querySelectorAll('.local-file-checkbox:checked')).map(cb => cb.value);
    if (selected.length === 0) return alert('请勾选要删除的文件');
    if (!confirm(`确定要批量删除这 ${selected.length} 个本地项目吗？`)) return;

    const data = await apiFetch('/api/local/files/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ root: currentLocalPath, files: selected })
    });
    if (data && data.success) loadLocalFiles();
    else alert('批量删除失败: ' + data.message);
}

async function renameLocalItem(oldName) {
    const newName = prompt('请输入新名称:', oldName);
    if (!newName || newName === oldName) return;
    const data = await apiFetch('/api/local/files/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            root: currentLocalPath,
            files: [{ from: oldName, to: newName }]
        })
    });
    if (data && data.success) loadLocalFiles();
    else alert('重命名失败: ' + data.message);
}

function toggleSelectAllLocalBtn() {
    const checkboxes = document.querySelectorAll('.local-file-checkbox');
    if (checkboxes.length === 0) return;
    const allChecked = Array.from(checkboxes).every(cb => cb.checked);
    checkboxes.forEach(cb => cb.checked = !allChecked);
}

function downloadLocalFile(name) {
    const filePath = currentLocalPath.endsWith('/') ? `${currentLocalPath}${name}` : `${currentLocalPath}/${name}`;
    window.open(`/api/local/files/download?file=${encodeURIComponent(filePath)}`, '_blank');
}

// ---------- 终端模拟器逻辑 ----------
let terminal = null;
let terminalSocket = null;
let fitAddon = null;
let terminalInitialized = false;

function toggleTerminal() {
    const checkbox = document.getElementById('showTerminal');
    const section = document.getElementById('terminalSection');

    if (checkbox && section) {
        const isChecked = checkbox.checked;
        section.style.display = isChecked ? 'block' : 'none';
        localStorage.setItem('mc_show_terminal', isChecked);

        if (isChecked) {
            if (!terminalInitialized) {
                initTerminal();
                terminalInitialized = true;
            } else {
                reconnectTerminal();
            }
        } else {
            if (terminalSocket) {
                terminalSocket.close();
                terminalSocket = null;
            }
            if (terminal) {
                terminal.dispose();
                terminal = null;
                terminalInitialized = false;
            }
        }
    }
}

function initTerminal() {
    const container = document.getElementById('terminalContainer');
    if (!container) return;

    terminal = new Terminal({
        cursorBlink: true,
        theme: {
            background: '#000000',
            foreground: '#ffffff'
        },
        fontSize: 14,
        fontFamily: 'Menlo, Monaco, "Courier New", monospace'
    });

    fitAddon = new FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(container);
    fitAddon.fit();

    window.addEventListener('resize', () => {
        if (fitAddon && terminal && document.getElementById('showTerminal').checked) {
            setTimeout(() => fitAddon.fit(), 100);
        }
    });

    terminal.onResize(size => {
        if (terminalSocket && terminalSocket.readyState === WebSocket.OPEN) {
            terminalSocket.send(JSON.stringify({ type: 'resize', cols: size.cols, rows: size.rows }));
        }
    });

    terminal.onData(data => {
        if (terminalSocket && terminalSocket.readyState === WebSocket.OPEN) {
            terminalSocket.send(data);
        }
    });

    connectWebSocket();
}

function connectWebSocket() {
    if (!terminal) return;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const cwd = terminalCurrentPath || currentLocalPath || '';
    const wsUrl = `${protocol}//${window.location.host}/api/terminal/ws?cwd=${encodeURIComponent(cwd)}`;

    terminalSocket = new WebSocket(wsUrl);

    terminalSocket.onopen = () => {
        terminal.clear();
        // terminal.write('\\x1b[32m[终端已连接]\\x1b[0m\\r\\n');
        const size = terminal.buffer.active.type === 'normal' ? {cols: terminal.cols, rows: terminal.rows} : {cols: 80, rows: 24};
        terminalSocket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }));
    };

    terminalSocket.onmessage = (event) => {
        if (!terminal) return;
        if (event.data instanceof Blob) {
            const reader = new FileReader();
            reader.onload = () => terminal.write(reader.result);
            reader.readAsText(event.data);
        } else {
            terminal.write(event.data);
        }
    };

    terminalSocket.onclose = () => {
        if (terminal) terminal.write('\\r\\n\\x1b[33m[终端连接已断开]\\x1b[0m\\r\\n');
        terminalSocket = null;
        if (document.getElementById('showTerminal').checked) {
            setTimeout(() => {
                if (!terminalSocket && document.getElementById('showTerminal').checked) connectWebSocket();
            }, 3000);
        }
    };

    terminalSocket.onerror = (err) => {
        console.error("Terminal WS Error", err);
    };
}

function reconnectTerminal() {
    if (terminalSocket) terminalSocket.close();
    if (terminal) {
        terminal.clear();
        terminal.write('\\x1b[33m[正在重新连接...]\\x1b[0m\\r\\n');
    }
    connectWebSocket();
}

function initTerminalVisibility() {
    const showTerminal = localStorage.getItem('mc_show_terminal');
    const checkbox = document.getElementById('showTerminal');
    if (checkbox) {
        const isChecked = (showTerminal === 'true');
        checkbox.checked = isChecked;
        document.getElementById('terminalSection').style.display = isChecked ? 'block' : 'none';
        if (isChecked && !terminalInitialized) {
            initTerminal();
            terminalInitialized = true;
        }
    }
}

window.addEventListener('beforeunload', () => {
    if (terminalSocket) terminalSocket.close();
});

// ---------- 本地管理 ----------
let uploadedFile = '';
async function uploadFile(){
    const fileInput = document.getElementById('extensionFile');
    const file = fileInput.files[0];
    if(!file) return alert('请选择要上传的文件');

    // 逻辑：如果本地文件管理勾选了文件夹，则上传到该文件夹；否则上传到当前浏览目录
    let uploadDir = currentLocalPath;
    const selected = Array.from(document.querySelectorAll('.local-file-checkbox:checked'));
    if (selected.length > 0) {
        const checkbox = selected[0];
        if (checkbox.dataset.isDir === 'true') {
            uploadDir = currentLocalPath.endsWith('/') ? `${currentLocalPath}${checkbox.value}` : `${currentLocalPath}/${checkbox.value}`;
        }
    }

    const form = new FormData();
    form.append('directory', uploadDir);
    form.append('file', file);

    const res = await fetch('/api/extensions/upload', { method: 'POST', body: form }); // multer不适合用apiFetch包装
    const data = await res.json();

    if (data && data.success) {
        uploadedFile = data.filename; // 存储返回的路径
        alert('上传成功: ' + data.filename);
        loadLocalFiles(); // 刷新下方的文件列表
    } else {
        alert('上传失败: ' + (data.message || '未知错误'));
    }
}

async function runFile(){
    if(!uploadedFile) return alert('请先上传文件');
    const data = await apiFetch('/api/extensions/run',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({filename:uploadedFile})
    });
    if(data && data.success){
        alert('运行成功:\\n' + data.output);
    } else if(data) {
        alert('运行失败:\\n' + data.output);
    }
}

// ---------- 进程管理 ----------
async function loadProcesses(){
    const data = await apiFetch('/api/processes');
    if (!data || !data.processes) return;
    const select = document.getElementById('processList');
    select.innerHTML = '';
    data.processes.forEach(p=>{
        const option = document.createElement('option');
        option.value = p.pid;
        option.text = `${p.pid} - ${p.name}`;
        select.appendChild(option);
    });
}

let currentPID = null;
async function findProcess() {
    const name = document.getElementById('processName').value.trim();
    if (!name) return;
    const processList = document.getElementById('processList');
    const data = await apiFetch(`/api/processes/find?name=${encodeURIComponent(name)}`);
    if (!data) return;

    if (processList && processList.options.length > 0) {
        // ps列表存在，定位到匹配项
        let found = false;
        for (let i = 0; i < processList.options.length; i++) {
            const option = processList.options[i];
            if (option.text.includes(name)) {
                processList.selectedIndex = i;
                found = true;
                currentPID = data.found ? data.pid : null;
                break;
            }
        }
        if (!found) {
            alert(data.found ? '未在列表中找到，PID: ' + data.pid : '未找到进程');
        }
    } else {
        // 列表为空，只返回 PID
        alert(data.found ? '找到进程 PID: ' + data.pid : '未找到进程');
    }
}

async function killProcess() {
    let pidToKill = null;
    const processList = document.getElementById('processList');

    if (processList.options.length > 0 && processList.selectedIndex >= 0) {
        pidToKill = processList.options[processList.selectedIndex].value;
    } else if (currentPID) {
        pidToKill = currentPID;
    } else {
        return alert('请选择或查找到要杀死的进程');
    }

    const data = await apiFetch('/api/processes/kill', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({pid: pidToKill})
    });
    if (data && data.success) alert('已杀死进程 ' + pidToKill);
    else if (data) alert('杀死进程失败: ' + data.message);
    currentPID = null;
    loadProcesses();
}

// ---------- 计划任务管理 ----------
async function loadCronTasks() {
    const data = await apiFetch('/api/cron/list');
    if (!data) return;
    const container = document.getElementById('cronListContainer');
    if (data && data.success) {
        if (data.tasks.length === 0) {
            container.innerHTML = '<div style="padding:10px; color:#9ca3af;">暂无计划任务</div>';
            return;
        }
        let html = '<table style="width:100%; border-collapse:collapse; color:#e5e7eb;">';
        data.tasks.forEach(task => {
            html += `
            <tr style="border-bottom:1px solid #374151;">
                <td style="padding:10px; font-family:monospace; font-size:13px;">${task}</td>
                <td style="padding:10px; text-align:right;">
                    <button style="background:#ef4444; font-size:12px;" onclick="deleteCronTask('${btoa(task)}')">删除</button>
                </td>
            </tr>`;
        });
        html += '</table>';
        container.innerHTML = html;
    }
}

async function addCronTask() {
    const task = document.getElementById('newCronTask').value.trim();
    if (!task) return;
    const data = await apiFetch('/api/cron/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task })
    });
    if (data && data.success) {
        document.getElementById('newCronTask').value = '';
        loadCronTasks();
    } else if(data) alert('添加失败: ' + data.message);
}

async function deleteCronTask(encodedTask) {
    const task = atob(encodedTask);
    if (!confirm('确定要删除此任务吗？')) return;
    const data = await apiFetch('/api/cron/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task })
    });
    if (data && data.success) loadCronTasks();
    else if (data) alert('删除失败: ' + data.message);
}

// 公共逻辑：根据当前开关状态加载本地数据
function loadActiveLocalData() {
    const fmSwitch = document.getElementById('showLocalFM');
    const pmSwitch = document.getElementById('showLocalPM');
    if(fmSwitch && fmSwitch.checked && typeof loadLocalFiles === 'function') loadLocalFiles();
    if(pmSwitch && pmSwitch.checked && typeof loadProcesses === 'function') loadProcesses();
}

// 点击标签加载项
function showTab(tab){
    document.getElementById('localTab').style.display = tab==='ext' ? 'block' : 'none';
    document.getElementById('advTab').style.display = tab==='adv' ? 'block' : 'none';

    document.getElementById('localTabBtn').classList.toggle('active',tab==='ext');
    document.getElementById('advTabBtn').classList.toggle('active',tab==='adv');

    if(tab === 'ext'){
        updateSystemStats();
        loadActiveLocalData();
        if (document.getElementById('showCron').checked) loadCronTasks();
    }
}

// ---------- 显隐控制与持久化逻辑 ----------
function updateVisibility(checkboxId, sectionId, storageKey) {
    const checkbox = document.getElementById(checkboxId);
    const section = document.getElementById(sectionId);
    if (checkbox && section) {
        const isChecked = checkbox.checked;
        section.style.display = isChecked ? 'block' : 'none';
        localStorage.setItem(storageKey, isChecked);

        // 联动：勾选开启时立即尝试获取最新数据
        if (isChecked) {
            if (checkboxId === 'showLocalFM' && typeof loadLocalFiles === 'function') loadLocalFiles();
            if (checkboxId === 'showLocalPM' && typeof loadProcesses === 'function') loadProcesses();
            if (checkboxId === 'showCron' && typeof loadCronTasks === 'function') loadCronTasks();
        }
    }
}

function initVisibility() {
    const configs = [
        { id: 'showLocalFM', section: 'localFileManagerSection', key: 'mc_show_local_fm' },
        { id: 'showLocalPM', section: 'processDisplayArea', key: 'mc_show_local_pm' },
        { id: 'showCron', section: 'cronSection', key: 'sys_show_cron' }
    ];

    configs.forEach(cfg => {
        const checkbox = document.getElementById(cfg.id);
        const section = document.getElementById(cfg.section);
        if (checkbox && section) {
            const stored = localStorage.getItem(cfg.key);
            const isVisible = (stored === null || stored === 'true'); // 默认展开
            checkbox.checked = isVisible;
            section.style.display = isVisible ? 'block' : 'none';
        }
    });
}

async function updateSystemStats() {
    const data = await apiFetch('/api/system/stats');
    if (data && data.success) {
        // CPU 负载
        document.getElementById('stat-cpu').textContent = data.load.map(l => l.toFixed(2)).join(' / ');

        // 内存状态及颜色预警 (超过90%变红)
        document.getElementById('stat-mem').textContent = `${data.memory.used} / ${data.memory.total} (${data.memory.percent}%)`;
        document.getElementById('stat-mem').style.color = data.memory.percent > 90 ? '#ef4444' : '#3b82f6';
        const memFill = document.getElementById('mem-fill');
        memFill.style.width = `${data.memory.percent}%`;
        memFill.style.backgroundColor = data.memory.percent > 90 ? '#ef4444' : '#3b82f6';

        // 磁盘状态及颜色预警 (超过90%变红)
        document.getElementById('stat-disk').textContent = `${data.disk.used} / ${data.disk.total} (${data.disk.percent}%)`;
        document.getElementById('stat-disk').style.color = data.disk.percent > 90 ? '#ef4444' : '#3b82f6';
        const diskFill = document.getElementById('disk-fill');
        diskFill.style.width = `${data.disk.percent}%`;
        diskFill.style.backgroundColor = data.disk.percent > 90 ? '#ef4444' : '#10b981';

        // 运行时间
        const days = Math.floor(data.uptime / 86400);
        const hours = Math.floor((data.uptime % 86400) / 3600);
        document.getElementById('stat-uptime').textContent = `${days}天 ${hours}小时`;
    }
}

setInterval(updateSystemStats, 5000);
document.addEventListener('DOMContentLoaded', () => {
    initVisibility();
    initTerminalVisibility();
    updateSystemStats();

    // 默认展示项的数据初始化
    loadActiveLocalData();
});

</script>
</body>
</html>
"""

@app.route('/')
@login_required
def index():
    """主页面"""
    return INDEX_PAGE

# ==================== 启动服务 ====================

if __name__ == '__main__':
    print(f"🚀 Panel started successfully: http://localhost:{PORT}")
    # 禁用 Werkzeug 的默认请求日志输出 (即隐藏 127.0.0.1 - - ...)
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
