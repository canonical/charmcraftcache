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
)


class State:
    def __init__(self):
        self.verbose = False

    @property
    def verbose(self):
        return self._verbose

    @verbose.setter
    def verbose(self, value: bool):
        self._verbose = value
        log_format = "[charmcraftcache] {message}"
        if value:
            log_format = "{asctime} " + log_format
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        logger.removeHandler(handler)
        handler.setFormatter(
            logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S", style="{")
        )
        logger.addHandler(handler)


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


@app.command()
def pack(verbose_: Verbose = False):
    if verbose_:
        # Verbose can be globally enabled from command level or app level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
    logger.info("Resolving dependencies")
    # todo: check if requirements.txt exists
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
    dependencies = report["install"]
    # Pack charm
    # TODO: remove hardcoded path
    build_base_cache_subdirectory = (
        charmcraft_cache_subdirectory
        / "charmcraft-buildd-base-v5.0/BuilddBaseAlias.JAMMY"
    )
    build_base_cache_subdirectory.mkdir(parents=True, exist_ok=True)
    logger.debug("Getting latest charmcraftcache-hub release via GitHub API")
    response = requests.get(
        "https://api.github.com/repos/carlcsaposs-canonical/charmcraftcache-hub/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    response.raise_for_status()
    clean_cache_if_version_changed(
        VersionType.CHARMCRAFTCACHE_HUB, response.json()["name"].split("-")[-1]
    )
    for dependency in rich.progress.track(
        dependencies,
        description="\[charmcraftcache] Downloading wheels",
        console=console,
    ):
        dependency_name = dependency["metadata"]["name"]
        dependency_version = dependency["metadata"]["version"]
        for asset in response.json()["assets"]:
            if asset["name"].startswith(
                f'{dependency_name.replace("-", "_")}-{dependency_version}-'
            ):
                # todo: wrap request, handle rate limit?
                # todo: add github auth for rate limit?
                name, parent = (
                    asset["name"]
                    .removesuffix(".charmcraftcachehub")
                    .split(".charmcraftcachehub.")
                )
                parent = parent.replace("_", "/")
                file_path = build_base_cache_subdirectory / parent / name
                if file_path.exists():
                    logger.debug(f"{name} already downloaded")
                else:
                    # Download wheel
                    response = requests.get(
                        f'https://api.github.com/repos/carlcsaposs-canonical/charmcraftcache-hub/releases/assets/{asset["id"]}',
                        headers={
                            "Accept": "application/octet-stream",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                        stream=True,
                    )
                    response.raise_for_status()
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "wb") as file:
                        for chunk in response.iter_content():
                            file.write(chunk)
                    logger.debug(f"Downloaded {name}")
                break
        else:
            # TODO: improve message
            logger.warning(
                f"No cached wheel found for {dependency_name} {dependency_version}"
            )
    logger.info("Packing charm")
    run_charmcraft(["pack"])


def clean_cache():
    logger.info("Deleting cached wheels")
    try:
        shutil.rmtree(cache_directory)
    except FileNotFoundError:
        pass
    cache_directory.mkdir(parents=True, exist_ok=True)


@app.command()
def clean():
    clean_cache()
    logger.info("Running `charmcraft clean`")
    run_charmcraft(["clean"])


# todo: add command for adding charm to charmcraftcache-hub


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


cache_directory = pathlib.Path("~/.cache/charmcraftcache/").expanduser()
cache_directory.mkdir(parents=True, exist_ok=True)
charmcraft_cache_subdirectory = cache_directory / "charmcraft"
state = State()
clean_cache_if_version_changed(
    VersionType.CHARMCRAFTCACHE, importlib.metadata.version("charmcraftcache")
)
