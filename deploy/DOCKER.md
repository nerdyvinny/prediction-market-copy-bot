# Running PMB with Docker Compose

The containerized alternative to the systemd setup in [MIGRATION.md](MIGRATION.md).
Same two services (paper loop + dashboard), same one-instance rule, same
loopback-only dashboard — but the whole thing runs identically on any host with
Docker (your VPS, a Pi, or your Windows desktop via Docker Desktop). Files live
at the repo root: `Dockerfile`, `.dockerignore`, `docker-compose.yml`.

## Prerequisites
- Docker Engine + the Compose plugin. On Ubuntu:
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```
  `docker compose` (v2, space, not the old `docker-compose`) is what these
  commands assume.

## 1. Get the code + config onto the host
```bash
git clone https://github.com/nerdyvinny/prediction-market-copy-bot.git
cd prediction-market-copy-bot
git checkout feat/exit-only-leaders          # newest work is NOT on main
```
Then put your **`.env`** next to `docker-compose.yml` (it's gitignored, so it
doesn't travel with the clone — `scp` it from your Windows box). Keep
`PMBOT_MODE=paper`. The compose file bind-mounts it read-only into the
container, so config loads exactly as it does today.

## 2. Build + start
```bash
docker compose up -d --build
```
`paper` builds the `pmbot:local` image; `dashboard` reuses it. Both come up with
`restart: unless-stopped`, so they survive crashes and host reboots.

## 3. (Optional) Seed the paper history
A fresh named volume starts with an empty DB — the bot will create one on first
run. To carry over your existing `pmbot.db` instead, copy it into the running
`paper` container's volume, then restart so every process reopens it cleanly:
```bash
docker compose cp ./pmbot.db paper:/data/pmbot.db
docker compose restart
```
Skip this if you'd rather start paper trading from a clean slate on the new host.

## 4. Verify
```bash
docker compose ps                 # both services "running"
docker compose logs -f paper      # ~10s heartbeat from the loop
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/   # expect 200
```

### Reach the dashboard from your laptop
It's published only on the host's loopback, so tunnel in — nothing is exposed
publicly:
```bash
ssh -N -L 8090:localhost:8090 you@YOUR_VPS_IP
```
Then open <http://localhost:8090>.

## 5. Decommission the other runner
Only one instance may be live. If you're moving off Windows, disable the
scheduled tasks (see [MIGRATION.md](MIGRATION.md) Step 8). If you're switching
from the systemd units to Docker on the same box:
```bash
sudo systemctl disable --now pmbot-paper pmbot-dashboard
```

---

## Day-2 cheat-sheet
| Action | Command |
|---|---|
| Tail the loop | `docker compose logs -f paper` |
| Restart after an `.env` change | `docker compose restart` |
| Deploy new code | `git pull && docker compose up -d --build` |
| Stop (DB persists) | `docker compose down` |
| Stop **and wipe the DB volume** | `docker compose down -v` ⚠️ deletes paper history |
| Open a shell in the container | `docker compose exec paper bash` |
| Inspect the DB volume | `docker volume inspect prediction-market-copy-bot_pmbot-data` |
| Back up the DB | `docker compose cp paper:/data/pmbot.db ./pmbot-backup-$(date +%F).db` |

## Notes
- **One instance only.** Don't run compose while the systemd units or Windows
  tasks are also live — same `pmbot.db` race, now across hosts.
- **DB permissions** are handled by the image: `/data` is created owned by the
  container's `appuser`, and a fresh named volume inherits that ownership, so
  the non-root process can write without any entrypoint chown step.
- **Going live later** is still just config: rebuild with the `live` extra,
  uncomment CLOB credentials in `.env`, flip `PMBOT_MODE`, `docker compose up -d
  --build`. Treat it as a deliberate, reviewed change.
- **Volume name** is `<project>_pmbot-data`, where `<project>` defaults to the
  directory name (`prediction-market-copy-bot`). Set `name:` in compose or the
  `-p` flag to override.
