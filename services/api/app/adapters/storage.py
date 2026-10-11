import asyncio
from pathlib import Path

from app.core.config import Settings
from app.core.errors import AppError


class Storage:
    async def put(self, key: str, data: bytes, mime: str) -> None:
        raise NotImplementedError

    async def get(self, key: str) -> bytes:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalStorage(Storage):
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise AppError("UPLOAD_FAILED", "Invalid storage key.", 400)
        return path

    async def put(self, key: str, data: bytes, mime: str) -> None:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise AppError("SESSION_NOT_FOUND", "Image object is missing.", 404)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._path(key)

        def _unlink() -> None:
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_unlink)


class S3Storage(Storage):
    def __init__(self, settings: Settings) -> None:
        import boto3

        self.bucket = settings.storage_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint or None,
            aws_access_key_id=settings.storage_access_key or None,
            aws_secret_access_key=settings.storage_secret_key or None,
            region_name=settings.storage_region,
        )

    async def put(self, key: str, data: bytes, mime: str) -> None:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=mime,
        )

    async def get(self, key: str) -> bytes:
        def _read() -> bytes:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"].read()

        return await asyncio.to_thread(_read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    def ensure_bucket(self) -> None:
        existing = {item["Name"] for item in self.client.list_buckets().get("Buckets", [])}
        if self.bucket not in existing:
            self.client.create_bucket(Bucket=self.bucket)


def build_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings.storage_local_dir)
