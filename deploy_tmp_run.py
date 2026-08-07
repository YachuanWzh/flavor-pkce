"""One-off deploy: sync code to 192.168.5.7 (password auth) and rebuild.

Server-specific files (.env, docker-compose.yml) are NOT overwritten.
P0-8 fail-fast needs AUTH_SECRET_KEY; it is appended to .env if missing.
"""
import io
import os
import secrets
import tarfile
import time

import paramiko

SERVER = "192.168.5.7"
USER = "wzh"
PASSWORD = os.environ["DEPLOY_PW"]
RDIR = "/home/wzh/pkce"

# Never sync these: server keeps its own .env / docker-compose.yml.
EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", ".pytest_cache",
    ".claude", ".flavor", ".worktrees", ".superharness", "dist",
    "data", "keys", "api_gateway.egg-info", "auth_server.egg-info",
}
EXCLUDE_FILES = {
    ".env", "docker-compose.yml", "deploy_tmp_probe.py",
    "deploy_tmp_run.py", "frontend.log", "frontend.err.log",
}
EXCLUDE_SUFFIXES = (".pyc", ".db")


def build_tar() -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                if f in EXCLUDE_FILES or f.endswith(EXCLUDE_SUFFIXES):
                    continue
                fp = os.path.join(root, f).replace("\\", "/").lstrip("./")
                t.add(os.path.join(root, f), arcname=fp)
    buf.seek(0)
    return buf


def run(c, cmd, label=None):
    if label:
        print(f"\n=== {label} ===")
    _, so, se = c.exec_command(cmd, timeout=1800)
    code = so.channel.recv_exit_status()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    if out.strip():
        print(out[-4000:])
    if err.strip():
        print("STDERR:", err[-2000:])
    print(f"[exit {code}]")
    return code, out, err


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(SERVER, username=USER, password=PASSWORD, timeout=15)

    run(c, f"cp {RDIR}/.env {RDIR}/.env.bak-$(date +%Y%m%d%H%M%S)", "Backup .env")

    print("\n=== Uploading source tarball ===")
    s = c.open_sftp()
    s.putfo(build_tar(), f"{RDIR}/deploy.tar.gz")
    s.close()
    code, _, _ = run(
        c,
        f"cd {RDIR} && tar xzf deploy.tar.gz && rm deploy.tar.gz",
    )
    if code != 0:
        raise SystemExit("extract failed")

    # P0-8: auth-server refuses to start without a non-default AUTH_SECRET_KEY.
    # The key only gates startup (no data is signed with it), so generating
    # one now is safe.
    secret = secrets.token_hex(32)
    run(
        c,
        f"grep -q '^AUTH_SECRET_KEY=' {RDIR}/.env "
        f"|| echo 'AUTH_SECRET_KEY={secret}' >> {RDIR}/.env",
        "Ensure AUTH_SECRET_KEY in .env",
    )

    # Direct PyPI/npm at CN mirrors: files.pythonhosted.org and
    # registry.npmjs.org time out from this network.
    code, _, _ = run(
        c,
        f"cd {RDIR} && docker compose build"
        " --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
        " --build-arg NPM_REGISTRY=https://registry.npmmirror.com",
        "Build images (CN mirrors)",
    )
    if code != 0:
        raise SystemExit("build failed — containers left untouched")

    code, _, _ = run(
        c,
        f"cd {RDIR} && docker compose up -d --force-recreate",
        "Recreate containers",
    )
    if code != 0:
        raise SystemExit("up failed")

    print("\nWaiting 15s for health checks...")
    time.sleep(15)
    run(
        c,
        f"cd {RDIR} && docker compose ps "
        f"&& echo --- && curl -s http://localhost:8092/health "
        f"&& echo && curl -s -o /dev/null -w 'report: %{{http_code}}\\n' http://localhost:8092/report "
        f"&& curl -s -o /dev/null -w 'stats(no-auth): %{{http_code}}\\n' http://localhost:8092/api/stats/tokens "
        f"&& curl -s -o /dev/null -w 'auth login page: %{{http_code}}\\n' http://localhost:8091/login",
        "Post-deploy verification",
    )
    c.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
