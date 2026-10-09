---
name: plugin-codex-authoring
description: 为明确选择 Codex 宿主的场景插件编写和审阅入口 Skill、纯客户端源码与安装说明；以固定场景能力契约和 Codex 交付规范为依据。
metadata:
  version: "1.0.0"
---

# Codex 场景插件编程方法

用于 `manifest.host=codex` 的编码或讨论。先读取当前 `delivery_profile`、`scenario_blueprint`、能力 Schema 和受信客户端签名；其他宿主使用其对应交付规范。当前平台提供 OpenAI 官方支持的 Codex 兼容布局；不要把本地交付声称为已提交官方目录或可在 ChatGPT 网页运行。

围绕用户业务目标建立“所选能力 → 当前输入 → 执行步骤 → 可核验结果”的对应关系。冻结本体提供业务语义，函数、规则、操作及工作流提供可调用行为；资料和蒸馏说明只能解释来源。缺少的能力明确指出，不在客户端补造业务算法，也不声称未选能力已由插件实现。

查看既有项目后，仅修改本轮必要的 Skill、说明、合成示例或纯客户端脚本。`.codex-plugin/plugin.json`、`.mcp.json`、受信适配器与场景参考由服务器生成并保护，不作为候选文件交付。Skill 参考文件采用相对自身目录的路径；不要复制 Claude 专属路径或不存在的 Codex CLI 命令。以项目内生成的 README 和受信安装器说明为准，区分注册安装源、实际安装、启用插件与执行成功。

入口 Skill 应清楚描述适用任务、输入缺口的追问、能力组合顺序、确认与审批、结果解读及失败恢复。依据 `client_contract` 的真实工具签名调用固定能力，运行参数必须来自本次明确输入。执行副作用前展示服务器预演并等待本次人工确认；同一请求重试复用幂等键。等待工作流时查询同一 `invocation_id`，遵循 `workflow_completion` 从成功且契约校验通过的声明输出步骤读取业务结果；入队、运行或待审批只能报告待完成，未知副作用不可盲重放。

依赖、连接地址和场景凭据在宿主安装后单独配置，不进入源码、示例或聊天。编码扩展的 Skill/MCP 不自动成为插件运行依赖。使用确定性项目校验检查候选文件并按错误修正；说明静态检查结果、仍需真实业务验收的范围与人工定版/发布步骤。工具调用或模型回答不能代替执行回执、已保存文件、已安装插件或已发布事实。

官方格式依据：[OpenAI 插件打包](https://developers.openai.com/plugins/build/plugins)、[Agent Skills](https://agentskills.io/specification)、[MCP 工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。采用项目冻结的交付规范，不在当前版本中擅自更换宿主、发布或协议语义。
