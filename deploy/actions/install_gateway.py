#!/usr/bin/env python3
"""One-time installer. Uses ONLY the provided public key; leaves login keys intact."""
from pathlib import Path
import base64
import os
import re
import shutil
import subprocess
import sys
import time


def install() -> None:
    os.umask(0o077)
    home = Path.home()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(home)):
        raise SystemExit("Unsupported home directory; install manually.")
    source = Path(__file__).resolve().parent / "server_gateway.py"
    public_file = Path(sys.argv[1]) if len(sys.argv) == 2 else home / "stock_actions.pub"
    key = public_file.read_text(encoding="utf-8-sig").strip().split()
    if len(key) < 2 or key[0] != "ssh-ed25519":
        raise SystemExit("Expected an Ed25519 PUBLIC key, not the private key.")
    try:
        base64.b64decode(key[1], validate=True)
    except ValueError:
        raise SystemExit("Invalid public key encoding.") from None
    subprocess.run(["ssh-keygen", "-l", "-f", str(public_file)], check=True, stdout=subprocess.DEVNULL)
    if not (home / "stock-agent-platform/deploy/server.env").is_file():
        raise SystemExit("Existing deployment not found at ~/stock-agent-platform.")
    subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    folder = home / ".local/lib/stock-actions"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = folder / "server_gateway.py"
    if script.is_symlink():
        raise SystemExit("Refusing symlink target.")
    shutil.copyfile(source, script)
    script.chmod(0o600)
    ssh = home / ".ssh"
    ssh.mkdir(mode=0o700, exist_ok=True)
    ssh.chmod(0o700)
    authorized = ssh / "authorized_keys"
    if authorized.is_symlink():
        raise SystemExit("Refusing symlink authorized_keys.")
    old = authorized.read_text() if authorized.exists() else ""
    if key[1] in old:
        raise SystemExit("This key is already authorized. Review the existing line; do not add unrestricted duplicates.")
    if old:
        backup = ssh / f"authorized_keys.before-stock-actions-{time.time_ns()}"
        backup.write_text(old)
        backup.chmod(0o600)
    line = f'restrict,command="/usr/bin/python3 {script}" {key[0]} {key[1]} stock-actions\n'
    with authorized.open("a") as handle:
        if old and not old.endswith("\n"):
            handle.write("\n")
        handle.write(line)
    authorized.chmod(0o600)
    print("Gateway installed. Existing SSH login keys were preserved.")
    host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
    print("Copy the following PUBLIC host key into GitHub SSH_HOST_KEY:")
    print(" ".join(host_key[:2]))
    print("No password, private key, server.env, or application token was printed.")


if __name__ == "__main__":
    install()
