from __future__ import annotations

import ast
from pathlib import Path
import unittest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = BACKEND_ROOT / "app" / "services"

CORE_MODULES = (
    "agent_engine.py",
    "agent_runtime_adapter.py",
    "builtin_capability_providers.py",
    "business_query_service.py",
    "capability_agent_extensions.py",
    "capability_application_service.py",
    "capability_contracts.py",
    "capability_mcp_service.py",
    "capability_registry.py",
    "capability_provider_keys.py",
    "deployment_service.py",
    "rag_service.py",
    "runtime_input_service.py",
    "capability_invoker.py",
    "assistant_capability_modeling_service.py",
)

PROTOCOL_MODULES = (
    BACKEND_ROOT / "app" / "agent_mcp_server.py",
    BACKEND_ROOT / "app" / "routers" / "agents.py",
    BACKEND_ROOT / "app" / "routers" / "external_capabilities.py",
)

FORBIDDEN_CORE_PROMPT_FRAGMENTS = (
    "审计汇总",
    "聚合审计",
    "本次审计的有效规则",
    "未发现违规",
    "财务报表",
    "涉及审计、排查或核验",
)

FORBIDDEN_BUSINESS_TERMS = (
    "medical_audit",
    "run_medical_audit",
    "医保",
    "诊断",
    "处方",
    "结算",
)

FORBIDDEN_GENERIC_RAG_VOCABULARY = frozenset(
    {
        "成本",
        "费用",
        "客户",
        "用户",
        "供应商",
        "vendor",
        "订单",
        "风险",
        "审批",
        "合同",
    }
)

FORBIDDEN_INVOCATION_FIELDS = {
    "data_source_id",
    "connection_string",
    "database_url",
    "table_name",
    "column_name",
    "sql",
    "password",
    "api_key",
    "token",
}

GENERIC_MODELING_PROMPTS = (
    (SERVICES_ROOT / "ontology_service.py", "_GEN_PROMPT"),
    (SERVICES_ROOT / "scenario_model_compiler.py", "_PROMPT"),
)

FORBIDDEN_GENERIC_MODELING_EXEMPLARS = (
    "business_object",
    "object_id",
    "object_name",
    "related_to",
    "related_objects",
    "related_from_business_objects",
    "开始日期",
    "结束日期",
    "合同金额",
)


def _existing_core_modules() -> list[Path]:
    service_modules = [
        SERVICES_ROOT / name for name in CORE_MODULES if (SERVICES_ROOT / name).exists()
    ]
    return service_modules + [path for path in PROTOCOL_MODULES if path.exists()]


class CapabilityArchitectureBoundaryTests(unittest.TestCase):
    def test_generic_capability_core_contains_no_business_scenario_branches(self) -> None:
        modules = _existing_core_modules()
        self.assertTrue(modules, "通用能力内核模块尚未建立")
        for module in modules:
            source = module.read_text(encoding="utf-8").lower()
            for term in FORBIDDEN_BUSINESS_TERMS:
                self.assertNotIn(
                    term.lower(),
                    source,
                    f"{module.name} 不得包含业务场景硬编码 {term!r}",
                )

    def test_generic_rag_contains_no_platform_owned_business_vocabulary(self) -> None:
        path = SERVICES_ROOT / "rag_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        leaked = [
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.casefold() in FORBIDDEN_GENERIC_RAG_VOCABULARY
        ]
        self.assertFalse(
            leaked,
            "通用 RAG 不得内置客户、供应商、订单等业务词表；语义术语必须来自场景或用户定义",
        )

    def test_generic_modeling_prompts_contain_no_business_semantic_examples(self) -> None:
        for module, variable_name in GENERIC_MODELING_PROMPTS:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            values = [
                ast.literal_eval(node.value)
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == variable_name
                    for target in node.targets
                )
            ]
            self.assertEqual(
                len(values),
                1,
                f"缺少通用建模 prompt: {module.name}:{variable_name}",
            )
            prompt = values[0]
            for exemplar in FORBIDDEN_GENERIC_MODELING_EXEMPLARS:
                self.assertNotIn(
                    exemplar,
                    prompt,
                    f"{module.name}:{variable_name} 不得用业务语义样例锚定通用建模",
                )

    def test_agent_shell_contains_no_provider_owned_business_prompt_policy(self) -> None:
        source = (SERVICES_ROOT / "agent_engine.py").read_text(encoding="utf-8")
        for fragment in FORBIDDEN_CORE_PROMPT_FRAGMENTS:
            self.assertNotIn(
                fragment,
                source,
                f"Agent 通用壳不得内置业务提示策略 {fragment!r}",
            )
        tree = ast.parse(source)
        imported_modules = {
            str(node.module or "")
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertFalse(
            any("providers.medical_audit" in module for module in imported_modules),
            "Agent 通用壳不得导入医保 Provider 实现",
        )

    def test_public_invocation_contract_does_not_accept_physical_data_details(self) -> None:
        contract_path = SERVICES_ROOT / "capability_contracts.py"
        self.assertTrue(contract_path.exists(), "缺少 capability_contracts.py")
        tree = ast.parse(contract_path.read_text(encoding="utf-8"))
        public_contracts = {
            "DataBindingOverride",
            "CapabilityInvocationRequest",
            "RuntimeDataContext",
        }
        found: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name not in public_contracts:
                continue
            found.add(node.name)
            fields = {
                child.target.id
                for child in node.body
                if isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
            }
            leaked = fields.intersection(FORBIDDEN_INVOCATION_FIELDS)
            self.assertFalse(
                leaked,
                f"{node.name} 暴露了物理数据或凭据字段: {sorted(leaked)}",
            )
        self.assertEqual(found, public_contracts)

    def test_provider_registry_does_not_import_database_selected_python_paths(self) -> None:
        registry_path = SERVICES_ROOT / "capability_registry.py"
        self.assertTrue(registry_path.exists(), "缺少 capability_registry.py")
        source = registry_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_calls: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
                forbidden_calls.append("import_module")
            if isinstance(node.func, ast.Name) and node.func.id in {"__import__", "eval", "exec"}:
                forbidden_calls.append(node.func.id)
        self.assertFalse(
            forbidden_calls,
            "Provider 必须由受信代码显式注册，不能按数据库字符串动态导入",
        )

    def test_assistant_modeling_sidecar_does_not_import_runtime_data_models(self) -> None:
        service_path = SERVICES_ROOT / "assistant_capability_modeling_service.py"
        self.assertTrue(service_path.exists(), "缺少顾问能力建模侧车")
        tree = ast.parse(service_path.read_text(encoding="utf-8"))
        forbidden_models = {
            "DataSource",
            "DataAsset",
            "DataAssetVersion",
            "LogicalDataset",
            "DatasetVersion",
            "ScenarioDatasetBinding",
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(
            imported.intersection(forbidden_models),
            "端口与数据角色建议只能消费去标识化证据，不能依赖运行数据模型",
        )


if __name__ == "__main__":
    unittest.main()
