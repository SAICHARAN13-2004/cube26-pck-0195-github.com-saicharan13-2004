"""Private media storage adapters for local development and S3-compatible deployments."""

import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


class LocalMediaStorage:
	backend = "local"

	def __init__(self, root: str | Path):
		self.root = Path(root).resolve()
		self.root.mkdir(parents=True, exist_ok=True)

	def _path(self, key: str) -> Path:
		parts = PurePosixPath(key)
		if parts.is_absolute() or not parts.parts or any(part in {"", ".", ".."} for part in parts.parts):
			raise ValueError("Invalid media key")
		path = self.root.joinpath(*parts.parts).resolve()
		if self.root not in path.parents:
			raise ValueError("Invalid media key")
		return path

	def save(self, key: str, upload: Any, content_type: str | None = None) -> str:
		path = self._path(key)
		path.parent.mkdir(parents=True, exist_ok=True)
		if hasattr(upload, "save"):
			upload.save(path)
		else:
			stream = getattr(upload, "stream", upload)
			stream.seek(0)
			with path.open("wb") as destination:
				shutil.copyfileobj(stream, destination)
		return key

	def read(self, key: str) -> bytes | None:
		path = self._path(key)
		return path.read_bytes() if path.is_file() else None

	def health_check(self) -> bool:
		return self.root.is_dir() and os.access(self.root, os.W_OK)


class S3MediaStorage:
	backend = "s3"

	def __init__(self, bucket: str, prefix: str = "", client: Any = None):
		if not bucket:
			raise ValueError("An S3 bucket is required")
		self.bucket = bucket
		self.prefix = prefix.strip("/")
		if client is None:
			import boto3

			client = boto3.client(
				"s3",
				endpoint_url=os.environ.get("PACKGUARD_S3_ENDPOINT_URL") or None,
				region_name=os.environ.get("PACKGUARD_S3_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
			)
		self.client = client

	@classmethod
	def from_url(cls, storage_url: str, client: Any = None):
		parsed = urlparse(storage_url)
		if parsed.scheme != "s3" or not parsed.netloc:
			raise ValueError("PACKGUARD_OBJECT_STORAGE_URL must be s3://bucket/optional-prefix")
		return cls(parsed.netloc, parsed.path, client)

	def _key(self, key: str) -> str:
		parts = PurePosixPath(key)
		if parts.is_absolute() or not parts.parts or any(part in {"", ".", ".."} for part in parts.parts):
			raise ValueError("Invalid media key")
		name = "/".join(parts.parts)
		return f"{self.prefix}/{name}" if self.prefix else name

	def save(self, key: str, upload: Any, content_type: str | None = None) -> str:
		stream = getattr(upload, "stream", upload)
		stream.seek(0)
		params = {
			"Bucket": self.bucket,
			"Key": self._key(key),
			"Body": stream,
			"ContentType": content_type or "application/octet-stream",
			"ServerSideEncryption": os.environ.get("PACKGUARD_S3_ENCRYPTION", "AES256"),
		}
		self.client.put_object(**params)
		return key

	def presign_put(self, key: str, content_type: str, expires: int = 900) -> str:
		return self.client.generate_presigned_url(
			"put_object",
			Params={"Bucket": self.bucket, "Key": self._key(key), "ContentType": content_type},
			ExpiresIn=expires,
		)

	def read(self, key: str) -> bytes | None:
		try:
			response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
		except Exception as error:
			response_error = getattr(error, "response", {}).get("Error", {})
			if response_error.get("Code") in {"NoSuchKey", "404", "NotFound"}:
				return None
			raise
		body = response["Body"]
		try:
			return body.read()
		finally:
			body.close()

	def health_check(self) -> bool:
		self.client.head_bucket(Bucket=self.bucket)
		return True


def create_media_storage(root: str | Path) -> LocalMediaStorage | S3MediaStorage:
	storage_url = os.environ.get("PACKGUARD_OBJECT_STORAGE_URL", "").strip()
	if not storage_url:
		return LocalMediaStorage(root)
	return S3MediaStorage.from_url(storage_url)
