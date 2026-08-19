# -*- coding: utf-8 -*-
import urllib.request, uuid, json, os

DS = "7296fec756624e939e813c2253c83482"
root = r"E:\gx\new_docs\逻辑资料V1\AI智能体相关法律、准则、底稿、案例、报告模版【20260703】"

files = [
    os.path.join(root, "8、审计报告模版", "审计报告正文.docx"),
    os.path.join(root, "8、审计报告模版", "一般企业报表.docx"),
    os.path.join(root, "8、审计报告模版", "一般企业附注.docx"),
    os.path.join(root, "9、管理建议书模版", "管理建议书模板.docx"),
    os.path.join(root, "11、复核要点、质量控制制度", "复核关注点.xlsx"),
]

def upload(path):
    name = os.path.basename(path)
    data = open(path, "rb").read()
    boundary = uuid.uuid4().hex
    ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if name.endswith(".xlsx"):
        ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8001/api/data-sources/{DS}/files",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    r = json.load(urllib.request.urlopen(req))
    for f in r:
        print(f["id"], f["filename"], f["status"], (f.get("error") or "")[:80])

for p in files:
    print("==", os.path.basename(p))
    try:
        upload(p)
    except Exception as e:
        print("ERR", e)
