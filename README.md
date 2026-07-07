<div align="center">
  <h1>⚡ FiroGate Community Edition</h1>
  <p><strong>Self-hosted Firo payment gateway</strong></p>
  <p>
    <a href="#quick-start">Quick Start</a> ·
    <a href="#docker">Docker</a> ·
    <a href="#api">API</a> ·
    <a href="#security">Security</a> ·
    <a href="CONTRIBUTING.md">Contributing</a>
  </p>
  <br>
  <img src="https://img.shields.io/badge/Python-3.11+-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/License-Apache%202.0-green?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Self--Hosted-Yes-orange?style=flat-square" alt="Self Hosted">
  <img src="https://img.shields.io/badge/Community-Edition-yellow?style=flat-square" alt="Community">
</div>

---

Accept **FIRO** payments on your website. Runs entirely on your own server — you control the keys, the data, and the infrastructure.

> ⚠️ **Legal Notice:** FiroGate is a payment processing tool. Users are solely responsible for complying with all applicable laws and regulations in their jurisdiction. The developers are not responsible for any illegal or unauthorized use of this software. See [LEGAL.md](LEGAL.md) for full terms.

---

## Features

| | |
|---|---|
| ⚡ **REST API** | Create payments, track status, manage webhooks |
| 🔄 **Realtime** | SSE-powered checkout — instant confirmation, no polling |
| 🔐 **HD Wallet** | Unique address per payment, no address reuse |
| 🪝 **Webhooks** | HMAC-SHA256 signed, automatic retry on failure |
| 🔑 **Multi API Keys** | SHA-256 hashed, revokable per integration |
| 💸 **Withdrawals** | Request withdrawals from dashboard or API |
| 🔗 **Payment Links** | No-code shareable payment pages |
| 🌐 **Languages etc...** | Arabic RTL, English, Russian, German, Chinese + more |
| 🎨 **Themes** | 7 checkout presets, fully customizable |
| 🧅 **Tor Ready** | Runs as .onion hidden service out of the box |
| 🐳 **Docker** | One-command deployment |

---

## Quick Start

### Requirements

- Python 3.11+
- [Firo Core](https://firo.org/get-firo/download/) node — synced with `txindex=1`
- Linux or Windows

### 1. Clone

```bash
git clone https://github.com/firogate/firogate-ce.git
cd firogate
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Generate required keys:
```bash
# SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# FIELD_ENCRYPTION_KEY
python3 -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Minimum `.env`:
```env
SECRET_KEY=...
FIELD_ENCRYPTION_KEY=...
BASE_URL=https://yourdomain.com
FIRO_RPC_USER=your_rpc_user
FIRO_RPC_PASSWORD=your_rpc_password
OPERATOR_EMAILS=control@your-gmail.com
```

### 3. Run server

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## Docker

```bash
cp .env.example .env
# Edit .env
docker compose up -d
```

---

## Firo Core Setup

`~/.firo/firo.conf`:
```ini
rpcuser=your_user
rpcpassword=your_password
rpcallowip=127.0.0.1
rpcport=18888
server=1
txindex=1
```

---

## API

**Auth:** `X-API-Key: fg_live_your_key`

```http
POST /api/payments/create
X-API-Key: fg_live_your_key
Content-Type: application/json

{
  "amount_firo": 1.5,
  "order_id": "ORD-001",
  "success_url": "https://yourstore.com/success",
  "cancel_url": "https://yourstore.com/cancel"
}
```

Full docs at `/docs` after starting.

---

## Security

See [SECURITY.md](SECURITY.md) for vulnerability reporting and security architecture.

---

## Legal

See [LEGAL.md](LEGAL.md) for terms of use, acceptable use policy, and privacy information.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Enterprise Edition

An Enterprise Edition is available for managed deployments.

Contact: enterprise@firogate.com

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

---

## Docker

The fastest way to deploy FiroGate.

### Prerequisites

- Docker Engine 24+
- Docker Compose v2+

### 1. Clone and configure

```bash
git clone https://github.com/firogate/firogate-ce.git
cd firogate
cp .env.example .env
```

Edit `.env` — generate your secret keys first:

```bash
# SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

# FIELD_ENCRYPTION_KEY
python3 -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

Minimum `.env` for Docker:

```env
SECRET_KEY=...
FIELD_ENCRYPTION_KEY=...
BASE_URL=https://yourdomain.com
OPERATOR_EMAILS=control@your-gmail.com

# Firo Core node — running on host machine
# On Linux use host gateway IP:
FIRO_RPC_HOST=172.17.0.1
# On Mac/Windows use:
# FIRO_RPC_HOST=host.docker.internal
FIRO_RPC_USER=your_rpc_user
FIRO_RPC_PASSWORD=your_rpc_password
```

> ⚠️ **Important:** FiroGate runs inside Docker but your Firo Core node runs on the host. Use `172.17.0.1` (Linux) or `host.docker.internal` (Mac/Windows) as `FIRO_RPC_HOST` — not `127.0.0.1`.

Also update `~/.firo/firo.conf` to allow Docker connections:

```ini
rpcallowip=127.0.0.1
rpcallowip=172.17.0.0/16
```

### 2. Start

```bash
docker compose up -d
```

FiroGate runs on `http://localhost:8000` — exposed only to localhost.

### 3. Check logs

```bash
docker compose logs -f firogate
```

### 4. Health check

```bash
curl http://localhost:8000/api/health
# {"status":"ok","edition":"community"}
```

### 5. Update

```bash
git pull
docker compose up -d --build
```

### 6. Stop

```bash
docker compose down
```

### Persistent Data

Docker volumes keep your data safe across restarts:

| Volume | Contains |
|--------|---------|
| `firogate_data` | SQLite database — all payments, merchants, keys |
| `firogate_logs` | Application logs |

To back up:

```bash
docker cp firogate:/app/data/gateway.db ./backup-$(date +%Y%m%d).db
```

### Production with nginx

FiroGate only listens on `127.0.0.1:8000`. Put nginx in front for HTTPS:

```nginx
server {
    listen 443 ssl http2;
    server_name pay.yourdomain.com;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }

    # SSE — disable buffering for realtime updates
    location /api/events/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 1800s;
        add_header         X-Accel-Buffering no;
    }
}
```
