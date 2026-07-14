"""Auth Server configuration."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = os.environ.get("AUTH_DB_PATH", str(BASE_DIR / "auth.db"))
SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "dev-secret-change-in-production")
JWT_PRIVATE_KEY_PATH = os.environ.get("JWT_PRIVATE_KEY_PATH", str(BASE_DIR / "keys" / "private.pem"))
JWT_PUBLIC_KEY_PATH = os.environ.get("JWT_PUBLIC_KEY_PATH", str(BASE_DIR / "keys" / "public.pem"))
JWT_ALGORITHM = "RS256"
JWT_EXPIRES_IN = 259200  # 3 days
AUTH_CODE_EXPIRES_IN = 600  # 10 minutes
