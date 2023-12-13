import json
import os
import pathlib
import shutil
import subprocess
import sys
import typing_extensions

import requests
import rich
import rich.console
import rich.progress
import rich.logging
import rich.highlighter
import logging
import typer

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


@app.command()
def pack(verbose_: Verbose = False):
    if verbose_:
        # Verbose can be globally enabled from command level or app level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
    cache_directory.mkdir(parents=True, exist_ok=True)
    logger.info("Resolving dependencies")
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
    env = os.environ
    env["CRAFT_SHARED_CACHE"] = str(cache_directory)
    # TODO: remove hardcoded path
    charmcraft_cache_subdirectory = (
        cache_directory / "charmcraft-buildd-base-v5.0/BuilddBaseAlias.JAMMY"
    )
    charmcraft_cache_subdirectory.mkdir(parents=True, exist_ok=True)
    logger.debug("Getting latest charmcraftcache-hub release via GitHub API")
    response = requests.get(
        "https://api.github.com/repos/carlcsaposs-canonical/charmcraftcache-hub/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    response.raise_for_status()
    for asset in rich.progress.track(
        response.json()["assets"],
        description="\[charmcraftcache] Downloading wheels",
        console=console,
    ):
        for dependency in dependencies:
            if asset["name"].startswith(
                f'{dependency["metadata"]["name"].replace("-", "_")}-{dependency["metadata"]["version"]}-'
            ):
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
                name, parent = (
                    asset["name"]
                    .removesuffix(".charmcraftcachehub")
                    .split(".charmcraftcachehub.")
                )
                parent = parent.replace("_", "/")
                file_path = charmcraft_cache_subdirectory / parent / name
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, "wb") as file:
                    for chunk in response.iter_content():
                        file.write(chunk)
                logger.debug(f"Downloaded {name}")
                break
    logger.info("Packing charm")
    command = ["charmcraft", "pack"]
    if state.verbose:
        command.append("-v")
    try:
        subprocess.run(command, check=True, env=env)
    except FileNotFoundError:
        raise Exception("charmcraft not installed")
    except subprocess.CalledProcessError as e:
        raise Exception(e.stderr)


@app.command()
def clean():
    # TODO: add status output
    try:
        shutil.rmtree(cache_directory)
    except FileNotFoundError:
        pass
    subprocess.run(["charmcraft", "clean"], check=True)


@app.callback()
def main(verbose: Verbose = False):
    if verbose:
        # Verbose can be globally enabled from app level or command level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True


cache_directory = pathlib.Path("~/.cache/charmcraftcache/").expanduser()
state = State()
