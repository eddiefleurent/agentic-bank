"""Onboard a synthetic customer: set a password and enrol an authenticator app (TOTP).

    uv run python scripts/setup_customer.py alex

Terraform creates the user without a password (it would sit in state). This script sets one, enrols
TOTP and stores {username, password, totp_secret} in ~/.config/agentic-bank/<user>.json (mode 600)
for the proof scripts. No-op if that file exists. Secrets are never printed.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import struct
import sys
import time
from pathlib import Path

import boto3

REGION = "us-east-1"
POOL_NAME = "agentic-bank-customers"


def totp_now(secret: str) -> str:
    """RFC 6238 code (same as proofs/_common.py). Waits for a fresh 30 s window."""
    time.sleep(30 - time.time() % 30 + 0.5)
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def main(user: str) -> None:
    path = Path.home() / ".config" / "agentic-bank" / f"{user}.json"
    if path.exists():
        print(f"{user}: already set up ({path})")
        return
    cognito = boto3.client("cognito-idp", region_name=REGION)
    pools = cognito.list_user_pools(MaxResults=60)["UserPools"]
    pool_id = next(p["Id"] for p in pools if p["Name"] == POOL_NAME)
    clients = cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]
    client_id = next(c["ClientId"] for c in clients if c["ClientName"] == "proof")

    # 24 random characters plus one of each class the password policy requires.
    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(24)) + "aA1!"
    cognito.admin_set_user_password(
        UserPoolId=pool_id, Username=user, Password=password, Permanent=True
    )

    # MFA is required and not yet set up, so the first sign-in returns an MFA_SETUP challenge.
    auth = cognito.admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": user, "PASSWORD": password},
    )
    if auth.get("ChallengeName") != "MFA_SETUP":
        raise SystemExit(f"{user}: expected MFA_SETUP, got {auth.get('ChallengeName')}")
    assoc = cognito.associate_software_token(Session=auth["Session"])
    totp_secret = assoc["SecretCode"]
    verified = cognito.verify_software_token(
        Session=assoc["Session"], UserCode=totp_now(totp_secret)
    )
    cognito.admin_respond_to_auth_challenge(
        UserPoolId=pool_id,
        ClientId=client_id,
        ChallengeName="MFA_SETUP",
        Session=verified["Session"],
        ChallengeResponses={"USERNAME": user},
    )
    cognito.admin_set_user_mfa_preference(
        UserPoolId=pool_id,
        Username=user,
        SoftwareTokenMfaSettings={"Enabled": True, "PreferredMfa": True},
    )

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"username": user, "password": password, "totp_secret": totp_secret}, f)
    print(f"{user}: password set, TOTP enrolled, credentials in {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: setup_customer.py <user>")
    main(sys.argv[1])
