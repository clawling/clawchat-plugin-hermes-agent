from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clawchat_gateway.api_client import DEFAULT_BASE_URL


class ProfileConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProfileConfig:
    base_url: str
    token: str
    user_id: str
    config_path: Path


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProfileConfigError(f"config.yaml not found at {path}; activate ClawChat first")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ProfileConfigError(f"failed to read {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProfileConfigError(f"invalid config.yaml at {path}: expected object")
    return loaded


def load_profile_config() -> ProfileConfig:
    hermes_home = _hermes_home()
    config_path = hermes_home / "config.yaml"
    config = _load_yaml(config_path)
    env = _load_env(hermes_home / ".env")
    extra = (
        config.get("platforms", {})
        .get("clawchat", {})
        .get("extra", {})
    )
    if not isinstance(extra, dict):
        extra = {}

    base_url = _first_non_empty(
        os.environ.get("CLAWCHAT_BASE_URL"),
        env.get("CLAWCHAT_BASE_URL"),
        extra.get("base_url"),
        DEFAULT_BASE_URL,
    ).rstrip("/")
    token = _first_non_empty(
        os.environ.get("CLAWCHAT_TOKEN"),
        env.get("CLAWCHAT_TOKEN"),
    )
    user_id = _first_non_empty(
        os.environ.get("CLAWCHAT_USER_ID"),
        env.get("CLAWCHAT_USER_ID"),
        extra.get("user_id"),
    )
    if not token:
        raise ProfileConfigError("missing CLAWCHAT_TOKEN; activate ClawChat first")
    if not user_id:
        raise ProfileConfigError("missing CLAWCHAT_USER_ID / platforms.clawchat.extra.user_id; activate ClawChat first")
    return ProfileConfig(base_url=base_url, token=token, user_id=user_id, config_path=config_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m clawchat_gateway.profile")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("get", help="Fetch the configured ClawChat account profile")

    get_user_parser = subparsers.add_parser("get-user", help="Fetch a ClawChat user profile by userId")
    get_user_parser.add_argument("user_id")

    friends_parser = subparsers.add_parser("friends", help="List the configured ClawChat account friends")
    friends_parser.add_argument("--page", type=int, default=None)
    friends_parser.add_argument("--page-size", type=int, default=None)

    update_parser = subparsers.add_parser("update", help="Update the configured ClawChat account profile")
    update_parser.add_argument("--nickname")
    update_parser.add_argument("--avatar-url")
    update_parser.add_argument("--bio")

    upload_avatar_parser = subparsers.add_parser("upload-avatar", help="Upload a local avatar image")
    upload_avatar_parser.add_argument("path", help="Absolute local avatar image path")

    upload_media_parser = subparsers.add_parser("upload-media", help="Upload a local media/file attachment")
    upload_media_parser.add_argument("path", help="Absolute local file path")

    args = parser.parse_args(argv)
    from clawchat_gateway import tools

    if args.command == "get":
        payload = asyncio.run(tools.get_account_profile())
    elif args.command == "get-user":
        payload = asyncio.run(tools.get_user_profile(args.user_id))
    elif args.command == "friends":
        payload = asyncio.run(tools.list_account_friends(page=args.page, page_size=args.page_size))
    elif args.command == "update":
        payload = asyncio.run(
            tools.update_account_profile(
                nickname=args.nickname,
                avatar_url=args.avatar_url,
                bio=args.bio,
            )
        )
    elif args.command == "upload-avatar":
        payload = asyncio.run(tools.upload_avatar_image(args.path))
    elif args.command == "upload-media":
        payload = asyncio.run(tools.upload_media_file(args.path))
    else:
        parser.error(f"unknown command: {args.command}")

    if payload.get("error"):
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
