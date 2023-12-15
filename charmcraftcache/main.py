import dataclasses
import datetime
import enum
import importlib.metadata
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys

import packaging.version
import requests
import rich
import rich.console
import rich.highlighter
import rich.logging
import rich.progress
import typer
import typing_extensions

app = typer.Typer()
Verbose = typing_extensions.Annotated[bool, typer.Option("--verbose", "-v")]
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
        self.verbose = False

    @property
    def verbose(self):
        return self._verbose

    @verbose.setter
    def verbose(self, value: bool):
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


@dataclasses.dataclass(frozen=True, kw_only=True)
class Dependency:
    name: str
    version: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class Asset:
    path: pathlib.Path
    id: int
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
    except subprocess.CalledProcessError as e:
        raise Exception(e.stderr)


def exit_for_rate_limit(response: requests.Response):
    """Display error & exit if rate limit exceeded"""
    if response.status_code not in (403, 429):
        return
    # https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api?apiVersion=2022-11-28#exceeding-the-rate-limit
    if int(response.headers.get("x-ratelimit-remaining")) == 0 and (
        reset := response.headers.get("x-ratelimit-reset")
    ):
        retry_time = datetime.datetime.fromtimestamp(
            float(reset), tz=datetime.timezone.utc
        )
        retry_delta = retry_time - datetime.datetime.now(tz=datetime.UTC)
    else:
        if after := response.headers.get("retry-after"):
            retry_delta = datetime.timedelta(seconds=float(after))
        else:
            retry_delta = datetime.timedelta(seconds=60)
        retry_time = datetime.datetime.now(tz=datetime.UTC) + retry_delta
    # Use try/except to chain exception
    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise Exception(
            f"GitHub API rate limit exceeded. Retry in {retry_delta} at {retry_time.astimezone()}"
        )


@app.command()
def pack(verbose: Verbose = False):
    if verbose:
        # Verbose can be globally enabled from command level or app level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
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
    dependencies = [
        Dependency(
            name=dependency["metadata"]["name"],
            version=dependency["metadata"]["version"],
        )
        for dependency in report["install"]
    ]
    # TODO: remove hardcoded path
    build_base_subdirectory = (
        charmcraft_cache_subdirectory
        / "charmcraft-buildd-base-v5.0/BuilddBaseAlias.JAMMY"
    )
    build_base_subdirectory.mkdir(parents=True, exist_ok=True)
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
    response = requests.get(
        "https://api.github.com/repos/carlcsaposs-canonical/charmcraftcache-hub/releases/latest",
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
    for dependency in dependencies:
        for asset in response_data["assets"]:
            if asset["name"].startswith(
                f'{dependency.name.replace("-", "_")}-{dependency.version}-'
            ):
                name, parent = (
                    asset["name"]
                    .removesuffix(".charmcraftcachehub")
                    .split(".charmcraftcachehub.")
                )
                parent = parent.replace("_", "/")
                file_path = build_base_subdirectory / parent / name
                if file_path.exists():
                    logger.debug(f"{name} already downloaded")
                else:
                    assets[dependency] = Asset(
                        path=file_path, id=asset["id"], name=name, size=asset["size"]
                    )
                break
        else:
            missing_wheels += 1
            logger.debug(
                f"No cached wheel found for {dependency.name} {dependency.version}"
            )
    if missing_wheels:
        # TODO: improve message
        logger.warning(
            f'{missing_wheels} cached wheel{"s" if missing_wheels > 1 else ""} not found.'
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
            response = requests.get(
                f"https://api.github.com/repos/carlcsaposs-canonical/charmcraftcache-hub/releases/assets/{asset.id}",
                headers={
                    "Accept": "application/octet-stream",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                stream=True,
            )
            exit_for_rate_limit(response)
            response.raise_for_status()
            chunk_size = 1
            with open(temporary_path, "wb") as file:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    file.write(chunk)
                    progress.update(task, advance=chunk_size)
            asset.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(temporary_path, asset.path)
            logger.debug(f"Downloaded {asset.name}")
        if not assets:
            # Set progress as completed if no wheels downloaded
            progress.update(task, completed=1, total=1)
    logger.info("Packing charm")
    run_charmcraft(["pack"])


def clean_cache():
    logger.info("Deleting cached wheels")
    try:
        shutil.rmtree(charmcraft_cache_subdirectory)
    except FileNotFoundError:
        pass
    charmcraft_cache_subdirectory.mkdir(parents=True, exist_ok=True)


@app.command()
def clean(verbose: Verbose = False):
    if verbose:
        # Verbose can be globally enabled from app level or command level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
    clean_cache()
    logger.info("Running `charmcraft clean`")
    run_charmcraft(["clean"])


# todo: add command for adding charm to charmcraftcache-hub
# todo: add github auth for rate limit? https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-with-a-github-app-on-behalf-of-a-user


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


state = State()
cache_directory = pathlib.Path("~/.cache/charmcraftcache/").expanduser()
cache_directory.mkdir(parents=True, exist_ok=True)
charmcraft_cache_subdirectory = cache_directory / "charmcraft"
clean_cache_if_version_changed(
    VersionType.CHARMCRAFTCACHE, importlib.metadata.version("charmcraftcache")
)
