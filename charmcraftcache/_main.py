import collections
import dataclasses
import datetime
import importlib.metadata
import json
import logging
import os
import pathlib
import platform
import shutil
import subprocess
import tarfile

import packaging.version
import requests
import rich.console
import rich.progress
import rich.text
import typer
import typing_extensions
import yaml

from . import _platforms

app = typer.Typer(help="Fast first-time builds for charmcraft")
Verbose = typing_extensions.Annotated[bool, typer.Option("--verbose", "-v")]
running_in_ci = os.environ.get("CI") == "true"
if running_in_ci:
    # Show colors in CI (https://rich.readthedocs.io/en/stable/console.html#terminal-detection)
    console = rich.console.Console(highlight=False, color_system="truecolor")
else:
    console = rich.console.Console(highlight=False)
logger = logging.getLogger(__name__)


class RichHandler(logging.Handler):
    """Use rich to print logs"""

    def emit(self, record):
        try:
            message = self.format(record)
            if getattr(record, "disable_wrap", False):
                console.print(message, overflow="ignore", crop=False)
            else:
                console.print(message)
        except Exception:
            self.handleError(record)


handler = RichHandler()


class WarningFormatter(logging.Formatter):
    """Only show log level if level >= logging.WARNING or verbose enabled"""

    def format(self, record):
        if record.levelno >= logging.WARNING or state.verbose:
            level = rich.text.Text(record.levelname, f"logging.level.{record.levelname.lower()}")
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
        handler.setFormatter(WarningFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S", style="{"))
        logger.addHandler(handler)
        logger.debug(f"Version: {installed_version}")


def run_charmcraft(command: list[str], *, charmcraft_cache_dir: pathlib.Path = None):
    try:
        version = json.loads(
            subprocess.run(
                ["charmcraft", "version", "--format", "json"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout
        )["version"]
    except FileNotFoundError:
        version = None
    if packaging.version.parse(version or "0.0.0") < packaging.version.parse("3.3.0"):
        raise Exception(f'charmcraft {version or "not"} installed. charmcraft >=3.3.0 required')
    env = os.environ
    if charmcraft_cache_dir:
        env["CRAFT_SHARED_CACHE"] = str(charmcraft_cache_dir)
        charmcraft_cache_dir.mkdir(parents=True, exist_ok=True)
    command = ["charmcraft", *command]
    if state.verbose:
        command.append("-v")
    try:
        logger.debug(f"Running {command} with {charmcraft_cache_dir=}")
        subprocess.run(command, check=True, env=env)
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
        retry_time = datetime.datetime.fromtimestamp(float(reset), tz=datetime.timezone.utc)
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
            f"GitHub API rate limit exceeded. Retry in {retry_delta} at "
            f"{retry_time.astimezone()}. Seeing this often? Please add a comment to this issue: "
            "https://github.com/canonical/charmcraftcache/issues/1"
        )
        if not os.environ.get("GH_TOKEN"):
            message += "\nIf running in CI, pass `GH_TOKEN` environment variable"
        raise Exception(message)


class UnableToDetectRelativePath(Exception):
    """Unable to detect relative path to charmcraft.yaml from git repository root"""


def get_relative_path_to_charmcraft_yaml():
    """Get relative path to charmcraft.yaml from git repository root"""
    assert pathlib.Path("charmcraft.yaml").exists()
    try:
        output = subprocess.run(
            ["git", "rev-parse", "--show-prefix"], capture_output=True, check=True, text=True
        ).stdout.strip()
    except FileNotFoundError:
        raise UnableToDetectRelativePath(
            "Git not installed. Unable to detect relative path to charmcraft.yaml from git "
            "repository root"
        )
    except subprocess.CalledProcessError as exception:
        if "not a git repository" in exception.stderr:
            raise UnableToDetectRelativePath(
                "Not in a git repository. Unable to detect relative path to charmcraft.yaml from "
                "git repository root"
            )
        else:
            raise
    relative_path_to_charmcraft_yaml = pathlib.PurePath(output)
    logger.debug(
        "Detected relative path to charmcraft.yaml from git repository root: "
        f"{relative_path_to_charmcraft_yaml}"
    )
    return relative_path_to_charmcraft_yaml


def get_github_repository(url: str, /):
    """Get GitHub repository name from URL"""
    # Example 1 `url`: "git@github.com:canonical/mysql-router-k8s-operator.git"
    # Example 2 `url`: "https://github.com/canonical/mysql-router-k8s-operator.git"

    url = url.removesuffix("/")
    url = url.removesuffix(".git")
    for prefix in ("git@github.com:", "https://github.com/"):
        if url.startswith(prefix):
            # Example 1: "canonical/mysql-router-k8s-operator"
            # Example 2: "canonical/mysql-router-k8s-operator"
            repo_name = url.removeprefix(prefix)
            return repo_name


def get_remote_repository(remote: str, /):
    """Get GitHub repository name for git remote"""
    url = subprocess.run(
        ["git", "remote", "get-url", remote], capture_output=True, check=True, text=True
    ).stdout.strip()
    repository_name = get_github_repository(url)
    if repository_name is None:
        logger.debug(f"Unable to parse GitHub repository for {remote=} from {url=}")
    return repository_name


def possible_github_repositories(*, charmcraft_yaml: pathlib.Path):
    """Possible GitHub repository names for this charm"""
    try:
        if repo := get_remote_repository("origin"):
            logger.debug("Attempting to use GitHub repository from 'origin' remote")
            yield repo
        else:
            logger.warning("Unable to parse GitHub repository from 'origin' remote")
    except subprocess.CalledProcessError as exception:
        if "No such remote" in exception.stderr:
            pass
        else:
            raise

    metadata_yaml = charmcraft_yaml.parent / "metadata.yaml"
    file_name = "metadata.yaml"
    source_urls = []
    if metadata_yaml.exists():
        # https://juju.is/docs/sdk/metadata-yaml#heading--source
        source_urls = yaml.safe_load(metadata_yaml.read_text()).get("source", [])
    if not source_urls:
        file_name = "charmcraft.yaml"
        # https://juju.is/docs/sdk/charmcraft-yaml#heading--links
        source_urls = yaml.safe_load(charmcraft_yaml.read_text()).get("links", {}).get("source", [])
    if not isinstance(source_urls, list):
        source_urls = [source_urls]
    for url in source_urls:
        assert isinstance(url, str)
        if repo := get_github_repository(url):
            logger.debug(f"Attempting to use GitHub repository from {file_name} source {url=}")
            yield repo

    remotes = (
        subprocess.run(["git", "remote"], capture_output=True, check=True, text=True)
        .stdout.strip()
        .split("\n")
    )
    for remote in remotes:
        if remote == "origin":
            # Already tried 'origin' remote
            continue
        if repo := get_remote_repository(remote):
            logger.debug(f"Attempting to use GitHub repository from {repr(remote)} remote")
            yield repo


@dataclasses.dataclass(frozen=True, kw_only=True)
class Asset:
    """charmcraftcache-hub GitHub release asset"""

    path: pathlib.Path
    download_url: str
    name: str
    size: int
    platform: _platforms.Platform


class Platform(_platforms.Platform):
    """Parser for typer parameter

    Use subclass instead of function to workaround https://github.com/fastapi/typer/discussions/618
    """

    def __new__(cls, value: str, /):
        try:
            return super().__new__(cls, value)
        except ValueError:
            raise typer.BadParameter(
                f"{repr(value)} is not a valid ST124 shorthand notation platform.\n\n"
                "More info: https://github.com/canonical/charmcraftcache?tab=readme-ov-file#step-1-update-charmcraftyaml-to-supported-syntax"
            )


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def pack(
    context: typer.Context,
    verbose: Verbose = False,
    selected_platforms: typing_extensions.Annotated[
        list[_platforms.Platform],
        typer.Option(
            "--platform",
            parser=Platform,
            show_default="all platforms for this machine's architecture",
            help="Platform(s) in charmcraft.yaml 'platforms' (e.g. 'ubuntu@22.04:amd64'). "
            "Shorthand notation required ('build-on' and 'build-for' not supported) in "
            "charmcraft.yaml",
        ),
    ] = None,
):
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
    charmcraft_yaml = pathlib.Path("charmcraft.yaml")
    if not charmcraft_yaml.exists():
        raise FileNotFoundError(
            "charmcraft.yaml not found. `cd` into the directory with charmcraft.yaml"
        )
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
    if selected_platforms is None:
        platforms = [
            platform_
            for platform_ in _platforms.get(charmcraft_yaml)
            if platform_.architecture == architecture
        ]
        logger.debug(f"Detected (for {architecture=}) {platforms=}")
    else:
        duplicate_selected_platforms = [
            key for key, value in collections.Counter(selected_platforms).items() if value > 1
        ]
        if duplicate_selected_platforms:
            raise ValueError(
                f"--platform {repr(duplicate_selected_platforms[0])} passed more than once. Is "
                "this a typo?"
            )
        charmcraft_yaml_platforms = _platforms.get(charmcraft_yaml)
        for platform_ in selected_platforms:
            if platform_ not in charmcraft_yaml_platforms:
                raise ValueError(
                    f"--platform {repr(platform_)} not found in charmcraft.yaml 'platforms': "
                    f"{repr(charmcraft_yaml_platforms)}"
                )
            if platform_.architecture != architecture:
                raise ValueError(
                    f"Architecture of --platform {repr(platform_)} does not match architecture of "
                    f"this machine ({repr(architecture)})"
                )
        platforms = selected_platforms
        logger.debug(f"Selected {platforms=}")

    logger.debug("Getting latest charmcraftcache-hub release via GitHub API")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
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
        with open(response_data_file) as file:
            response_data = json.load(file)
    else:
        logger.debug("HTTP cache miss for latest release")
        response_data = response.json()
        with open(response_data_file, "w") as file:
            json.dump(response_data, file)
        etag_file.write_text(response.headers["ETag"])

    # Clean cache if new GitHub release on charmcraftcache-hub
    release_name_file = cache_directory / "cache_downloaded_from_release_name.txt"
    try:
        last_release_name = release_name_file.read_text()
    except FileNotFoundError:
        assert not (cache_directory / "archives").exists()
        assert not (cache_directory / "charms").exists()
    else:
        if last_release_name != response_data["name"]:
            clean_cache(log_prefix="Cached wheels are outdated")
    finally:
        release_name_file.write_text(response_data["name"])

    logger.info("Searching for this charm's cache")
    relative_path_to_charmcraft_yaml = get_relative_path_to_charmcraft_yaml()
    logger.debug("Detecting GitHub repository")
    for github_repository in possible_github_repositories(charmcraft_yaml=charmcraft_yaml):
        # Platform: name of GitHub release asset
        expected_asset_names = {}
        for platform_ in platforms:
            asset_name = f"{github_repository}_ccchub1_{relative_path_to_charmcraft_yaml}_ccchub2_{platform_.name_in_release}.tar.gz"
            asset_name = asset_name.replace("/", "_")
            expected_asset_names[platform_] = asset_name
        # If at least one GitHub release asset is found for this `github_repository`, assume this
        # `github_repository` is correct.
        for asset in response_data["assets"]:
            if asset["name"] in expected_asset_names.values():
                asset_found = True
                break
        else:
            asset_found = False
        if asset_found:
            logger.debug(f"Detected {github_repository=}")
            break
    else:
        logger.error("Unable to find pre-built cache for this charm")
        add()
        exit(1)
    assets = []
    for platform_, asset_name in expected_asset_names.items():
        for asset in response_data["assets"]:
            name = asset["name"]
            if name == asset_name:
                path = cache_directory / "archives" / name
                assets.append(
                    Asset(
                        path=path,
                        download_url=asset["browser_download_url"],
                        name=name,
                        size=asset["size"],
                        platform=platform_,
                    )
                )
                break
        else:
            logger.error(
                f"Unable to find pre-built cache for GitHub repository {repr(github_repository)} "
                f"and platform {repr(platform_)}"
            )
            add()
            exit(1)

    assets_to_download = []
    for asset in assets:
        if asset.path.exists():
            logger.debug(f"Cache already downloaded for platform: {repr(asset.platform)}")
        else:
            assets_to_download.append(asset)
    with rich.progress.Progress(console=console) as progress:
        task = progress.add_task(
            description="\[charmcraftcache] Downloading cache",
            total=sum(asset.size for asset in assets_to_download),
        )
        # Use temporary path in case download is interrupted
        temporary_path = cache_directory / "current.tar.gz.part"
        for asset in assets_to_download:
            # Download archive
            response = requests.get(asset.download_url, stream=True)
            exit_for_rate_limit(response)
            response.raise_for_status()
            with open(temporary_path, "wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    file.write(chunk)
                    progress.update(task, advance=len(chunk))
            asset.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(temporary_path, asset.path)
            logger.debug(f"Downloaded cache for platform: {repr(asset.platform)}")
        if not assets_to_download:
            # Set progress as completed if no archives downloaded
            progress.update(task, completed=1, total=1)

    logger.info("Unpacking download")
    charm_cache = (
        cache_directory
        / "charms"
        / f'{github_repository.replace("/", "_")}:{str(relative_path_to_charmcraft_yaml).replace("/", "_")}'
    )
    all_archives_fully_unpacked = charm_cache / "all_archives_fully_unpacked"
    if not all_archives_fully_unpacked.exists() and charm_cache.exists():
        # charmcraftcache was interrupted while unpacking an archive; delete unpack destination &
        # retry
        logger.debug("Partially unpacked archive detected. Deleting & re-unpacking")
        shutil.rmtree(charm_cache)
    all_archives_fully_unpacked.unlink(missing_ok=True)
    for asset in assets:
        charmcraft_cache_dir = charm_cache / asset.platform
        if charmcraft_cache_dir.exists():
            logger.debug(f"Cache already unpacked for platform: {repr(asset.platform)}")
            continue
        # TODO: remove hardcoded paths
        build_base_subdirectory = charmcraft_cache_dir / "charmcraft-buildd-base-v7"
        build_base_subdirectory.mkdir(parents=True)
        with tarfile.open(asset.path) as file:
            file.extractall(build_base_subdirectory, filter="data")
        logger.debug(f"Unpacked cache for platform: {repr(asset.platform)}")
    all_archives_fully_unpacked.touch()

    for platform_ in platforms:
        logger.info(f"Packing platform: {repr(platform_)}")
        run_charmcraft(
            ["pack", "--platform", platform_, *context.args],
            charmcraft_cache_dir=charm_cache / platform_,
        )


def clean_cache(*, log_prefix: str = None):
    if log_prefix:
        message = f"{log_prefix}. Deleting cached wheels"
    else:
        message = "Deleting cached wheels"
    logger.info(message)
    try:
        shutil.rmtree(cache_directory)
    except FileNotFoundError:
        pass
    cache_directory.mkdir()
    version_file.write_text(installed_version)


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
            ["git", "symbolic-ref", "--quiet", "HEAD"], capture_output=True, check=True, text=True
        ).stdout.rstrip()
    except FileNotFoundError:
        return
    except subprocess.CalledProcessError:
        return
    if not local_branch:
        return
    try:
        output = subprocess.run(
            ["git", "for-each-ref", "--format", "%(upstream:short)", local_branch],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.rstrip()
    except subprocess.CalledProcessError:
        return
    if not output:
        return
    remote_name, *remote_branch = output.split("/")
    remote_branch = "/".join(remote_branch)
    try:
        if github_repository := get_remote_repository(remote_name):
            return remote_branch, github_repository
    except subprocess.CalledProcessError:
        return


@app.command()
def add(verbose: Verbose = False):
    """Add your charm to the cache

    charmcraftcache downloads wheels from a pre-built cache.
    Each charm has an isolated cache.
    Each charm must be added to the pre-built cache before it can be used with charmcraftcache

    The pre-built cache is generated from a list of charms.
    This command adds your charm to that list
    """
    if verbose:
        # Verbose can be globally enabled from app level or command level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True
    params = {
        "template": "add_charm_branch.yaml",
        "labels": "add-charm",
        "title": "Add charm branch",
    }
    result = get_remote_branch_and_url()
    if result:
        remote_branch, repo_name = result
        params["repo"] = repo_name
        params["ref"] = remote_branch
    if pathlib.Path("charmcraft.yaml").exists():
        try:
            relative_path_to_charmcraft_yaml = get_relative_path_to_charmcraft_yaml()
        except UnableToDetectRelativePath:
            pass
        else:
            params["charm-directory"] = str(relative_path_to_charmcraft_yaml)
    issue_url = (
        requests.Request(
            url="https://github.com/canonical/charmcraftcache-hub/issues/new", params=params
        )
        .prepare()
        .url
    )
    if running_in_ci:
        # Hyperlink ASCII escape code (used by rich links) not supported by GitHub Actions
        logger.info(
            # Space after newline needed to show blank line on GitHub Actions
            f"To add your charm to the pre-built cache, open an issue here:\n \n{issue_url}\n ",
            # Prevent issue URL from getting wrapped
            extra={"disable_wrap": True},
        )
    else:
        logger.info(
            f"To add your charm to the pre-built cache, open an issue here:\n\n"
            f"[link={issue_url}]{issue_url}\n"
        )
        typer.launch(issue_url)
    logger.info(
        "After the issue is opened, it will be automatically processed. Then, it will take a few "
        "minutes to build the cache. After the cache has been built, `ccc pack` will be available "
        "for this charm."
    )


@app.callback()
def main(verbose: Verbose = False):
    if verbose:
        # Verbose can be globally enabled from app level or command level
        # (Therefore, we should only enable verbose—not disable it)
        state.verbose = True


installed_version = importlib.metadata.version("charmcraftcache")
state = State()
cache_directory = pathlib.Path("~/.cache/charmcraftcache/").expanduser()
cache_directory.mkdir(parents=True, exist_ok=True)

# Clean cache if charmcraftcache updated
version_file = cache_directory / "charmcraftcache_version.txt"
try:
    last_version = version_file.read_text()
except FileNotFoundError:
    pass
else:
    if last_version != installed_version:
        clean_cache(
            log_prefix=f"charmcraftcache update from {last_version} to {installed_version} detected"
        )
finally:
    version_file.write_text(installed_version)

response_ = requests.get("https://pypi.org/pypi/charmcraftcache/json")
response_.raise_for_status()
latest_pypi_version = response_.json()["info"]["version"]
if installed_version != latest_pypi_version:
    logger.info(
        f"Update available. Run `pipx upgrade charmcraftcache` ({installed_version} -> "
        f"{latest_pypi_version})"
    )
