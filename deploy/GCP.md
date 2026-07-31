# Provisioning PMB on Google Cloud (Compute Engine)

GCP-specific provisioning only. Once the VM exists and you can SSH in, the
install is **identical** to [MIGRATION.md](MIGRATION.md) Steps 2-9 — Ubuntu is
Ubuntu. This file covers what GCP does differently: free-tier constraints, SSH
key format, OS Login, ephemeral IPs, and swap for the 1 GB e2-micro.

---

## Part A — in the GCP Console (you must do this; it needs billing)

### A1. Project + billing + API
1. Console → project picker → **New Project** (e.g. `pmbot`).
2. **Billing** → link a billing account. Required even for free-tier resources.
3. **Compute Engine API** → Enable (first VM creation prompts this anyway).

### A2. Create the instance
Compute Engine → **VM instances** → **Create instance**:

| Field | Value | Why |
|---|---|---|
| Name | `pmbot` | |
| Region | **us-west1**, **us-central1**, or **us-east1** | Always Free e2-micro exists ONLY in these three |
| Machine type | **e2-micro** (E2 series) | Always Free tier: 1 per month |
| Boot disk → OS | **Ubuntu** / **Ubuntu 24.04 LTS (x86-64)** | Default is Debian — you must change it |
| Boot disk → type | **Standard persistent disk** | ⚠️ Free tier covers 30 GB-months of *standard* PD only. The default *Balanced* disk is NOT free |
| Boot disk → size | **30 GB** | Max that stays free; 10 GB also fine |
| Firewall | leave both boxes **unchecked** | We open nothing. Default VPC already allows SSH/22; the dashboard stays on localhost |

Free-tier egress is 1 GB/month to North America — the bot polls JSON APIs and
uses a tiny fraction of that.

### A3. Add your SSH key (bootstrap access only)
Still on the create page: **Advanced options → Security → Manage Access →
Add item**, and paste your public key in GCP's `username:key` form:

```
pmbot:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJxYxDjM8/00dnzprRN7Ny4vaqtkuZVjcrAi3jgYPInx vobic-pmbot-vps
```

> ⚠️ **You will probably NOT get the username you typed.** GCP's guest agent
> derives Linux accounts from your Google identity and the key comment, not
> reliably from the `username:` prefix. This VM ended up with accounts named
> `veestudios1` (from the billing account's gmail) and `vobic-pmbot-vps` (from
> the key's trailing comment) — never the requested prefix. Treat whatever you
> land in as **temporary bootstrap access** and move to a real account in A6.

> ⚠️ **Do NOT enable OS Login.** If `enable-oslogin=TRUE` is set (project- or
> instance-level), GCP **ignores** this metadata key entirely and your key won't
> work. The default is off; leave it off.

Then click **Create**.

### A4. (Optional but recommended) Reserve a static IP
The external IP is **ephemeral** — it changes if you ever stop/start the VM,
which would break your SSH config and tunnel. VPC network → **IP addresses** →
find the `pmbot` row → **Reserve**. Free while attached to a running VM.

### A5. First connect (accepts the host fingerprint)
From your Windows machine, using whichever account GCP actually created (see
the A3 warning — try your gmail localpart first, then the key comment):
```bash
ssh YOUR_GCP_DERIVED_USER@YOUR_EXTERNAL_IP
```
Type `yes` at the fingerprint prompt. If it hangs or refuses, see
Troubleshooting below. `getent passwd | tail` on the box lists what exists.

### A6. Create a stable admin account (do this immediately)
The guest-agent-managed accounts from A3 are **not durable**: the agent
rewrites their `~/.ssh/authorized_keys` from instance metadata, so anything you
add by hand there gets scrubbed, and the account itself can change name or
vanish if the metadata changes. Every later step — and every future SSH — should
use an account GCP does not manage.

From the bootstrap session:
```bash
sudo adduser --disabled-password --gecos "" pmbadmin
sudo install -d -m 700 -o pmbadmin -g pmbadmin /home/pmbadmin/.ssh
sudo tee /home/pmbadmin/.ssh/authorized_keys >/dev/null <<'KEY'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJxYxDjM8/00dnzprRN7Ny4vaqtkuZVjcrAi3jgYPInx vobic-pmbot-vps
KEY
sudo chmod 600 /home/pmbadmin/.ssh/authorized_keys
sudo chown pmbadmin:pmbadmin /home/pmbadmin/.ssh/authorized_keys
echo 'pmbadmin ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/pmbadmin
sudo chmod 440 /etc/sudoers.d/pmbadmin
```

Verify from a **second terminal** before closing the bootstrap one — locking
yourself out here means rebuilding the VM:
```bash
ssh pmbadmin@YOUR_EXTERNAL_IP "sudo whoami"    # must print: root
```

`pmbadmin` is the login for everything below and for day-to-day operation.

---

## Part B — install PMB (identical to MIGRATION.md, plus swap)

Run [MIGRATION.md](MIGRATION.md) **Steps 2 through 9** verbatim, with one GCP
addition first.

### B0. Add swap (do this before Step 2)
e2-micro has 1 GB RAM and **no swap by default**. pandas' import footprint plus
the `pip install` peak can push it close, and the Linux OOM killer would take
out the bot with no warning. 1 GB of swap removes the risk for ~1 GB of disk:

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # persist across reboot
free -h                                                       # verify: Swap 1.0Gi
```

Also pin the timezone so journal timestamps are predictable:
```bash
sudo timedatectl set-timezone UTC
```

### B1-B8. The rest
Follow MIGRATION.md Steps 2-9 exactly as written:
2. `apt install python3 python3-venv python3-pip git` + create the `pmbot` user
3. Clone → `git checkout feat/exit-only-leaders`
4. venv + `pip install -e ".[dev,dashboard]"`
5. `scp` your `.env` and `pmbot.db` across
6. Symlink the systemd units
7. `systemctl enable --now` both services
8. Verify (`status`, `journalctl`, `curl` the dashboard)
9. Only then: disable the Windows scheduled tasks

The `scp` in Step 5 uses the A6 account:
```powershell
scp .env     pmbadmin@YOUR_EXTERNAL_IP:/tmp/pmbot.env
scp pmbot.db pmbadmin@YOUR_EXTERNAL_IP:/tmp/pmbot.db
```

> Note the two accounts have different jobs and neither is the other:
> **`pmbadmin`** is who you log in as. **`pmbot`** is the unprivileged service
> account created in MIGRATION.md Step 2 that owns `/opt/pmbot` and runs the
> units — it has no login key. Since `/opt/pmbot` is `pmbot`-owned, git and
> python commands there run as `sudo -u pmbot …`; plain `cd /opt/pmbot/...` from
> `pmbadmin` gets Permission denied, and bare `sudo git` trips git's
> `dubious ownership` guard.

### Dashboard access
Unchanged — bound to localhost, reached by tunnel, nothing exposed publicly:
```bash
ssh -N -L 8090:localhost:8090 pmbadmin@YOUR_EXTERNAL_IP
```
Then open <http://localhost:8090>.

---

## GCP troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Permission denied (publickey)` as `pmbadmin` | The A6 account or its key is gone. Get back in via the A3 bootstrap account (or `gcloud compute ssh`) and redo A6 |
| `Permission denied (publickey)` on first connect | OS Login is enabled → disable it, or use `gcloud compute ssh pmbot` instead. Also check you're using the account GCP actually derived, not the `username:` prefix you typed (A3) |
| SSH key you added by hand stopped working | You added it to a guest-agent-managed account; the agent rewrites those `authorized_keys` from metadata. Use the unmanaged `pmbadmin` account (A6) |
| `fatal: detected dubious ownership` from git | You ran `sudo git` in `/opt/pmbot/...`, which `pmbot` owns. Use `sudo -u pmbot git -C /opt/pmbot/prediction-market-copy-bot …` |
| SSH times out | The VM has no external IP, or the default `allow-ssh` firewall rule was deleted. Check VPC → Firewall for a rule allowing tcp:22 |
| IP changed after a reboot | Ephemeral IP — reserve a static one (A4) |
| Bot dies silently, `journalctl` shows nothing | Likely OOM. Confirm with `dmesg -T \| grep -i oom`; add swap (B0) |
| Surprise bill | Check disk type is **Standard** not Balanced, machine is **e2-micro**, and region is one of the three free ones. Set a Budget alert at $1 |

## Notes
- **Free tier is one e2-micro per billing account per month**, not per project —
  a second instance elsewhere makes both partly billable.
- **`gcloud compute ssh pmbot --zone YOUR_ZONE`** is an alternative to raw `ssh`
  that manages keys for you; handy if the metadata-key route gives you trouble.
- **Stopped VMs still bill for the disk**, only the free 30 GB is covered.
- Everything else (one-instance rule, backups, going live later) is as in
  MIGRATION.md.
