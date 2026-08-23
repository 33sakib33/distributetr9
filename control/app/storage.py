from __future__ import annotations

import gzip
import hashlib
import io
from datetime import timedelta

from minio import Minio
from minio.error import S3Error

from app.config import settings


def _client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


class Storage:
    def __init__(self) -> None:
        self.client = _client()
        self.bucket = settings.minio_bucket

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise

    def stat_size(self, key: str) -> int:
        info = self.client.stat_object(self.bucket, key)
        return int(info.size)

    def copy(self, src_key: str, dst_key: str) -> None:
        from minio.commonconfig import CopySource

        self.client.copy_object(self.bucket, dst_key, CopySource(self.bucket, src_key))

    def put_log_chunk(self, key: str, body: bytes) -> None:
        compressed = gzip.compress(body)
        self.put_bytes(key, compressed, "application/gzip")

    def get_log_chunk(self, key: str) -> bytes:
        return gzip.decompress(self.get_bytes(key))

    def presigned_get(self, key: str, expires: timedelta = timedelta(hours=1)) -> str:
        return self.client.presigned_get_object(self.bucket, key, expires=expires)


storage = Storage()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
