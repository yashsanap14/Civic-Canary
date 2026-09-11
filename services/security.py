from __future__ import annotations

import hashlib
import hmac
import os
import re

import boto3


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ReviewTokenVerifier:
    def __init__(self) -> None:
        self._expected_digest: str | None = None

    def _load_digest(self) -> str:
        if self._expected_digest:
            return self._expected_digest
        local_token = os.getenv("REVIEW_TOKEN")
        if local_token:
            self._expected_digest = token_digest(local_token)
            return self._expected_digest
        secret_arn = os.getenv("REVIEW_TOKEN_SECRET_ARN")
        if not secret_arn:
            raise RuntimeError("REVIEW_TOKEN or REVIEW_TOKEN_SECRET_ARN must be configured")
        secret = boto3.client(
            "secretsmanager",
            region_name=os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        ).get_secret_value(SecretId=secret_arn)["SecretString"]
        if secret.startswith("sha256:") and re.fullmatch(r"[0-9a-f]{64}", secret[7:]):
            self._expected_digest = secret[7:]
        else:
            self._expected_digest = token_digest(secret)
        return self._expected_digest

    def verify(self, supplied_token: str | None) -> bool:
        if not supplied_token:
            return False
        return hmac.compare_digest(token_digest(supplied_token), self._load_digest())
