"""Build self-hostable marketplace material from the exact reviewed artifact."""
from __future__ import annotations

import io
import json
import zipfile


def marketplace_artifact(plugin: bytes, name: str, version: str) -> bytes:
    marketplace = f'{name}-marketplace'
    index = {'name': marketplace, 'owner': {'name': 'Scenario capability platform'},
             'description': 'Human-reviewed business scenario capability plugins',
             'plugins': [{'name': name, 'source': f'./plugins/{name}', 'version': version,
                          'description': 'Reviewed scenario capability plugin'}]}
    guide = (f'# 自托管插件发布材料\n\n插件：{name}；版本：{version}。本包尚未上传到任何外部仓库。\n\n'
             f'1. 解压后验证：`claude plugin validate ./{marketplace}`。\n'
             f'2. 本地安装：`claude plugin marketplace add ./{marketplace}`，然后 '
             f'`claude plugin install {name}@{marketplace}`。\n'
             '3. Git 自托管：将市场目录内容放入你有权发布的仓库根目录；第三方使用 '
             '`claude plugin marketplace add <你的仓库地址>` 注册，再使用上述 install 命令。\n'
             '4. 私有仓库由第三方自行取得仓库读取权限；业务调用另行配置其场景专属凭据。\n'
             '5. 更新必须重新验证业务发布与代码，使用新插件版本并更新市场条目；不得覆盖旧发布身份。\n'
             '插件停用和权限仍由原平台裁决。市场安装成功不代表业务调用已验收。\n')
    entries = {f'{marketplace}/.claude-plugin/marketplace.json': json.dumps(index, indent=2),
               f'{marketplace}/DISTRIBUTION.md': guide}
    with zipfile.ZipFile(io.BytesIO(plugin)) as original:
        for path in original.namelist():
            entries[f'{marketplace}/plugins/{path}'] = original.read(path)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, value in sorted(entries.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, value.encode() if isinstance(value, str) else value)
    return output.getvalue()
