FROM python:3.10-slim

# 系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN python --version && node --version && npm --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    # 修复 protobuf-to-dict 在 Python 3.10+ 的兼容问题
    sed -i -e 's/\blong\b/int/g' -e 's/\bunicode\b/str/g' /usr/local/lib/python3.10/site-packages/protobuf_to_dict.py

# 抖音签名脚本（static/dy_ab.js 等）在 Node 里 require jsrsasign，
# 而 .dockerignore 排除了本机 node_modules（那是 Windows 编译的，Linux 用不了），
# 所以必须在容器内装一份 Linux 版；缺这一步会构建成功、健康检查通过，
# 但一点监听就报 Cannot find module 'jsrsasign'。
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY . .

RUN mkdir -p /app/data

EXPOSE 5000

ENV PYTHONUNBUFFERED=1
ENV NODE_ENV=production

# 单进程多线程：监听状态在内存里，多 worker 会分裂状态并重复连抖音。
# 长连接（SSE）每条占一个线程直到断开，所以线程数就是同时在线的上限；
# 这些线程绝大部分时间阻塞在 queue.get() 上、不占 CPU，加线程几乎只花内存。
# 384 线程 ≈ 290 条长连接 + 富余请求线程；容器 ulimit -n 实测 524288，不构成限制。
# timeout 0 保长连接不被掐。
CMD ["sh", "-c", "gunicorn --workers 1 --threads 384 --worker-class gthread --timeout 0 --bind 0.0.0.0:${PORT:-5000} --access-logfile - --error-logfile - web_listener:app"]
