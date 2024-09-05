import dataclasses
import datetime
import enum
import importlib.metadata
import json
import logging
import os
import pathlib
import platform
import shutil
import subprocess
import sys

import packaging.utils
import packaging.version
import requests
import rich
import rich.console
import rich.highlighter
import rich.logging
import rich.progress
import typer
import typing_extensions
import yaml

app = typer.Typer(help="Fast first-time builds for charmcraft")
Verbose = typing_extensions.Annotated[bool, typer.Option("--verbose", "-v")]
if os.environ.get("CI") == "true":
    # Show colors in CI (https://rich.readthedocs.io/en/stable/console.html#terminal-detection)
    console = rich.console.Console(
        highlight=False, force_terminal=True, force_interactive=False
    )
else:
    console = rich.console.Console(highlight=False)
logger = logging.getLogger(__name__)
handler = rich.logging.RichHandler(
    console=console,
    show_time=False,
    omit_repeated_times=False,
    show_level=False,
    show_path=False,
    highlighter=rich.highlighter.NullHighlighter(),
    markup=True,
)


class WarningFormatter(logging.Formatter):
    """Only show log level if level >= logging.WARNING or verbose enabled"""

    def format(self, record):
        if record.levelno >= logging.WARNING or state.verbose:
            level = handler.get_level_text(record)
            # Rich adds padding to level—remove it
            level.rstrip()
            replacement = f"{level.markup} "
        else:
            replacement = ""
        old_format = self._style._fmt
        self._style._fmt = old_format.replace("{levelname} ", replacement)
        result = super().format(record)
        self._style._fmt = old_format
        return result


class State:
    def __init__(self):
        self._verbose = None
        self.verbose = False

    @property
    def verbose(self):
        return self._verbose

    @verbose.setter
    def verbose(self, value: bool):
        if value == self._verbose:
            return
        self._verbose = value
        log_format = "\[charmcraftcache] {levelname} {message}"
        if value:
            log_format = "{asctime} " + log_format
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        logger.removeHandler(handler)
        handler.setFormatter(
            WarningFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S", style="{")
        )
        logger.addHandler(handler)
        logger.debug(f"Version: {installed_version}")


@dataclasses.dataclass(frozen=True, kw_only=True)
class Dependency:
    name: packaging.utils.NormalizedName
    version: packaging.version.Version
    series: str
    architecture: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Asset:
    path: pathlib.Path
    download_url: str
    name: str
    size: int


def run_charmcraft(command: list[str]):
    try:
        version = json.loads(
            subprocess.run(
                ["charmcraft", "version", "--format", "json"],
                capture_output=True,
                check=True,
                encoding="utf-8",
            ).stdout
        )["version"]
    except FileNotFoundError:
        version = None
    if packaging.version.parse(version or "0.0.0") < packaging.version.parse("2.5.4"):
        raise Exception(
            f'charmcraft {version or "not"} installed. charmcraft >=2.5.4 required'
        )
    env = os.environ
    env["CRAFT_SHARED_CACHE"] = str(charmcraft_cache_subdirectory)
    charmcraft_cache_subdirectory.mkdir(parents=True, exist_ok=True)
    if state.verbose:
        command.append("-v")
    try:
        subprocess.run(["charmcraft", *command], check=True, env=env)
    except subprocess.CalledProcessError as exception:
        # `charmcraft` stderr will be shown in terminal, no need to raise exception—just log
        # traceback.
        logger.exception("charmcraft command failed:")
        exit(exception.returncode)


def exit_for_rate_limit(response: requests.Response):
    """Display error & exit if rate limit exceeded"""
    if response.status_code not in (403, 429):
        return
    # https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28#exceeding-the-rate-limit
    if int(response.headers.get("x-ratelimit-remaining", -1)) == 0 and (
        reset := response.headers.get("x-ratelimit-reset")
    ):
        retry_time = datetime.datetime.fromtimestamp(
            float(reset), tz=datetime.timezone.utc
        )
        retry_delta = retry_time - datetime.datetime.now(tz=datetime.timezone.utc)
    else:
        if after := response.headers.get("retry-after"):
            retry_delta = datetime.timedelta(seconds=float(after))
        else:
            retry_delta = datetime.timedelta(seconds=60)
        retry_time = datetime.datetime.now(tz=datetime.timezone.utc) + retry_delta
    # Round delta
    retry_delta = datetime.timedelta(seconds=round(retry_delta.total_seconds()))
    # Use try/except to chain exception
    try:
        response.raise_for_status()
    except requests.HTTPError:
        message = (
            f"GitHub API rate limit exceeded. Retry in {retry_delta} at {retry_time.astimezone()}. "
            "Seeing this often? Please add a comment to this issue: "
            "https://github.com/canonical/charmcraftcache/issues/1"
        )
        if not os.environ.get("GH_TOKEN"):
            message += "\nIf running in CI, pass `GH_TOKEN` environment variable"
        raise Exception(message)


def get_charmcraft_yaml_bases(
    *, charmcraft_yaml: pathlib.Path, architecture: str
) -> list[str]:
    """Get bases from charmcraft.yaml

    e.g. ["20.04", "22.04"]
    """
    bases = yaml.safe_load(charmcraft_yaml.read_text())["bases"]
    versions = []
    for base in bases:
        # Handle multiple bases formats
        # See https://discourse.charmhub.io/t/charmcraft-bases-provider-support/4713
        build_on = base.get("build-on")
        if build_on:
            assert isinstance(build_on, list) and len(build_on) == 1
            base = build_on[0]
        build_on_architectures = base.get("architectures", ["amd64"])
        assert (
            len(build_on_architectures) == 1
        ), f"Multiple architectures ({build_on_architectures}) in one (charmcraft.yaml) base not supported. Use one base per architecture"
        if build_on_architectures[0] == CHARMCRAFT_ARCHITECTURES[architecture]:
            versions.append(base["channel"])
    return versions


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def pack(context: typer.Context, verbose: Verbose = False):
    """Download pre-built wheels & `charmcraft pack`

    Unrecognized command arguments are passed to `charmcraft pack`
    """
    if verbose:
        # Verbose can be globally enabled from command level or app level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
    if context.args:
        logger.info(
            f'Passing unrecognized arguments to `charmcraft pack`: {" ".join(context.args)}'
        )
    logger.info("Resolving dependencies")
    if not pathlib.Path("requirements.txt").exists():
        if not pathlib.Path("charmcraft.yaml").exists():
            raise FileNotFoundError(
                "requirements.txt not found. `cd` into the directory with charmcraft.yaml"
            )
        else:
            raise FileNotFoundError(
                "requirements.txt not found. Are you using a pack wrapper (e.g. `tox run -e build-dev`)? If so, call charmcraftcache via the wrapper."
            )
    report_file = cache_directory / "report.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "-r",
            "requirements.txt",
            "--ignore-installed",
            "--report",
            str(report_file),
        ],
        stdout=None if state.verbose else subprocess.DEVNULL,
        check=True,
    )
    with open(report_file, "r") as file:
        report = json.load(file)
    dependencies = []
    charmcraft_yaml = pathlib.Path("charmcraft.yaml")
    architecture = platform.machine()
    bases = get_charmcraft_yaml_bases(
        charmcraft_yaml=charmcraft_yaml, architecture=architecture
    )
    binary_packages: list[str] = (
        yaml.safe_load(charmcraft_yaml.read_text())
        .get("parts", {})
        .get("charm", {})
        .get("charm-binary-python-packages", [])
    )
    for dependency in report["install"]:
        wheel_file_name = dependency["metadata"]["name"]
        if wheel_file_name in binary_packages:
            logger.debug(
                f"{wheel_file_name} in charm-binary-python-packages. Skipping wheel download"
            )
            continue
        for base in bases:
            dependencies.append(
                Dependency(
                    name=packaging.utils.canonicalize_name(
                        wheel_file_name, validate=True
                    ),
                    version=packaging.version.Version(
                        dependency["metadata"]["version"]
                    ),
                    series=SERIES[base],
                    architecture=architecture,
                )
            )
    logger.debug("Getting latest charmcraftcache-hub release via GitHub API")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    etag_file = cache_directory / "latest_release_etag.txt"
    try:
        etag = etag_file.read_text()
    except FileNotFoundError:
        pass
    else:
        headers["If-None-Match"] = etag
    if github_token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {github_token}"
    response = requests.get(
        "https://api.github.com/repos/canonical/charmcraftcache-hub/releases/latest",
        headers=headers,
    )
    exit_for_rate_limit(response)
    response.raise_for_status()
    response_data_file = cache_directory / "latest_release.json"
    if response.status_code == 304:
        logger.debug("HTTP cache hit for latest release")
        with open(response_data_file, "r") as file:
            response_data = json.load(file)
    else:
        logger.debug("HTTP cache miss for latest release")
        response_data = response.json()
        with open(response_data_file, "w") as file:
            json.dump(response_data, file)
        etag_file.write_text(response.headers["ETag"])
    # Example: build-1702562019-v1
    release_name = response_data["name"]
    # Example: v1
    hub_version = release_name.split("-")[-1]
    clean_cache_if_version_changed(VersionType.CHARMCRAFTCACHE_HUB, hub_version)
    missing_wheels = 0
    assets = {}
    # TODO: remove hardcoded paths
    build_base_subdirectory = (
        charmcraft_cache_subdirectory / "charmcraft-buildd-base-v7"
    )
    build_base_subdirectory.mkdir(parents=True, exist_ok=True)
    # Backwards compatability for charmcraft 2.7
    c27_base_subdirectory = (
        charmcraft_cache_subdirectory / "charmcraft-buildd-base-v8.0"
    )
    if not c27_base_subdirectory.is_symlink():
        try:
            shutil.rmtree(c27_base_subdirectory)
        except FileNotFoundError:
            pass
        c27_base_subdirectory.symlink_to(build_base_subdirectory)
    logger.debug(
        f'Selecting wheels for Ubuntu versions ({architecture}): {", ".join(bases)}'
    )
    for dependency in dependencies:
        for asset in response_data["assets"]:
            wheel_file_name, rest = asset["name"].split(".ccchub1.")
            # https://packaging.python.org/en/latest/specifications/binary-distribution-format/#file-name-convention
            wheel_package_name, wheel_version, *_ = (
                packaging.utils.parse_wheel_filename(wheel_file_name)
            )
            if (
                wheel_package_name == dependency.name
                and wheel_version == dependency.version
            ):
                series, rest = rest.split(".ccchub2.")
                architecture_, rest = rest.split(".ccchub3.")
                parent = rest.removesuffix(".charmcraftcachehub").split("_")
                file_path = (
                    build_base_subdirectory
                    / f"BuilddBaseAlias.{series.upper()}"
                    / "/".join(parent)
                    / wheel_file_name
                )
                if series != dependency.series:
                    continue
                if architecture_ != dependency.architecture:
                    continue
                if file_path.exists():
                    logger.debug(
                        f"{wheel_file_name} already downloaded for {dependency.series}"
                    )
                else:
                    assets[dependency] = Asset(
                        path=file_path,
                        download_url=asset["browser_download_url"],
                        name=wheel_file_name,
                        size=asset["size"],
                    )
                break
        else:
            missing_wheels += 1
            logger.debug(
                f"No pre-built wheel found for {dependency.name} {dependency.version} {dependency.series} {dependency.architecture}"
            )
    if missing_wheels:
        logger.warning(
            f'{missing_wheels} wheel{"s" if missing_wheels > 1 else ""} not pre-built. Run `ccc add` for faster builds.'
        )
    with rich.progress.Progress(console=console) as progress:
        task = progress.add_task(
            description="\[charmcraftcache] Downloading wheels",
            total=sum(asset.size for asset in assets.values()),
        )
        # Use temporary path in case download is interrupted
        temporary_path = cache_directory / "current.whl.part"
        for dependency, asset in assets.items():
            # Download wheel
            response = requests.get(asset.download_url, stream=True)
            exit_for_rate_limit(response)
            response.raise_for_status()
            chunk_size = 1
            with open(temporary_path, "wb") as file:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    file.write(chunk)
                    progress.update(task, advance=chunk_size)
            asset.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(temporary_path, asset.path)
            logger.debug(f"Downloaded {asset.name} for {dependency.series}")
        if not assets:
            # Set progress as completed if no wheels downloaded
            progress.update(task, completed=1, total=1)
    logger.info("Packing charm")
    run_charmcraft(["pack", *context.args])


def clean_cache():
    logger.info("Deleting cached wheels")
    try:
        shutil.rmtree(charmcraft_cache_subdirectory)
    except FileNotFoundError:
        pass
    charmcraft_cache_subdirectory.mkdir(parents=True, exist_ok=True)


@app.command()
def clean(verbose: Verbose = False):
    """Delete cached wheels & `charmcraft clean`"""
    if verbose:
        # Verbose can be globally enabled from app level or command level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
    clean_cache()
    logger.info("Running `charmcraft clean`")
    run_charmcraft(["clean"])


def get_remote_branch_and_url() -> tuple[str, str] | None:
    """Get remote branch name & GitHub repository name for current branch"""
    try:
        local_branch = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "HEAD"],
            capture_output=True,
            encoding="utf-8",
        ).stdout.rstrip()
    except FileNotFoundError:
        logger.debug("git not installed")
        return
    if not local_branch:
        return
    output = subprocess.run(
        ["git", "for-each-ref", "--format", "%(upstream:short)", local_branch],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.rstrip()
    if not output:
        return
    remote_name, *remote_branch = output.split("/")
    remote_branch = "/".join(remote_branch)
    remote_url = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.rstrip()
    remote_url = remote_url.removesuffix(".git")
    for prefix in ("git@github.com:", "https://github.com/"):
        if remote_url.startswith(prefix):
            repo_name = remote_url.removeprefix(prefix)
            return remote_branch, repo_name


@app.command()
def add(verbose: Verbose = False):
    """Pre-build wheels for your charm

    charmcraftcache uses a repository of pre-built wheels generated from a list of charms. For the
    best performance, add your charm to the list.
    """
    if verbose:
        # Verbose can be globally enabled from app level or command level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
    issue_url = "https://github.com/canonical/charmcraftcache-hub/issues/new?template=add_charm_branch.yaml&labels=add-charm&title=Add+charm+branch"
    result = get_remote_branch_and_url()
    if result:
        remote_branch, repo_name = result
        issue_url += f"&repo={repo_name}&ref={remote_branch}"
    logger.info(
        f"To add your charm, open an issue here:\n\n[link={issue_url}]{issue_url}[/link]\n"
    )
    typer.launch(issue_url)
    logger.info(
        "After the issue is opened, it will be automatically processed. Then, it will take a few minutes to build the wheels."
    )


@app.callback()
def main(verbose: Verbose = False):
    if verbose:
        # Verbose can be globally enabled from app level or command level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True


class VersionType(str, enum.Enum):
    """Type of version number that clears cache directory when changed"""

    CHARMCRAFTCACHE = "charmcraftcache"  # This python package
    CHARMCRAFTCACHE_HUB = (
        "charmcraftcachehub"  # GitHub repository where wheels are built
    )


def clean_cache_if_version_changed(version_type: VersionType, current_version: str):
    file = cache_directory / f"{version_type}_version.txt"
    try:
        last_version = file.read_text()
    except FileNotFoundError:
        pass
    else:
        if last_version != current_version:
            logger.info(
                f"{version_type} update from {last_version} to {current_version} detected. Cleaning cache"
            )
            clean_cache()
    finally:
        file.write_text(current_version)


SERIES = {"20.04": "focal", "22.04": "jammy"}
CHARMCRAFT_ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}
installed_version = importlib.metadata.version("charmcraftcache")
state = State()
cache_directory = pathlib.Path("~/.cache/charmcraftcache/").expanduser()
cache_directory.mkdir(parents=True, exist_ok=True)
charmcraft_cache_subdirectory = cache_directory / "charmcraft"
clean_cache_if_version_changed(VersionType.CHARMCRAFTCACHE, installed_version)
response_ = requests.get("https://pypi.org/pypi/charmcraftcache/json")
response_.raise_for_status()
latest_pypi_version = response_.json()["info"]["version"]
if installed_version != latest_pypi_version:
    logger.info(
        f"Update available. Run `pipx upgrade charmcraftcache` ({installed_version} -> {latest_pypi_version})"
    )
