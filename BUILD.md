# Build & Run

## Requirements

* Docker
* A Firo Core node with JSON-RPC enabled
* A Spark-compatible wallet capable of exporting a View Key

## Install Docker (if you don't have it)

Linux:
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # then log out and back in
```

Mac / Windows: install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and make sure it's running.

Confirm it's working:
```bash
docker --version
docker compose version
```

## Configure

```bash
git clone https://github.com/firogate/firogate-ce.git
cd firogate-ce
cp .env.example .env
```

Fill in the values in `.env`. See `.env.example` for the full list of variables.

## Run

```bash
docker compose up -d      # build + start, backgrounded
docker compose logs -f    # follow logs
docker compose stop       # stop, keep containers/volumes
docker compose down       # stop and remove containers (volumes are kept)
```

The app is reachable at `http://127.0.0.1:8000` once healthy (`docker compose ps` shows `healthy`, usually within ~15s).

### Changing the port

Edit the `ports:` line in `docker-compose.yml`, left side is the host port:

```yaml
    ports:
      - "127.0.0.1:8000:8000"   # change 8000 (left) to e.g. 8080 to use http://127.0.0.1:8080
```

```bash
docker compose up -d      # re-apply after editing
```

Running a second FiroGate instance on the same machine (e.g. this project alongside another one)? Give it a different host port *and* a different `container_name` in `docker-compose.yml` otherwise the second `docker compose up -d` fails with a name conflict.

### Where the database lives

By default `docker-compose.yml` stores `/app/data` (containing `gateway.db`) in a **Docker-managed named volume** (`firogate_data`), not directly in this project's `./data/` folder on your host the two are separate even though the folder looks the same.

To always use `./data/gateway.db` on the host instead (easier to back up or inspect directly), replace the volume line in `docker-compose.yml`:

```yaml
    volumes:
      - ./data:/app/data        # was: firogate_data:/app/data
      - firogate_logs:/app/logs
```

```bash
docker compose up -d      # re-apply after editing
```

### Connecting to your Firo node

FiroGate needs a Firo Core node (`firod`) with JSON-RPC enabled. It does not run inside this container — point `.env` at wherever your node actually is.

**`FIRO_RPC_HOST`** — `127.0.0.1` (the default if left blank) only works when both the node and FiroGate run on the same machine *outside* Docker. Once FiroGate is containerized, `127.0.0.1` means the container itself, not your host — the RPC connection will fail. Use instead:
* Node running on the Docker host → `FIRO_RPC_HOST=host.docker.internal` (Linux: uncomment the `extra_hosts:` block already in `docker-compose.yml` — Docker Desktop on Mac/Windows already resolves this automatically, nothing to uncomment)
* Node running in its own container on the same Docker network → `FIRO_RPC_HOST=<that container's service name>`
* **Linux only** — simpler alternative: uncomment `network_mode: host` in `docker-compose.yml` instead (and remove the `ports:` block, it's ignored in host mode), leave `FIRO_RPC_HOST=127.0.0.1`. The container then shares the host's network stack directly. Not supported the same way on Docker Desktop for Mac/Windows (it runs Linux in a VM, so "host" there means the VM, not your actual machine) — use `host.docker.internal` there instead.

**`FIRO_RPC_PORT`** — set explicitly in `.env`:
```bash
FIRO_RPC_PORT=8888    # mainnet
FIRO_RPC_PORT=18888   # testnet
```

**`FIRO_RPC_USER`** / **`FIRO_RPC_PASSWORD`** — match `rpcuser=`/`rpcpassword=` in your node's `firo.conf`.

Your node's `firo.conf` also needs to actually accept connections from FiroGate (by default `firod` only allows RPC from `127.0.0.1`):

```
rpcallowip=172.16.0.0/12
```

That covers Docker's default bridge network range — narrow it to FiroGate's actual container IP/subnet where possible instead of leaving it wide open. Restart `firod` after editing `firo.conf`.

## Updating

```bash
git pull
docker compose up -d --build
```

## Documentation

* [README.md](README.md) — overview, architecture, API
* [SECURITY.md](SECURITY.md) — security policy
* [AUDIT.md](AUDIT.md) — trust model and security boundaries
* `/docs` (in-app) — full API reference
