import os
import re
import sys
import tarfile
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]


def load_local_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env(ROOT / ".deploy.local.env")
HOST = os.environ.get("DEPLOY_SSH_HOST", "").strip()
PORT = int(os.environ.get("DEPLOY_SSH_PORT", "22"))
USER = os.environ.get("DEPLOY_SSH_USER", "root").strip() or "root"
SYNC_DOMAIN = os.environ.get("SYNC_DOMAIN", "e2e.fishmaple.cn").strip()
SYNC_PORT = os.environ.get("SYNC_PORT", "8791").strip()
APP_ENV_KEYS = (
    "STORAGE",
    "BLOB_INLINE_MAX_BYTES",
    "R2_ENDPOINT",
    "R2_BUCKET",
    "R2_API_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "DATABASE_URL",
)


def main() -> None:
    password = os.environ.get("DEPLOY_SSH_PASSWORD", "")
    if not password or not HOST or not SYNC_DOMAIN:
        sys.exit("Set DEPLOY_SSH_HOST, DEPLOY_SSH_PASSWORD, and SYNC_DOMAIN")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", SYNC_DOMAIN) or not re.fullmatch(r"\d{2,5}", SYNC_PORT):
        sys.exit("SYNC_DOMAIN or SYNC_PORT is invalid")
    dist = ROOT / "dist" / "index.js"
    if not dist.exists():
        sys.exit("Build open-server/dist/index.js first")

    plugin = plugin_dir()
    bundle = ROOT / "dist" / "inkwell-server.tgz"
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(dist, arcname="dist/index.js")
        map_file = ROOT / "dist" / "index.js.map"
        if map_file.exists():
            tar.add(map_file, arcname="dist/index.js.map")
        for name in ("manifest.json", "main.js", "styles.css"):
            tar.add(plugin / name, arcname=f"plugin/{name}")
        tar.add(ROOT / "public" / "favicon.png", arcname="public/favicon.png")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=password, timeout=30, allow_agent=False, look_for_keys=False)
    try:
        sftp = client.open_sftp()
        env_text = app_env_text()
        if env_text:
            with sftp.file("/etc/inkwell.env", "w") as remote_env:
                remote_env.write(env_text)
        sftp.put(str(bundle), "/tmp/inkwell-server.tgz")
        script = (ROOT / "scripts" / "remote-setup.sh").read_bytes().replace(b"\r\n", b"\n")
        with sftp.file("/tmp/inkwell-remote-setup.sh", "w") as remote:
            remote.write(script)
        sftp.close()
        run(client, f"SYNC_DOMAIN={SYNC_DOMAIN} SYNC_PORT={SYNC_PORT} bash /tmp/inkwell-remote-setup.sh")
    finally:
        client.close()
        bundle.unlink(missing_ok=True)


def plugin_dir() -> Path:
    env = os.environ.get("CLIENT_DIR", "").strip()
    candidates = [Path(env)] if env else []
    candidates.extend((ROOT.parent / "client", ROOT / "plugin"))
    for path in candidates:
        if (path / "manifest.json").is_file() and (path / "main.js").is_file():
            return path
    sys.exit("Plugin build not found. Build the client, or set CLIENT_DIR")


def app_env_text() -> str:
    skip = set()
    if os.environ.get("R2_ACCESS_KEY_ID", "").strip() and os.environ.get("R2_SECRET_ACCESS_KEY", "").strip():
        skip.add("R2_API_TOKEN")
    lines = []
    for key in APP_ENV_KEYS:
        if key in skip:
            continue
        value = os.environ.get(key, "").strip()
        if value:
            lines.append(f"{key}={value}")
    return ("\n".join(lines) + "\n") if lines else ""


def run(client: paramiko.SSHClient, command: str) -> None:
    _stdin, stdout, stderr = client.exec_command(command, get_pty=True)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    if code != 0:
        sys.exit(code)


if __name__ == "__main__":
    main()
