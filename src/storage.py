import json
import os
import pathlib
from typing import Optional
from base64 import b64encode
from functools import lru_cache


@lru_cache
def get_config_dir() -> pathlib.Path:
    cache_dir_root = os.path.expanduser("~")
    assert os.path.isdir(cache_dir_root)
    cache_dir = cache_dir_root + "/.docker-pull-push/"
    if not os.path.exists(cache_dir):
        print("Creating cache directory: " + cache_dir)
        os.makedirs(cache_dir)
    return pathlib.Path(cache_dir)


def get_config_file() -> pathlib.Path:
    cache_dir = get_config_dir()
    config_file = cache_dir / "config.json"
    if not config_file.is_file():
        config_file.write_text(json.dumps({"auths": {}}, indent=2))
    return config_file


def get_config() -> dict:
    config_file = get_config_file()
    try:
        config = json.loads(config_file.read_text())
        return config
    except json.JSONDecodeError:
        print(f"Could not read config file at {config_file}: JSON is invalid")
        return {}


def get_credentials(url: str) -> Optional[str]:
    creds = get_config()
    if "auths" in creds and url in creds["auths"] and "auth" in creds["auths"][url]:
        return creds["auths"][url]["auth"]
    return None


def save_credentials(url: str, username: str, password: str):
    creds = get_config()
    token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    creds["auths"][url] = {"auth": token}
    get_config_file().write_text(json.dumps(creds, indent=2))
