"""Static checks on deploy/. These manifests have NOT been applied to any cluster (no cluster, kubeconform or helm in the build
environment); the tests only guard the security-relevant properties from regressing."""
import glob
import os

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "k8s")


def docs():
    out = []
    for f in sorted(glob.glob(os.path.join(ROOT, "*.yaml"))):
        out += [(os.path.basename(f), d) for d in yaml.safe_load_all(open(f)) if d]
    return out


def of(kind):
    return [(f, d) for f, d in docs() if d["kind"] == kind]


def test_every_document_is_valid_and_kustomization_lists_existing_files():
    k = yaml.safe_load(open(os.path.join(ROOT, "kustomization.yaml")))
    for r in k["resources"]:
        assert os.path.exists(os.path.join(ROOT, r)), r
    assert all("apiVersion" in d and "kind" in d and "metadata" in d for _, d in docs() if d["kind"] != "Kustomization")


def test_workloads_are_hardened():
    deps = of("Deployment")
    assert {d["metadata"]["name"] for _, d in deps} == {"api", "worker", "web"}
    for _, d in deps:
        spec = d["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True and spec["automountServiceAccountToken"] is False
        for c in spec["containers"] + spec.get("initContainers", []):
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True and sc["capabilities"]["drop"] == ["ALL"]
            assert c["resources"]["limits"]["memory"] and c["resources"]["requests"]["cpu"]
            assert not c["image"].endswith(":latest")


def test_api_has_probes_and_worker_runs_the_job_runner():
    api = next(d for _, d in of("Deployment") if d["metadata"]["name"] == "api")["spec"]["template"]["spec"]["containers"][0]
    assert api["readinessProbe"]["httpGet"]["path"] == "/health/ready" and api["livenessProbe"]["httpGet"]["path"] == "/health"
    w = next(d for _, d in of("Deployment") if d["metadata"]["name"] == "worker")["spec"]["template"]["spec"]["containers"][0]
    assert w["command"] == ["python", "-m", "app.cli", "worker"]


def test_no_secrets_in_manifests_and_residency_is_strict():
    cm = of("ConfigMap")[0][1]["data"]
    assert cm["DATA_RESIDENCY_STRICT"] == "1" and cm["ALLOW_DEMO"] == "0" and cm["APP_ENV"] == "prod"
    text = "".join(open(f).read() for f in glob.glob(os.path.join(ROOT, "*.yaml")))
    for bad in ("JWT_SECRET:", "PASSWORD", "password:", "BEGIN PRIVATE KEY"):
        assert bad not in text, bad
    assert all(not any(k in cm for k in ("JWT_SECRET", "DATABASE_URL")) for _ in [0])


def test_network_policy_is_default_deny_and_never_allows_the_whole_internet():
    nps = {d["metadata"]["name"]: d for _, d in of("NetworkPolicy")}
    assert nps["default-deny-all"]["spec"]["podSelector"] == {} and set(nps["default-deny-all"]["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    text = open(os.path.join(ROOT, "networkpolicy.yaml")).read()
    assert "0.0.0.0/0" not in text and "::/0" not in text
    assert nps["backend-egress-allowlist"]["spec"]["podSelector"]["matchLabels"] == {"tier": "backend"}


def test_dockerfiles_run_as_non_root():
    be = open(os.path.join(ROOT, "..", "..", "backend", "Dockerfile")).read()
    assert "USER 10001" in be and "app.cli worker" in be
    assert "nginx-unprivileged" in open(os.path.join(ROOT, "..", "..", "frontend", "Dockerfile")).read()


def test_default_deployment_uses_object_storage_not_a_shared_volume():
    cm = of("ConfigMap")[0][1]["data"]
    assert cm["STORAGE_BACKEND"] == "s3" and cm["S3_BUCKET"] == "REPLACE-ME", "must fail loudly until the operator sets a real bucket"
    assert cm["S3_SSE"] in ("aws:kms", "AES256") and cm["S3_REGION"]
    assert not of("PersistentVolumeClaim")
    for _, d in of("Deployment"):
        spec = d["spec"]["template"]["spec"]
        assert not any("persistentVolumeClaim" in v for v in spec.get("volumes", []))
    opt = open(os.path.join(ROOT, "..", "k8s-optional-shared-volume.yaml")).read()
    assert "ReadWriteMany" in opt
    assert not any(k in cm for k in ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID")), "object-storage credentials come from the pod identity, never a manifest"


# ------------------------------------------------------------------------------------------------ single-VM docker compose
import re  # noqa: E402
import subprocess  # noqa: E402

VM = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "vm")


def compose():
    return yaml.safe_load(open(os.path.join(VM, "docker-compose.yml")))["services"]


def test_vm_only_caddy_publishes_ports_and_the_database_is_never_exposed():
    svcs = compose()
    published = {n: s["ports"] for n, s in svcs.items() if s.get("ports")}
    assert published == {"caddy": ["80:80", "443:443"]}
    assert "ports" not in svcs["db"] and svcs["db"]["networks"] == ["internal"]
    nets = yaml.safe_load(open(os.path.join(VM, "docker-compose.yml")))["networks"]
    assert nets["internal"]["internal"] is True, "db/web/backup have no route to the internet"
    assert set(svcs["caddy"]["networks"]) == {"internal", "public"} and "egress" not in svcs["db"]["networks"] + svcs["backup"]["networks"]


def test_vm_app_services_are_hardened_and_have_health_gates():
    svcs = compose()
    for n in ("api", "worker", "web"):
        s = svcs[n]
        assert s["read_only"] is True and s["cap_drop"] == ["ALL"] and "no-new-privileges:true" in s["security_opt"] and s["restart"] == "unless-stopped", n
    assert "healthcheck" in svcs["db"] and "healthcheck" in svcs["api"] and "/health/ready" in svcs["api"]["healthcheck"]["test"][-1]
    assert svcs["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert svcs["api"]["depends_on"]["db"]["condition"] == "service_healthy"
    env = svcs["api"]["environment"]
    assert env["APP_ENV"] == "prod" and env["ALLOW_DEMO"] == "0" and env["AUTO_MIGRATE"] == "0" and env["DATA_RESIDENCY_STRICT"] == "1"
    assert env["CORS_ORIGINS"].startswith("https://") and svcs["worker"]["command"] == ["python", "-m", "app.cli", "worker"]
    assert "logging" in svcs["api"] and svcs["api"]["logging"]["options"]["max-size"]


def test_vm_every_compose_variable_is_documented_and_secrets_ship_empty():
    text = open(os.path.join(VM, "docker-compose.yml")).read()
    used = set(re.findall(r"\$\{([A-Z_]+)[:?-]", text))
    lines = [ln for ln in open(os.path.join(VM, ".env.example")).read().splitlines() if ln.strip() and not ln.startswith("#")]
    env = dict(ln.split("=", 1) for ln in lines)
    assert used <= set(env), used - set(env)
    for k in ("POSTGRES_PASSWORD", "JWT_SECRET", "BACKUP_PASSPHRASE"):
        assert env[k] == "", f"{k} must be generated by install.sh, never shipped"
    # compose reads "KEY=   # text" as the value "# text": a hand-copied file would then run with a comment as its secret
    assert all("#" not in v for v in env.values())


def test_vm_scripts_parse_and_the_proxy_sets_hsts():
    for f in ("install.sh", "ops.sh", "scripts/backup.sh", "scripts/restore.sh", "scripts/backup-loop.sh"):
        p = os.path.join(VM, f)
        assert os.access(p, os.X_OK), f
        assert subprocess.run(["bash", "-n", p], capture_output=True).returncode == 0, f
    c = open(os.path.join(VM, "caddy", "Caddyfile")).read()
    assert "Strict-Transport-Security" in c and "reverse_proxy web:8080" in c and "{$DOMAIN}" in c
    install = open(os.path.join(VM, "install.sh")).read()
    assert "chmod 600 .env" in install and "OFF THIS MACHINE" in install and "ADMIN_PASSWORD" in install
