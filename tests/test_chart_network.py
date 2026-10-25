"""Chart network: the api Service and the /matrix/api Ingress; nothing reaches the bot."""

from test_chart_workloads import render

ING = {
    "ingress": {
        "enabled": True,
        "className": "nginx",
        "host": "lab.example.test",
        "tls": {"secretName": "lab-tls"},
    }
}


def by_kind(docs, kind):
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def test_service_selects_api_only():
    svcs = by_kind(render(ING), "Service")
    assert set(svcs) == {"mx-agents-matrix-connector-api"}
    svc = svcs["mx-agents-matrix-connector-api"]
    assert svc["spec"]["type"] == "ClusterIP"
    port = svc["spec"]["ports"][0]
    assert port["name"] == "http" and port["port"] == 8000 and port["targetPort"] == "http"
    assert svc["spec"]["selector"] == {
        "app.kubernetes.io/name": "agents-matrix-connector",
        "app.kubernetes.io/instance": "mx",
        "app.kubernetes.io/component": "api",
    }


def test_ingress_routes_api_prefix():
    ings = by_kind(render(ING), "Ingress")
    assert len(ings) == 1
    ing = next(iter(ings.values()))
    assert ing["spec"]["ingressClassName"] == "nginx"
    assert ing["spec"]["tls"] == [{"hosts": ["lab.example.test"], "secretName": "lab-tls"}]
    (rule,) = ing["spec"]["rules"]
    assert rule["host"] == "lab.example.test"
    (path,) = rule["http"]["paths"]
    assert path["path"] == "/matrix/api" and path["pathType"] == "Prefix"
    assert path["backend"]["service"] == {
        "name": "mx-agents-matrix-connector-api",
        "port": {"number": 8000},
    }
    assert "cert-manager.io/cluster-issuer" not in ing["metadata"].get("annotations", {})
    assert "bot" not in str(ing["spec"]["rules"])


def test_ingress_annotations_pass_through():
    ing = {"ingress": {**ING["ingress"], "annotations": {"a": "b"}}}
    (rendered,) = by_kind(render(ing), "Ingress").values()
    assert rendered["metadata"]["annotations"] == {"a": "b"}


def test_ingress_disabled():
    assert not by_kind(render(), "Ingress")
    assert not by_kind(render({"ingress": {"enabled": False}}), "Ingress")
