from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
import time
from pathlib import Path

import pytest

from repoboss.agents import BUILTIN, AgentSpec
from repoboss.challenge import create_challenges
from repoboss.config import RepoConfig
from repoboss.procs import run_process
from repoboss.run import run_challenge
from repoboss.runners import (
    DockerRunner,
    EnvInfo,
    Limits,
    LocalRunner,
    build_docker_create_argv,
    docker_available,
    extract_tar_safely,
    render_dockerfile,
    resolve_command,
    tar_directory,
)
from repoboss.util import rmtree, walk_files
from repoboss.workspace import Workspace

from .conftest import DEMO


def test_timeout_kills_process_tree() -> None:
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print('started', flush=True)\n"
        "time.sleep(120)\n"
    )
    start = time.monotonic()
    res = run_process([sys.executable, "-c", code], cwd=None, env=None, timeout_s=2)
    assert res.timed_out and res.exit_code is None
    assert time.monotonic() - start < 30
    assert "started" in res.output


def test_log_is_capped_but_keeps_head_and_tail() -> None:
    code = "import sys\nsys.stdout.write('HEAD' + 'x' * 3_000_000 + 'TAIL')\n"
    res = run_process([sys.executable, "-c", code], cwd=None, env=None, timeout_s=60, log_limit=10_000)
    assert res.truncated and res.output_bytes >= 3_000_008
    assert len(res.output) < 11_000
    assert res.output.startswith("HEAD") and res.output.endswith("TAIL")
    assert "omitted by RepoBoss log limit" in res.output


def test_missing_executable_is_a_launch_error() -> None:
    res = run_process(["definitely-not-a-command-rb"], cwd=None, env=None, timeout_s=5)
    assert res.exit_code is None and res.launch_error is not None and not res.timed_out


def test_dependency_links_are_never_followed_on_delete(tmp_path: Path) -> None:
    context = tmp_path / "env" / "context"
    (context / "node_modules" / "dep").mkdir(parents=True)
    (context / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "a.js").write_text("", encoding="utf-8")
    env = EnvInfo(runner="local", key="k", python=sys.executable, platform="p", details={"context": str(context)})
    config = RepoConfig(name="x", install=(), test=("node", "--test"), dependency_dirs=("node_modules",))
    try:
        LocalRunner(tmp_path / "envs").attach_dependencies(env, tree, config)
    except OSError as exc:
        pytest.skip(f"directory links not available: {exc}")
    assert (tree / "node_modules" / "dep" / "index.js").is_file()
    files, odd = walk_files(tree, skip=frozenset({"node_modules"}))
    assert files == ["src/a.js"] and odd == []
    rmtree(tree)
    assert not tree.exists()
    assert (context / "node_modules" / "dep" / "index.js").is_file()  # the shared install survives


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_commands_resolve_like_a_shell_without_one() -> None:
    import os

    resolved = resolve_command(["npm", "--version"], {"PATH": os.environ["PATH"]}, sys.executable, None)
    assert Path(resolved[0]).is_file() and resolved[1:] == ["--version"]
    assert resolve_command(["python", "-V"], {}, "/opt/py", None) == ["/opt/py", "-V"]


def _argv(network: bool) -> list[str]:
    return build_docker_create_argv(
        image="repoboss-env:abc",
        name="repoboss-x",
        argv=["python", "-m", "pytest"],
        network=network,
        limits=Limits(cpus=1.5, memory="1g", pids=128),
        extra_env={"REPOBOSS_TASK_FILE": "/repoboss/out/task.json"},
    )


def test_docker_command_is_locked_down() -> None:
    cmd = _argv(network=False)
    joined = " ".join(cmd)
    assert cmd[:2] == ["docker", "create"]
    assert cmd[cmd.index("--network") + 1] == "none"
    for flag, value in (
        ("--cpus", "1.5"),
        ("--memory", "1g"),
        ("--memory-swap", "1g"),
        ("--pids-limit", "128"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
    ):
        assert cmd[cmd.index(flag) + 1] == value
    # Nothing from the host is mounted: files travel as tar streams (docker cp).
    assert "--mount" not in cmd and "-v" not in cmd and "--volume" not in cmd
    assert "docker.sock" not in joined and "/home" not in joined and ".ssh" not in joined
    assert "--privileged" not in cmd
    # The image and argv come last; argv is passed as separate items, never through a shell.
    assert cmd[-4:] == ["repoboss-env:abc", "python", "-m", "pytest"]


def test_docker_network_is_opt_in() -> None:
    cmd = _argv(network=True)
    assert cmd[cmd.index("--network") + 1] == "bridge"


def test_dockerfile_uses_exec_form_and_keeps_only_dependencies() -> None:
    text = render_dockerfile("node:22-slim", [["npm", "ci", "; rm -rf /"]], ["node_modules"])
    assert 'RUN ["npm", "ci", "; rm -rf /"]' in text
    find_line = next(line for line in text.splitlines() if '"find"' in line)
    find = json.loads(find_line[4:])
    assert find[:4] == ["find", "/workspace", "-mindepth", "1"]
    assert find[-4:-1] == ["!", "-path", "/workspace/node_modules/*"] and find[-1] == "-delete"
    assert 'RUN ["find", "/workspace", "-mindepth", "1", "-type", "d", "-empty", "-delete"]' in text
    with pytest.raises(ValueError):
        render_dockerfile("python:3.12\nRUN curl evil", [])
    for line in text.splitlines():
        if line.startswith("RUN "):
            json.loads(line[4:])  # every RUN is an exec-form JSON array


def test_tar_roundtrip_rejects_links_and_traversal(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (src / "node_modules" / "dep").mkdir(parents=True)
    (src / "node_modules" / "dep" / "i.js").write_text("", encoding="utf-8")
    data = tar_directory(src, skip=frozenset({"node_modules"}))
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
    assert names == ["pkg/a.py"]

    # Simulate what `docker cp container:/workspace -` returns, with hostile entries added.
    archive = tmp_path / "out.tar"
    with tarfile.open(archive, "w") as tar:
        for name, payload in (("workspace/pkg/a.py", b"x = 2\n"), ("workspace/node_modules/dep/i.js", b"")):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        link = tarfile.TarInfo("workspace/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        evil = tarfile.TarInfo("workspace/../evil.txt")
        evil.size = 1
        tar.addfile(evil, io.BytesIO(b"x"))
    dest = tmp_path / "dest"
    rejected = extract_tar_safely(archive, dest, strip="workspace", skip=frozenset({"node_modules"}))
    assert (dest / "pkg" / "a.py").read_text() == "x = 2\n"
    assert not (dest / "node_modules").exists()
    assert not (tmp_path / "evil.txt").exists() and not (dest / "escape").exists()
    assert "link or special file: escape" in rejected
    assert any(r.startswith("unsafe path") for r in rejected)


@pytest.mark.docker
@pytest.mark.skipif(not docker_available()[0], reason="Docker daemon not available")
def test_full_pipeline_in_docker(demo_repo: Path, ws: Workspace, tmp_path: Path) -> None:
    # The container has no pytest: use the demo's real config, which installs it while building the image.
    shutil.copyfile(DEMO / "repoboss.toml", demo_repo / "repoboss.toml")
    runner = DockerRunner(ws.envs_dir)
    _, results = create_challenges(ws, demo_repo, runner, transforms=["logic_condition"], seed=0)
    cid = results[0].challenge_id
    assert cid is not None, results[0].verification.to_dict()
    ok = run_challenge(ws, cid, BUILTIN["demo-oracle"], runner)[0]
    bad = run_challenge(ws, cid, BUILTIN["demo-noop"], runner)[0]
    assert ok.outcome == "passed" and bad.outcome == "failed"
    row = ws.get_challenge(cid)
    assert row is not None and row["env"]["image_id"] and row["env"]["base_image"] == "python:3.12-slim"

    agent_dir = tmp_path / "probe-agent"
    agent_dir.mkdir()
    (agent_dir / "probe.py").write_text(
        "import json, os, pathlib, socket\n"
        "out = pathlib.Path(os.environ['REPOBOSS_OUTPUT_DIR'])\n"
        "result = {'tests_visible': pathlib.Path('/workspace/tests').exists()\n"
        "          or any(p.name.startswith('test_') for p in pathlib.Path('/workspace').rglob('*.py'))}\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3); result['network'] = True\n"
        "except OSError:\n"
        "    result['network'] = False\n"
        "try:\n"
        "    (pathlib.Path(os.environ['REPOBOSS_AGENT_DIR']) / 'x').write_text('x'); result['agent_dir_rw'] = True\n"
        "except OSError:\n"
        "    result['agent_dir_rw'] = False\n"
        "result['docker_sock'] = pathlib.Path('/var/run/docker.sock').exists()\n"
        "(out / 'probe.json').write_text(json.dumps(result))\n",
        encoding="utf-8",
    )
    spec = AgentSpec(
        name="probe",
        kind="custom",
        is_demo=False,
        description="probe",
        command=("python", "{agent_dir}/probe.py"),
        agent_dir=agent_dir,
        timeout_s=60.0,
    )
    run = run_challenge(ws, cid, spec, runner)[0]
    probe = json.loads((ws.run_dir(run.run_id) / "agent-out" / "probe.json").read_text())
    # /agent is a copy inside the container: writing there must never reach the host folder.
    assert probe["tests_visible"] is False and probe["network"] is False and probe["docker_sock"] is False
    assert not (agent_dir / "x").exists()
