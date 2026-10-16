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
