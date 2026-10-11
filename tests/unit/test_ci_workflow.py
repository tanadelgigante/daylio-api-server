"""Structural tests for ``.gitea/workflows/ci.yml`` (FASE 7, Task 7.1/7.6).

=============================== MAINTENANCE NOTES ===============================
Module   : tests/unit/test_ci_workflow.py
Scope    : The CI definition is executable configuration, so it is tested like
            code. These tests are static (no runner required) and lock the
            invariants the WORKPLAN confirms for FASE 7:

              D9/D14 : the six jobs of §8.2 exist with the documented
                       dependencies; ``[skip-ci]`` gates lint/test/build;
              D14    : the ``build`` job runs on ``push`` or on a manual
                       ``workflow_dispatch job=build`` (which forces the build
                       even when the Dockerfile is unchanged), never on a
                       pull_request, diff-guards the Dockerfile on push, uses
                       buildx for ``linux/amd64,linux/arm64`` and never puts a
                       password on the command line; the artifact goes to the
                       GITEA container registry, not to Docker Hub;
              D15    : image/tag fallbacks (vars.IMAGE_REPO, version.ini,
                       vars.APP_VERSION, matrix.tag);
              §8.2   : ``release`` is reachable ONLY through
                       ``workflow_dispatch`` with ``job=release`` — never on a
                       push or on a tag (confirmed decision, §11 #7);
              D16    : ``notify`` always runs and summarises every job;
              D18    : the ``test`` job builds ``INFLUX_URL`` from
                       ``vars.INFLUX_INSTANCE`` and degrades to ``-m "not
                       influx"`` when the endpoint is absent.

            Changing a job name, a guard or a token here is a WORKPLAN-visible
            decision: update this module in the same commit.
================================================================================
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".gitea" / "workflows" / "ci.yml"

#: §8.2 job order and identity.
EXPECTED_JOBS = ["ci-skip-check", "lint", "test", "build", "release", "notify"]


@pytest.fixture(scope="module")
def workflow() -> dict:
    """Parsed ``.gitea/workflows/ci.yml`` (module-scoped: parsed once)."""
    assert WORKFLOW_PATH.is_file(), f"missing CI workflow: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


def _steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def _run_text(job: dict) -> str:
    """Concatenate every ``run`` block of a job (for substring assertions)."""
    return "\n".join(step.get("run", "") for step in _steps(job))


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_workflow_is_valid_yaml_with_the_expected_jobs(workflow: dict) -> None:
    assert list(workflow["jobs"]) == EXPECTED_JOBS


def test_triggers_cover_push_pull_request_and_manual_dispatch(workflow: dict) -> None:
    triggers = workflow.get("on", workflow.get(True))
    assert "push" in triggers
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers
    # The release selector input (flatpress pattern).
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["job"]["default"] == "release"


def test_job_dependencies_follow_the_workplan(workflow: dict) -> None:
    jobs = workflow["jobs"]
    assert "needs" not in jobs["ci-skip-check"]
    assert jobs["lint"]["needs"] == ["ci-skip-check"]
    assert jobs["test"]["needs"] == ["ci-skip-check"]
    assert jobs["build"]["needs"] == ["ci-skip-check"]
    assert jobs["release"]["needs"] == ["lint", "test", "ci-skip-check"]
    assert set(jobs["notify"]["needs"]) == set(EXPECTED_JOBS) - {"notify"}


# --------------------------------------------------------------------------- #
# D9 / D14: the [skip-ci] gate
# --------------------------------------------------------------------------- #
def test_skip_check_exposes_should_skip_and_skip_build(workflow: dict) -> None:
    outputs = workflow["jobs"]["ci-skip-check"]["outputs"]
    assert "should_skip" in outputs
    assert "skip_build" in outputs


def test_skip_check_never_skips_a_manual_dispatch(workflow: dict) -> None:
    run = _run_text(workflow["jobs"]["ci-skip-check"])
    assert "[skip-ci]" in run
    assert "[skip]" in run
    # A manual dispatch short-circuits the token scan.
    assert "workflow_dispatch" in run


def test_lint_and_test_are_gated_by_should_skip(workflow: dict) -> None:
    for name in ("lint", "test"):
        assert "needs.ci-skip-check.outputs.should_skip != 'true'" in (
            workflow["jobs"][name]["if"]
        )


# --------------------------------------------------------------------------- #
# D14 / D15: the build job
# --------------------------------------------------------------------------- #
def test_build_runs_on_push_or_manual_dispatch(workflow: dict) -> None:
    condition = workflow["jobs"]["build"]["if"]
    assert "github.event_name == 'push'" in condition
    assert "github.event.inputs.job == 'build'" in condition
    assert "pull_request" not in condition


def test_manual_dispatch_forces_the_build_with_unchanged_dockerfile(
    workflow: dict,
) -> None:
    """The Dockerfile diff-guard applies to pushes only; a manual
    ``workflow_dispatch job=build`` skips the guard and forces the build."""
    run = _run_text(workflow["jobs"]["build"])
    assert "NOT modified, skipping build" in run
    assert 'github.event_name' in run
    assert "job=build" in run


def test_build_is_gated_by_both_skip_tokens(workflow: dict) -> None:
    condition = workflow["jobs"]["build"]["if"]
    assert "needs.ci-skip-check.outputs.should_skip != 'true'" in condition
    assert "needs.ci-skip-check.outputs.skip_build != 'true'" in condition


def test_build_matrix_targets_the_project_image(workflow: dict) -> None:
    matrix = workflow["jobs"]["build"]["strategy"]
    assert matrix["fail-fast"] is False
    include = matrix["matrix"]["include"]
    assert len(include) == 1
    entry = include[0]
    assert entry["image_repo"] == "tanadelgigante/daylio-api-server"  # D15a
    assert entry["dockerfile"] == "Dockerfile"
    assert entry["context"] == "."
    assert entry["tag"]  # D15b last-resort fallback


def test_build_step_follows_the_infrastructure_tools_pattern(workflow: dict) -> None:
    run = _run_text(workflow["jobs"]["build"])
    # Custom CA + QEMU + buildx bootstrap.
    assert "apk_install_with_retry" in run
    assert "multiarch/qemu-user-static" in run
    assert "docker buildx create --name multiarch" in run
    assert "update-ca-certificates" in run
    # Diff-guard on the Dockerfile (§8.3 step 4).
    assert "git diff --name-only HEAD~1 HEAD" in run
    # Tag resolution order (D15b).
    assert "version.ini" in run
    assert "-SNAPSHOT" in run
    assert "APP_VERSION" in run
    # Login through stdin only: the password is never an argument.
    assert "--password-stdin" in run
    assert "docker login --username" in run
    assert 'docker login "$DOCKER_PASSWORD"' not in run
    # Multi-arch push.
    assert "--platform linux/amd64,linux/arm64" in run
    assert "--push" in run


def test_build_checks_out_the_full_history(workflow: dict) -> None:
    checkouts = [
        step
        for step in _steps(workflow["jobs"]["build"])
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts, "the build job must check out the repository"
    assert checkouts[0]["with"]["fetch-depth"] == 0


def test_build_installs_the_alpine_openssl_package(workflow: dict) -> None:
    """``openssl-client`` does not exist in the Alpine 3.23 repositories (it is
    a Debian/Fedora-style name); the Alpine package that ships the ``openssl``
    CLI is simply ``openssl``.
    """
    run = _run_text(workflow["jobs"]["build"])
    assert "openssl-client" not in run
    assert re.search(r"apk_install_with_retry .*openssl ", run)


def test_build_pushes_to_the_gitea_container_registry(workflow: dict) -> None:
    """User decision 2026-10-11: the build artifact goes to the Gitea
    container registry (so it can be smoke-tested before any release), never
    to Docker Hub. Credentials: DUNGEONPAT via stdin, no DOCKER_* secrets."""
    run = _run_text(workflow["jobs"]["build"])
    assert "GITHUB_SERVER_URL" in run
    assert "docker login --username" in run
    assert "GITHUB_ACTOR" in run
    assert "--password-stdin" in run
    assert "DUNGEONPAT" in run
    assert "--platform linux/amd64,linux/arm64" in run
    assert "--push" in run
    # Docker Hub is strictly out of scope for the build job.
    assert "DOCKER_USERNAME" not in run
    assert "DOCKER_PASSWORD" not in run
    assert "docker.io" not in run


def test_build_tells_buildkit_to_trust_the_internal_ca(workflow: dict) -> None:
    """BuildKit performs the push from its OWN container, which cannot see the
    internal Dungeon CA installed in the job container, so the push to the
    internal registry fails with "x509: certificate signed by unknown
    authority". ``docker buildx create --config`` (buildkitd.toml) proved
    unreliable, so the build job bakes the CA into a custom BuildKit image
    (``buildkit-dungeon:local``) and creates the docker-container builder with
    ``--driver-opt image=buildkit-dungeon:local``: TLS stays ENABLED (verified
    against the Dungeon CA) and Docker Hub is never flagged insecure."""
    setup = [
        step
        for step in _steps(workflow["jobs"]["build"])
        if step.get("name") == "Setup Docker buildx (custom BuildKit image with the internal CA)"
    ]
    assert setup, "missing the custom BuildKit setup step"
    run = setup[0]["run"]
    # The CA is baked into a custom buildkit image used as the worker image.
    assert "DUNGEON_CA" in setup[0].get("env", {})
    assert "buildkit-dungeon:local" in run
    assert "--driver-opt image=buildkit-dungeon:local" in run
    assert "docker build -t buildkit-dungeon:local" in run
    # The stock builder is recreated every run so a stale container built from
    # the stock image is never reused.
    assert "docker buildx rm multiarch" in run
    assert "docker buildx create --name multiarch --driver docker-container" in run
    assert "moby/buildkit" in run
    # HTTPS/trust is kept for the internal registry via the CA; no `insecure`
    # registry flag may be introduced (it would also weaken Docker Hub). The
    # word "buildkitd.toml" may still appear in a comment explaining the
    # abandoned approach, but never as an active config.
    assert "insecure = true" not in run
    assert "docker.io" not in run


# --------------------------------------------------------------------------- #
# Runner compatibility: node for actions/checkout (nektos/act issue #107)
# --------------------------------------------------------------------------- #
def test_every_checkout_job_provides_node(workflow: dict) -> None:
    """JS actions (actions/checkout@v4) need node in the job container.

    Alpine and Debian-slim images ship no node, so act and the Gitea runner
    fail with "exec: node: executable file not found in $PATH" (nektos/act
    #107) when a job runs ``actions/checkout``. Every job that checks out the
    repository must install ``nodejs`` (apk on Alpine, apt-get on Debian)
    before the checkout step.
    """
    for name in EXPECTED_JOBS:
        job = workflow["jobs"][name]
        uses_checkout = any(
            str(step.get("uses", "")).startswith("actions/checkout")
            for step in _steps(job)
        )
        if uses_checkout:
            assert "nodejs" in _run_text(job), (
                f"job {name!r} uses actions/checkout but never installs node"
            )


# --------------------------------------------------------------------------- #
# D15 / §11 #7: the release job is MANUAL ONLY
# --------------------------------------------------------------------------- #
def test_release_is_never_automatic(workflow: dict) -> None:
    """A push or a tag push must never create a release (confirmed §11 #7)."""
    condition = workflow["jobs"]["release"]["if"]
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event.inputs.job == 'release'" in condition
    # No tag ref in the condition: `startsWith(github.ref, 'refs/tags/v')` is
    # the flatpress behaviour that the WORKPLAN explicitly rejected.
    assert "refs/tags" not in condition


def test_release_creates_the_release_and_bumps_the_version(workflow: dict) -> None:
    run = _run_text(workflow["jobs"]["release"])
    assert "/api/v1/repos/" in run
    assert "DUNGEONPAT" in run
    assert "version.ini" in run
    assert "scripts/bump_version.py" in run
    # The bump commit carries [skip-ci] so it does not re-trigger the pipeline.
    assert "[skip-ci]" in run


def test_release_has_write_permission(workflow: dict) -> None:
    assert workflow["jobs"]["release"]["permissions"]["contents"] == "write"


def test_release_promotes_the_image_to_docker_hub(workflow: dict) -> None:
    """User decision 2026-10-11: Docker Hub is reached ONLY at release time.
    The release job pulls the tag from the Gitea registry and re-pushes the
    same tag to Docker Hub (docker.io / vars.IMAGE_REPO)."""
    run = _run_text(workflow["jobs"]["release"])
    assert "docker pull" in run
    assert "docker tag" in run
    assert "docker push" in run
    # Pull side (Gitea): DUNGEONPAT.
    assert "DUNGEONPAT" in run
    assert "GITHUB_ACTOR" in run
    # Push side (Docker Hub): DOCKER_* secrets + image from vars.IMAGE_REPO.
    assert "DOCKER_USERNAME" in run
    assert "DOCKER_PASSWORD" in run
    assert "IMAGE_REPO" in run
    assert "tanadelgigante/daylio-api-server" in run
    assert "--password-stdin" in run


# --------------------------------------------------------------------------- #
# D16: the notify job
# --------------------------------------------------------------------------- #
def test_notify_always_runs_and_summarises_every_job(workflow: dict) -> None:
    notify = workflow["jobs"]["notify"]
    assert notify["if"] == "always()"
    run = _run_text(notify)
    assert "NTFY_URL" in run
    assert "NTFY_TOPIC" in run
    for job in EXPECTED_JOBS[:-1]:
        assert f"needs.{job}.result" in workflow["jobs"]["notify"]["steps"][-1]["env"][
            "JOB_RESULTS"
        ]


# --------------------------------------------------------------------------- #
# D18: InfluxDB wiring in the test job
# --------------------------------------------------------------------------- #
def test_test_job_builds_the_influx_url_from_the_instance_var(workflow: dict) -> None:
    run = _run_text(workflow["jobs"]["test"])
    job_yaml = yaml.dump(workflow["jobs"]["test"])
    assert "vars.INFLUX_INSTANCE" in job_yaml
    assert 'INFLUX_URL=http://${INFLUX_INSTANCE}' in run
    assert "INFLUX_ORG=tanadelgigante" in run
    assert "INFLUX_BUCKET=daylio" in run
    # The token is wired through a step env so it never appears on the command
    # line (secrets are masked by the runner); check the whole job, not `run`.
    assert "INFLUX_TOKEN" in job_yaml


def test_test_job_falls_back_to_the_fake_only_suite(workflow: dict) -> None:
    run = _run_text(workflow["jobs"]["test"])
    assert "CI_SKIP_INFLUX=1" in run
    assert 'pytest -q -m "not influx"' in run
    # And the full suite when the endpoint IS configured.
    assert "python -m pytest -q\n" in run


def test_test_job_runs_ruff_and_pytest_in_python_containers(workflow: dict) -> None:
    for name, check in (("lint", "ruff check src tests scripts"), ("test", "pytest")):
        job = workflow["jobs"][name]
        assert job["container"]["image"] == "python:3.12-slim"
        assert check in _run_text(job)
