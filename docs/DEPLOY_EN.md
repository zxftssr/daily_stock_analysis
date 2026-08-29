# Deployment Guide

This document explains how to deploy the AI Stock Analysis System to a server.

## Deployment Options Comparison

| Option | Pros | Cons | Recommended For |
|------|------|------|----------|
| **Docker Compose** ⭐ | One-click deploy, isolated environment, easy migration, easy upgrade | Requires Docker installation | **Recommended**: Most scenarios |
| **Direct Deployment** | Simple, no extra dependencies | Environment dependencies, migration difficulties | Temporary testing |
| **Systemd Service** | System-level management, auto-start on boot | Complex configuration | Long-term stable operation |
| **Supervisor** | Process management, auto-restart | Requires additional installation | Multi-process management |

**Conclusion: Docker Compose is recommended for the fastest and most convenient migration!**

---

## Option 1: Docker Compose Deployment (Recommended)

### 1. Install Docker

```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# CentOS
sudo yum install -y docker docker-compose
sudo systemctl start docker
sudo systemctl enable docker
```

### 2. Prepare Configuration Files

```bash
# Clone code (or upload code to server)
git clone <your-repo-url> /opt/stock-analyzer
cd /opt/stock-analyzer

# Copy and edit configuration file
cp .env.example .env
vim .env  # Fill in real API Keys and configuration
```

### 3. Recommended Startup: Separate Web/API and Scheduler Containers

```bash
# Start only the Web/API service
docker-compose -f ./docker/docker-compose.yml up -d server

# Start the scheduled-analysis container when daily automatic analysis is needed
docker-compose -f ./docker/docker-compose.yml up -d analyzer

# Start both Web/API and scheduled analysis
docker-compose -f ./docker/docker-compose.yml up -d server analyzer

# View logs
docker-compose -f ./docker/docker-compose.yml logs -f

# View running status
docker-compose -f ./docker/docker-compose.yml ps
```

Recommended production deployments use two containers:

- `stock-server`: Web/API only.
- `stock-analyzer`: scheduled analysis only.

This keeps Web restarts from accidentally triggering analysis, and scheduler failures from directly taking the Web console offline. If you only need manual analysis from the Web UI, starting `server` is enough.

The `analyzer` writes `scheduler_status.json` beside the configured database, and `server` reads it to show scheduler liveness and the latest minute check on the Strategy Plans page. The provided Compose services already share `/app/data`; custom containers or systemd units must use the same `DATABASE_PATH` directory for both processes, otherwise the page reports that the scheduler has not started. A heartbeat older than 90 seconds is shown as offline, and `restart: unless-stopped` restarts the analyzer after an unexpected process exit.

Before enabling scheduled analysis, set the following in `.env`:

```env
SCHEDULE_ENABLED=true
SCHEDULE_TIME=18:00
SCHEDULE_RUN_IMMEDIATELY=false
```

`SCHEDULE_RUN_IMMEDIATELY=false` means the `analyzer` container waits for the next `SCHEDULE_TIME` instead of running analysis immediately on startup. Set it to `true` if you want one analysis run as soon as the container starts.

### 4. Common Management Commands

```bash
# Stop services
docker-compose -f ./docker/docker-compose.yml down

# Restart services
docker-compose -f ./docker/docker-compose.yml restart

# Redeploy after code update
git pull
docker-compose -f ./docker/docker-compose.yml build --no-cache
docker-compose -f ./docker/docker-compose.yml up -d server analyzer

# Enter the scheduler container for debugging
docker-compose -f ./docker/docker-compose.yml exec -u dsa stock-analyzer bash

# Manually run analysis once
docker-compose -f ./docker/docker-compose.yml exec -u dsa stock-analyzer python main.py --no-notify
```

### 5. Sync Code Updates with the Deploy Script

The repository includes `scripts/deploy_server.sh` for syncing the current local checkout to an existing Docker Compose server and running `docker compose up -d --build server` remotely. By default, the script rebuilds only the Web/API service; the scheduler container keeps running with the current image. If the update changes scheduled-analysis logic, run this after the script completes:

Before deploying, if `docs/DEPLOY_TARGET.local.md` exists locally, read it first and verify the server, SSH user, key, and remote path. This file stores personal deployment targets, is ignored by Git, and must not be replaced by real server addresses or credentials in public documentation.

```bash
ssh <user>@<server> 'cd /opt/daily_stock_analysis && docker compose -f docker/docker-compose.yml up -d --build analyzer'
```

By default, the script protects server runtime state and will not sync or delete:

- `.env`
- `data/*.db*`
- `data/cache/`
- `data/*.lock*`
- `data/.admin_*`
- `data/.session_secret`
- `docker/docker-compose.yml`
- `logs/`
- `reports/`

Run from your local checkout:

```bash
DSA_DEPLOY_HOST=<server-ip-or-domain> \
DSA_DEPLOY_USER=<ssh-user> \
DSA_DEPLOY_KEY=~/.ssh/<deploy-key> \
DSA_DEPLOY_PATH=/opt/daily_stock_analysis \
scripts/deploy_server.sh
```

If the remote user needs sudo for Docker:

```bash
DSA_DEPLOY_HOST=<server-ip-or-domain> \
DSA_DEPLOY_USER=<ssh-user> \
DSA_DEPLOY_KEY=~/.ssh/<deploy-key> \
DSA_DEPLOY_PATH=/opt/daily_stock_analysis \
DSA_REMOTE_COMPOSE_CMD='sudo docker compose' \
scripts/deploy_server.sh
```

Preview synced files without rebuilding the remote service:

```bash
DSA_DEPLOY_HOST=<server-ip-or-domain> \
DSA_DEPLOY_USER=<ssh-user> \
DSA_DEPLOY_KEY=~/.ssh/<deploy-key> \
DSA_DEPLOY_PATH=/opt/daily_stock_analysis \
DSA_DEPLOY_DRY_RUN=true \
scripts/deploy_server.sh
```

Only set the following for first-time deployment or when you intentionally want to overwrite the remote `.env`:

```bash
DSA_DEPLOY_SYNC_ENV=true
```

Only set the following when you intentionally want to overwrite the remote Compose file:

```bash
DSA_DEPLOY_SYNC_COMPOSE=true
```

This matters for servers with reverse proxy or port-binding customizations. For example, the remote Compose file may bind the app port to `127.0.0.1` and expose HTTPS through Caddy or Nginx.

For normal code updates, do not sync `.env`; otherwise you may overwrite the server's admin password, MFA state, API keys, database path, or notification settings.

### 6. Data Persistence

Data is automatically saved to host directories:
- `./data/` - Database files
- `./logs/` - Log files
- `./reports/` - Analysis reports

### 7. Permissions

The Docker image startup entrypoint automatically creates and fixes ownership for the mounted `./data`, `./logs`, and `./reports` directories, then drops privileges to the non-root `dsa` user (UID 1000). Normal deployments do not require manual host-side `chown` / `chmod`.

If you explicitly set `--user` / Compose `user:`, or use read-only mounts, rootless Docker, NFS, or another environment that prevents the container from fixing ownership, make sure the actual runtime user can write to these directories.

---

## Option 2: Direct Deployment

### 1. Install Python Environment

```bash
# Install Python 3.10+
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3-pip

# Create virtual environment
python3.10 -m venv /opt/stock-analyzer/venv
source /opt/stock-analyzer/venv/bin/activate
```

### 2. Install Dependencies

```bash
cd /opt/stock-analyzer
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3. Configure Environment Variables

```bash
cp .env.example .env
vim .env  # Fill in configuration
```

### 4. Run

```bash
# Single run
python main.py

# Scheduled task mode (foreground)
python main.py --schedule

# Background run (using nohup)
nohup python main.py --schedule > /dev/null 2>&1 &
```

---

## Option 3: Systemd Service

Create systemd service file for auto-start on boot and auto-restart:

### 1. Create Service File

```bash
sudo vim /etc/systemd/system/stock-analyzer.service
```

Contents:
```ini
[Unit]
Description=AI Stock Analysis System
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/stock-analyzer
Environment="PATH=/opt/stock-analyzer/venv/bin"
ExecStart=/opt/stock-analyzer/venv/bin/python main.py --schedule
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### 2. Start Service

```bash
# Reload configuration
sudo systemctl daemon-reload

# Start service
sudo systemctl start stock-analyzer

# Enable auto-start on boot
sudo systemctl enable stock-analyzer

# View status
sudo systemctl status stock-analyzer

# View logs
journalctl -u stock-analyzer -f
```

---

## Configuration Guide

### Required Configuration

| Config Item | Description | How to Get |
|--------|------|----------|
| `ANSPIRE_API_KEYS` / `AIHUBMIX_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Configure at least one AI model key; Anspire or AIHubMix is recommended first | Provider console |
| `STOCK_LIST` | Watchlist | Comma-separated stock codes |
| Notification channel | Configure at least one, such as WeChat Work, Feishu, Telegram, or email | Notification provider |

### Optional Configuration

| Config Item | Default | Description |
|--------|--------|------|
| `SCHEDULE_ENABLED` | `false` | Enable scheduled tasks |
| `SCHEDULE_TIME` | `18:00` | Daily execution time |
| `MARKET_REVIEW_ENABLED` | `true` | Enable market review |
| `ANSPIRE_API_KEYS` | - | Anspire LLM and news search (recommended) |
| `AIHUBMIX_KEY` | - | AIHubMix one-key multi-model access (recommended) |
| `SERPAPI_API_KEYS` | - | SerpAPI realtime financial news search (recommended) |
| `TAVILY_API_KEYS` | - | Tavily news search (optional) |
| `MINIMAX_API_KEYS` | - | MiniMax search (optional) |

---

## Proxy Configuration

If server is in mainland China, accessing Gemini API requires proxy:

### Docker Method

Edit `docker-compose.yml`:
```yaml
environment:
  - http_proxy=http://your-proxy:port
  - https_proxy=http://your-proxy:port
```

### Direct Deployment Method

Edit top of `main.py`:
```python
os.environ["http_proxy"] = "http://your-proxy:port"
os.environ["https_proxy"] = "http://your-proxy:port"
```

---

## Monitoring & Maintenance

### View Logs

```bash
# Docker method
docker-compose -f ./docker/docker-compose.yml logs -f --tail=100

# Direct deployment
tail -f /opt/stock-analyzer/logs/stock_analysis_*.log
```

### Health Check

```bash
# Check process
ps aux | grep main.py

# Check recent reports
ls -la /opt/stock-analyzer/reports/
```

### Routine Maintenance

```bash
# Clean old logs (keep 7 days)
find /opt/stock-analyzer/logs -mtime +7 -delete

# Clean old reports (keep 30 days)
find /opt/stock-analyzer/reports -mtime +30 -delete
```

---

## FAQ

### 1. Docker build failed

```bash
# Clear cache and rebuild
docker-compose -f ./docker/docker-compose.yml build --no-cache
```

### 2. API access timeout

Check proxy configuration, ensure server can access Gemini API.

### 3. Database locked

```bash
# Stop service then delete lock file
rm /opt/stock-analyzer/data/*.lock
```

### 4. Insufficient memory

Adjust memory limits in `docker-compose.yml`:
```yaml
deploy:
  resources:
    limits:
      memory: 1G
```

### 5. `stock-analyzer` is healthy, but scheduled jobs no longer run

`healthy` only confirms that the container process and health check are alive; it does not prove that a scheduled callback completed. Check the container start time, job start/completion logs, and the newest report first:

```bash
docker inspect -f 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{end}} started={{.State.StartedAt}}' stock-analyzer
docker logs --timestamps --since 7d stock-analyzer 2>&1 | grep -E '定时任务开始执行|定时任务执行完成|调度器运行中|下次执行时间|Scheduled task|scheduler|next run|Traceback|ERROR'
ls -lt reports/ | head
```

If a job-start message is followed by no completion message or later scheduler heartbeat, the callback is usually stuck in a synchronous third-party call. The current version applies caller-side timeouts to the Eastmoney and Sina AkShare whole-market endpoints used for A-share market statistics. A timed-out function enters cooldown, is not submitted again while its underlying worker is still running, and the shared AkShare pool is capped at 2 workers.

After updating the code, rebuild only the scheduler container and keep watching its logs:

```bash
docker compose -f docker/docker-compose.yml up -d --build analyzer
docker compose -f docker/docker-compose.yml logs -f --tail=200 analyzer
```

Python cannot forcibly terminate a third-party call already running in a thread. Rebuild or restart the `analyzer` container to clear calls that became stuck on an older version; the Web/API `server` container does not need to restart.

---

## Quick Migration

Migrate from one server to another:

```bash
# Source server: Package
cd /opt/stock-analyzer
tar -czvf stock-analyzer-backup.tar.gz .env data/ logs/ reports/

# Target server: Deploy
mkdir -p /opt/stock-analyzer
cd /opt/stock-analyzer
git clone <your-repo-url> .
tar -xzvf stock-analyzer-backup.tar.gz
docker-compose -f ./docker/docker-compose.yml up -d
```

---

## Option 4: GitHub Actions Deployment (Serverless)

**The simplest option!** No server needed, leverages GitHub's free compute resources.

### Advantages
- ✅ **Completely free** (2000 minutes/month)
- ✅ **No server needed**
- ✅ **Auto-scheduled execution**
- ✅ **Zero maintenance cost**

### Limitations
- ⚠️ Stateless (fresh environment each run)
- ⚠️ Scheduled timing may have few minutes delay
- ⚠️ Cannot provide HTTP API

### Deployment Steps

#### 1. Create GitHub Repository

```bash
# Initialize git (if not already)
cd /path/to/daily_stock_analysis
git init
git add .
git commit -m "Initial commit"

# Create GitHub repo and push
# After creating new repo on GitHub web:
git remote add origin https://github.com/your-username/daily_stock_analysis.git
git branch -M main
git push -u origin main
```

#### 2. Configure Secrets (Important!)

Go to repo page → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these Secrets:

| Secret Name | Description | Required |
|------------|------|------|
| `ANSPIRE_API_KEYS` | Anspire Open API Key (one key for LLM and search) | Recommended |
| `AIHUBMIX_KEY` | AIHubMix API Key (one key for multiple model families) | Recommended |
| `ANTHROPIC_API_KEY` | Anthropic API Key | Optional |
| `GEMINI_API_KEY` | Gemini AI API Key | Optional |
| `OPENAI_API_KEY` | OpenAI-compatible API Key | Optional |
| `WECHAT_WEBHOOK_URL` | WeChat Work Bot Webhook | Optional* |
| `FEISHU_WEBHOOK_URL` | Feishu Bot Webhook | Optional* |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | Optional* |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | Optional* |
| `TELEGRAM_MESSAGE_THREAD_ID` | Telegram Topic ID | Optional* |
| `EMAIL_SENDER` | Sender email | Optional* |
| `EMAIL_PASSWORD` | Email authorization code | Optional* |
| `SERVERCHAN3_SENDKEY` | ServerChan v3 Sendkey | Optional* |
| `CUSTOM_WEBHOOK_URLS` | Custom Webhook (comma-separated for multiple) | Optional* |
| `STOCK_LIST` | Watchlist, e.g., `600519,300750` | ✅ |
| `SERPAPI_API_KEYS` | SerpAPI Key | Recommended |
| `TAVILY_API_KEYS` | Tavily Search API Key | Optional |
| `BOCHA_API_KEYS` | Bocha Search API Key | Optional |
| `BRAVE_API_KEYS` | Brave Search API Key | Optional |
| `MINIMAX_API_KEYS` | MiniMax Coding Plan Web Search | Optional |
| `TUSHARE_TOKEN` | Tushare Token | Optional |
| `GEMINI_MODEL` | Model name (default gemini-2.0-flash) | Optional |

> *Note: Configure at least one notification channel, multiple channels supported for simultaneous push

#### 3. Verify Workflow File

Ensure `.github/workflows/daily_analysis.yml` file exists and is committed:

```bash
git add .github/workflows/daily_analysis.yml
git commit -m "Add GitHub Actions workflow"
git push
```

#### 4. Manual Test Run

1. Go to repo page → **Actions** tab
2. Select **"Daily Stock Analysis"** workflow
3. Click **"Run workflow"** button
4. Select run mode:
   - `full` - Full analysis (stocks + market)
   - `market-only` - Market review only
   - `stocks-only` - Stock analysis only
5. Click green **"Run workflow"** button

#### 5. View Execution Logs

- Actions page shows run history
- Click specific run record to view detailed logs
- Analysis reports are saved as Artifacts for 30 days

### Schedule Details

Default configuration: **Monday to Friday, 18:00 Beijing Time** auto-execution

Modify time: Edit cron expression in `.github/workflows/daily_analysis.yml`:

```yaml
schedule:
  - cron: '0 10 * * 1-5'  # UTC time, +8 = Beijing time
```

Common cron examples:
| Expression | Description |
|--------|------|
| `'0 10 * * 1-5'` | Mon-Fri 18:00 (Beijing) |
| `'30 7 * * 1-5'` | Mon-Fri 15:30 (Beijing) |
| `'0 10 * * *'` | Daily 18:00 (Beijing) |
| `'0 2 * * 1-5'` | Mon-Fri 10:00 (Beijing) |

### Modify Watchlist

Method 1: Modify repo Secret `STOCK_LIST`

Method 2: Modify code directly then push:
```bash
# Modify .env.example or set default value in code
git commit -am "Update stock list"
git push
```

### FAQ

**Q: Why isn't the scheduled task running?**
A: GitHub Actions scheduled tasks may have 5-15 minute delays, and only trigger when repo has activity. Long periods without commits may cause workflow to be disabled.

**Q: How to view historical reports?**
A: Actions → Select run record → Artifacts → Download `analysis-reports-xxx`

**Q: Is the free quota enough?**
A: Each run takes about 2-5 minutes, 22 workdays per month = 44-110 minutes, well below the 2000 minute limit.

---

**Wishing you a smooth deployment!**
