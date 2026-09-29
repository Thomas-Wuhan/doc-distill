#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档蒸馏流水线：切分 → 提炼 → 结构化 → 场景渲染

核心思想：文档只"蒸"一次，之后每个场景按模板快速渲染。
    文档 → [切分] → [提炼] → 知识卡(JSON) → [场景模板] → 定制输出

用法:
    python distill.py <文档.md> [--out 输出目录]
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROUTER = "http://127.0.0.1:8787/v1"      # 走本机路由代理（免费池承压）
API_KEY = "router-local"


async def llm(prompt: str, model: str = "deepseek-v4-flash", max_tokens: int = 3000,
              temperature: float = 0.2, route: str = "default") -> str:
    """route: 显式声明路由意图。
    蒸馏/渲染都是「质量敏感」任务 —— 错误会放大到所有下游场景，
    所以默认走 default（强模型直连），而不是让代理按关键词猜。
    """
    async with httpx.AsyncClient(timeout=240.0) as c:
        r = await c.post(f"{ROUTER}/chat/completions",
                         headers={"Authorization": f"Bearer {API_KEY}",
                                  "Content-Type": "application/json",
                                  "X-Route": route},
                         json={"model": model,
                               "messages": [{"role": "user", "content": prompt}],
                               "max_tokens": max_tokens, "temperature": temperature})
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()


# ---------------- 1. 切分（结构感知，不是定长切）----------------
def split_document(text: str, target: int = 1400, max_chunks: int = 14) -> list[dict]:
    """按标题层级切片：保持语义单元完整，并保留章节路径作为上下文元数据。"""
    lines = text.split("\n")
    chunks, cur, cur_title, path = [], [], "", []

    def flush():
        if cur and sum(len(x) for x in cur) > 60:
            chunks.append({"title": cur_title or "（开头）",
                           "path": " > ".join(path) if path else "",
                           "text": "\n".join(cur).strip()})

    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            flush()
            cur, cur_title = [], title
            path = path[: level - 1] + [title]
            cur.append(f"【{title}】")
        else:
            cur.append(ln)
            if sum(len(x) for x in cur) >= target:
                flush()
                cur, cur_title = [], cur_title          # 同标题下继续切
    flush()
    return chunks[:max_chunks]


# ---------------- 2. 提炼（蒸馏的灵魂：抽什么）----------------
DISTILL_PROMPT = """你是技术文档蒸馏器。把下面这段文档**提炼成结构化知识**，不要摘要、不要评论。

抽取规则：
- concepts：术语/概念（含别名、白话解释）
- parameters：具体数值/规格/限制（必须带上下文，不能脱离条件）
- procedures：可执行流程（步骤要能照着做）
- constraints：边界/陷阱/注意事项（这是最有价值的部分）
- faq：能被问到的问题 + 答案

要求：
1. 只输出 JSON，不要任何解释文字或 markdown 代码块标记
2. 没抽到的类别给空数组
3. 保留原文中的具体数字、单位、专有名词，不要模糊化
4. 每条都要带 source_hint（来自哪个小节）

JSON 结构：
{"concepts":[{"term":"","aliases":[],"plain":"","source_hint":""}],
 "parameters":[{"name":"","value":"","context":"","source_hint":""}],
 "procedures":[{"name":"","steps":[],"source_hint":""}],
 "constraints":[{"item":"","reason":"","source_hint":""}],
 "faq":[{"q":"","a":"","source_hint":""}]}

文档片段（章节：{title}）：
{text}"""


def _extract_json(s: str) -> dict:
    s = s.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j+1]
    try:
        return json.loads(s)
    except Exception:
        return {}


async def distill_chunk(ch: dict) -> dict:
    try:
        # ⚠️ 用 replace 而非 format：prompt 里的 JSON 示例含 {}，会被 format 当占位符（踩过的坑）
        prompt = DISTILL_PROMPT.replace("{title}", ch["title"]).replace("{text}", ch["text"][:6000])
        raw = await llm(prompt, max_tokens=6000)
        data = _extract_json(raw)
    except Exception as e:
        print(f"  ⚠️ 切片[{ch['title']}]提炼失败: {type(e).__name__}: {e}", file=sys.stderr)
        return {}
    for k in ("concepts", "parameters", "procedures", "constraints", "faq"):
        data.setdefault(k, [])
    data["_chunk"] = ch["title"]
    return data


def merge_cards(cards: list[dict], doc_name: str) -> dict:
    """合并各切片的知识卡，按名称去重（保留信息更全的那条）。"""
    out = {"doc": doc_name, "chunks": len(cards),
           "concepts": [], "parameters": [], "procedures": [], "constraints": [], "faq": []}
    seen = {k: set() for k in ("concepts", "parameters", "procedures", "constraints", "faq")}
    keyf = {"concepts": lambda x: x.get("term", ""), "parameters": lambda x: x.get("name", ""),
            "procedures": lambda x: x.get("name", ""), "constraints": lambda x: x.get("item", "")[:20],
            "faq": lambda x: x.get("q", "")[:20]}
    for c in cards:
        for cat in seen:
            for item in c.get(cat, []):
                k = keyf[cat](item).strip().lower()
                if not k or k in seen[cat]:
                    continue
                seen[cat].add(k)
                out[cat].append(item)
    return out


# ---------------- 3. 场景渲染（同一知识 → 不同形态）----------------
SCENARIOS = {
    "newbie": {
        "label": "新人问答（白话解释）",
        "prompt": """你是带新人的老工程师。用**最白话**的方式讲解，多用比喻，不用专业术语（必须用时立刻解释）。
新人问：{question}

只能依据下面的知识卡作答，知识卡里没有的就说"这个文档里没写清楚，得问带你的师傅"。
输出格式：先一句话直答 → 再打比方 → 最后给一个"记住这一点就够了"。

知识卡：
{knowledge}""",
        "question": "CAN 协议到底通用吗？一条报文里每个字节是什么意思？",
    },
    "customer": {
        "label": "客户答疑（结论先行）",
        "prompt": """你是对客户做技术支持的工程师。回答要**结论先行、简洁**，不绕弯子。
客户问：{question}

只能依据知识卡作答；不确定的明确说"需要确认"。
输出格式：① 一句话结论（支持/不支持/看情况）② 3 条以内的依据 ③ 一句风险提示（如有）。

知识卡：
{knowledge}""",
        "question": "我们能不能直接用抓包工具看懂你们设备的报文？需要你们提供什么？",
    },
    "proposal": {
        "label": "技术方案（参数清单）",
        "prompt": """你在写技术方案书。基于知识卡产出**可直接进方案的参数与约束清单**。
主题：{question}

要求：
- 参数用清单列出，保留具体数值与单位
- 约束/风险单列一节，注明原因
- 每条末尾用括号标注来源小节，便于追溯

知识卡：
{knowledge}""",
        "question": "CAN 报文解析与私有协议逆向实施要点",
    },
    "exam": {
        "label": "培训考核（出题）",
        "prompt": """你是内部培训出题人。基于知识卡出题考察理解，重点考"边界和陷阱"。
主题：{question}

输出：3 道单选题（含答案解析）+ 1 道场景题（含参考答案）。题目必须能直接从知识卡推出答案，不要超纲。

知识卡：
{knowledge}""",
        "question": "CAN 协议基础与报文解读",
    },
}


async def render(knowledge: dict, scenario_key: str) -> str:
    sc = SCENARIOS[scenario_key]
    kn = json.dumps(knowledge, ensure_ascii=False)[:12000]
    # ⚠️ 踩坑：DeepSeek 是推理模型，reasoning 会先吃掉 token，2500 会导致正文为空
    txt = await llm(sc["prompt"].format(question=sc["question"], knowledge=kn), max_tokens=8000)
    if not txt.strip():
        # 空输出通常是 reasoning 吃光 token，翻倍重试一次
        txt = await llm(sc["prompt"].format(question=sc["question"], knowledge=kn), max_tokens=16000)
    return txt


# ---------------- 主流程 ----------------
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc")
    ap.add_argument("--out", default="distill_out")
    ap.add_argument("--scenarios", default="newbie,customer,proposal,exam")
    args = ap.parse_args()

    src = Path(args.doc)
    text = src.read_text(encoding="utf-8")
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    print(f"📄 文档: {src.name}（{len(text)} 字）")

    chunks = split_document(text)
    print(f"✂️  切分为 {len(chunks)} 个语义切片")

    print("🔥 提炼中（并发）...")
    cards = await asyncio.gather(*[distill_chunk(c) for c in chunks])
    knowledge = merge_cards([c for c in cards if c], src.stem)
    (outdir / "knowledge.json").write_text(json.dumps(knowledge, ensure_ascii=False, indent=2), encoding="utf-8")

    n = sum(len(knowledge[k]) for k in ("concepts", "parameters", "procedures", "constraints", "faq"))
    print(f"✅ 知识卡: 概念 {len(knowledge['concepts'])} / 参数 {len(knowledge['parameters'])} / "
          f"流程 {len(knowledge['procedures'])} / 约束 {len(knowledge['constraints'])} / FAQ {len(knowledge['faq'])}"
          f"  (共 {n} 条)")
    print(f"   蒸馏耗时: {time.monotonic()-t0:.1f}s → {outdir/'knowledge.json'}")

    print("\n🎨 场景渲染（同一份知识 → 不同形态）:")
    for key in args.scenarios.split(","):
        key = key.strip()
        if key not in SCENARIOS:
            continue
        t1 = time.monotonic()
        try:
            txt = await render(knowledge, key)
        except Exception as e:
            txt = f"（渲染失败: {e}）"
        (outdir / f"scene_{key}.md").write_text(
            f"# 场景：{SCENARIOS[key]['label']}\n\n**问题**：{SCENARIOS[key]['question']}\n\n---\n\n{txt}\n",
            encoding="utf-8")
        print(f"   ✓ {SCENARIOS[key]['label']}  ({time.monotonic()-t1:.1f}s, {len(txt)} 字)")

    print(f"\n⏱ 总耗时: {time.monotonic()-t0:.1f}s ｜ 产物: {outdir}/")


if __name__ == "__main__":
    asyncio.run(main())
