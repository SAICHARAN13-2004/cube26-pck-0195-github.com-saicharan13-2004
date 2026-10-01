from io import BytesIO

import pytest

import app as packguard_app
from storage import LocalMediaStorage, S3MediaStorage


class FakeS3Client:
	def __init__(self):
		self.objects = {}
		self.put_arguments = None

	def put_object(self, **kwargs):
		self.put_arguments = kwargs
		self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"].read()

	def get_object(self, Bucket, Key):
		return {"Body": BytesIO(self.objects[(Bucket, Key)])}

	def head_bucket(self, Bucket):
		return {"Bucket": Bucket}


def test_s3_storage_keeps_objects_private_and_encrypts_uploads(monkeypatch):
	monkeypatch.delenv("PACKGUARD_S3_ENCRYPTION", raising=False)
	client = FakeS3Client()
	storage = S3MediaStorage("private-bucket", "evidence", client)

	key = storage.save("org-a/record-a/photo.jpg", BytesIO(b"private evidence"), "image/jpeg")

	assert key == "org-a/record-a/photo.jpg"
	assert client.put_arguments["Key"] == "evidence/org-a/record-a/photo.jpg"
	assert client.put_arguments["ServerSideEncryption"] == "AES256"
	assert "ACL" not in client.put_arguments
	assert storage.read(key) == b"private evidence"
	assert storage.health_check() is True


def test_media_route_requires_login_and_enforces_organization_scope(monkeypatch):
	storage = LocalMediaStorage(packguard_app.UPLOAD_DIR / "access-test")
	storage.save("org_demo_alpha/record-a/photo.jpg", BytesIO(b"private evidence"), "image/jpeg")
	monkeypatch.setattr(packguard_app, "MEDIA_STORAGE", storage)
	anonymous = packguard_app.app.test_client()
	alpha = packguard_app.app.test_client()
	bravo = packguard_app.app.test_client()
	alpha.post("/login", data={"username": "alpha.operator", "password": "alpha-demo"})
	bravo.post("/login", data={"username": "bravo.operator", "password": "bravo-demo"})
	media_url = "/media/org_demo_alpha/record-a/photo.jpg"

	assert anonymous.get(media_url).status_code == 302
	alpha_response = alpha.get(media_url)
	assert alpha_response.data == b"private evidence"
	assert alpha_response.headers["Cache-Control"] == "private, no-store"
	assert bravo.get(media_url).status_code == 404


def test_media_storage_rejects_path_traversal(tmp_path):
	storage = LocalMediaStorage(tmp_path)
	with pytest.raises(ValueError):
		storage.read("org-a/../private.jpg")
