# Running this for real: one Linux VM with Docker Compose

Everything is in `deploy/vm/`. It runs: Caddy (HTTPS) → nginx (UI + `/api` proxy) → API → PostgreSQL 16, plus a worker and an encrypted nightly backup.

**What has and has not been tested.** The backup, retention and restore scripts, `create-user`, `create-entity`, `reset-password`, `doctor`, the production-mode API (docs off, lockout, change-password) and the compose file's syntax were all exercised against a real PostgreSQL 16. **The container images and the compose stack have never been run**: the build environment has no Docker daemon. Your first `./install.sh` is the first real run, so do it on a throwaway VM before the one you care about, and expect to fix something small.

## 1. What you need first (you said you have none of this yet)

| Item | Detail |
|---|---|
| VM | Ubuntu 22.04/24.04 LTS or similar, **in an Indian region** (the app refuses to start otherwise). 2 vCPU / 4 GB RAM / 40 GB disk is enough for a pilot; add disk if you import years of statements. OCR of scanned advices is the heaviest thing it does. |
| Firewall | Inbound **80 and 443 only** (80 is needed for the certificate challenge). SSH (22) only from your own IP, key-based, password login off. Nothing else, in particular **not 5432**. |
| Domain | An A record for e.g. `cash.yourcompany.in` pointing at the VM's public IP. For a short pilot with no domain, `<ip-with-dashes>.sslip.io` (e.g. `203-0-113-10.sslip.io`) resolves to that IP and Let's Encrypt will issue for it. |
| Docker | Install Docker Engine with the compose plugin from Docker's official apt instructions, add your user to the `docker` group, log out and in. Check `docker compose version`. |
| Backups off the machine | Decide where `/backups` is copied (a bucket in India, another VM). The stack does not do this for you; see section 5. |

## 2. Install

```bash
git clone <your repo> && cd <repo>/deploy/vm
git checkout claude/festive-pasteur-hypbgb      # or the branch you deploy from
./install.sh
```

It asks for the domain, region, administrator email and company, generates `POSTGRES_PASSWORD`, `JWT_SECRET` and `BACKUP_PASSPHRASE` into a `chmod 600` `.env`, builds, migrates, starts, creates the administrator with a random password **printed once**, creates the company, and runs `doctor`. **Save `BACKUP_PASSPHRASE` somewhere that is not this VM** when it tells you to: a backup you cannot decrypt is not a backup.

Then open `https://<domain>`, sign in, and use *Change password* in the header immediately.

## 3. First day

1. `./ops.sh doctor` should show no `FAIL`. Warnings you will see and should understand: *local passwords only (no MFA)* and *SQLite/other engine* (not applicable here).
2. The company starts in **suggest-only mode**: nothing posts by itself. Leave it so for the first weeks. Turn on straight-through in Settings only after reviewers have confirmed matches for a while and the *Model health* panel looks sane.
3. Create people with `./ops.sh create-user person@co.in --name "Name" --role ar_analyst` (roles: ar_analyst, ar_manager, treasury, finance_head, auditor, admin). Give finance_head and admin to as few people as possible. Policy: 12+ characters, no common passwords, five failed sign-ins lock the account for 15 minutes.
4. Import in this order (Import page): customers/open invoices, then bank statements. Use **synthetic or anonymised data** for the pilot, as agreed.
5. Add the bank accounts you did not enter at install (Connections).
6. **Test your ERP export against a test company** before trusting it. The Tally/SAP/Business Central/NetSuite renderers were written from published formats and have never been imported into a real ERP.
7. Run a **restore drill** now (section 5) so you know it works before you need it.

## 4. Routine operation

| Task | Command |
|---|---|
| Health, last good backup | `./ops.sh status` |
| Logs | `./ops.sh logs api` (or `worker`, `caddy`, `db`) |
| Add a user / reset a password | `./ops.sh create-user …` / `./ops.sh reset-password email` (signs their sessions out) |
| Upgrade | `./ops.sh upgrade` — backs up first, pulls, rebuilds, migrates, restarts, runs doctor |
| Uptime alert | Point any external monitor (UptimeRobot, a cron on another host) at `https://<domain>/health/ready`; it returns `{"ready":true}` when the API and database are fine. |
| OS patches | `unattended-upgrades` for the host; `docker compose pull && ./ops.sh upgrade` monthly for Postgres/Caddy base images. |
| Rotate `JWT_SECRET` | Edit `.env`, `docker compose up -d api worker`. Everyone is signed out. |
| Rotate `POSTGRES_PASSWORD` | `ALTER ROLE cashapp PASSWORD '…'` inside `./ops.sh shell-db`, then edit `.env`, then `docker compose up -d`. |

## 5. Backups and restore

* A backup runs at 20:00 UTC (01:30 IST) and once at first start: `pg_dump` plus the exports/feeds files, each AES-256 encrypted with your passphrase, with a SHA-256 manifest. 14 days are kept (`BACKUP_KEEP_DAYS`). They live in the `backups` Docker volume **on the same machine**.
* **That is not enough.** Copy them off the VM daily, e.g. `docker run --rm -v vm_backups:/b -v $PWD:/o alpine tar -C /b -cf - . | ssh backup-host 'cat > cashapp-$(date +%F).tar'`, or sync to an object store in India. They are encrypted, so the destination does not need to be trusted with the contents.
* **Drill** (touches nothing live): `./ops.sh drill` restores the latest backup into a throwaway database, prints row counts and drops it. Do it monthly and after every upgrade.
* **Restore for real**: `./ops.sh restore 20261017T142518Z` (asks you to type RESTORE; stops api/worker, replaces the database and files, migrates, restarts). If the VM is lost: new VM, `./install.sh`, then copy the backup into the `backups` volume and restore.
* Database dumps can be restored into a newer app version (the migration step runs after); not into an older one.

## 6. What this does not give you (be honest with yourself)

* **No high availability.** One VM, one database. If it dies you restore from backup: expect hours, and up to a day of lost data unless you copy backups more often.
* **No off-site backup by default** (section 5).
* **No MFA.** Local passwords only. SSO (OIDC) exists in the app and was tested only against a mock provider; run a real sign-in with your provider before enforcing it.
* **No WAF or intrusion detection.** Caddy gives you TLS and security headers; sign-in is rate-limited per account, not per IP.
* **Disk encryption** is up to the VM provider. Enable it. Database files and the `.env` are plaintext on that disk.
* **Compliance**: this is a pilot setup. It is not an audit-ready control environment for real receivables data until you have addressed the above and had it reviewed.
* **Outbound access**: the API and worker can reach the internet (FX rate fetch, SFTP, IMAP, ERP webhooks) but the app's own `ALLOWED_EGRESS_HOSTS` list blocks every host not named. If you set `ANTHROPIC_API_KEY`, remittance text leaves India.

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| Browser certificate error / site not loading | DNS points at the VM? Ports 80/443 open in the provider's firewall as well as the host? `./ops.sh logs caddy` shows the ACME error. Let's Encrypt rate-limits repeated failures: fix DNS first. |
| `api` unhealthy | `./ops.sh logs api`. Usual causes: migration failed (`docker compose run --rm migrate`), wrong `DEPLOY_REGION`, weak `JWT_SECRET`. |
| "Too many failed attempts" | Five bad passwords lock that account 15 minutes. Wait, or `reset-password` (does not clear the lock; waiting does). |
| A user's session ends suddenly | Their password was changed or reset: by design, all older sessions stop. |
| Disk filling | `docker system df`; the `backups` volume and `json-file` logs (capped at 5×10 MB per service) are the usual suspects. |
