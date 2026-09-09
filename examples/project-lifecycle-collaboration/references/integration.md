# 第三方接入边界

## 离线参考包

包中的脚本只读输入并输出校验结果或本地 Word；没有网络调用、消息发送、项目数据库、审批数据库或后台定时器。Python 3.12，依赖见 `../requirements.txt`。在第三方批准的环境执行，输入文件属于其工作目录。不要将本包作为租户上传代码安装到平台 Skill 执行器。

在包根目录、已准备好的 Python 3.12 环境运行：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/validate_analysis.py fixtures/kickoff-context.json fixtures/kickoff-analysis.json
python scripts/render_prd.py fixtures/kickoff-context.json fixtures/feedback-prd.json feedback-draft.docx
```

依赖安装需要可访问的包源或第三方预备的离线依赖。三个 fixture 文件均为合成样例，实际项目需按契约替换。Word 输出路径必须尚不存在；重新导出使用新文件名。把 `SKILL.md` 及其相对目录整体交给支持 Skill 的第三方 Agent，模型与工具授权仍由该运行环境提供。

流程：第三方读取项目 -> 按 context 契约提供最小上下文 -> Agent 加载 Skill -> 生成 analysis -> 本地 validate_analysis -> 按组织策略批准实际动作 -> 第三方通过自己的 API 提及成员、投递评审和文件 -> 第三方核验反馈 -> check_decision -> 第三方事务提交 -> 新版本上下文再次调用。

校验器检查证据 ID 是否存在及引用状态、路由身份/角色/群、版本和决定绑定。它不能自动证明自然语言事实真实、成员已经授权或所有专业建议正确，仍需业务质量评估和第三方身份认证。

## 身份、决定与并发

context 中的 participants 必须来自第三方可信成员目录，不能从 LLM 自动填写。成员 ID 与 channel_id 联合限定项目群；别名只展示。正式请求的内容摘要由第三方对保存后的审阅内容计算。JSON 摘要可使用 UTF-8、键排序、无多余空格、拒绝 NaN/Infinity；文件摘要对实际字节计算。摘要算法和内容格式在第三方契约中固定。

decision 文件必须由已认证连接器产生，和模型 analysis 分开传入。脚本不接收 `verified: true` 这样的自证字段；真实性由调用方负责。`external_receipt_id` 用于第三方追溯认证证据，不是签名本身。请求与回传均绑定 `review_version`，内容字节不变也不能把旧版本批准静默转用到新版本。

发起正式请求前，第三方持久保存 request_id 与 review 的版本和 SHA-256。决定绑定 project_id、context_revision、request_id、actor_id、review_sha256。授权角色撤销、离群、请求过期、项目版本变化、审阅内容变化、驳回/修改、已消费均不得当批准继续。

`check_decision` 是纯校验，不能防止多个进程同时通过检查。实际提交必须在第三方事务中重新读取当前版本、校验 expected revision，使用请求/决定唯一约束或 CAS，并原子写业务状态与 outbox。`consumed_decision_ids` 是显式输入快照，仅辅助检测，不能替代数据库幂等。发送结果未知需核对回执，不能盲目重发。

## 在线平台现状

当前平台有统一 REST v2、MCP、SDK 调用和入队回执。工作流 Provider 将全部工作流视为需要确认的入队动作，结果只返回 workflow_run_id 和平台 task_url；尚缺外部受控工作流结果查询/事件订阅。工作流输入和输出会在运行服务持久化，不能承诺零数据回传。

本轮在平台配置的工作流是参考验证入口，不是已完成的第三方在线交付接口。不要把平台浏览器 cookie 发给第三方，也不要把访问运行治理作为第三方业务审批步骤。离线包是手工维护的独立交付物，当前平台没有一键生成全场景 Skill 的通用导出功能。

正式在线接入需要通用平台另行具备：跨协议一致的异步结果/产物读取契约、权限与租户隔离、最小保留策略、持久回调投递或轮询、输出 Schema 验证、能力包签名及版本兼容。客户业务审批仍可在外部请求/回执循环完成，不因此搬回平台。
