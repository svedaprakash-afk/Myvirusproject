# Deploying anywhere in India

Cloud-agnostic by construction: the application needs only (1) a container runtime or Kubernetes, (2) PostgreSQL, (3) an S3-compatible bucket (or one shared
file volume), (4) an HTTPS ingress, (5) a secrets store. Nothing in the code calls a cloud-vendor API.

| Need | AWS | Azure | GCP | Oracle Cloud | On-prem / any Indian host |
|---|---|---|---|---|---|
| Indian regions (`DEPLOY_REGION`) | `ap-south-1` Mumbai, `ap-south-2` Hyderabad | `centralindia`, `southindia`, `westindia` | `asia-south1` Mumbai, `asia-south2` Delhi | `ap-mumbai-1`, `ap-hyderabad-1` | `on-prem-india` |
| Kubernetes | EKS | AKS | GKE | OKE | any conformant distribution |
| PostgreSQL 15+ | RDS / Aurora PG | Database for PostgreSQL – Flexible | Cloud SQL / AlloyDB | OCI Database with PostgreSQL | self-managed + Patroni/pgBackRest |
| Object storage, S3 API (exports, bank-file landing zone) | S3 | **not S3-compatible** — use an S3 gateway (e.g. MinIO) or the local backend with Azure Files | GCS in S3-interoperability mode (HMAC keys) | Object Storage (S3 compatibility API) | MinIO / Ceph RGW / any S3-compatible store |
| Encryption keys | KMS (in-region key) | Key Vault | Cloud KMS | Vault | HSM / Vault |
| Secrets → `cashapp-secrets` | Secrets Manager + External Secrets | Key Vault + CSI | Secret Manager + External Secrets | Vault | Vault / sealed-secrets |

(Region labels and S3-compatibility claims are from memory; confirm against your provider before relying on them. Any other region label is rejected by strict mode.)

## Steps
1. Provision PostgreSQL, the object-storage bucket and the key material **in an Indian region**; keep backups and any DR replica in India (e.g. Mumbai ↔ Hyderabad). Turn off any default cross-border replication.
2. Build and push the two images to a registry you control: `backend/Dockerfile` (API **and** worker) and `frontend/Dockerfile`.
3. Object storage: create a bucket **in an Indian region** with *Block Public Access* on, versioning on, a TLS-only bucket policy, default encryption with an in-region KMS key, and (recommended for audit) Object Lock/WORM plus a lifecycle rule. Grant the pods' cloud identity only `s3:PutObject/GetObject/DeleteObject/ListBucket` on `cashapp/*` — no credentials in manifests. Set `S3_BUCKET`, `S3_REGION`, `S3_SSE`, `S3_KMS_KEY_ID`, and add the S3 host to `ALLOWED_EGRESS_HOSTS`. Verify with **Connections → Data residency → Test storage**.
4. Create `cashapp-secrets` with `DATABASE_URL` (`sslmode=require`), `JWT_SECRET` (≥ 32 random bytes), and any `*_PASSWORD` / `*_TOKEN` / `*_SECRET` referenced by mailbox, bank-feed and webhook configuration. These are referenced **by environment-variable name**; the app never stores them.
5. Edit `deploy/k8s/configmap.yaml` (`DEPLOY_REGION`, `ALLOWED_EGRESS_HOSTS`, `FX_FEED_URL`, `CORS_ORIGINS`), `ingress.yaml` (host, class, TLS) and `networkpolicy.yaml` (replace the PLACEHOLDER CIDRs).
6. `kubectl apply -k deploy/k8s`. The API's init container runs `python -m app.cli migrate`; create the first admin with `kubectl exec deploy/api -- python -m app.cli create-user you@corp.in --name "You" --role admin`.
7. Verify: `/health` reports `residency_strict: true` and your region; Connections → Data residency is green; run **Fetch now** on the FX tab once and compare with fbil.org.in.

Docker Compose (`docker-compose.yml`) is provided for a single VM; it runs api + worker + web + Postgres with the same strict settings.

## What is and isn't verified
- `backend/tests/test_deploy_manifests.py` lints the manifests (hardened pod settings, probes, no secrets, default-deny egress, no `0.0.0.0/0`).
- **Nothing here has been applied to a real cluster**, and no Terraform/Helm is provided: cloud-specific IaC would be unverifiable here and a wrong guess is worse than none. Treat the manifests as a reviewed starting point.
- **Migrations**: Alembic, run by the `migrate` init container (a Postgres advisory lock serialises replicas). In production the API refuses to start if the schema is not at the release's revision. Tested on PostgreSQL 16 and SQLite, including 4 concurrent migrators; never run against your managed PostgreSQL service. Always take a backup before upgrading; `downgrade` loses data.
- Object storage is tested against **moto** (an in-process S3 mock), not real AWS/Oracle/Ceph/MinIO; bucket policy, KMS permissions and network paths are yours to verify (use *Test storage*). The code does not configure Object Lock or lifecycle rules: do that on the bucket. Without object storage, `deploy/k8s-optional-shared-volume.yaml` shows the shared-volume alternative.
- The standard NetworkPolicy cannot match hostnames; use FQDN-capable policies if your CNI offers them.
