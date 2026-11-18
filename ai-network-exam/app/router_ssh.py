"""Paramiko SSH to Cisco IOS 15.x routers (interactive shell).

IOS 15.2 only offers diffie-hellman-group14-sha1, ssh-rsa host keys and CBC ciphers.
Paramiko >= 4 dropped those, so paramiko is pinned to 3.5.1 and the algorithms are
forced through a Transport subclass. Credentials come from the environment only.
"""
from __future__ import annotations

import re
import socket
import time

import paramiko

from app import config

PROMPT_RE = re.compile(r"^([A-Za-z0-9_.:/-]+)(?:\([^)]+\))?([>#])\s*$")
IOS_ERROR_RE = re.compile(
    r"^%\s*(Invalid input|Incomplete command|Ambiguous command|Authorization failed|Access denied|"
    r"Unknown command|.*overlaps with|.*not a valid|.*Bad mask|Invalid)", re.I | re.M)


class RouterSSHError(RuntimeError):
    pass


class LegacyCiscoTransport(paramiko.Transport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        opts = self.get_security_options()
        opts.kex = ("diffie-hellman-group14-sha1",)
        opts.key_types = ("ssh-rsa",)
        opts.ciphers = ("aes128-cbc", "aes256-cbc", "aes128-ctr", "aes256-ctr")


def _known_hosts() -> paramiko.HostKeys:
    path = config.SSH_KNOWN_HOSTS
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    return paramiko.HostKeys(str(path))


def fetch_and_trust_host_key(host: str, port: int | None = None) -> str:
    """Read the router's ssh-rsa host key with the legacy transport and store it in known_hosts.
    (ssh-keyscan from modern OpenSSH cannot negotiate with IOS 15.2.)"""
    port = port or config.SSH_PORT
    sock = socket.create_connection((host, port), timeout=config.SSH_TIMEOUT)
    t = LegacyCiscoTransport(sock)
    try:
        t.start_client(timeout=config.SSH_TIMEOUT)
        key = t.get_remote_server_key()
    finally:
        t.close()
    hk = _known_hosts()
    entry = host if port == 22 else f"[{host}]:{port}"
    hk.add(entry, key.get_name(), key)
    hk.save(str(config.SSH_KNOWN_HOSTS))
    return key.get_fingerprint().hex(":")


class RouterSession:
    """One interactive IOS CLI session. Use as a context manager."""

    def __init__(self, host: str, username: str | None = None, password: str | None = None,
                 timeout: float | None = None):
        self.host = host
        self.username = username or config.SSH_USERNAME
        self.password = password if password is not None else config.SSH_PASSWORD
        self.timeout = timeout or config.SSH_TIMEOUT
        self.client: paramiko.SSHClient | None = None
        self.chan = None
        self.hostname: str | None = None
        self.privileged = False

    # ---- connection -------------------------------------------------
    def __enter__(self) -> "RouterSession":
        if self.chan is None:          # connect_router() returns an already connected session
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self) -> None:
        if not self.username or not self.password:
            raise RouterSSHError("SSH_USERNAME / SSH_PASSWORD are not set in .env")
        client = paramiko.SSHClient()
        _known_hosts()                                # make sure the file exists
        client.load_host_keys(str(config.SSH_KNOWN_HOSTS))   # strict: unknown or changed keys are rejected
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                self.host, port=config.SSH_PORT, username=self.username, password=self.password,
                timeout=self.timeout, banner_timeout=self.timeout, auth_timeout=self.timeout,
                allow_agent=False, look_for_keys=False, transport_factory=LegacyCiscoTransport,
            )
        except paramiko.AuthenticationException as exc:
            raise RouterSSHError(f"{self.host}: SSH authentication failed (check SSH_USERNAME/SSH_PASSWORD)") from exc
        except paramiko.BadHostKeyException as exc:
            raise RouterSSHError(f"{self.host}: host key CHANGED - run lab/check_routers.py --trust after "
                                 f"confirming the router was re-keyed") from exc
        except paramiko.SSHException as exc:
            msg = str(exc)
            if "not found in known_hosts" in msg:
                raise RouterSSHError(f"{self.host}: unknown host key - run `.venv/bin/python lab/check_routers.py "
                                     f"--trust` once") from exc
            raise RouterSSHError(f"{self.host}: SSH error: {msg}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RouterSSHError(f"{self.host}: SSH connection timed out after {self.timeout:.0f}s "
                                 f"(management unreachable?)") from exc
        except OSError as exc:
            hint = ""
            if "Unable to connect to port" in str(exc):
                hint = " - router refused SSH (all vty lines busy? check `show users` on the console)"
            raise RouterSSHError(f"{self.host}: cannot connect: {exc}{hint}") from exc
        self.client = client
        try:
            self.chan = client.invoke_shell(width=511, height=1000)
            self.chan.settimeout(self.timeout)
            banner = self._read_until_prompt(self.timeout)
            m = self._last_prompt(banner)
            if not m:
                raise RouterSSHError(f"{self.host}: no CLI prompt received")
            self.hostname = m.group(1)
            if m.group(2) != "#":
                raise RouterSSHError(f"{self.host}: logged in at user EXEC level; privilege 15 is required")
            self.send("terminal length 0")
            self.send("terminal width 511")
            priv = self.send("show privilege")
            self.privileged = "15" in priv
            if not self.privileged:
                raise RouterSSHError(f"{self.host}: SSH user is not privilege 15 ({priv.strip()})")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Log out with 'exit' so IOS frees the vty line itself. Only closing the TCP socket is not enough
        here: through VMware NAT the router never sees the close, and with 5 vty lines it then refuses
        new SSH logins until exec-timeout expires."""
        try:
            if self.chan is not None and not self.chan.closed:
                try:
                    self.chan.send("end\nexit\n")
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and not self.chan.closed and not self.chan.exit_status_ready():
                        if self.chan.recv_ready():
                            self.chan.recv(65535)
                        else:
                            time.sleep(0.05)
                except (OSError, EOFError, paramiko.SSHException):
                    pass
                self.chan.close()
        finally:
            if self.client is not None:
                self.client.close()
            self.chan = self.client = None

    # ---- I/O ----------------------------------------------------------
    @staticmethod
    def _last_prompt(buf: str):
        lines = [ln for ln in buf.replace("\r", "").split("\n") if ln.strip()]
        return PROMPT_RE.match(lines[-1].strip()) if lines else None

    def _read_until_prompt(self, timeout: float) -> str:
        buf, deadline = "", time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.chan.recv_ready():
                buf += self.chan.recv(65535).decode("utf-8", errors="replace")
                if self._last_prompt(buf):
                    # IOS may still be sending; give it a moment and confirm.
                    time.sleep(0.05)
                    if not self.chan.recv_ready():
                        return buf
                continue
            if self.chan.closed or self.chan.exit_status_ready():
                raise RouterSSHError(f"{self.host}: SSH session closed by router")
            time.sleep(0.05)
        raise RouterSSHError(f"{self.host}: timed out after {timeout:.0f}s waiting for the CLI prompt")

    def send(self, command: str, timeout: float | None = None) -> str:
        while self.chan.recv_ready():
            self.chan.recv(65535)
        self.chan.send(command + "\n")
        raw = self._read_until_prompt(timeout or max(self.timeout, 30))
        lines = raw.replace("\r", "").split("\n")
        # drop echoed command (first line) and trailing prompt
        if lines and command.strip() and command.strip()[:20] in lines[0]:
            lines = lines[1:]
        while lines and (not lines[-1].strip() or PROMPT_RE.match(lines[-1].strip())):
            lines = lines[:-1]
        return "\n".join(lines)

    def run_config(self, commands: list[str]) -> tuple[str, list[str]]:
        """Send config lines one by one; returns (transcript, ios_errors)."""
        transcript, errors = [], []
        for cmd in commands:
            out = self.send(cmd)
            transcript.append(f"{self.hostname}> {cmd}")
            if out.strip():
                transcript.append(out)
            for m in IOS_ERROR_RE.finditer(out):
                errors.append(f"'{cmd}': {m.group(0).strip()}")
        return "\n".join(transcript), errors


def connect_router(host: str, expected_hostname: str | None = None) -> RouterSession:
    session = RouterSession(host)
    session.connect()
    if expected_hostname and (session.hostname or "").lower() != expected_hostname.lower():
        name = session.hostname
        session.close()
        raise RouterSSHError(f"{host}: router hostname is '{name}', expected '{expected_hostname}' "
                             f"(wrong management IP in .env?)")
    return session


def parse_ip_interface_brief(output: str) -> list[dict]:
    rows = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[0] != "Interface" and re.match(r"^[A-Za-z]+[0-9]", parts[0]):
            status = " ".join(parts[4:-1])
            rows.append({"name": parts[0], "ip": parts[1], "status": status, "protocol": parts[-1]})
    return rows
