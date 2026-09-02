"""MinIO adapter matching Java ``MinioConfig`` and ``PlanHtmlTools``."""
from __future__ import annotations

from io import BytesIO

from backend.config import get_settings


class ObjectStorageError(RuntimeError):
    pass


class MinioObjectStorage:
    def __init__(self, client=None) -> None:
        self.settings = get_settings()
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(
            self._client
            or (self.settings.minio_endpoint
                and self.settings.minio_access_key
                and self.settings.minio_secret_key)
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.enabled:
            raise ObjectStorageError("MinIO 未配置")
        try:
            from minio import Minio
        except ImportError as exc:
            raise ObjectStorageError("需要安装 minio Python SDK") from exc
        endpoint = str(self.settings.minio_endpoint).replace("https://", "").replace(
            "http://", ""
        ).rstrip("/")
        secure = str(self.settings.minio_endpoint).startswith("https://")
        self._client = Minio(
            endpoint,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key,
            secure=secure,
        )
        return self._client

    def ensure_bucket(self) -> None:
        client = self._get_client()
        bucket = self.settings.minio_bucket
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
        except Exception as exc:
            raise ObjectStorageError(f"MinIO bucket 初始化失败: {exc}") from exc

    def put_html(self, object_key: str, html: str) -> str:
        self.ensure_bucket()
        payload = html.encode("utf-8")
        try:
            self._get_client().put_object(
                self.settings.minio_bucket,
                object_key,
                BytesIO(payload),
                length=len(payload),
                content_type="text/html; charset=utf-8",
            )
        except Exception as exc:
            raise ObjectStorageError(f"MinIO 上传失败: {exc}") from exc
        return object_key

    def get_html(self, object_key: str) -> str:
        try:
            response = self._get_client().get_object(self.settings.minio_bucket, object_key)
            try:
                return response.read().decode("utf-8")
            finally:
                response.close()
                response.release_conn()
        except Exception as exc:
            raise ObjectStorageError(f"MinIO 下载失败: {exc}") from exc


object_storage = MinioObjectStorage()
