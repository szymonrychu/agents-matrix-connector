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


RELEASED = "steps.version.outputs.version != ''"
RELEASE_IMAGE = "harbor.szymonrichert.pl/containers/agents-matrix-connector"


def _copy_steps(steps: list[dict]) -> list[int]:
    return [i for i, s in enumerate(steps) if "regctl image copy" in s.get("run", "")]


def test_release_job_runs_last_on_main_push():
    job = JOBS["release"]
    assert job["if"] == MAIN
    assert set(job["needs"]) == set(JOBS) - {"release"}
    assert set(job["needs"]) == {"lint", "test", "chart", "images", "chart-publish"}
    assert job["permissions"] == {"contents": "write"}
    assert job["concurrency"] == {"group": "release", "cancel-in-progress": False}


def test_release_job_fetches_full_history_and_tags():
    checkout = JOBS["release"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["fetch-tags"] is True


def test_release_job_computes_version_with_script():
    steps = JOBS["release"]["steps"]
    version = next(i for i, s in enumerate(steps) if s.get("id") == "version")
    assert steps[version]["run"] == (
        'echo "version=$(python3 scripts/next_version.py)" >> "$GITHUB_OUTPUT"'
    )
    later = steps[version + 1 :]
    assert later
    assert all(s.get("if") == RELEASED for s in later)


def test_release_job_retags_manifest_only():
    job = JOBS["release"]
    steps = job["steps"]
    assert job["env"]["IMAGE"] == RELEASE_IMAGE
    assert job["env"]["HARBOR_URL"] == "harbor.szymonrichert.pl"
    copies = [
        line.strip()
        for s in steps
        for line in s.get("run", "").splitlines()
        if "regctl image copy" in line
    ]
    assert copies == [
        'regctl image copy "$IMAGE:$SHORTSHA" "$IMAGE:$VERSION"',
        'regctl image copy "$IMAGE:$SHORTSHA" "$IMAGE:$VERSION_MINOR"',
        'regctl image copy "$IMAGE:$SHORTSHA" "$IMAGE:$VERSION_MAJOR"',
    ]
    tag = steps[_copy_steps(steps)[0]]["run"]
    assert 'SHORTSHA="${GITHUB_SHA::7}"' in tag
    assert 'VERSION_MINOR="${VERSION%.*}"' in tag
    assert 'VERSION_MAJOR="${VERSION%%.*}"' in tag
    installers = [s["uses"] for s in steps if "regctl-installer" in s.get("uses", "")]
    assert len(installers) == 1
    assert re.fullmatch(r"regclient/actions/regctl-installer@[0-9a-f]{40}", installers[0])
    logins = [s for s in steps if "regctl registry login" in s.get("run", "")]
    assert len(logins) == 1
    assert logins[0]["env"] == {
        "HARBOR_USERNAME": "${{ secrets.HARBOR_USERNAME }}",
        "HARBOR_PASSWORD": "${{ secrets.HARBOR_PASSWORD }}",
    }
    text = yaml.safe_dump(job)
    for banned in ("docker", "regctl image import", "--blob-chunk"):
        assert banned not in text, banned


def test_release_job_pushes_git_tag_last():
    steps = JOBS["release"]["steps"]
    last = steps[-1]["run"].strip().splitlines()
    assert [line.strip() for line in last] == [
        'git tag "v$VERSION" "$GITHUB_SHA"',
        'git push origin "refs/tags/v$VERSION"',
    ]
    assert max(_copy_steps(steps)) < len(steps) - 1
    assert steps[-1]["env"]["VERSION"] == "${{ steps.version.outputs.version }}"
    for s in steps:
        assert not re.search(r"(^|\s)(--force|-f)(\s|$)", s.get("run", "")), s
    assert "secrets.GITHUB_TOKEN" not in TEXT


def test_no_tag_trigger_or_tag_publish_job():
    on = WF.get("on", WF.get(True))
    assert "tags" not in on["push"]
    assert "chart-publish-tag" not in JOBS
    assert "startsWith(github.ref, 'refs/tags" not in TEXT
