"""S3-compatible object storage client.

Written strictly against the S3 API. Locally it targets MinIO via
S3_ENDPOINT_URL with static creds and path-style addressing (MinIO has no
virtual-host bucket DNS); with the endpoint unset, boto3's default credential
chain targets real S3 — the calling code is identical in both worlds.
"""

import logging
from collections.abc import Iterator
from typing import BinaryIO

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from docfactory_core.config import Settings, get_settings

log = logging.getLogger(__name__)


class ObjectStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        kwargs: dict = {"region_name": self._settings.aws_region}
        if self._settings.s3_endpoint_url:
            kwargs.update(
                endpoint_url=self._settings.s3_endpoint_url,
                aws_access_key_id=self._settings.s3_access_key,
                aws_secret_access_key=self._settings.s3_secret_key,
                config=BotoConfig(s3={"addressing_style": "path"}),
            )
        self._s3 = boto3.client("s3", **kwargs)

    @property
    def bucket(self) -> str:
        return self._settings.s3_bucket

    def ensure_bucket(self) -> None:
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchBucket"}:
                raise
            params: dict = {"Bucket": self.bucket}
            if self._settings.aws_region != "us-east-1":
                params["CreateBucketConfiguration"] = {
                    "LocationConstraint": self._settings.aws_region
                }
            self._s3.create_bucket(**params)
            log.info("created bucket", extra={"bucket": self.bucket})

    @staticmethod
    def _assert_tenant_scoped(key: str) -> None:
        """Refuse a key outside the caller's tenant prefix.

        The bucket has no equivalent of row level security, so this is the
        object-store counterpart: every key must begin with the bound tenant's
        prefix. A path traversal or a hand-built foreign key raises here rather
        than silently reading another tenant's document.
        """
        from docfactory_core.db import current_tenant

        tenant = current_tenant.get()
        if tenant is None:
            return  # no tenant bound: seeding/admin tooling, not a request path
        if ".." in key:
            raise PermissionError(f"path traversal in object key: {key!r}")
        if not key.startswith(f"{tenant}/"):
            raise PermissionError(f"object key {key!r} is outside tenant prefix {tenant!r}")

    def put_object(self, key: str, body: bytes | BinaryIO, content_type: str) -> None:
        self._assert_tenant_scoped(key)
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def get_object(self, key: str) -> bytes:
        self._assert_tenant_scoped(key)
        return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def list_keys(self, prefix: str) -> Iterator[str]:
        self._assert_tenant_scoped(prefix)
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"]
