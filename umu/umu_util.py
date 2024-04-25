from tarfile import open as tar_open, TarInfo
from os import environ
from umu_consts import CONFIG, UMU_LOCAL
from typing import Any, Dict, List, Callable
from json import load, dump
from umu_log import log
from pathlib import Path
from shutil import rmtree, move, copy
from umu_plugins import enable_zenity
from urllib.request import urlopen
from ssl import create_default_context, SSLContext
from http.client import HTTPException
from tempfile import mkdtemp
from concurrent.futures import ThreadPoolExecutor, Future

SSL_DEFAULT_CONTEXT: SSLContext = create_default_context()

try:
    from tarfile import tar_filter
except ImportError:
    tar_filter: Callable[[str, str], TarInfo] = None


def setup_runtime(json: Dict[str, Any]) -> None:  # noqa: D103
    tmp: Path = Path(mkdtemp())
    ret: int = 0  # Exit code from zenity
    archive: str = "SteamLinuxRuntime_sniper.tar.xz"  # Archive containing the rt
    runtime_platform_value: str = json["umu"]["versions"]["runtime_platform"]
    codename: str = "steamrt3"
    log.debug("Version: %s", runtime_platform_value)

    # Define the URL of the file to download
    base_url: str = f"https://repo.steampowered.com/{codename}/images/{runtime_platform_value}/{archive}"
    log.debug("URL: %s", base_url)

    # Download the runtime
    # Optionally create a popup with zenity
    if environ.get("UMU_ZENITY") == "1":
        bin: str = "curl"
        opts: List[str] = [
            "-LJO",
            "--silent",
            f"{base_url}",
            "--output-dir",
            tmp.as_posix(),
        ]
        msg: str = "Downloading UMU-Runtime ..."
        ret = enable_zenity(bin, opts, msg)
        if ret:
            tmp.joinpath(archive).unlink(missing_ok=True)
            log.warning("zenity exited with the status code: %s", ret)
            log.console("Retrying from Python ...")
    if not environ.get("UMU_ZENITY") or ret:
        log.console(f"Downloading {codename} {runtime_platform_value}, please wait ...")
        with urlopen(  # noqa: S310
            base_url, timeout=300, context=SSL_DEFAULT_CONTEXT
        ) as resp:
            if resp.status != 200:
                err: str = f"repo.steampowered.com returned the status: {resp.status}"
                raise HTTPException(err)
            log.debug("Writing: %s", tmp.joinpath(archive))
            with tmp.joinpath(archive).open(mode="wb") as file:
                file.write(resp.read())

    log.debug("Opening: %s", tmp.joinpath(archive))
    # Open the tar file
    with tar_open(tmp.joinpath(archive), "r:xz") as tar:
        if tar_filter:
            log.debug("Using filter for archive")
            tar.extraction_filter = tar_filter
        else:
            log.debug("Using no filter for archive")
            log.warning("Archive will be extracted insecurely")

        # Ensure the target directory exists
        UMU_LOCAL.mkdir(parents=True, exist_ok=True)

        # Extract the 'depot' folder to the target directory
        log.debug("Extracting archive files -> %s", tmp)
        for member in tar.getmembers():
            if member.name.startswith("SteamLinuxRuntime_sniper/"):
                tar.extract(member, path=tmp)

        # Move the files to the correct location
        source_dir = tmp.joinpath("SteamLinuxRuntime_sniper")
        log.debug("Source: %s", source_dir)
        log.debug("Destination: %s", UMU_LOCAL)

        # Move each file to the destination directory, overwriting if it exists
        with ThreadPoolExecutor() as executor:
            futures: List[Future] = [
                executor.submit(_move, file, source_dir, UMU_LOCAL)
                for file in source_dir.glob("*")
            ]
            for _ in futures:
                _.result()

        # Remove the extracted directory and all its contents
        log.debug("Removing: %s/SteamLinuxRuntime_sniper", tmp)
        if tmp.joinpath("SteamLinuxRuntime_sniper").exists():
            rmtree(tmp.joinpath("SteamLinuxRuntime_sniper").as_posix())

        log.debug("Removing: %s", tmp.joinpath(archive))
        tmp.joinpath(archive).unlink(missing_ok=True)

        log.debug("Renaming: _v2-entry-point -> umu")

        # Rename _v2-entry-point
        UMU_LOCAL.joinpath("_v2-entry-point").rename(UMU_LOCAL.joinpath("umu"))


def setup_umu(root: Path, local: Path) -> None:
    """Copy the launcher and its tools to ~/.local/share/umu.

    Performs full copies of tools on new installs and selectively on new updates
    The tools that will be copied are:
    Pressure Vessel, Reaper, SteamRT, ULWLG launcher and the umu-launcher
    The umu-launcher will be copied to .local/share/Steam/compatibilitytools.d
    """
    log.debug("Root: %s", root)
    log.debug("Local: %s", local)
    json: Dict[str, Any] = _get_json(root, CONFIG)

    # New install or umu dir is empty
    if not local.exists() or not any(local.iterdir()):
        return _install_umu(root, local, json)

    return _update_umu(local, json, _get_json(local, CONFIG))


def _install_umu(root: Path, local: Path, json: Dict[str, Any]) -> None:
    """For new installations, copy all of the umu tools at a user-writable location.

    The designated locations to copy to will be:
    ~/.local/share/umu, ~/.local/share/Steam/compatibilitytools.d

    The tools that will be copied are:
    umu-launcher, umu Launcher files, reaper and umu_version.json
    """
    log.debug("New install detected")
    log.console("Setting up Unified Launcher for Windows Games on Linux ...")
    local.mkdir(parents=True, exist_ok=True)

    # Config
    log.console(f"Copied {CONFIG} -> {local}")
    copy(root.joinpath(CONFIG), local.joinpath(CONFIG))

    # Runtime platform
    setup_runtime(json)

    log.console("Completed.")


def _update_umu(
    local: Path,
    json_root: Dict[str, Any],
    json_local: Dict[str, Any],
) -> None:
    """For existing installations, update the umu tools at a user-writable location.

    The configuration file (umu_version.json) saved in the root dir
    will determine whether an update will be performed or not

    This happens by way of comparing the key/values of the local
    umu_version.json against the root configuration file

    In the case that existing writable directories we copy to are in a partial
    state, a best effort is made to restore the missing files
    """
    executor: ThreadPoolExecutor = ThreadPoolExecutor()
    futures: List[Future] = []
    log.debug("Existing install detected")

    # Attempt to copy only the updated versions
    # Compare the local to the root config
    # When a directory for a specific tool doesn't exist, remake the copy
    # Be lazy and just trust the integrity of local
    for key, val in json_root["umu"]["versions"].items():
        if key == "reaper":
            if val == json_local["umu"]["versions"]["reaper"]:
                continue
            log.console(f"Updating {key} to {val}")
            json_local["umu"]["versions"]["reaper"] = val
        elif key == "runtime_platform":
            current: str = json_local["umu"]["versions"]["runtime_platform"]
            runtime: Path = None

            for dir in local.glob(f"*{current}"):
                log.debug("Current runtime: %s", dir)
                runtime = dir
                break

            # Redownload the runtime if absent
            if not runtime or not local.joinpath("pressure-vessel").is_dir():
                log.warning("Runtime Platform not found")
                if runtime and runtime.is_dir():
                    rmtree(runtime.as_posix())
                if local.joinpath("pressure-vessel").is_dir():
                    rmtree(local.joinpath("pressure-vessel").as_posix())
                futures.append(executor.submit(setup_runtime, json_root))
                log.console(f"Restoring Runtime Platform to {val} ...")
                json_local["umu"]["versions"]["runtime_platform"] = val
            elif (
                runtime
                and local.joinpath("pressure-vessel").is_dir()
                and val != current
            ):
                # Update
                log.console(f"Updating {key} to {val}")
                rmtree(runtime.as_posix())
                rmtree(local.joinpath("pressure-vessel").as_posix())
                futures.append(executor.submit(setup_runtime, json_root))
                json_local["umu"]["versions"]["runtime_platform"] = val
        elif key == "launcher":
            if val == json_local["umu"]["versions"]["launcher"]:
                continue
            log.console(f"Updating {key} to {val}")
            json_local["umu"]["versions"]["launcher"] = val
        elif key == "runner":
            if val == json_local["umu"]["versions"]["runner"]:
                continue
            log.console(f"Updating {key} to {val}")
            json_local["umu"]["versions"]["runner"] = val

    for _ in futures:
        _.result()
    executor.shutdown()

    # Finally, update the local config file
    with local.joinpath(CONFIG).open(mode="w") as file:
        dump(json_local, file, indent=4)


def _get_json(path: Path, config: str) -> Dict[str, Any]:
    """Check the state of the configuration file (umu_version.json) in the given path.

    The configuration files are expected to reside in:
    a root directory (e.g., /usr/share/umu) and ~/.local/share/umu
    """
    json: Dict[str, Any] = None

    # The file in /usr/share/umu should always exist
    if not path.joinpath(config).is_file():
        err: str = (
            f"File not found: {config}\n"
            "Please reinstall the package to recover configuration file"
        )
        raise FileNotFoundError(err)

    with path.joinpath(config).open(mode="r") as file:
        json = load(file)

    # Raise an error if "umu" and "versions" doesn't exist
    if not json or not json.get("umu") or not json.get("umu").get("versions"):
        err: str = (
            f"Failed to load {config} or 'umu' or 'versions' not in: {config}\n"
            "Please reinstall the package"
        )
        raise ValueError(err)

    return json


def _move(file: Path, src: Path, dst: Path) -> None:
    """Move a file or directory to a destination.

    In order for the source and destination directory to be identical, when
    moving a directory, the contents of that same directory at the
    destination will be removed
    """
    src_file: Path = src.joinpath(file.name)
    dest_file: Path = dst.joinpath(file.name)

    if dest_file.is_dir():
        log.debug("Removing directory: %s", dest_file)
        rmtree(dest_file.as_posix())

    if src.is_file() or src.is_dir():
        log.debug("Moving: %s -> %s", src_file, dest_file)
        move(src_file, dest_file)
