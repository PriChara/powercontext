"""Regenerate moon-post.yml: python -m pip install pyyaml; python build_workflow.py."""

import ast
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SOURCE = (HERE / "game.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
IMPORTS = "\n".join(ast.get_source_segment(SOURCE, n) for n in TREE.body if isinstance(n, ast.Import))


class DSL(yaml.SafeDumper):
    pass


DSL.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))


def code(name, inputs, outputs):
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return {
        "code_language": "python3",
        "code": IMPORTS + "\n\n" + ast.get_source_segment(SOURCE, fn).replace(f"def {name}(", "def main(", 1) + "\n",
        "variables": [{"variable": k, "value_selector": v.split(".")} for k, v in inputs.items()],
        "outputs": {k: {"type": "string", "children": None} for k in outputs},
    }


def http(path, value):
    return {
        "url": "{{#env.MOON_POST_PC_URL#}}" + path,
        "method": "post", "authorization": {"type": "no-auth", "config": None},
        "headers": "Content-Type:application/json", "params": "",
        "body": {"type": "raw-text", "data": [{"id": "body", "key": "", "type": "text", "value": "{{#" + value + "#}}"}]},
        "timeout": {"max_connect_timeout": 5, "max_read_timeout": 15, "max_write_timeout": 5},
        "retry_config": {"retry_enabled": False, "max_retries": 0, "retry_interval": 100},
    }


nodes, edges = [], []


def node(id_, type_, title, x, y=0, **data):
    nodes.append({"id": id_, "type": "custom", "position": {"x": x, "y": y},
                  "positionAbsolute": {"x": x, "y": y}, "width": 244, "height": 85,
                  "sourcePosition": "right", "targetPosition": "left",
                  "data": {"type": type_, "title": title, "desc": "", "selected": False, **data}})


def edge(src, dst, handle="source"):
    types = {n["id"]: n["data"]["type"] for n in nodes}
    edges.append({"id": f"{src}-{handle}-{dst}", "source": src, "target": dst, "type": "custom",
                  "sourceHandle": handle, "targetHandle": "target",
                  "data": {"sourceType": types[src], "targetType": types[dst], "isInIteration": False}})


def output(variable, value):
    return {"variable": variable, "value_selector": value.split("."), "value_type": "string"}


node("start", "start", "进月球邮局", 0, variables=[
    {"label": "操作", "variable": "action", "type": "select", "required": True,
     "options": ["开局：寄存月兔章", "开局：寄存星环章", "回来：走脚印门", "回来：走光环门"]},
    {"label": "回执（仅回来时填写）", "variable": "ticket", "type": "text-input", "max_length": 32, "required": False, "options": []},
])
node("entry", "code", "核对操作和回执", 300, **code("prepare", {"action": "start.action", "ticket": "start.ticket"}, ["scope_body", "ticket", "mode", "seal"]))
node("scope", "http-request", "打开 PowerContext 存档", 600, **http("/v1/scopes", "entry.scope_body"))
node("payload", "code", "整理寄存或检索请求", 900, **code("payload", {"body": "scope.body", "status": "scope.status_code", "seal": "entry.seal"}, ["remember_body", "recall_body"]))
node("route", "if-else", "寄存还是取信", 1200, cases=[{"case_id": "true", "logical_operator": "and", "conditions": [
    {"id": "deposit-mode", "varType": "string", "variable_selector": ["entry", "mode"], "comparison_operator": "is", "value": "deposit"}
]}])
node("remember", "http-request", "将印章写入 PowerContext", 1500, -130, **http("/v1/memory/remember", "payload.remember_body"))
node("recall", "http-request", "从 PowerContext 找回印章", 1500, 150, **http("/v1/context/prepare", "payload.recall_body"))
node("receipt", "code", "发放回执和线索", 1800, -130, **code("receipt", {"body": "remember.body", "status": "remember.status_code", "ticket": "entry.ticket", "seal": "entry.seal"}, ["story"]))
node("finale", "code", "根据记忆判定结局", 1800, 150, **code("finale", {"body": "recall.body", "status": "recall.status_code", "seal": "entry.seal"}, ["story", "ending"]))
node("end-deposit", "end", "拿走回执", 2100, -130, outputs=[output("story", "receipt.story"), output("ticket", "entry.ticket")])
node("end-return", "end", "查看结局", 2100, 150, outputs=[output("story", "finale.story"), output("ending", "finale.ending")])

for src, dst in (("start", "entry"), ("entry", "scope"), ("scope", "payload"), ("payload", "route"),
                 ("remember", "receipt"), ("recall", "finale"), ("receipt", "end-deposit"), ("finale", "end-return")):
    edge(src, dst)
edge("route", "remember", "true")
edge("route", "recall", "false")

if __name__ == "__main__":
    dsl = {
        "kind": "app", "version": "0.7.0", "dependencies": [],
        "app": {"name": "月球邮局", "mode": "workflow", "description": "寄存一枚印章，下次拿回执回来解谜。",
                "icon": "🌙", "icon_background": "#e4e8ff", "use_icon_as_answer_icon": False},
        "workflow": {"conversation_variables": [], "features": {}, "environment_variables": [
            {"id": "e6167666-7554-4f2f-9575-962264c7a2a7", "name": "MOON_POST_PC_URL", "value": "http://moon-post-memory:8000", "value_type": "string", "description": "Docker 网络中的 PowerContext 地址"}
        ], "graph": {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 0.65}}},
    }
    (HERE / "moon-post.yml").write_text(yaml.dump(dsl, Dumper=DSL, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
