# Running on Proxmox

bunch and its gateway are light (well under 1 GB RAM together) and need no GPU, because the models run on your Ollama machines. Two good options:

## Option A: Debian LXC with Docker (lightest)

1. **Create the container** (web UI or shell on the Proxmox host):
   - Template: Debian 12 standard
   - Unprivileged: yes
   - Resources: 2 cores, 2048 MB RAM, 512 MB swap, 8 GB disk
   - Network: bridge `vmbr0`, DHCP. Then give it a fixed address with a DHCP reservation on your router.
   - Options → Features: enable **nesting** and **keyctl** (Docker needs both in an unprivileged LXC)

   From the Proxmox host shell, the features step is:
   ```bash
   pct set <CTID> --features nesting=1,keyctl=1
   ```

2. **Install Docker** inside the container:
   ```bash
   apt update && apt install -y ca-certificates curl git
   curl -fsSL https://get.docker.com | sh
   docker run --rm hello-world
   ```

3. **Deploy** following the README's Quick start. Set `BIND_ADDR` in `.env` to the container's LAN IP.

4. **Start on boot:** Options → Start at boot = yes. The compose services use `restart: unless-stopped`, so they come back with the container.

## Option B: a small VM

Use this if you'd rather not run Docker inside an LXC. A Debian 12 VM with 2 vCPU, 2 GB RAM and a 16 GB disk is plenty. Install Docker the same way.

## Making Ollama reachable from bunch

By default, Ollama listens only on its own machine. On each Ollama host:

```bash
sudo systemctl edit ollama
```

Add these lines:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

Then restart Ollama and test it from the bunch server:

```bash
sudo systemctl restart ollama
curl http://<ollama-host>:11434/api/tags     # run this from the bunch server
```

Ollama has no authentication. Limit who can reach it: allow only the bunch server's IP with your firewall (Proxmox firewall, `ufw allow from <bunch-server-ip> to any port 11434`, or your router).

If Ollama runs **in a Proxmox VM or LXC with GPU passthrough**, the same applies inside that guest.

## Backups

- Proxmox's own backup (vzdump) of the container or VM covers everything, including bunch's database (in the Docker volume `bunch_bunch-data`).
- For a file-level copy of just the chat history:
  ```bash
  docker compose exec bunch python -c "import sqlite3; s=sqlite3.connect('/data/bunch.db'); s.backup(sqlite3.connect('/data/backup.db'))"
  docker compose cp bunch:/data/backup.db ./bunch-backup.db
  ```

## Updating

```bash
cd bunch && git pull
docker compose pull litellm
docker compose up -d --build
```

## Keeping your homelab repo and this repo separate

A pattern that keeps this repo public and your details private:

```
homelab/ (private)                     bunch/ (public)
  hosts/bunch/bunch.env    ──copy──►   .env
  hosts/bunch/litellm.yaml ──copy──►   litellm/config.yaml
```

Encrypt the private copies if your homelab repo is hosted anywhere (sops + age works well), and deploy with a small script or Ansible task that copies them into place before running `docker compose up -d`.
