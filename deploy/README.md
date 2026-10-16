# Deploying anywhere in India

Cloud-agnostic by construction: the application needs only (1) a container runtime or Kubernetes, (2) PostgreSQL, (3) one shared
file volume, (4) an HTTPS ingress, (5) a secrets store. Nothing in the code calls a cloud-vendor API.

| Need | AWS | Azure | GCP | Oracle Cloud | On-prem / any Indian host |
|---|---|---|---|---|---|
| Indian regions (`DEPLOY_REGION`) | `ap-south-1` Mumbai, `ap-south-2` Hyderabad | `centralindia`, `southindia`, `westindia` | `asia-south1` Mumbai, `asia-south2` Delhi | `ap-mumbai-1`, `ap-hyderabad-1` | `on-prem-india` |
| Kubernetes | EKS | AKS | GKE | OKE | any conformant distribution |
| PostgreSQL 15+ | RDS / Aurora PG | Database for PostgreSQL – Flexible | Cloud SQL / AlloyDB | OCI Database with PostgreSQL | self-managed + Patroni/pgBackRest |
| Shared RWX volume (exports, feed landing zone) | EFS | Azure Files (NFS) | Filestore | File Storage | NFS / CephFS |
| Encryption keys | KMS (in-region key) | Key Vault | Cloud KMS | Vault | HSM / Vault |
| Secrets → `cashapp-secrets` | Secrets Manager + External Secrets | Key Vault + CSI | Secret Manager + External Secrets | Vault | Vault / sealed-secrets |

(Region labels listed from memory; confirm against your provider before relying on them. Any other label is rejected by strict mode.)

## Steps
1. Provision PostgreSQL, the RWX volume and the key material **in an Indian region**; keep backups and any DR replica in India (e.g. Mumbai ↔ Hyderabad). Turn off any default cross-border replication.
2. Build and push the two images to a registry you control: `backend/Dockerfile` (API **and** worker) and `frontend/Dockerfile`.
3. Create `cashapp-secrets` with `DATABASE_URL` (`sslmode=require`), `JWT_SECRET` (≥ 32 random bytes), and any `*_PASSWORD` / `*_TOKEN` / `*_SECRET` referenced by mailbox, bank-feed and webhook configuration. These are referenced **by environment-variable name**; the app never stores them.
4. Edit `deploy/k8s/configmap.yaml` (`DEPLOY_REGION`, `ALLOWED_EGRESS_HOSTS`, `FX_FEED_URL`, `CORS_ORIGINS`), `ingress.yaml` (host, class, TLS) and `networkpolicy.yaml` (replace the PLACEHOLDER CIDRs).
5. `kubectl apply -k deploy/k8s`. The API's init container runs `python -m app.cli init-db`; create the first admin with `kubectl exec deploy/api -- python -m app.cli create-user you@corp.in --name "You" --role admin`.
6. Verify: `/health` reports `residency_strict: true` and your region; Connections → Data residency is green; run **Fetch now** on the FX tab once and compare with fbil.org.in.

Docker Compose (`docker-compose.yml`) is provided for a single VM; it runs api + worker + web + Postgres with the same strict settings.

## What is and isn't verified
- `backend/tests/test_deploy_manifests.py` lints the manifests (hardened pod settings, probes, no secrets, default-deny egress, no `0.0.0.0/0`).
- **Nothing here has been applied to a real cluster**, and no Terraform/Helm is provided: cloud-specific IaC would be unverifiable here and a wrong guess is worse than none. Treat the manifests as a reviewed starting point.
- **Schema migrations are not implemented.** `init-db` creates missing tables only; it will not alter existing ones. Add Alembic before the first production schema change.
- Exports and feed files use a shared volume; there is no object-storage backend yet.
- The standard NetworkPolicy cannot match hostnames; use FQDN-capable policies if your CNI offers them.
