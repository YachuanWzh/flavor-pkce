import paramiko, os, tarfile, io, time
SERVER="192.168.5.7"; USER="wzh"; RDIR="/home/wzh/pkce"
EX={".git","node_modules","__pycache__",".venv",".pytest_cache","frontend/dist",".claude",".flavor","*.db","deploy.py"}

def tar():
    b=io.BytesIO()
    with tarfile.open(fileobj=b,mode="w:gz") as t:
        for r,ds,fs in os.walk("."):
            ds[:]=[d for d in ds if d not in EX and not d.endswith(".egg-info")]
            for f in fs:
                if f.endswith((".pyc",".db")): continue
                fp=os.path.join(r,f).replace("\\","/")
                if any(e in fp for e in EX): continue
                t.add(os.path.join(r,f),arcname=fp)
    b.seek(0); return b

k=paramiko.Ed25519Key.from_private_key_file(os.path.expanduser("~/.ssh/id_ed25519"))
c=paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(SERVER,username=USER,pkey=k,timeout=15)

print("Syncing...")
s=c.open_sftp(); s.putfo(tar(),f"{RDIR}/deploy.tar.gz"); s.close()
_,so,_=c.exec_command(
    f"cd {RDIR} && tar xzf deploy.tar.gz && rm deploy.tar.gz && "
    f"sed -i 's#^PUBLIC_GATEWAY_URL=.*#PUBLIC_GATEWAY_URL=http://{SERVER}:8092#' .env"
)
so.channel.recv_exit_status()

print("Building & restarting...")
_,so,_=c.exec_command(f"cd {RDIR} && docker compose build 2>&1 && docker compose up -d --force-recreate 2>&1",get_pty=True)
so.channel.recv_exit_status()

time.sleep(12)
print("=== Status ===")
_,so,_=c.exec_command(f"cd {RDIR} && docker compose ps && echo --- && curl -s -o /dev/null -w 'auth: %{{http_code}}\n' http://localhost:8091/ && curl -s http://localhost:8092/health")
print(so.read().decode()); c.close()
print("Done!")
