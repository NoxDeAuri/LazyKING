"""
Bitwarden integration.

Flow:
  1. Load BW_CLIENTID and BW_CLIENTSECRET from .env
  2. bw login --apikey  (uses those env vars)
  3. Prompt for master password via hidden terminal input (never a CLI arg, never written to disk)
  4. bw unlock via stdin -> parse session key
  5. Use session key for all subsequent bw get calls
"""

import getpass
import json
import os
import subprocess


def _run_bw(args: list[str], input_data: bytes = None, extra_env: dict = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bw"] + args,
        input=input_data,
        capture_output=True,
        env=env,
    )


def login() -> None:
    """Login to Bitwarden using API key from environment. Safe to call if already logged in."""
    client_id = os.environ.get("BW_CLIENTID")
    client_secret = os.environ.get("BW_CLIENTSECRET")
    if not client_id or not client_secret:
        raise EnvironmentError("BW_CLIENTID and BW_CLIENTSECRET must be set in .env")

    result = _run_bw(
        ["login", "--apikey"],
        extra_env={"BW_CLIENTID": client_id, "BW_CLIENTSECRET": client_secret},
    )
    stderr = result.stderr.decode()
    if result.returncode != 0 and "already logged in" not in stderr:
        raise RuntimeError(f"Bitwarden login failed: {stderr}")


def unlock() -> str:
    """Prompt for master password and unlock vault. Returns session key. Password is never stored."""
    # In headless deployments BW_MASTER_PASSWORD may be set in the platform secret store.
    # In local dev it is always empty, so getpass() prompts the terminal.
    password = os.environ.get("BW_MASTER_PASSWORD") or getpass.getpass("Bitwarden master password: ")
    result = _run_bw(["unlock", "--raw"], input_data=(password + "\n").encode())
    password = None  # clear from memory as soon as possible

    if result.returncode != 0:
        raise RuntimeError(f"Bitwarden unlock failed: {result.stderr.decode()}")

    session = result.stdout.decode().strip()
    if not session:
        raise RuntimeError("Could not parse session key from bw unlock output")

    return session


def get_credentials(item_name: str, session: str) -> dict:
    """Fetch login credentials for a named Bitwarden item."""
    result = _run_bw(["get", "item", item_name], extra_env={"BW_SESSION": session})
    if result.returncode != 0:
        raise RuntimeError(f"Could not fetch '{item_name}': {result.stderr.decode()}")

    item = json.loads(result.stdout.decode())
    login_data = item.get("login", {})
    return {
        "username": login_data.get("username"),
        "password": login_data.get("password"),
        "uris": [u.get("uri") for u in login_data.get("uris", [])],
    }


def start_session() -> str:
    """Login and unlock Bitwarden. Returns the session key for this run."""
    login()
    return unlock()
