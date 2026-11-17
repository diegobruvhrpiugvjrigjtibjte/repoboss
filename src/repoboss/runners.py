"""Execution backends.

* ``docker`` — the isolated backend. Dependencies are installed once into an image (network
  allowed). Every attempt and every evaluation then runs in a fresh container: files are copied in
  and out as tar streams (nothing from the host is bind-mounted), the network is disabled by
  default, capabilities are dropped and CPU/memory/PID limits apply. Docker reduces risk; it is not
  a perfect security boundary.
* ``local`` — runs commands as normal host processes. It is **not isolated**: the agent can read
  your files, use the network and see the hidden tests on disk. Use it only for trusted code and
  for the deterministic demo.
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from repoboss.config import IMAGE_RE, RepoConfig
from repoboss.procs import DEFAULT_LOG_LIMIT, ExecResult, run_process
from repoboss.util import (
    NOISE_DIR_NAMES,
    copy_files,
    is_link_like,
    new_id,
    now_iso,
    rmtree,
    sha256_json,
    walk_files,
)

INSTALL_FAILURE_HINTS = (
    "No matching distribution found",
    "Could not find a version that satisfies",
    "Failed to establish a new connection",
    "Temporary failure in name resolution",
    "getaddrinfo failed",
    "Name or service not known",
    "ENOTFOUND",
    "EAI_AGAIN",
    "npm ERR! 404",
    "npm error 404",
    "Could not resolve host",
    "failed to download",
    "error NU1101",
    "error NU1301",
    "Could not resolve dependencies",
    "Could not transfer artifact",
)

# Host environment variables passed to local processes (toolchains need them). Anything else,
# notably API keys and tokens, is not inherited.
LOCAL_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "OS",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "JAVA_HOME",
    "MAVEN_HOME",
    "M2_HOME",
    "GRADLE_HOME",
    "GRADLE_USER_HOME",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN",
    "GOPATH",
    "GOROOT",
    "GOCACHE",
    "GOMODCACHE",
    "DOTNET_ROOT",
    "NUGET_PACKAGES",
    "NVM_HOME",
    "NVM_SYMLINK",
    "GEM_HOME",
    "GEM_PATH",
    "COMPOSER_HOME",
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
)


@dataclass
class Limits:
    cpus: float = 2.0
    memory: str = "4g"
    pids: int = 512
    log_limit_bytes: int = DEFAULT_LOG_LIMIT
    tmpfs_size: str = "512m"

    @classmethod
    def from_settings(cls, data: Mapping[str, Any]) -> Limits:
        lim = cls()
        for key in ("cpus", "memory", "pids", "log_limit_bytes", "tmpfs_size"):
            if key in data:
                setattr(lim, key, type(getattr(lim, key))(data[key]))
        return lim


@dataclass
class EnvInfo:
    runner: str
    key: str
    python: str
    platform: str
    python_version: str | None = None
    image: str | None = None
    image_id: str | None = None
    base_image: str | None = None
    base_digest: str | None = None
    prepared_at: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EnvInfo:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class PrepareResult:
    status: str  # ready | setup_error | dependency_missing | timeout | runner_unavailable
    env: EnvInfo | None
    log: str


@dataclass
class Paths:
    workspace: str
    output: str
    agent: str | None


def classify_install_failure(result: ExecResult) -> str:
    if result.timed_out:
        return "timeout"
    if result.launch_error:
        return "dependency_missing"  # the install tool itself (npm, cargo, ...) is not available
    if any(hint in result.output for hint in INSTALL_FAILURE_HINTS):
        return "dependency_missing"
    return "setup_error"


def context_files(snapshot_files: Sequence[str], config: RepoConfig) -> list[str]:
    """Files used to install dependencies: the whole snapshot.

    Test projects can declare their own dependencies (e.g. a .NET test project), so they are part of
    the install context. In Docker images every non-dependency file is deleted after installation,
    so neither tests nor original sources remain readable in the image.
    """
    del config
    return list(snapshot_files)


def skip_names(config: RepoConfig) -> frozenset[str]:
    return frozenset(config.dependency_dirs)


class Runner(ABC):
    kind: ClassVar[str]
    isolated: ClassVar[bool]

    def __init__(self, envs_dir: Path, limits: Limits | None = None) -> None:
        self.envs_dir = envs_dir
        self.limits = limits or Limits()

    @abstractmethod
    def available(self) -> tuple[bool, str]: ...

    @abstractmethod
    def prepare(
        self, snapshot_root: Path, snapshot_files: Sequence[str], snapshot_hash: str, config: RepoConfig
    ) -> PrepareResult: ...

    @abstractmethod
    def paths(self, workdir: Path, outdir: Path, agent_dir: Path | None) -> Paths: ...

    def attach_dependencies(self, env: EnvInfo, tree: Path, config: RepoConfig) -> None:  # noqa: B027
        """Make installed dependency directories (e.g. node_modules) available inside ``tree``."""

    @abstractmethod
    def execute(
        self,
        env: EnvInfo,
        argv: Sequence[str],
        *,
        workdir: Path,
        outdir: Path,
        agent_dir: Path | None = None,
        network: bool = False,
        timeout_s: float,
        extra_env: Mapping[str, str] | None = None,
        config: RepoConfig | None = None,
    ) -> ExecResult: ...


# --------------------------------------------------------------------------- local (not isolated)


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def link_dir(target: Path, link: Path) -> None:
    """Directory link without copying: a junction on Windows, a symlink elsewhere."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def resolve_command(argv: Sequence[str], env: Mapping[str, str], python: str, cwd: Path | None) -> list[str]:
    """Resolve argv[0] like a shell would, without using one (npm.cmd, gradlew.bat, ...)."""
    out = list(argv)
    if not out:
        return out
    head = out[0]
    if head == "python":
        out[0] = python
        return out
    if head.startswith(("./", ".\\")) and cwd is not None:
        base = cwd / head[2:]
        candidates = [base]
        if sys.platform == "win32":
            candidates = [base.with_name(base.name + ext) for ext in (".bat", ".cmd", ".exe")] + [base]
        for cand in candidates:
            if cand.is_file():
                out[0] = str(cand)
                return out
        return out
    found = shutil.which(head, path=env.get("PATH"))
    if found:
        out[0] = found
    return out


class LocalRunner(Runner):
    kind = "local"
    isolated = False

    def available(self) -> tuple[bool, str]:
        return True, f"host processes, Python {platform.python_version()} (not isolated)"

    def base_env(self, scratch: Path, python: str, config: RepoConfig | None, cache: str | None) -> dict[str, str]:
        env = {k: os.environ[k] for k in LOCAL_ENV_ALLOWLIST if k in os.environ}
        tmp = scratch / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env.update(
            TEMP=str(tmp),
            TMP=str(tmp),
            TMPDIR=str(tmp),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED="0",
            PYTHONUTF8="1",
            PYTHONNOUSERSITE="1",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            CI="1",
            NO_COLOR="1",
            FORCE_COLOR="0",
            DOTNET_CLI_TELEMETRY_OPTOUT="1",
            DOTNET_NOLOGO="1",
            DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
            # Tool output must be in English for the result parsers (localised Windows installs).
            DOTNET_CLI_UI_LANGUAGE="en",
            VSLANG="1033",
            # No lingering build servers (they would keep files locked and outlive the run).
            MSBUILDDISABLENODEREUSE="1",
            DOTNET_CLI_USE_MSBUILD_SERVER="0",
            UseSharedCompilation="false",
            GRADLE_OPTS="-Dorg.gradle.daemon=false",
        )
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        if config is not None:
            for key, value in config.env.items():
                env[key] = value.replace("{cache}", cache or str(tmp))
        return env

    def prepare(
        self, snapshot_root: Path, snapshot_files: Sequence[str], snapshot_hash: str, config: RepoConfig
    ) -> PrepareResult:
        spec = {
            "runner": self.kind,
            "python": sys.version,
            "executable": sys.executable,
            "install": [list(c) for c in config.install],
            "deps": list(config.dependency_dirs),
            "env": config.env,
            "snapshot": snapshot_hash,
        }
        key = sha256_json(spec)[:20]
        env_dir = self.envs_dir / f"local-{key}"
        info = EnvInfo(
            runner=self.kind,
            key=key,
            python=sys.executable,
            platform=platform.platform(),
            python_version=platform.python_version(),
            prepared_at=now_iso(),
            details={
                "isolated": False,
                "network_enforced": False,
                "limits_enforced": ["timeout", "log size"],
                "env_dir": str(env_dir),
                "cache": str(env_dir / "cache"),
                "context": str(env_dir / "context"),
            },
        )
        marker = env_dir / "ready.json"
        if marker.is_file():
            return PrepareResult("ready", EnvInfo.from_dict(json.loads(marker.read_text("utf-8"))), "reused\n")
        rmtree(env_dir)
        (env_dir / "cache").mkdir(parents=True)
        context = env_dir / "context"
        copy_files(snapshot_root, context, context_files(snapshot_files, config))
        log: list[str] = []
        if any(cmd[0] == "python" for cmd in config.install):
            res = run_process(
                [sys.executable, "-m", "venv", str(env_dir / "venv")],
                cwd=env_dir,
                env=None,
                timeout_s=config.prepare_timeout_s,
            )
            log.append(res.output)
            if res.exit_code != 0:
                return PrepareResult("setup_error", None, "".join(log))
            info.python = str(_venv_python(env_dir / "venv"))
            info.details["venv"] = str(env_dir / "venv")
        env = self.base_env(env_dir, info.python, config, str(env_dir / "cache"))
        for cmd in config.install:
            argv = resolve_command(cmd, env, info.python, context)
            res = run_process(
                argv, cwd=context, env=env, timeout_s=config.prepare_timeout_s, log_limit=self.limits.log_limit_bytes
            )
            log.append(f"$ {' '.join(cmd)}\n{res.output}\n")
            if res.exit_code != 0:
                return PrepareResult(classify_install_failure(res), None, "".join(log))
        missing = [d for d in config.dependency_dirs if not (context / d).is_dir()]
        if missing:
            log.append(f"dependency_dirs not produced by install: {', '.join(missing)}\n")
            return PrepareResult("setup_error", None, "".join(log))
        marker.write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")
        return PrepareResult("ready", info, "".join(log))

    def attach_dependencies(self, env: EnvInfo, tree: Path, config: RepoConfig) -> None:
        context = env.details.get("context")
        if not context:
            return
        for dep in config.dependency_dirs:
            source = Path(context) / dep
            target = tree / dep
            if source.is_dir() and not target.exists() and not is_link_like(target):
                link_dir(source, target)

    def paths(self, workdir: Path, outdir: Path, agent_dir: Path | None) -> Paths:
        return Paths(str(workdir), str(outdir), str(agent_dir) if agent_dir else None)

    def execute(
        self,
        env: EnvInfo,
        argv: Sequence[str],
        *,
        workdir: Path,
        outdir: Path,
        agent_dir: Path | None = None,
        network: bool = False,
        timeout_s: float,
        extra_env: Mapping[str, str] | None = None,
        config: RepoConfig | None = None,
    ) -> ExecResult:
        proc_env = self.base_env(outdir, env.python, config, env.details.get("cache"))
        if extra_env:
            proc_env.update(extra_env)
        resolved = resolve_command(argv, proc_env, env.python, workdir)
        return run_process(
            resolved, cwd=workdir, env=proc_env, timeout_s=timeout_s, log_limit=self.limits.log_limit_bytes
        )


# --------------------------------------------------------------------------- docker (isolated)

CONTAINER_WORKSPACE = "/workspace"
CONTAINER_OUTPUT = "/repoboss/out"
CONTAINER_AGENT = "/agent"
CONTAINER_CACHE = "/repoboss/cache"


def docker_available() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker executable not found on PATH"
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker not responding: {exc}"
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        return False, message.splitlines()[-1] if message else "docker daemon not reachable"
    return True, f"Docker server {proc.stdout.strip()}"


def render_dockerfile(base_image: str, install: Sequence[Sequence[str]], dependency_dirs: Sequence[str] = ()) -> str:
    if not IMAGE_RE.fullmatch(base_image):
        raise ValueError(f"invalid image reference: {base_image!r}")
    lines = [
        f"FROM {base_image}",
        "ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONHASHSEED=0 "
        "CI=1 NO_COLOR=1 DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1",
        f"WORKDIR {CONTAINER_WORKSPACE}",
        f"COPY context/ {CONTAINER_WORKSPACE}/",
    ]
    # Exec form (JSON array): arguments are never interpreted by a shell.
    lines.extend(f"RUN {json.dumps(list(cmd))}" for cmd in install)
    # Keep only the installed dependency directories: sources are copied in for every run, and the
    # original (bug-free) sources must not stay readable inside the image.
    find = ["find", CONTAINER_WORKSPACE, "-mindepth", "1", "(", "-type", "f", "-o", "-type", "l", ")"]
    for dep in dependency_dirs:
        find += ["!", "-path", f"{CONTAINER_WORKSPACE}/{dep}/*"]
    find += ["-delete"]
    lines.append(f"RUN {json.dumps(find)}")
    lines.append(f"RUN {json.dumps(['mkdir', '-p', CONTAINER_OUTPUT, CONTAINER_AGENT, CONTAINER_CACHE])}")
    return "\n".join(lines) + "\n"


def build_docker_create_argv(
    *,
    image: str,
    name: str,
    argv: Sequence[str],
    network: bool,
    limits: Limits,
    extra_env: Mapping[str, str],
) -> list[str]:
    cmd = [
        "docker",
        "create",
        "--name",
        name,
        "--network",
        "bridge" if network else "none",
        "--cpus",
        str(limits.cpus),
        "--memory",
        limits.memory,
        "--memory-swap",
        limits.memory,
        "--pids-limit",
        str(limits.pids),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        f"/tmp:rw,exec,size={limits.tmpfs_size}",  # noqa: S108 - path inside the container
        "--workdir",
        CONTAINER_WORKSPACE,
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONHASHSEED=0",
    ]
    for key, value in sorted(extra_env.items()):
        cmd += ["--env", f"{key}={value}"]
    cmd.append(image)
    cmd.extend(argv)
    return cmd


def tar_directory(src: Path, skip: frozenset[str] = frozenset()) -> bytes:
    """Regular files and directories of ``src`` as a tar archive (links are never included)."""
    buf = io.BytesIO()
    files, _odd = walk_files(src, skip=skip)
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in files:
            data = (src / rel).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if os.access(src / rel, os.X_OK) and not rel.endswith((".py", ".txt")) else 0o644
            info.uid = info.gid = 0
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def extract_tar_safely(archive: Path, dest: Path, *, strip: str, skip: frozenset[str]) -> list[str]:
    """Extract regular files from a ``docker cp`` archive. Returns rejected entries (links, devices).

    ``strip`` is the leading directory docker adds (e.g. ``workspace``). Paths are validated; links
    are reported, never created.
    """
    rejected: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="r:*") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if parts and parts[0] == strip:
                parts = parts[1:]
            if not parts:
                continue
            rel = "/".join(parts)
            if any(p in ("..", "") or p.startswith("/") for p in parts) or ":" in parts[0]:
                rejected.append(f"unsafe path: {member.name}")
                continue
            if parts[0] in skip or any(p in NOISE_DIR_NAMES for p in parts[:-1]):
                continue
            if member.isdir():
                (dest / rel).mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(handle.read())
            else:
                rejected.append(f"link or special file: {rel}")
    return rejected


class DockerRunner(Runner):
    kind = "docker"
    isolated = True

    def available(self) -> tuple[bool, str]:
        return docker_available()

    def _inspect(self, ref: str, fmt: str) -> str | None:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", fmt, ref],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

    def prepare(
        self, snapshot_root: Path, snapshot_files: Sequence[str], snapshot_hash: str, config: RepoConfig
    ) -> PrepareResult:
        ok, why = self.available()
        if not ok:
            return PrepareResult("runner_unavailable", None, why)
        spec = {
            "runner": self.kind,
            "image": config.docker_image,
            "install": [list(c) for c in config.install],
            "deps": list(config.dependency_dirs),
            "snapshot": snapshot_hash,
        }
        key = sha256_json(spec)[:20]
        tag = f"repoboss-env:{key[:16]}"
        log = ""
        if self._inspect(tag, "{{.Id}}") is None:
            with tempfile.TemporaryDirectory(prefix="repoboss-build-") as tmp:
                ctx = Path(tmp)
                copy_files(snapshot_root, ctx / "context", context_files(snapshot_files, config))
                dockerfile = render_dockerfile(config.docker_image, config.install, config.dependency_dirs)
                (ctx / "Dockerfile").write_text(dockerfile, encoding="utf-8")
                res = run_process(
                    ["docker", "build", "--pull", "-t", tag, str(ctx)],
                    cwd=ctx,
                    env=None,
                    timeout_s=config.prepare_timeout_s,
                    log_limit=self.limits.log_limit_bytes,
                )
                log = res.output
                if res.exit_code != 0:
                    return PrepareResult(classify_install_failure(res), None, log)
        digests = self._inspect(config.docker_image, "{{json .RepoDigests}}")
        info = EnvInfo(
            runner=self.kind,
            key=key,
            python="python",
            platform=f"docker on {platform.system()}",
            image=tag,
            image_id=self._inspect(tag, "{{.Id}}"),
            base_image=config.docker_image,
            base_digest=(json.loads(digests)[0] if digests and digests != "[]" else None),
            prepared_at=now_iso(),
            details={
                "isolated": True,
                "network_default": "none",
                "limits": asdict(self.limits),
                "cache": CONTAINER_CACHE,
            },
        )
        return PrepareResult("ready", info, log)

    def paths(self, workdir: Path, outdir: Path, agent_dir: Path | None) -> Paths:
        return Paths(CONTAINER_WORKSPACE, CONTAINER_OUTPUT, CONTAINER_AGENT if agent_dir else None)

    def _copy_in(self, name: str, src: Path, target: str, skip: frozenset[str]) -> None:
        subprocess.run(
            ["docker", "cp", "-", f"{name}:{target}"],
            input=tar_directory(src, skip),
            capture_output=True,
            timeout=600,
            check=True,
        )

    def _copy_out(self, name: str, source: str, dest: Path, skip: frozenset[str]) -> list[str]:
        with tempfile.TemporaryDirectory(prefix="repoboss-cp-") as tmp:
            archive = Path(tmp) / "out.tar"
            with archive.open("wb") as handle:
                subprocess.run(
                    ["docker", "cp", f"{name}:{source}", "-"],
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    timeout=600,
                    check=True,
                )
            fresh = Path(tmp) / "fresh"
            rejected = extract_tar_safely(archive, fresh, strip=PurePosixPath(source).name, skip=skip)
            # Keep host-side dependency links (local runs) untouched; replace everything else.
            rmtree(dest)
            shutil.move(str(fresh), str(dest))
        return rejected

    def execute(
        self,
        env: EnvInfo,
        argv: Sequence[str],
        *,
        workdir: Path,
        outdir: Path,
        agent_dir: Path | None = None,
        network: bool = False,
        timeout_s: float,
        extra_env: Mapping[str, str] | None = None,
        config: RepoConfig | None = None,
    ) -> ExecResult:
        if env.image is None:
            raise ValueError("docker environment has no image")
        name = new_id("repoboss")
        env_vars = dict(extra_env or {})
        if config is not None:
            for key, value in config.env.items():
                env_vars.setdefault(key, value.replace("{cache}", CONTAINER_CACHE))
        skip = skip_names(config) if config else frozenset()
        create = build_docker_create_argv(
            image=env.image, name=name, argv=argv, network=network, limits=self.limits, extra_env=env_vars
        )
        made = subprocess.run(create, capture_output=True, text=True, timeout=120, check=False)
        if made.returncode != 0:
            msg = f"docker create failed: {made.stderr.strip()}"
            return ExecResult(list(argv), None, False, 0.0, msg, len(msg), False, msg)
        try:
            self._copy_in(name, workdir, CONTAINER_WORKSPACE, skip)
            self._copy_in(name, outdir, CONTAINER_OUTPUT, frozenset())
            if agent_dir is not None:
                self._copy_in(name, agent_dir, CONTAINER_AGENT, frozenset())

            def _kill() -> None:
                subprocess.run(["docker", "kill", name], capture_output=True, timeout=30, check=False)

            result = run_process(
                ["docker", "start", "-a", name],
                cwd=None,
                env=None,
                timeout_s=timeout_s,
                log_limit=self.limits.log_limit_bytes,
                on_timeout=_kill,
            )
            result.argv = list(argv)
            rejected = self._copy_out(name, CONTAINER_WORKSPACE, workdir, skip)
            self._copy_out(name, CONTAINER_OUTPUT, outdir, frozenset())
            result.rejected = rejected
            return result
        except subprocess.SubprocessError as exc:
            msg = f"docker file transfer failed: {exc}"
            return ExecResult(list(argv), None, False, 0.0, msg, len(msg), False, msg)
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)


RUNNERS: dict[str, type[Runner]] = {"local": LocalRunner, "docker": DockerRunner}


def get_runner(kind: str, envs_dir: Path, limits: Limits | None = None) -> Runner:
    if kind not in RUNNERS:
        raise ValueError(f"unknown runner {kind!r}; use 'docker' or 'local'")
    return RUNNERS[kind](envs_dir, limits)
