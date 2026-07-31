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

### A3. Add your SSH key (GCP's format is special)
Still on the create page: **Advanced options → Security → Manage Access →
Add item**, and paste **exactly** this:

```
vinny:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJxYxDjM8/00dnzprRN7Ny4vaqtkuZVjcrAi3jgYPInx vobic-pmbot-vps
```

GCP derives the Linux username from the `username:` prefix — that is what makes
your login `vinny`. Change the prefix if you want a different account name.

> ⚠️ **Do NOT enable OS Login.** If `enable-oslogin=TRUE` is set (project- or
> instance-level), GCP **ignores** this metadata key and your key won't work.
> The default is off; leave it off.

Then click **Create**.

### A4. (Optional but recommended) Reserve a static IP
The external IP is **ephemeral** — it changes if you ever stop/start the VM,
which would break your SSH config and tunnel. VPC network → **IP addresses** →
find the `pmbot` row → **Reserve**. Free while attached to a running VM.

### A5. First connect (accepts the host fingerprint)
From your Windows machine:
```bash
ssh vinny@YOUR_EXTERNAL_IP
```
Type `yes` at the fingerprint prompt. If it hangs or refuses, see
Troubleshooting below.

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

The `scp` in Step 5 uses your GCP username:
```powershell
scp .env     vinny@YOUR_EXTERNAL_IP:/tmp/pmbot.env
scp pmbot.db vinny@YOUR_EXTERNAL_IP:/tmp/pmbot.db
```

### Dashboard access
Unchanged — bound to localhost, reached by tunnel, nothing exposed publicly:
```bash
ssh -N -L 8090:localhost:8090 vinny@YOUR_EXTERNAL_IP
```
Then open <http://localhost:8090>.

---

## GCP troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Permission denied (publickey)` | OS Login is enabled → disable it, or use `gcloud compute ssh pmbot` instead. Also check the metadata key kept its `vinny:` prefix |
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
