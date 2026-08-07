import os, paramiko

SERVER = "192.168.5.7"
USER = "wzh"
PASSWORD = os.environ["DEPLOY_PW"]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(SERVER, username=USER, password=PASSWORD, timeout=15)

def run(cmd):
    _, so, se = c.exec_command(cmd)
    code = so.channel.recv_exit_status()
    out = so.read().decode(errors="replace")
    err = se.read().decode(errors="replace")
    print(f"$ {cmd}\n{out}{err}[exit {code}]\n")

# Server-side env/compose (mask secret values)
run("cd /home/wzh/pkce && cat .env | sed -E 's#(KEY|TOKEN|PASSWORD|SECRET)=.*#\\1=***#'")
run("cd /home/wzh/pkce && cat docker-compose.yml")
run("cd /home/wzh/pkce && docker volume ls | grep pkce")
c.close()
