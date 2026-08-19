# -*- coding: utf-8 -*-
"""上传年审案例参考文档到文档桶。"""
import urllib.request, uuid, json, sys

DS = "7296fec756624e939e813c2253c83482"
path = r"E:\work\test\backend\data\audit_doc\年审案例参考-北京有限公司2023.md"
data = open(path, "rb").read()
b = uuid.uuid4().hex
body = (
    f"--{b}\r\n"
    f'Content-Disposition: form-data; name="files"; filename="年审案例参考-北京有限公司2023.md"\r\n'
    f"Content-Type: text/markdown\r\n\r\n"
).encode() + data + f"\r\n--{b}--\r\n".encode()
req = urllib.request.Request(
    f"http://127.0.0.1:8001/api/data-sources/{DS}/files",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={b}"},
    method="POST",
)
r = json.load(urllib.request.urlopen(req))
for f in r:
    print(f["id"], f["filename"], f["status"], (f.get("error") or "")[:80])
