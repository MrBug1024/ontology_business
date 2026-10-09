"""Deterministic, bounded inspection of candidate files; no shell or code execution."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .plugin_project_validation import validate_project
from .plugin_source_policy import editable_path
from .plugin_coding_source import project_files
from .plugin_coding_project_state import project_state


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectFilesArguments(EmptyArguments):
    paths: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(default_factory=list, max_length=5)


class SearchFilesArguments(EmptyArguments):
    query: str = Field(min_length=1, max_length=200)


BASE_TOOLS = {
    "inspect_plugin_files": (InspectFilesArguments, "读取项目文件", "查看当前项目目录，或读取最多五个明确文件；受保护适配器与固定契约可只读查看，总返回内容受限。"),
    "search_plugin_files": (SearchFilesArguments, "检索项目文件", "在当前项目文件中按字面文本检索，最多返回二十处匹配；不执行正则或命令。"),
    "inspect_scenario_contract": (EmptyArguments, "读取场景能力契约", "读取本项目固定发布版本的完整输入、输出、确认和回执契约。"),
    "validate_plugin_project": (EmptyArguments, "检查插件项目", "运行服务端确定性源码与结构校验；不执行候选脚本，不代表业务验收或发布。"),
}
MAX_INSPECTION_CHARS = 40_000


class CodingToolInputError(ValueError):
    """Only deterministic argument errors may be corrected in the same round."""

    MESSAGES = {
        'invalid_tool_arguments': '工具参数不符合封闭契约，请按工具 Schema 重新提交；不执行任何读取或修改。',
        'duplicate_project_paths': '一次读取不能重复选择同一文件，请使用最多五个不同文件路径。',
        'unknown_project_path': '所选文件不在当前项目目录。先调用 inspect_plugin_files 且 paths=[] 读取目录，再使用返回的完整文件路径；目录不是文件。',
    }

    def __init__(self, code: str):
        self.code = code
        self.message = self.MESSAGES[code]
        super().__init__(self.message)

    def result(self) -> dict:
        return {'ok': False, 'read_only': True, 'error': {'code': self.code, 'message': self.message},
                'operation_performed': False, 'retryable': True}


def catalog() -> list[dict]:
    return [{"key": key, "title": title, "description": description}
            for key, (_schema, title, description) in BASE_TOOLS.items()]


def definitions() -> list[dict]:
    return [{"type": "function", "function": {"name": key, "description": description,
            "parameters": schema.model_json_schema()}}
            for key, (schema, _title, description) in BASE_TOOLS.items()]


def execute(document: dict, name: str, arguments: dict) -> dict:
    if name not in BASE_TOOLS:
        raise ValueError("模型请求了不可用的编程工具")
    try:
        payload = BASE_TOOLS[name][0].model_validate(arguments)
    except ValidationError:
        raise CodingToolInputError('invalid_tool_arguments') from None
    files = ({item['path']: item['content'] for item in project_files(document)}
             if isinstance(payload, (InspectFilesArguments, SearchFilesArguments)) else document["files"])
    if isinstance(payload, InspectFilesArguments):
        if not payload.paths:
            return {"files": [{"path": path, "characters": len(content), "editable": editable_path(path)}
                              for path, content in sorted(files.items())]}
        if len(set(payload.paths)) != len(payload.paths):
            raise CodingToolInputError('duplicate_project_paths')
        if any(path not in files for path in payload.paths):
            raise CodingToolInputError('unknown_project_path')
        remaining = MAX_INSPECTION_CHARS
        values = []
        for path in payload.paths:
            content = files[path]
            values.append({"path": path, "content": content[:remaining], "truncated": len(content) > remaining})
            remaining = max(0, remaining - len(content))
        return {"files": values}
    if isinstance(payload, SearchFilesArguments):
        matches = []
        has_more = False
        for path, content in sorted(files.items()):
            for index, line in enumerate(content.splitlines(), 1):
                if payload.query in line:
                    if len(matches) >= 20:
                        has_more = True
                        break
                    position = line.find(payload.query)
                    start = max(0, position - 100)
                    matches.append({"path": path, "line": index, "excerpt": line[start:start + 500]})
            if has_more:
                break
        return {"matches": matches, "has_more": has_more, "limit": 20}
    if name == "inspect_scenario_contract":
        return {"contract": document["coding_contract"], "definition_source": "release",
                "project_state": project_state(document)}
    issues = validate_project(document)
    state = project_state(document)
    return {"valid": not issues, "issues": issues[:50],
            "business_accepted": state['business_acceptance']['record_present'], "code_executed": False,
            "project_state": state}
