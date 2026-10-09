"""One supported delivery profile, separate from the external format standards."""
from __future__ import annotations

from copy import deepcopy


DELIVERY_PROFILE_VERSION = 'scenario-plugin-delivery-profile.v1'
BLUEPRINT_REFERENCE_PATH = 'references/scenario-blueprint.json'
PROTECTED_REFERENCE_PATHS = frozenset({'references/scenario.json', BLUEPRINT_REFERENCE_PATH})
READ_ONLY_SKILL_TOOLS = ('list_scenario_capabilities', 'get_scenario_receipt', 'read_scenario_approval')
SCENARIO_CREDENTIAL_SCOPES = ('capability:read', 'capability:invoke')
AUTHORING_STANDARDS = {
    'agent_skills': 'https://agentskills.io/specification',
    'claude_code_plugins': 'https://code.claude.com/docs/en/plugins-reference',
    'mcp_tools': 'https://modelcontextprotocol.io/specification/2025-11-25/server/tools',
}

_PROFILE = {
    'version': DELIVERY_PROFILE_VERSION,
    'host': {'key': 'claude_code', 'label': 'Claude Code', 'scope': 'host_specific'},
    'standards': [
        {'key': 'agent_skills', 'label': 'Agent Skills', 'url': AUTHORING_STANDARDS['agent_skills'],
         'purpose': '规定 SKILL.md、YAML 元数据和渐进读取方法资料的格式；方法包不证明执行或授权。'},
        {'key': 'claude_code_plugins', 'label': 'Claude Code 插件', 'url': AUTHORING_STANDARDS['claude_code_plugins'],
         'purpose': '规定当前宿主的安装布局、插件身份、技能与 MCP 配置；此安装包不保证其他宿主兼容。'},
        {'key': 'mcp_tools', 'label': 'MCP 工具协议', 'url': AUTHORING_STANDARDS['mcp_tools'],
         'purpose': '规定工具发现、输入输出和错误协议；SDK 协商协议版本，服务端继续裁决业务权限与执行。'},
    ],
    'components': [
        {'key': 'skill', 'label': '入口技能', 'required': True, 'supported': True,
         'purpose': '按目标发现选定能力、询问本次输入，解释确认、审批、回执与结果。'},
        {'key': 'mcp_adapter', 'label': '受信 MCP 客户端', 'required': True, 'supported': True,
         'purpose': '固定场景与发布身份，连接平台统一执行入口；AI 不能修改执行器或扩大能力范围。'},
        {'key': 'references', 'label': '契约与场景画像', 'required': True, 'supported': True,
         'purpose': '服务端生成固定发布的业务目标、模型和能力范围，作为只读交付参考。'},
        {'key': 'examples', 'label': '合成调用示例', 'required': False, 'supported': True,
         'purpose': '编码 AI 任务按项目校验要求交付合成示例；通用包可不含示例，示例和静态检查不代表业务已执行。'},
        {'key': 'client_scripts', 'label': '客户端辅助脚本', 'required': False, 'supported': True,
         'purpose': '可按需求处理显式输入及受信客户端调用，不在插件复制业务算法。'},
        {'key': 'hooks', 'label': '宿主 hooks 与动态 shell', 'required': False, 'supported': False,
         'purpose': 'Claude Code 允许这类扩展；本平台交付范围禁用，避免候选文本成为任意宿主执行入口。'},
        {'key': 'agents', 'label': '宿主子代理与其他组件', 'required': False, 'supported': False,
         'purpose': '当前交付范围未实现这些组件，不以宿主支持某功能推断本插件已包含。'},
    ],
    'boundaries': [
        {'key': 'modeling_materials', 'label': '建模资料',
         'purpose': '用于理解术语、流程、约束和来源，不自动成为正式运行输入。'},
        {'key': 'distillation', 'label': '业务蒸馏',
         'purpose': '形成目标、任务、规则、流程和验收候选，经治理后才能成为正式定义。'},
        {'key': 'ontology', 'label': '本体模型',
         'purpose': '定义对象、属性、身份和关系，为 typed 输入输出提供共同业务语义。'},
        {'key': 'capabilities', 'label': '场景能力',
         'purpose': '按业务需要建设计算、规则、动作与流程；只执行明确启用发布中受支持的可调用能力。'},
        {'key': 'plugin', 'label': '插件交付',
         'purpose': '提供宿主入口、方法、客户端和安装说明，消费固定能力，不补造缺少的业务实现。'},
        {'key': 'coding_extensions', 'label': '编码 AI 扩展',
         'purpose': '受信编码技能和只读 MCP 资料辅助开发，不自动装入所发布插件，也不授予业务权限。'},
    ],
    'platform_rules': [
        {'key': 'fixed_release', 'label': '固定发布与选择范围',
         'purpose': '场景、Release 和 Definition 身份固定，所选能力以服务端契约为准，不切换当前草稿。'},
        {'key': 'runtime_inputs', 'label': '本次输入与数据边界',
         'purpose': '只接受显式 typed 输入和受管端口，不打包客户资料、历史输入、连接信息或密钥。'},
        {'key': 'skill_execution', 'label': '技能执行边界',
         'purpose': '本平台禁 hooks、动态 shell 和任意执行代码；allowed-tools 仅可精确预授权发现、回执和审批读取。'},
        {'key': 'portable_names', 'label': '可移植技能命名',
         'purpose': '本平台路径使用 ASCII 小写命名；Skill 名称与目录相同。宿主扩展和内部版本不冒称通用字段。'},
        {'key': 'guarded_execution', 'label': '确认、审批与重试',
         'purpose': '服务端裁决权限、预演确认、审批 audience、幂等与结果未知状态；未知副作用不得盲重放。'},
        {'key': 'evidence', 'label': '独立完成证据',
         'purpose': '区分静态检查、源码人工定版、真实业务验收、插件发布和宿主安装；各自需要实际证据。'},
    ],
    'protected_references': sorted(PROTECTED_REFERENCE_PATHS),
}


def delivery_profile() -> dict:
    return deepcopy(_PROFILE)


def require_delivery_profile(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value != _PROFILE:
        raise ValueError('插件交付规范版本不可用，请新建受支持版本，不能替换已有产物')
    return value
