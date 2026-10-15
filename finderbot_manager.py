#!/usr/bin/env python3
"""Interactive process and data manager for the Telegram CSV search bot."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from config import Settings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parent
BOT_SCRIPT = PROJECT_ROOT / "bot.py"
IMPORT_SCRIPT = PROJECT_ROOT / "import_csv.py"
VENV_CANDIDATES = (
    PROJECT_ROOT / "venv" / "bin" / "python",
    PROJECT_ROOT / ".venv" / "bin" / "python",
)
VENV_PYTHON = next((path for path in VENV_CANDIDATES if path.is_file()), VENV_CANDIDATES[0])
SERVICE_NAME = "telegram-csv-search"
PID_FILE = PROJECT_ROOT / "run" / "finderbot.pid"
CONSOLE_LOG_NAME = "finderbot-console.log"
STOP_TIMEOUT_SECONDS = 20
GITHUB_REPOSITORY = "https://github.com/arefhere51/telegram_csv_search.git"
GITHUB_BRANCH = "main"
GITHUB_RAW_ROOT = "https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main"
UPDATE_RELEASE_MARKER = "FINDERBOT_RELEASE_20261015_FULL_FIELDS_AND_MENU_UPDATE"


def _systemd_available() -> bool:
    return bool(shutil.which("systemctl") and Path("/run/systemd/system").exists())


def _systemd_unit_for_project() -> bool:
    if not _systemd_available():
        return False
    result = subprocess.run(
        ["systemctl", "show", f"{SERVICE_NAME}.service", "--property=ExecStart", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and str(PROJECT_ROOT) in result.stdout and "bot.py" in result.stdout


def _systemd_active() -> bool:
    if not _systemd_unit_for_project():
        return False
    result = subprocess.run(
        ["systemctl", "is-active", f"{SERVICE_NAME}.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "active"


def _systemctl_control(action: str) -> subprocess.CompletedProcess:
    """Use the current root shell or sudo for actions that change the service."""
    command = ["systemctl", action, f"{SERVICE_NAME}.service"]
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            return subprocess.CompletedProcess(command, 1, "", "sudo is not installed")
        command = [sudo, *command]
    return subprocess.run(command, check=False)


def find_bot_pids(project_root: Path = PROJECT_ROOT) -> list[int]:
    """Find bot.py processes belonging to this project, not other bot instances."""
    expected_script = (project_root / "bot.py").resolve()
    matched: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return matched
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [part.decode(errors="replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
            cwd = Path(os.readlink(entry / "cwd")).resolve()
        except (OSError, PermissionError, ProcessLookupError):
            continue
        if not argv or any(Path(argument).name == "finderbot_manager.py" for argument in argv):
            continue
        for argument in argv:
            candidate = Path(argument)
            if candidate.name != "bot.py":
                continue
            if (candidate.is_absolute() and candidate.resolve() == expected_script) or (
                not candidate.is_absolute() and candidate.as_posix() == "bot.py" and cwd == project_root.resolve()
            ):
                matched.append(int(entry.name))
                break
    return sorted(set(matched))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def current_status(project_root: Path = PROJECT_ROOT) -> tuple[bool, list[int], str]:
    pids = find_bot_pids(project_root)
    if _systemd_active():
        return True, pids, "systemd"
    if pids:
        return True, pids, "process"
    return False, [], "stopped"


def _tail(path: Path, lines: int = 35) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def start_bot(settings: Settings) -> bool:
    running, pids, _ = current_status()
    if running:
        print(f"FinderBot is already running (PID {', '.join(map(str, pids)) or 'managed by systemd'}).")
        return True
    if not VENV_PYTHON.is_file():
        print(f"Python virtual environment not found: {VENV_PYTHON}")
        print("Install the project requirements first, or adjust the finderbot wrapper.")
        return False
    if _systemd_unit_for_project():
        result = _systemctl_control("start")
        if result.returncode != 0:
            print("Could not start the systemd service. Check your permissions and journalctl logs.")
            return False
        time.sleep(1)
        running, pids, _ = current_status()
        print(f"FinderBot {'started' if running else 'did not become active'} (PID {', '.join(map(str, pids)) or 'systemd'}).")
        return running

    settings.log_directory.mkdir(parents=True, exist_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    console_log = settings.log_directory / CONSOLE_LOG_NAME
    try:
        with console_log.open("ab") as log_handle:
            process = subprocess.Popen(
                [str(VENV_PYTHON), "-u", str(BOT_SCRIPT)],
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        PID_FILE.write_text(f"{process.pid}\n", encoding="ascii")
    except OSError as exc:
        print(f"Could not start FinderBot: {exc}")
        return False

    time.sleep(2)
    if process.poll() is not None:
        PID_FILE.unlink(missing_ok=True)
        print(f"FinderBot exited during startup (exit code {process.returncode}).")
        print(f"Recent output from {console_log}:")
        print(_tail(console_log) or "(no output recorded)")
        return False
    print(f"FinderBot started in the background (PID {process.pid}).")
    print(f"Console output: {console_log}")
    return True


def stop_bot() -> bool:
    if _systemd_unit_for_project() and _systemd_active():
        result = _systemctl_control("stop")
        if result.returncode != 0:
            print("Could not stop the systemd service. Check your permissions and service logs.")
            return False
        time.sleep(0.5)

    pids = find_bot_pids()
    if not pids:
        PID_FILE.unlink(missing_ok=True)
        print("FinderBot is not running.")
        return True

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"Permission denied while stopping PID {pid}; run this command as the bot owner or root.")
            return False

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        pids = [pid for pid in find_bot_pids() if _pid_alive(pid)]
        if not pids:
            PID_FILE.unlink(missing_ok=True)
            print("FinderBot stopped gracefully.")
            return True
        time.sleep(0.25)

    pids = [pid for pid in find_bot_pids() if _pid_alive(pid)]
    if pids:
        print(f"FinderBot did not stop within {STOP_TIMEOUT_SECONDS} seconds (PID {', '.join(map(str, pids))}).")
        answer = input("Force-stop these FinderBot process(es)? Type y to confirm [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Stop cancelled; FinderBot may still be running.")
            return False
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"Permission denied while force-stopping PID {pid}.")
                return False
        time.sleep(0.5)
    still_running = [pid for pid in find_bot_pids() if _pid_alive(pid)]
    if still_running:
        print(f"FinderBot is still running (PID {', '.join(map(str, still_running))}).")
        return False
    PID_FILE.unlink(missing_ok=True)
    print("FinderBot stopped.")
    return True


def restart_bot(settings: Settings) -> bool:
    if not stop_bot():
        return False
    return start_bot(settings)


def rebuild_indexes_and_restart(settings: Settings) -> bool:
    if not stop_bot():
        return False
    if not IMPORT_SCRIPT.is_file():
        print(f"Importer not found: {IMPORT_SCRIPT}")
        return False
    print("Rebuilding search indexes from the CSV files. This may take a while...")
    result = subprocess.run(
        [str(VENV_PYTHON), str(IMPORT_SCRIPT), "--rebuild-indexes"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        print(f"Index rebuild failed with exit code {result.returncode}; FinderBot was left stopped.")
        return False
    return start_bot(settings)


def update_from_github(project_root: Path = PROJECT_ROOT) -> bool:
    """Run the repository's data-preserving installer, then start the new version."""
    project_root = project_root.resolve()
    installer = project_root / "install.sh"
    if not installer.is_file():
        print(f"Update installer not found: {installer}")
        return False
    try:
        with urlopen(f"{GITHUB_RAW_ROOT}/finderbot_manager.py", timeout=20) as response:
            remote_manager = response.read(256 * 1024).decode("utf-8", errors="replace")
    except (OSError, URLError) as exc:
        print(f"Could not check the latest GitHub release: {exc}")
        print("No files were changed and the bot was not stopped.")
        return False
    if UPDATE_RELEASE_MARKER not in remote_manager:
        print("The GitHub main branch does not yet contain this release.")
        print("Update was blocked to prevent replacing this version with older code.")
        print("Publish this release to the repository first, then run FinderBot update again.")
        return False
    print("\nFinderBot will download the latest source from the public GitHub main branch.")
    print("The project code will be replaced; .env, databases, CSV files, logs, and virtualenv are preserved.")
    print("The bot will be stopped during the update and started again afterward.")
    try:
        answer = input("To continue, type exactly UPDATE: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("Update cancelled.")
        return False
    if answer != "UPDATE":
        print("Update cancelled.")
        return False

    systemd_mode = _systemd_available()
    mode = "systemd" if systemd_mode else "foreground"
    if not systemd_mode and not stop_bot():
        print("Update was not started because the running bot could not be stopped.")
        return False

    command = ["bash", str(installer), "--mode", mode, "--no-start"]
    environment = os.environ.copy()
    environment.update({
        "PROJECT_DIR": str(project_root),
        "REPO_URL": GITHUB_REPOSITORY,
        "BRANCH": GITHUB_BRANCH,
    })
    if systemd_mode and os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            print("Updating the systemd installation requires root or sudo.")
            return False
        command = [
            sudo,
            "env",
            f"PROJECT_DIR={project_root}",
            f"REPO_URL={GITHUB_REPOSITORY}",
            f"BRANCH={GITHUB_BRANCH}",
            *command,
        ]
        environment = os.environ.copy()

    print(f"Updating from {GITHUB_REPOSITORY} (branch {GITHUB_BRANCH})...")
    try:
        update_result = subprocess.run(command, cwd=project_root, env=environment, check=False)
    except OSError as exc:
        print(f"Could not run the updater: {exc}")
        return False
    if update_result.returncode != 0:
        print(f"Update failed with exit code {update_result.returncode}; check the installer output above.")
        print("The bot may be stopped; inspect the installer output before starting it manually.")
        return False

    wrapper = project_root / "finderbot"
    if not wrapper.is_file():
        print(f"Updated source is missing the FinderBot launcher: {wrapper}")
        return False
    print("Update downloaded and installed. Starting the bot with the updated code...")
    try:
        start_result = subprocess.run([str(wrapper), "start"], cwd=project_root, check=False)
    except OSError as exc:
        print(f"The update completed, but FinderBot could not be started: {exc}")
        return False
    if start_result.returncode != 0:
        print(f"The update completed, but FinderBot did not start (exit code {start_result.returncode}).")
        print("Check the service or bot log before retrying.")
        return False
    print("Update and restart completed successfully.")
    print("This menu was opened by the previous version; run finderbot again to use the updated menu.")
    return True


def _safe_managed_directory(path: Path, label: str, project_root: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    root = project_root.resolve()
    broad_protected = {
        Path("/"),
        Path.home().resolve(),
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/data"),
        Path("/var/log"),
    }
    if candidate in broad_protected or candidate == root or candidate in root.parents:
        raise ValueError(f"Refusing to clear unsafe {label} directory: {candidate}")
    if path.is_symlink():
        raise ValueError(f"Refusing to clear symlinked {label} directory: {path}")
    return candidate


def _database_files(database_path: Path, project_root: Path) -> list[Path]:
    configured_path = database_path.expanduser()
    if configured_path.is_symlink():
        raise ValueError(f"Refusing to remove a symlinked database path: {configured_path}")
    path = configured_path.resolve(strict=False)
    protected_files = {
        (project_root / ".env").resolve(),
        (project_root / "bot.py").resolve(),
        (project_root / "config.py").resolve(),
    }
    if path in protected_files or path == project_root.resolve() or path.name in {"", ".", ".."}:
        raise ValueError(f"Refusing to remove unsafe database path: {path}")
    results: set[Path] = {path}
    results.update(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
    parent = path.parent
    protected_parents = {
        Path("/"), Path.home().resolve(), Path("/tmp"), Path("/var/tmp"),
        Path("/data"), project_root.resolve(),
    }
    if parent.is_dir() and parent not in protected_parents:
        for candidate in parent.iterdir():
            name = candidate.name.lower()
            is_database = candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
            is_sidecar = any(name.endswith(extension) for extension in (".db-wal", ".db-shm", ".db-journal", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal", ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal"))
            if is_database or is_sidecar:
                results.add(candidate)
    return sorted(results, key=lambda item: str(item))


def cleanup_plan(settings: Settings, project_root: Path = PROJECT_ROOT) -> list[str]:
    """Return human-readable paths to be cleared without changing the filesystem."""
    root = project_root.resolve()
    managed_directories = [
        _safe_managed_directory(settings.data_directory, "data", root),
        _safe_managed_directory(settings.log_directory, "log", root),
    ]
    archive = settings.data_directory.resolve(strict=False).parent / (
        f"{settings.data_directory.name}_deleted_sources"
    )
    if archive.exists():
        managed_directories.append(_safe_managed_directory(archive, "deleted-source archive", root))
    unique_directories = list(dict.fromkeys(managed_directories))

    plan = [f"All contents under: {directory}" for directory in unique_directories if directory.exists()]
    database_parent = settings.database_path.expanduser().resolve(strict=False).parent
    if database_parent.exists():
        plan.extend(str(path) for path in _database_files(settings.database_path, root) if path.exists())
    else:
        plan.extend(str(path) for path in _database_files(settings.database_path, root) if path.exists())
    pid_file = root / "run" / "finderbot.pid"
    if pid_file.exists() or pid_file.is_symlink():
        plan.append(str(pid_file))
    return list(dict.fromkeys(plan))


def _remove_path(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def delete_all_bot_data(settings: Settings, project_root: Path = PROJECT_ROOT) -> list[str]:
    """Delete configured bot data while preserving project code, venv and .env."""
    root = project_root.resolve()
    data_directory = _safe_managed_directory(settings.data_directory, "data", root)
    log_directory = _safe_managed_directory(settings.log_directory, "log", root)
    archive_directory = settings.data_directory.resolve(strict=False).parent / (
        f"{settings.data_directory.name}_deleted_sources"
    )
    has_archive = archive_directory.exists()
    directories = [data_directory, log_directory]
    if has_archive:
        directories.append(_safe_managed_directory(archive_directory, "deleted-source archive", root))
    directories = list(dict.fromkeys(directories))

    removed: list[str] = []
    for directory in directories:
        if not directory.exists():
            continue
        for child in list(directory.iterdir()):
            _remove_path(child)
            removed.append(str(child))

    if has_archive and archive_directory.exists():
        archive_directory.rmdir()
        removed.append(str(archive_directory))

    for database_file in _database_files(settings.database_path, root):
        if database_file.exists() or database_file.is_symlink():
            _remove_path(database_file)
            removed.append(str(database_file))

    pid_file = root / "run" / "finderbot.pid"
    if pid_file.exists() or pid_file.is_symlink():
        _remove_path(pid_file)
        removed.append(str(pid_file))
        try:
            pid_file.parent.rmdir()
        except OSError:
            pass

    return removed


def _print_status() -> None:
    running, pids, source = current_status()
    if running:
        pid_text = ", ".join(map(str, pids)) if pids else "systemd"
        print(f"FinderBot status: RUNNING ({source}; PID {pid_text})")
    else:
        print("FinderBot status: NOT RUNNING")


def _confirm_delete(settings: Settings) -> bool:
    try:
        targets = cleanup_plan(settings)
    except ValueError as exc:
        print(f"Safety check failed: {exc}")
        return False
    print("\nWARNING: this permanently removes the bot's indexed data and downloaded files.")
    print("The project source code, virtual environment, and .env configuration are kept.")
    print("Items that will be cleared:")
    if targets:
        for target in targets:
            print(f"  - {target}")
    else:
        print("  (no existing bot data files found)")
    print("\nTo continue, type exactly: DELETE ALL BOT DATA")
    try:
        phrase = input("> ").strip()
        if phrase != "DELETE ALL BOT DATA":
            print("Deletion cancelled.")
            return False
        final = input("This cannot be undone. Continue? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nDeletion cancelled.")
        return False
    return final in {"y", "yes"}


def delete_all_data(settings: Settings) -> bool:
    if not _confirm_delete(settings):
        return False
    if not stop_bot():
        print("Data was not deleted because the bot could not be stopped.")
        return False
    try:
        removed = delete_all_bot_data(settings)
    except (OSError, ValueError) as exc:
        print(f"Cleanup failed: {exc}")
        return False
    print(f"Deleted {len(removed)} bot data item(s).")
    print("The next start creates a fresh empty database; the bot's configuration is unchanged.")
    return True


def show_menu(settings: Settings) -> None:
    while True:
        print("\n" + "=" * 42)
        print("FinderBot management menu")
        print("=" * 42)
        _print_status()
        print("\nPlease select:")
        print("1) Start bot")
        print("2) Stop bot")
        print("3) Restart bot")
        print("4) Delete all databases and downloaded bot files")
        print("5) Rebuild search indexes and restart bot")
        print("6) Update FinderBot from GitHub and restart")
        print("0) Exit")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting FinderBot manager.")
            return
        if choice == "1":
            start_bot(settings)
        elif choice == "2":
            stop_bot()
        elif choice == "3":
            restart_bot(settings)
        elif choice == "4":
            delete_all_data(settings)
        elif choice == "5":
            rebuild_indexes_and_restart(settings)
        elif choice == "6":
            if update_from_github():
                return
        elif choice == "0":
            print("Exiting FinderBot manager.")
            return
        else:
            print("Invalid selection. Enter 0, 1, 2, 3, 4, 5, or 6.")
        try:
            input("\nPress Enter to return to the menu...")
        except (EOFError, KeyboardInterrupt):
            return


def main() -> int:
    if not VENV_PYTHON.is_file() and Path(sys.executable).resolve() not in {
        path.resolve() for path in VENV_CANDIDATES if path.exists()
    }:
        print(f"Project virtual environment is missing: {VENV_PYTHON}", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"Could not load the bot settings: {exc}", file=sys.stderr)
        return 2
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "status":
            _print_status()
            return 0
        if command == "start":
            return 0 if start_bot(settings) else 1
        if command == "stop":
            return 0 if stop_bot() else 1
        if command == "restart":
            return 0 if restart_bot(settings) else 1
        if command == "update":
            return 0 if update_from_github() else 1
        print("Usage: finderbot [status|start|stop|restart|update]", file=sys.stderr)
        return 2
    show_menu(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
