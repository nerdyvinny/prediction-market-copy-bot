# Migrating PMB to a Linux VPS (24/7, crash-proof)

Goal: run the paper loop + dashboard on an always-on Linux server under
`systemd`, so a desktop crash can never take the bot down. `systemd` also
replaces the two Windows workarounds in `scripts/run_*.ps1` (the orphaned
`cmd.exe` holding `pmbot.db` / port 8090, and the log-append-handle conflict) —
it owns the process tree and does real supervision, so neither bug can occur.

**Golden rule: run in exactly one place at a time.** The VPS and your Windows
desktop both copy-trading the same leaders would double-trade and diverge the
DBs. Disable the Windows tasks the moment the VPS is confirmed healthy (Step 8).

Paths below assume the repo lives at
`/opt/pmbot/prediction-market-copy-bot`. Adjust everywhere if you choose
another location — the two `.service` files hardcode it in three places each
(`WorkingDirectory`, `ExecStart`, `ReadWritePaths`).

---

## 0. Provision the server
Any small Linux VM works — the bot is tiny (1 vCPU / 1 GB is plenty).
- **Hetzner** CX22 (~€4/mo) or **DigitalOcean/Vultr** basic (~$6/mo), or
- **Oracle Cloud Always Free** ARM VM ($0). On ARM everything below is
  identical; wheels for pandas/pydantic are all available for aarch64.

Pick **Ubuntu 24.04 LTS** (or Debian 12). The rest of this guide assumes it.

## 1. Base packages + a dedicated user
SSH in as root (or a sudo user), then:

```bash
apt update && apt install -y python3 python3-venv python3-pip git
# Dedicated unprivileged service account — the units run as this user.
useradd --system --create-home --home-dir /opt/pmbot --shell /usr/sbin/nologin pmbot
```

`python3` on Ubuntu 24.04 is 3.12, which satisfies `requires-python >=3.11`.

## 2. Clone the repo on the correct branch
```bash
sudo -u pmbot git clone https://github.com/nerdyvinny/prediction-market-copy-bot.git \
  /opt/pmbot/prediction-market-copy-bot
cd /opt/pmbot/prediction-market-copy-bot
# Newest work is NOT on main:
sudo -u pmbot git checkout feat/exit-only-leaders
```

## 3. Create the venv + install
```bash
sudo -u pmbot python3 -m venv .venv
sudo -u pmbot .venv/bin/pip install --upgrade pip
sudo -u pmbot .venv/bin/pip install -e ".[dev,dashboard]"
```
This installs the `pmbot` console script and uvicorn/fastapi into
`.venv/bin/` — the exact paths the units reference. Do **not** install
`.[live]`; you're staying in paper mode.

## 4. Copy the two files git does NOT carry
`.env` is gitignored, and `pmbot.db` holds your paper history. From your
**Windows** machine (PowerShell), with the bot stopped there (see Step 8),
`scp` both across:

```powershell
scp .env         pmbot-admin@YOUR_VPS_IP:/tmp/pmbot.env
scp pmbot.db     pmbot-admin@YOUR_VPS_IP:/tmp/pmbot.db
```
(Use whatever sudo-capable login you created; `pmbot` itself has `nologin`.)

Then on the VPS, put them in place with correct ownership:
```bash
install -o pmbot -g pmbot -m 600 /tmp/pmbot.env /opt/pmbot/prediction-market-copy-bot/.env
install -o pmbot -g pmbot -m 644 /tmp/pmbot.db  /opt/pmbot/prediction-market-copy-bot/pmbot.db
rm /tmp/pmbot.env /tmp/pmbot.db
```

**Skip the `pmbot.db` copy** if you'd rather start paper trading from a clean
slate on the VPS — the bot will create a fresh DB on first run. Copying it
preserves the current `fills`/`positions`/`followed_leaders` state so the bot
resumes exactly where Windows left off.

Sanity-check `.env` against the template by key (it's gitignored, so drift is
easy):
```bash
diff <(grep -o '^[A-Z_]*' .env.example | sort) <(grep -o '^[A-Z_]*' .env | sort)
```
Keep `PMBOT_MODE=paper`. Live CLOB credential lines should stay commented out.

## 5. Install the systemd units
The unit files live in the repo at `deploy/systemd/`. Symlink them into place
(a symlink means `git pull` updates the units automatically):

```bash
ln -s /opt/pmbot/prediction-market-copy-bot/deploy/systemd/pmbot-paper.service     /etc/systemd/system/pmbot-paper.service
ln -s /opt/pmbot/prediction-market-copy-bot/deploy/systemd/pmbot-dashboard.service /etc/systemd/system/pmbot-dashboard.service
systemctl daemon-reload
```

## 6. Start + enable (enable = auto-start on boot)
```bash
systemctl enable --now pmbot-paper.service
systemctl enable --now pmbot-dashboard.service
```

## 7. Verify it's healthy
```bash
systemctl status pmbot-paper pmbot-dashboard --no-pager
journalctl -u pmbot-paper -f          # live loop output (Ctrl-C to detach)
```
You want `active (running)` and the paper loop printing its ~10s heartbeat.
Confirm the dashboard is answering locally on the box:
```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8090/   # expect 200
```

### Reach the dashboard from your laptop (SSH tunnel — no public port)
The dashboard is bound to 127.0.0.1 on purpose. Tunnel it:
```bash
ssh -N -L 8090:localhost:8090 pmbot-admin@YOUR_VPS_IP
```
Then open <http://localhost:8090> in your browser. Nothing is exposed to the
internet.

## 8. Decommission the Windows tasks (do this once the VPS is confirmed)
On the Windows machine, stop and disable both tasks so only one instance of the
bot is ever live:
```powershell
Stop-ScheduledTask   -TaskName PmbotPaperLoop
Stop-ScheduledTask   -TaskName PmbotDashboard
Disable-ScheduledTask -TaskName PmbotPaperLoop
Disable-ScheduledTask -TaskName PmbotDashboard
```
Then confirm no orphaned process is still holding `pmbot.db` (the known Windows
gotcha): `Get-Process pmbot,python -ErrorAction SilentlyContinue`. Kill any
survivors. Keep the disabled tasks around as a fallback until you trust the VPS;
delete them later with `Unregister-ScheduledTask` if you want.

---

## Day-2 operations cheat-sheet
| Action | Command |
|---|---|
| Tail the loop | `journalctl -u pmbot-paper -f` |
| Last 200 lines | `journalctl -u pmbot-paper -n 200 --no-pager` |
| Restart after a config/.env change | `systemctl restart pmbot-paper` |
| Deploy new code | `sudo -u pmbot git -C /opt/pmbot/prediction-market-copy-bot pull` then `systemctl restart pmbot-paper pmbot-dashboard` |
| New/updated deps after a pull | re-run the `pip install -e ".[dev,dashboard]"` from Step 3, then restart |
| Stop everything | `systemctl stop pmbot-paper pmbot-dashboard` |
| Check it survives reboot | `systemctl reboot`, then re-run Step 7 after it comes back |

## Notes / gotchas
- **One instance only.** If you ever run `pmbot paper` by hand on the VPS for
  debugging, `systemctl stop pmbot-paper` first — same DB race as on Windows.
- **Going live later** is a config change on the server, not a re-migration:
  install `.[live]`, uncomment the CLOB credentials in `.env`, flip
  `PMBOT_MODE`, `systemctl restart pmbot-paper`. Treat that as a deliberate,
  reviewed step.
- **Backups.** `pmbot.db` is your ledger. A daily copy is enough:
  `systemctl` timer or a one-line cron —
  `0 6 * * * cp /opt/pmbot/prediction-market-copy-bot/pmbot.db /opt/pmbot/backups/pmbot-$(date +\%F).db`
- **Time zone.** Set the box to a known TZ (`timedatectl set-timezone UTC`) so
  log timestamps and any date-based logic are predictable.
