"""Minimal S3/Spaces client (SigV4): PUT file, GET to file, DELETE.

Why not boto3: boto3+botocore is ~80 MB installed, over the 48 MB Functions
build limit. This file is infrastructure glue, NOT counted as state tax
(see tools/count_tax.py), but it is a real cost of the stateless arm.

Env: SPACES_ENDPOINT (https://nyc3.digitaloceanspaces.com), SPACES_REGION,
     SPACES_BUCKET, SPACES_KEY, SPACES_SECRET
"""

import datetime
import hashlib
import hmac
import http.client
import os
import urllib.parse

UNSIGNED = "UNSIGNED-PAYLOAD"


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method, host, path, region, access_key, secret_key,
                  payload_hash=UNSIGNED, service="s3", now=None, query="",
                  extra_headers=None):
    """Return headers dict including Authorization. Pure function (testable)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-date": amz_date}
    if service == "s3":
        headers["x-amz-content-sha256"] = payload_hash
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = v
    signed = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
    canon_path = urllib.parse.quote(path, safe="/-_.~")
    canonical = "\n".join([method, canon_path, query, canon_headers, signed,
                           payload_hash])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(("AWS4" + secret_key).encode(), date)
    k = _sign(k, region)
    k = _sign(k, service)
    k = _sign(k, "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                f"SignedHeaders={signed}, Signature={sig}")
    return headers


class Spaces:
    def __init__(self, endpoint=None, region=None, bucket=None, key=None,
                 secret=None):
        self.endpoint = endpoint or os.environ["SPACES_ENDPOINT"]
        self.region = region or os.environ.get("SPACES_REGION", "us-east-1")
        self.bucket = bucket or os.environ["SPACES_BUCKET"]
        self.key = key or os.environ["SPACES_KEY"]
        self.secret = secret or os.environ["SPACES_SECRET"]
        u = urllib.parse.urlsplit(self.endpoint)
        self.scheme, self.host = u.scheme, u.netloc

    def _conn(self):
        cls = (http.client.HTTPSConnection if self.scheme == "https"
               else http.client.HTTPConnection)
        return cls(self.host, timeout=300)

    def _req(self, method, obj_key, body=None, length=None):
        path = f"/{self.bucket}/{obj_key}"
        extra = {"content-length": str(length)} if length is not None else None
        h = sigv4_headers(method, self.host, path, self.region, self.key,
                          self.secret, extra_headers=extra)
        c = self._conn()
        c.request(method, urllib.parse.quote(path, safe="/-_.~"), body=body,
                  headers=h)
        return c, c.getresponse()

    def put_file(self, obj_key, file_path):
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            c, r = self._req("PUT", obj_key, body=f, length=size)
            data = r.read()
        c.close()
        if r.status >= 300:
            raise IOError(f"PUT {obj_key}: {r.status} {data[:200]!r}")
        return size

    def get_to_file(self, obj_key, file_path):
        c, r = self._req("GET", obj_key)
        if r.status >= 300:
            data = r.read()
            c.close()
            raise IOError(f"GET {obj_key}: {r.status} {data[:200]!r}")
        n = 0
        with open(file_path, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
        c.close()
        return n

    def delete(self, obj_key):
        c, r = self._req("DELETE", obj_key)
        r.read()
        c.close()
        return r.status
