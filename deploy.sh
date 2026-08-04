#!/usr/bin/env bash
set -euo pipefail

SERVER="192.168.5.7"
REMOTE_DIR="/opt/pkce"
SSH_USER="${SSH_USER:-root}"

echo "=== 1. Check .env file ==="
if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Create it first:"
    echo "  cp api-gateway/.env .env"
    exit 1
fi

echo "=== 2. Sync project to server ==="
rsync -avz --delete \
    --exclude='.git' \
    --exclude='node_modules' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.egg-info' \
    --exclude='.pytest_cache' \
    --exclude='frontend/dist' \
    --exclude='.claude' \
    --exclude='.flavor' \
    --exclude='*.db' \
    ./ "${SSH_USER}@${SERVER}:${REMOTE_DIR}/"

echo "=== 3. Build and start containers ==="
ssh "${SSH_USER}@${SERVER}" << 'ENDSSH'
    cd /opt/pkce

    if ! command -v docker > /dev/null 2>&1; then
        echo "Installing Docker..."
        curl -fsSL https://get.docker.com | sh
    fi

    docker compose build --no-cache
    docker compose up -d

    echo ""
    echo "=== Container Status ==="
    docker compose ps

    echo ""
    echo "=== Recent Logs ==="
    docker compose logs --tail=20
ENDSSH

echo ""
echo "=== Deployment complete ==="
echo "Auth Server:  http://192.168.5.7:8091"
echo "API Gateway:  http://192.168.5.7:8092"
echo ""
echo "Check logs:   ssh ${SSH_USER}@${SERVER} 'cd ${REMOTE_DIR} && docker compose logs -f'"
echo "Stop:         ssh ${SSH_USER}@${SERVER} 'cd ${REMOTE_DIR} && docker compose down'"
