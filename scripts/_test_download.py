# -*- coding: utf-8 -*-
"""验证文件下载端点。"""
import urllib.request, json

r = json.load(urllib.request.urlopen("http://127.0.0.1:8001/api/data-sources/7296fec756624e939e813c2253c83482/files", timeout=10))
print("files:", len(r))
if r:
    f = r[0]
    print("first:", f["filename"], f["id"])
    req = urllib.request.Request(f"http://127.0.0.1:8001/api/data-sources/files/{f['id']}/download")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read()
    print("download status:", resp.status, "bytes:", len(data))
    print("content-disposition:", resp.headers.get("Content-Disposition"))
    print("content-type:", resp.headers.get("Content-Type"))
