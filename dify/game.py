"""Pure functions embedded in the Dify Code nodes by build_workflow.py."""

import hashlib
import json
import re
import uuid


def prepare(action: str, ticket: str) -> dict:
    """Open a new story or recover a returning player's namespace."""
    if action == "开局：寄存月兔章" or action == "开局：寄存星环章":
        ticket = uuid.uuid4().hex
        mode = "deposit"
        seal = "rabbit" if "月兔" in action else "ring"
    elif action in ("回来：走脚印门", "回来：走光环门"):
        ticket = (ticket or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{32}", ticket):
            raise ValueError("请输入开局时取得的 32 位回执。")
        mode, seal = "recall", "rabbit" if "脚印" in action else "ring"
    else:
        raise ValueError("请选择一个邮局操作。")
    scope_body = json.dumps({
        "title": "月球邮局单局存档",
        "summary": "Stores the seal chosen for one Moon Post Office game.",
        "idempotency_key": "moon-post-" + hashlib.sha256(ticket.encode()).hexdigest(),
    })
    return {"scope_body": scope_body, "ticket": ticket, "mode": mode, "seal": seal}


def payload(body: str, status: int, seal: str) -> dict:
    if not 200 <= status < 300:
        raise ValueError("PowerContext 无法打开邮局存档。")
    scope = json.loads(body)["scope_id"]
    return {
        "remember_body": json.dumps({
            "scope_id": scope, "kind": "decision", "text": f"moonpostseal {seal}",
            "reason": "Player chose their moon post office seal",
        }),
        "recall_body": json.dumps({"scope_id": scope, "query": "moonpostseal", "max_bytes": 4000}),
    }


def receipt(body: str, status: int, ticket: str, seal: str) -> dict:
    if not 200 <= status < 300 or not json.loads(body).get("entry"):
        raise ValueError("PowerContext 未确认寄存，请重新开局。")
    name, hint = ("月兔章", "它在月尘上留下两串脚印。") if seal == "rabbit" else ("星环章", "它像一圈绕月飞行的光。")
    return {"story": f"邮差把{name}锁进信柜：『下次回来，找与你的印章相配的门。{hint}』\n回执：{ticket}"}


def finale(body: str, status: int, seal: str) -> dict:
    if not 200 <= status < 300:
        raise ValueError("PowerContext 无法读取邮局存档，请稍后再试。")
    prepared = json.loads(body)
    matches = set(re.findall(r"\bmoonpostseal (rabbit|ring)\b", prepared.get("content") or ""))
    if prepared.get("status") != "ready" or len(matches) != 1:
        return {"story": "邮差翻遍信柜也没找到这张回执的印章。请检查回执，或重新开局。", "ending": "missing"}
    if seal not in matches:
        return {"story": "门后只传来一阵月风。邮差说：『想想印章留下的线索，再试另一扇门。』", "ending": "retry"}
    reward = "月兔替你签收了第一封地球来信。" if seal == "rabbit" else "光环展开成星图，标出了下一站。"
    return {"story": f"门上的凹槽亮了。{reward}", "ending": "open"}
