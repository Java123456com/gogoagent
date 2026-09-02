from io import BytesIO

from backend.infrastructure.object_storage import MinioObjectStorage


class _Response(BytesIO):
    def release_conn(self):
        return None


class _MinioClient:
    def __init__(self):
        self.objects = {}

    def bucket_exists(self, _bucket):
        return True

    def make_bucket(self, _bucket):
        raise AssertionError("existing bucket must not be recreated")

    def put_object(self, bucket, key, stream, length, content_type):
        self.objects[(bucket, key)] = stream.read(length)

    def get_object(self, bucket, key):
        return _Response(self.objects[(bucket, key)])


def test_minio_html_round_trip(monkeypatch):
    client = _MinioClient()
    storage = MinioObjectStorage(client)
    monkeypatch.setattr(storage.settings, "minio_bucket", "gogo-travel")
    assert storage.put_html("plans/order/1.html", "<h1>方案</h1>") == "plans/order/1.html"
    assert storage.get_html("plans/order/1.html") == "<h1>方案</h1>"
