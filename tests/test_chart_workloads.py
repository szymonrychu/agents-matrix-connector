"""Chart workloads: api and singleton bot Deployments, secrets wiring and the token file mount."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agents-matrix-connector"

DB_PASS = "".join(["db", "-", "fixture", "-", "value"])
ORCH = "".join(["orch", "-", "fixture", "-", "value"])
MX = "".join(["mx", "-", "fixture", "-", "value"])

BASE = {
    "image": {"tag": "1.2.3"},
    "postgres": {"url": "postgresql://" + "u:" + DB_PASS + "@db:5432/m"},
    "orchestrator": {"url": "http://orch.agents.svc", "token": ORCH},
    "matrix": {
        "homeserverUrl": "http://synapse.messaging.svc:8008",
        "userId": "@hermes:example.org",
        "accessToken": MX,
    },
}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _run(values: dict) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    assert helm, "helm binary not found on PATH; the chart tests require it"
    with tempfile.TemporaryDirectory() as tmp:
        vf = Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        return subprocess.run(
            [helm, "template", "mx", str(CHART), "-n", "agents", "-f", str(vf)],
            capture_output=True,
            text=True,
        )


def render(overrides: dict | None = None, *, base: dict | None = None) -> list[dict]:
    proc = _run(_merge(BASE if base is None else base, overrides or {}))
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def deployments(docs: list[dict]) -> dict[str, dict]:
    return {
        d["metadata"]["labels"]["app.kubernetes.io/component"]: d
        for d in docs
        if d["kind"] == "Deployment"
    }


def pod(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]


def main_container(dep: dict) -> dict:
    return pod(dep)["containers"][0]


def env_of(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def test_deployments_render():
    deps = deployments(render())
    assert set(deps) == {"api", "bot"}
    for name, dep in deps.items():
        assert dep["metadata"]["name"] == f"mx-agents-matrix-connector-{name}"
        c = main_container(dep)
        assert c["command"] == ["agents-matrix-connector"]
        assert c["args"] == [name]
        assert c["image"] == ("harbor.szymonrichert.pl/containers/agents-matrix-connector:1.2.3")
        assert c["ports"] == [{"name": "http", "containerPort": 8000, "protocol": "TCP"}]
        assert c["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": 8000}
        assert c["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 8000}
        assert c["envFrom"] == [{"configMapRef": {"name": "mx-agents-matrix-connector-settings"}}]
        init = pod(dep)["initContainers"][0]
        assert init["name"] == "migrate"
        assert init["command"] == ["agents-matrix-connector"]
        assert init["args"] == ["migrate"]
        assert init["image"] == c["image"]
        assert {"name": "tmp", "mountPath": "/tmp"} in c["volumeMounts"]
        assert {"name": "tmp", "emptyDir": {}} in pod(dep)["volumes"]
    assert deps["api"]["spec"]["replicas"] == 1
    api_env = env_of(main_container(deps["api"]))
    assert api_env["MATRIX_API_ALLOWED_CIDRS"]["value"] == "127.0.0.1/32"
    assert "MATRIX_API_ALLOWED_CIDRS" not in env_of(main_container(deps["bot"]))


def test_bot_singleton_strategy():
    deps = deployments(render({"components": {"api": {"replicas": 3}}}))
    assert deps["bot"]["spec"]["replicas"] == 1
    assert deps["bot"]["spec"]["strategy"] == {"type": "Recreate"}
    assert deps["api"]["spec"]["replicas"] == 3
    assert "strategy" not in deps["api"]["spec"]


def test_access_token_file_only_in_bot():
    docs = render()
    deps = deployments(docs)
    bot = deps["bot"]
    env = env_of(main_container(bot))
    assert env["MATRIX_ACCESS_TOKEN_FILE"]["value"] == "/var/run/secrets/matrix/token"
    assert "MATRIX_ACCESS_TOKEN" not in env
    mount = next(m for m in main_container(bot)["volumeMounts"] if m["name"] == "matrix-token")
    assert mount["mountPath"] == "/var/run/secrets/matrix"
    assert mount["readOnly"] is True
    vol = next(v for v in pod(bot)["volumes"] if v["name"] == "matrix-token")
    assert vol["secret"]["secretName"] == "mx-agents-matrix-connector"
    assert vol["secret"]["defaultMode"] == 0o440
    assert vol["secret"]["items"] == [{"key": "matrix-access-token", "path": "token"}]
    # nowhere else: api pod, migrate initContainers, ConfigMap, other env values
    api = deps["api"]
    assert "matrix-token" not in {v["name"] for v in pod(api)["volumes"]}
    for dep in deps.values():
        for c in pod(dep)["initContainers"]:
            assert "MATRIX_ACCESS_TOKEN_FILE" not in env_of(c)
            assert all(m["name"] != "matrix-token" for m in c["volumeMounts"])
    assert "MATRIX_ACCESS_TOKEN_FILE" not in env_of(main_container(api))
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    assert MX not in yaml.safe_dump(cm)
    assert not any(k.startswith("MATRIX_ACCESS_TOKEN") for k in cm["data"])
    for dep in deps.values():
        assert MX not in yaml.safe_dump(dep)


def test_access_token_existing_secret():
    docs = render(
        {"matrix": {"accessToken": "", "accessTokenSecret": {"name": "hermes", "key": "t"}}}
    )
    secrets = [d for d in docs if d["kind"] == "Secret"]
    assert all("matrix-access-token" not in s["stringData"] for s in secrets)
    vol = next(v for v in pod(deployments(docs)["bot"])["volumes"] if v["name"] == "matrix-token")
    assert vol["secret"]["secretName"] == "hermes"
    assert vol["secret"]["items"] == [{"key": "t", "path": "token"}]


def test_secret_variants():
    docs = render()
    secret = next(d for d in docs if d["kind"] == "Secret")
    assert secret["stringData"] == {
        "database-url": BASE["postgres"]["url"],
        "orchestrator-token": ORCH,
        "matrix-access-token": MX,
    }
    for name, dep in deployments(docs).items():
        env = env_of(main_container(dep))
        assert env["MATRIX_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "mx-agents-matrix-connector",
            "key": "database-url",
        }
        assert env["MATRIX_ORCHESTRATOR_TOKEN"]["valueFrom"]["secretKeyRef"] == {
            "name": "mx-agents-matrix-connector",
            "key": "orchestrator-token",
        }, name
        init_env = env_of(pod(dep)["initContainers"][0])
        assert "MATRIX_DATABASE_URL" in init_env
    existing = {
        "postgres": {"url": "", "existingSecret": "cnpg-app", "existingSecretKey": "uri"},
        "orchestrator": {"token": "", "tokenSecret": {"name": "orch-tok", "key": "k"}},
        "matrix": {"accessToken": "", "accessTokenSecret": {"name": "mx", "key": "t"}},
    }
    docs = render(existing)
    assert not [d for d in docs if d["kind"] == "Secret"]
    for dep in deployments(docs).values():
        env = env_of(main_container(dep))
        assert env["MATRIX_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": "cnpg-app",
            "key": "uri",
        }
        assert env["MATRIX_ORCHESTRATOR_TOKEN"]["valueFrom"]["secretKeyRef"] == {
            "name": "orch-tok",
            "key": "k",
        }
    inline_only_db = render({"orchestrator": {"token": "", "tokenSecret": {"name": "o"}}})
    secret = next(d for d in inline_only_db if d["kind"] == "Secret")
    assert set(secret["stringData"]) == {"database-url", "matrix-access-token"}


def test_missing_sources_fail():
    for bad in (
        {"postgres": {"url": ""}},
        {"orchestrator": {"token": ""}},
        {"matrix": {"accessToken": ""}},
        {"orchestrator": {"url": ""}},
        {"matrix": {"homeserverUrl": ""}},
        {"image": {"tag": ""}},
    ):
        proc = _run(_merge(BASE, bad))
        assert proc.returncode != 0, bad


def test_config_rendered():
    docs = render(
        {
            "matrix": {"selfIdentities": ["@a:x", "@b:x"]},
            "policy": {"default": "allow", "groupTrigger": "all"},
            "limits": {"maxNewSessions": 5, "retentionDays": 7},
            "env": {"MATRIX_LOG_LEVEL": "DEBUG"},
        }
    )
    data = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert data["MATRIX_ORCHESTRATOR_URL"] == "http://orch.agents.svc"
    assert data["MATRIX_HOMESERVER_URL"] == "http://synapse.messaging.svc:8008"
    assert data["MATRIX_USER_ID"] == "@hermes:example.org"
    assert data["MATRIX_DEVICE_ID"] == "agents-matrix-connector"
    assert data["MATRIX_SELF_IDENTITIES"] == "@a:x,@b:x"
    assert data["MATRIX_BRIDGE_BOTS"] == "@facebookbot:szymonrichert.pl"
    assert data["MATRIX_DEFAULT_POLICY"] == "allow"
    assert data["MATRIX_GROUP_TRIGGER"] == "all"
    assert data["MATRIX_MAX_NEW_SESSIONS"] == "5"
    assert data["MATRIX_MAX_QUEUED_PER_ROOM"] == "20"
    assert data["MATRIX_RETENTION_DAYS"] == "7"
    assert data["MATRIX_POLL_SECONDS"] == "3"
    assert data["MATRIX_LOG_LEVEL"] == "DEBUG"
    assert "MATRIX_API_ALLOWED_CIDRS" not in data
    assert "MATRIX_PUPPET_PATTERN" in data


def test_config_rejects_foreign_env_key():
    proc = _run(_merge(BASE, {"env": {"FOO": "1"}}))
    assert proc.returncode != 0
    assert "must start with MATRIX_" in proc.stderr


def test_security_context():
    for dep in deployments(render()).values():
        spec = pod(dep)
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["securityContext"]["runAsUser"] == 1000
        assert spec["automountServiceAccountToken"] is False
        for c in spec["containers"] + spec["initContainers"]:
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"] == {"drop": ["ALL"]}


def test_pod_annotations_per_component():
    deps = deployments(render({"components": {"api": {"podAnnotations": {"only/api": "1"}}}}))
    assert "only/api" in deps["api"]["spec"]["template"]["metadata"]["annotations"]
    assert "only/api" not in deps["bot"]["spec"]["template"]["metadata"]["annotations"]
