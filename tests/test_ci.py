"""The CI workflow publishes image and chart only from main and reads only the Harbor secrets."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / ".github/workflows/ci.yaml").read_text()
WF = yaml.safe_load(TEXT)
JOBS = WF["jobs"]
MAIN = "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def steps_text(job: str) -> str:
    return yaml.safe_dump(JOBS[job]["steps"])


def test_main_publishes_image_and_chart():
    on = WF.get("on", WF.get(True))
    assert on["push"]["branches"] == ["main"]
    images = steps_text("images")
    assert (
        JOBS["images"]["env"]["IMAGE"]
        == "harbor.szymonrichert.pl/containers/agents-matrix-connector"
    )
    assert "docker build" in images
    assert '"$IMAGE:$SHORTSHA"' in images
    assert "SHORTSHA=${GITHUB_SHA::7}" in images
    for step in JOBS["images"]["steps"]:
        if "regctl" in str(step) or "HARBOR" in str(step):
            assert step.get("if") == MAIN, step
    publish = JOBS["chart-publish"]
    assert publish["if"] == MAIN
    assert publish["env"]["CHART_REPO"] == "oci://harbor.szymonrichert.pl/charts"
    text = steps_text("chart-publish")
    assert "scripts/chart-version.sh" in text
    assert "helm push" in text
    assert "agents-matrix-connector-$VERSION.tgz" in text
    assert "chart/agents-matrix-connector" in text
    version = subprocess.run(
        ["bash", str(ROOT / "scripts/chart-version.sh"), "branch", "main", "abcdef1234567"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert version == "0.1.0-main.abcdef1"


def test_pr_checks_and_secrets():
    on = WF.get("on", WF.get(True))
    assert "pull_request" in on
    assert "mise run lint" in steps_text("lint")
    assert "mise run test" in steps_text("test")
    assert "mise run lint:chart" in steps_text("chart")
    for name in ("lint", "test", "chart"):
        assert "secrets." not in steps_text(name)
        assert "if" not in JOBS[name]
    assert set(re.findall(r"secrets\.(\w+)", TEXT)) == {"HARBOR_USERNAME", "HARBOR_PASSWORD"}
    for name, job in JOBS.items():
        for step in job["steps"]:
            if "secrets." in str(step):
                assert job.get("if") == MAIN or step.get("if") == MAIN, (name, step)


def test_dockerfile_user_and_entrypoint():
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    assert "USER 1000" in lines
    assert 'ENTRYPOINT ["agents-matrix-connector"]' in lines
    assert not any(line.startswith("CMD") for line in lines)


def test_images_pinned():
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    froms = [line for line in lines if line.startswith("FROM ")]
    copies = [line for line in lines if line.startswith("COPY --from=")]
    refs = [line.split()[1] for line in froms] + [
        line.split()[1].removeprefix("--from=") for line in copies
    ]
    assert len(refs) == 2
    for ref in refs:
        tag = ref.split(":", 1)[1]
        assert tag != "latest" and "latest" not in tag, ref
    assert re.fullmatch(r"python:\d+\.\d+\.\d+-slim-bookworm", froms[0].split()[1])
    assert re.fullmatch(
        r"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+", copies[0].split()[1].removeprefix("--from=")
    )
