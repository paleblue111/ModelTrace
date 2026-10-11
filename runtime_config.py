"""Docker startup: create shared secrets once, then start the requested process."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

from cryptography.fernet import Fernet

SECRET_NAMES = ("MODELTRACE_API_TOKEN", "CREDENTIAL_ENCRYPTION_KEY")


def initialize_secrets(directory: Path | None = None) -> None:
    missing = [name for name in SECRET_NAMES if not os.getenv(name)]
    if missing:
        directory = directory or Path(os.getenv("MODELTRACE_RUNTIME_DIR", "/app/runtime"))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # API and workers may start together. Publish one complete file under a shared lock.
        with (directory / ".secrets.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = directory / "secrets.json"
            values = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(values, dict) or any(
                name not in SECRET_NAMES or not isinstance(value, str) or not value
                for name, value in values.items()
            ):
                raise ValueError("持久化密钥文件格式无效；请恢复备份，不要重新生成")
            changed = False
            for name in missing:
                if name not in values:
                    values[name] = Fernet.generate_key().decode() if name == "CREDENTIAL_ENCRYPTION_KEY" else secrets.token_urlsafe(32)
                    changed = True
            if "CREDENTIAL_ENCRYPTION_KEY" in values:
                Fernet(values["CREDENTIAL_ENCRYPTION_KEY"].encode())
            if changed:
                fd, temporary = tempfile.mkstemp(prefix=".secrets-", dir=directory)
                try:
                    with os.fdopen(fd, "w") as handle:
                        json.dump(values, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            for name in missing:
                os.environ[name] = values[name]
    Fernet(os.environ["CREDENTIAL_ENCRYPTION_KEY"].encode())


def main():
    initialize_secrets()
    if sys.argv[1:] == ["token"]:
        # Only an explicit administrator command prints the service token, never startup logs.
        print(os.environ["MODELTRACE_API_TOKEN"])
    elif len(sys.argv) > 1:
        os.execvp(sys.argv[1], sys.argv[1:])
    else:
        raise SystemExit("请提供启动命令，或使用 token 查看服务访问令牌")


if __name__ == "__main__":
    main()
