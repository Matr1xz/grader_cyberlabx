import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
from typing import Dict, Optional, Set, Tuple
from urllib.parse import quote

import requests


RemoteFile = Dict[str, str]
RemoteFolder = Dict[str, object]


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


def parse_repo_url(repo_url: str) -> Tuple[str, str]:
    """
    Parse GitHub URL and return (owner, repo).
    Supports:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git
    """
    https_match = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url.strip())
    if https_match:
        return https_match.group(1), https_match.group(2)

    ssh_match = re.match(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", repo_url.strip())
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)

    raise ValueError(f"GitHub URL khong hop le: {repo_url}")


def build_headers(token: Optional[str]) -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-folder-sync-script",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_get_json(url: str, headers: Dict[str, str], timeout: int = 30):
    resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"GitHub API error {resp.status_code}: {resp.text}")
    return resp.json()


def get_default_branch(owner: str, repo: str, headers: Dict[str, str]) -> str:
    repo_api = f"https://api.github.com/repos/{owner}/{repo}"
    data = github_get_json(repo_api, headers)
    branch = data.get("default_branch")
    if not branch:
        raise RuntimeError("Khong lay duoc default_branch tu GitHub API")
    return branch


def local_path_for_repo_path(local_root: str, repo_path: str) -> str:
    """Return a safe local path for a repository-relative POSIX path."""
    parts = repo_path.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise RuntimeError(f"Duong dan remote khong hop le: {repo_path}")

    root = os.path.abspath(local_root)
    candidate = os.path.abspath(os.path.join(root, *parts))
    try:
        inside_root = os.path.commonpath((root, candidate)) == root
    except ValueError:
        inside_root = False
    if not inside_root:
        raise RuntimeError(f"Duong dan remote nam ngoai local root: {repo_path}")
    return candidate


def raw_file_url(owner: str, repo: str, branch: str, repo_path: str) -> str:
    encoded_branch = quote(branch, safe="")
    encoded_path = quote(repo_path, safe="/")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{encoded_branch}/{encoded_path}"


def canonical_manifest_hash(entries: Dict[str, str]) -> str:
    """Hash sorted path/type/content records consistently on remote and local."""
    digest = hashlib.sha256()
    for path in sorted(entries):
        digest.update(f"{path}\0{entries[path]}\n".encode("utf-8"))
    return digest.hexdigest()


def discover_remote_tree(
    owner: str,
    repo: str,
    branch: str,
    headers: Dict[str, str],
) -> Dict[str, RemoteFolder]:
    """Build all root-folder manifests with one Git Trees API request."""
    encoded_branch = quote(branch, safe="")
    tree_api = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{encoded_branch}?recursive=1"
    data = github_get_json(tree_api, headers)

    if not isinstance(data, dict) or not isinstance(data.get("tree"), list):
        raise RuntimeError("Du lieu Git Trees API khong dung dinh dang")
    if data.get("truncated"):
        raise RuntimeError("Git Trees API tra ve tree bi truncated; bo qua chu ky nay de tranh xoa nham")

    folders: Dict[str, RemoteFolder] = {}
    for item in data["tree"]:
        item_path = item.get("path")
        item_type = item.get("type")
        item_sha = item.get("sha")
        item_mode = item.get("mode", "")
        if not item_path or not item_type or not item_sha:
            continue

        path_parts = item_path.split("/")
        if len(path_parts) < 2:
            # The existing tool owns only root-level folders, not root files.
            continue
        root_folder = path_parts[0]
        relative_path = "/".join(path_parts[1:])
        folder = folders.setdefault(root_folder, {"entries": {}, "files": {}, "hash": ""})
        entries = folder["entries"]
        files = folder["files"]

        if item_type == "blob" and not item_mode.startswith("120"):
            entries[relative_path] = f"blob:{item_sha}"
            files[item_path] = {
                "sha": item_sha,
                "download_url": raw_file_url(owner, repo, branch, item_path),
            }
        elif item_type == "tree":
            entries[relative_path] = "tree"
        else:
            log(f"Bo qua entry khong ho tro: {item_path} ({item_type}, mode={item_mode})")

    for folder in folders.values():
        folder["hash"] = canonical_manifest_hash(folder["entries"])
    return folders


def git_blob_sha1(file_path: str) -> str:
    """Calculate the SHA-1 used by Git for a blob, without loading it all in memory."""
    file_size = os.path.getsize(file_path)
    digest = hashlib.sha1()
    digest.update(f"blob {file_size}\0".encode("ascii"))
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_folder_manifest(folder_path: str) -> Dict[str, str]:
    """Build a canonical manifest without following symlinked directories."""
    entries: Dict[str, str] = {}

    def visit(current_path: str, relative_parent: str) -> None:
        with os.scandir(current_path) as current_entries:
            for entry in current_entries:
                relative_path = os.path.join(relative_parent, entry.name) if relative_parent else entry.name
                relative_path = relative_path.replace(os.sep, "/")
                if entry.is_symlink():
                    entries[relative_path] = "symlink"
                elif entry.is_dir(follow_symlinks=False):
                    entries[relative_path] = "tree"
                    visit(entry.path, relative_path)
                else:
                    entries[relative_path] = f"blob:{git_blob_sha1(entry.path)}"

    if os.path.isdir(folder_path) and not os.path.islink(folder_path):
        visit(folder_path, "")
    return entries


def folder_hash(folder_path: str) -> str:
    return canonical_manifest_hash(local_folder_manifest(folder_path))


def download_file(download_url: str, dest_path: str, headers: Dict[str, str], timeout: int = 60) -> None:
    """Download to a temporary sibling file and atomically replace the destination."""
    parent = os.path.dirname(dest_path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".github-sync-", suffix=".tmp", dir=parent)
    os.close(fd)

    try:
        resp = requests.get(download_url, headers=headers, timeout=timeout, stream=True)
        try:
            if resp.status_code >= 400:
                raise RuntimeError(f"Khong tai duoc file {download_url}. HTTP {resp.status_code}")
            with open(temp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        finally:
            resp.close()
        os.replace(temp_path, dest_path)
    except Exception:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise


def remove_local_path(path: str) -> None:
    """Remove a file, directory, or symlink without following symlinked directories."""
    if os.path.islink(path) or not os.path.isdir(path):
        os.remove(path)
    else:
        shutil.rmtree(path)


def replace_folder_atomically(
    local_root: str,
    folder_name: str,
    remote_folder: RemoteFolder,
    headers: Dict[str, str],
) -> None:
    """Stage, verify, and replace one root folder without losing the old copy on failure."""
    destination = local_path_for_repo_path(local_root, folder_name)
    parent = os.path.dirname(destination)
    stage_root = tempfile.mkdtemp(prefix=".github-sync-stage-", dir=os.path.dirname(local_root))
    stage_folder = os.path.join(stage_root, folder_name)
    os.makedirs(stage_folder, exist_ok=True)
    backup_path = None

    try:
        entries = remote_folder["entries"]
        for relative_path, entry_value in entries.items():
            if entry_value == "tree":
                os.makedirs(local_path_for_repo_path(stage_folder, relative_path), exist_ok=True)

        for repo_path, remote_file in remote_folder["files"].items():
            relative_path = repo_path.split("/", 1)[1]
            destination_path = local_path_for_repo_path(stage_folder, relative_path)
            download_file(remote_file["download_url"], destination_path, headers)

        if folder_hash(stage_folder) != remote_folder["hash"]:
            raise RuntimeError(f"Hash folder tai ve khong khop: {folder_name}")

        if os.path.lexists(destination):
            backup_path = tempfile.mkdtemp(prefix=".github-sync-backup-", dir=parent)
            os.rmdir(backup_path)
            os.replace(destination, backup_path)
        os.replace(stage_folder, destination)
        if backup_path:
            remove_local_path(backup_path)
            backup_path = None
    except Exception:
        if backup_path and not os.path.lexists(destination):
            os.replace(backup_path, destination)
            backup_path = None
        raise
    finally:
        if backup_path and os.path.lexists(backup_path):
            remove_local_path(backup_path)
        if os.path.isdir(stage_root):
            shutil.rmtree(stage_root, ignore_errors=True)


def list_local_root_folders(local_root: str) -> Set[str]:
    folders: Set[str] = set()
    with os.scandir(local_root) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                folders.add(entry.name)
    return folders


def sync_once(local_root: str, owner: str, repo: str, branch: str, headers: Dict[str, str]) -> None:
    remote_folders = discover_remote_tree(owner, repo, branch, headers)
    remote_names = set(remote_folders)
    local_names = list_local_root_folders(local_root)
    stats = {"unchanged": 0, "added": 0, "replaced": 0, "deleted": 0}

    log(f"Remote folders: {len(remote_names)} | Local folders: {len(local_names)}")
    for folder_name in sorted(remote_names):
        remote_folder = remote_folders[folder_name]
        local_folder = local_path_for_repo_path(local_root, folder_name)
        if os.path.isdir(local_folder) and not os.path.islink(local_folder):
            if folder_hash(local_folder) == remote_folder["hash"]:
                stats["unchanged"] += 1
                continue
            stats["replaced"] += 1
            log(f"Folder thay doi, dang ghi de: {folder_name}")
        else:
            stats["added"] += 1
            log(f"Dang tai folder moi: {folder_name}")
        replace_folder_atomically(local_root, folder_name, remote_folder, headers)

    for folder_name in sorted(local_names - remote_names):
        local_folder = local_path_for_repo_path(local_root, folder_name)
        remove_local_path(local_folder)
        stats["deleted"] += 1
        log(f"Da xoa folder khong con tren GitHub: {folder_name}")

    log(
        "Dong bo xong: "
        f"giu nguyen {stats['unchanged']}, them {stats['added']}, "
        f"ghi de {stats['replaced']}, xoa {stats['deleted']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dong bo folder bang hash tu GitHub ve local moi 60 giay"
    )
    parser.add_argument("local_folder", help="Duong dan folder local de luu")
    parser.add_argument("repo_url", help="Link GitHub repo")
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Chu ky dong bo (giay), mac dinh 60",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Nhanh can theo doi. Neu bo trong se dung default_branch cua repo",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GITHUB_TOKEN"),
        help="GitHub token (hoac dat qua env GITHUB_TOKEN)",
    )
    args = parser.parse_args()

    local_root = os.path.abspath(args.local_folder)
    owner, repo = parse_repo_url(args.repo_url)
    headers = build_headers(args.token)

    if not os.path.isdir(local_root):
        os.makedirs(local_root, exist_ok=True)

    branch = args.branch or get_default_branch(owner, repo, headers)
    log(f"Start sync: {owner}/{repo} | branch={branch} | local={local_root}")

    while True:
        try:
            sync_once(local_root, owner, repo, branch, headers)
        except Exception as exc:
            log(f"Loi khi dong bo: {exc}")

        log(f"Sleep {args.interval} giay")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
