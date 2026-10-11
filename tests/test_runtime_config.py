import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from runtime_config import initialize_secrets, SECRET_NAMES


@pytest.fixture
def no_secrets(monkeypatch):
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, "")


def test_generated_secrets_survive_restart(tmp_path, monkeypatch, no_secrets):
    initialize_secrets(tmp_path)
    first = {name: os.environ[name] for name in SECRET_NAMES}
    encrypted = Fernet(first["CREDENTIAL_ENCRYPTION_KEY"].encode()).encrypt(b"upstream-key")
    for name in SECRET_NAMES:
        monkeypatch.delenv(name)
    initialize_secrets(tmp_path)
    assert {name: os.environ[name] for name in SECRET_NAMES} == first
    assert Fernet(os.environ["CREDENTIAL_ENCRYPTION_KEY"].encode()).decrypt(encrypted) == b"upstream-key"
    assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600


def test_explicit_secrets_need_no_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELTRACE_API_TOKEN", "custom-token")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    initialize_secrets(tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def test_partial_override_preserves_generated_secret(tmp_path, monkeypatch, no_secrets):
    monkeypatch.setenv("MODELTRACE_API_TOKEN", "custom-token")
    initialize_secrets(tmp_path)
    assert os.environ["MODELTRACE_API_TOKEN"] == "custom-token"
    assert "MODELTRACE_API_TOKEN" not in json.loads((tmp_path / "secrets.json").read_text())


def test_concurrent_container_startup_uses_same_secrets(tmp_path, no_secrets):
    environment = {**os.environ, "MODELTRACE_RUNTIME_DIR": str(tmp_path)}
    def start(_):
        return subprocess.check_output([sys.executable, str(Path(__file__).parents[1] / "runtime_config.py"), "token"], env=environment, text=True).strip()
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(start, range(4)))
    assert len(set(tokens)) == 1
    assert tokens[0] == json.loads((tmp_path / "secrets.json").read_text())["MODELTRACE_API_TOKEN"]


def test_corrupt_file_is_not_silently_replaced(tmp_path, no_secrets):
    path = tmp_path / "secrets.json"
    path.write_text('{"CREDENTIAL_ENCRYPTION_KEY": "broken"}')
    with pytest.raises(ValueError):
        initialize_secrets(tmp_path)
    assert path.read_text() == '{"CREDENTIAL_ENCRYPTION_KEY": "broken"}'
