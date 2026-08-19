import urllib.request, uuid, json

DS = "7296fec756624e939e813c2253c83482"
path = r"E:\work\test\backend\data\audit_doc\年度审计业务规范.md"
data = open(path, "rb").read()
boundary = uuid.uuid4().hex
body = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="files"; filename="年度审计业务规范.md"\r\n'
    f"Content-Type: text/markdown\r\n\r\n"
).encode() + data + f"\r\n--{boundary}--\r\n".encode()

req = urllib.request.Request(
    f"http://127.0.0.1:8001/api/data-sources/{DS}/files",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST",
)
r = json.load(urllib.request.urlopen(req))
for f in r:
    print(f["id"], f["filename"], f["status"], (f.get("error") or "")[:100])
