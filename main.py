"""AstrBot 情感迭代：状态、记忆种子、聊天反思。

不做模型微调；用持久状态 + 情感记忆 + 小步更新，让表达慢慢变化。
只对配置的主人私聊生效；群聊和定时任务不训练。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.config.astrbot_config import AstrBotConfig

DATA_ROOT = Path(__file__).resolve().parents[2]
STORE_DIR = DATA_ROOT / "plugin_data" / "astrbot_plugin_dafeiyu_emotion"
STATE_PATH = STORE_DIR / "state.json"
DEFAULT_LM_DB = DATA_ROOT / "plugin_data" / "astrbot_plugin_livingmemory" / "livingmemory.db"

DEFAULTS: dict[str, Any] = {
    "affection": 55.0,
    "trust": 55.0,
    "security": 55.0,
    "mood": 0.0,
    "energy": 70.0,
    "possessiveness": 35.0,
    "stage": "熟悉",
    "profile": "",
    "events": [],
    "baseline": {
        "affection": 55.0,
        "trust": 55.0,
        "security": 55.0,
        "mood": 0.0,
        "energy": 70.0,
        "possessiveness": 35.0,
    },
    "seeded": False,
    "last_day": "",
    "last_reflection": "",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _stage(affection: float) -> str:
    if affection < 30:
        return "生疏"
    if affection < 50:
        return "熟悉"
    if affection < 70:
        return "亲近"
    if affection < 85:
        return "很亲近"
    return "信任"


def _state_template() -> dict[str, Any]:
    data = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    data["baseline"] = dict(data["baseline"])
    data["events"] = []
    data["last_day"] = date.today().isoformat()
    return data


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


class EmotionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.owner = str(self.config.get("owner_qq", "") or "").strip()
        self.enabled = bool(self.config.get("enabled", True))
        try:
            self.reflect_every = max(1, int(self.config.get("reflect_every", 6) or 6))
        except (TypeError, ValueError):
            self.reflect_every = 6
        raw_db = str(self.config.get("livingmemory_db", "") or "").strip()
        self.lm_db = Path(raw_db).expanduser() if raw_db else DEFAULT_LM_DB
        self.state = self._load_state()
        self._pending: list[dict[str, str]] = []
        self._turns = 0
        self._lock = asyncio.Lock()
        self._reflect_task: asyncio.Task | None = None
        self._seed_task: asyncio.Task | None = None

    async def initialize(self):
        if self.enabled and not self.state.get("seeded"):
            self._seed_task = asyncio.create_task(self._seed_from_memories())

    async def terminate(self):
        for task in (self._seed_task, self._reflect_task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *[t for t in (self._seed_task, self._reflect_task) if t],
            return_exceptions=True,
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            if STATE_PATH.is_file():
                value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    merged = _state_template()
                    merged.update(value)
                    merged["baseline"] = {**DEFAULTS["baseline"], **(value.get("baseline") or {})}
                    merged["events"] = list(value.get("events") or [])[-30:]
                    return merged
        except Exception as exc:
            logger.warning("[emotion] 读取状态失败：%s", exc)
        return _state_template()

    def _save_state(self):
        try:
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(STATE_PATH)
        except Exception as exc:
            logger.warning("[emotion] 保存状态失败：%s", exc)

    def _daily_decay(self):
        today = date.today().isoformat()
        if self.state.get("last_day") == today:
            return
        base = self.state.get("baseline") or {}
        self.state["mood"] = round(
            self.state["mood"] * 0.7 + float(base.get("mood", 0.0)) * 0.3, 1
        )
        self.state["energy"] = round(min(100.0, self.state["energy"] + 5.0), 1)
        self.state["last_day"] = today
        self._save_state()

    def _is_private(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private())
        except Exception:
            try:
                return not bool(event.get_group_id())
            except Exception:
                return False

    def _eligible(self, event: AstrMessageEvent) -> bool:
        if not self.enabled or not self.owner:
            return False
        try:
            if str(event.get_sender_id()) != str(self.owner):
                return False
        except Exception:
            return False
        name = type(event).__name__.lower()
        if "cron" in name:
            return False
        return self._is_private(event)

    def _state_block(self) -> str:
        st = self.state
        recent = st.get("events") or []
        recent_text = ""
        if recent:
            recent_text = "；".join(
                f"{e.get('event', '')}（{e.get('feeling', '')}）"
                for e in recent[-2:]
                if e.get("event")
            )
        return (
            "【当前情感状态（内部参考，不要报数值，也不要解释这段）】\n"
            f"你和主人的关系阶段：{st.get('stage', '熟悉')}；"
            f"好感 {float(st.get('affection', 55)):.0f}/100，"
            f"信任 {float(st.get('trust', 55)):.0f}/100，"
            f"安全感 {float(st.get('security', 55)):.0f}/100，"
            f"心情 {float(st.get('mood', 0)):+.0f}，"
            f"精力 {float(st.get('energy', 70)):.0f}/100。\n"
            + (
                f"关系背景：{str(st.get('profile') or '')[:300]}\n"
                if st.get("profile")
                else ""
            )
            + (f"最近触发：{recent_text}。\n" if recent_text else "")
            + "表达要求：保持角色原本的傲娇、嘴硬心软；心情低时更安静、更别扭，"
            "心情好时更黏；不要客服腔，不要提数值，不要突然换人格。\n"
        )

    @filter.on_llm_request()
    async def inject_state(self, event: AstrMessageEvent, req):
        if not self._eligible(event):
            return
        self._daily_decay()
        if not self.state.get("seeded") and not self._seed_task:
            self._seed_task = asyncio.create_task(self._seed_from_memories())
        req.extra_user_content_parts.append(TextPart(text=self._state_block()))

    @filter.on_llm_response()
    async def observe(self, event: AstrMessageEvent, response):
        if not self._eligible(event):
            return
        user = (getattr(event, "message_str", "") or "").strip()
        assistant = (getattr(response, "completion_text", "") or "").strip()
        if not user or not assistant:
            return
        self._pending.append(
            {
                "user": user[:800],
                "assistant": assistant[:800],
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self._pending = self._pending[-8:]
        self._turns += 1
        if self._turns >= self.reflect_every and not self._reflect_task:
            self._reflect_task = asyncio.create_task(self._reflect())
        if len(self.state.get("events", [])) > 30:
            self.state["events"] = self.state["events"][-30:]

    async def _seed_from_memories(self):
        async with self._lock:
            if self.state.get("seeded"):
                return
            try:
                text = await asyncio.to_thread(self._read_memories)
            except Exception as exc:
                logger.warning("[emotion] 读取旧记忆失败：%s", exc)
                text = ""
            data: dict[str, Any] = {}
            if text:
                prompt = (
                    "你是这个角色，正在整理和主人相处以来的关系记忆。只根据下面的记忆，"
                    "写一段 300 字以内的关系档案：关系怎么走到现在、重要的事、主人的偏好和雷区、"
                    "你目前的好感/信任/安全感/心情/精力。不要编造，不要写真实姓名或隐私。\n"
                    "输出严格 JSON："
                    '{"profile":"...","affection":0-100,"trust":0-100,"security":0-100,'
                    '"mood":-50-50,"energy":0-100,"possessiveness":0-100}\n\n'
                    f"记忆：\n{text[:7000]}"
                )
                for attempt in range(3):
                    data = await self._ask_json(prompt, "你是关系记忆整理器，只输出 JSON。")
                    if data:
                        break
                    await asyncio.sleep(5)
                if data:
                    for key in ("affection", "trust", "security", "mood", "energy", "possessiveness"):
                        if key in data:
                            try:
                                self.state[key] = float(data[key])
                            except (TypeError, ValueError):
                                pass
                    self.state["profile"] = str(data.get("profile") or "")[:1200]
                elif text:
                    # 模型暂时不可用时，至少把旧记忆里的原话留作关系背景。
                    self.state["profile"] = "我记得的事：" + "；".join(
                        part.strip()[:80] for part in text.splitlines()[:6] if part.strip()
                    )[:500]
            if not self.state.get("profile"):
                self.state["profile"] = (
                    "我和主人已经相处了一段时间，知道他忙起来会顾不上回消息，"
                    "也知道他嘴上不饶人但会把我说过的话记着。"
                )
            self.state["stage"] = _stage(float(self.state.get("affection", 55)))
            self.state["baseline"] = {
                "affection": float(self.state["affection"]),
                "trust": float(self.state["trust"]),
                "security": float(self.state["security"]),
                "mood": 0.0,
                "energy": 70.0,
                "possessiveness": float(self.state["possessiveness"]),
            }
            self.state["seeded"] = True
            self._save_state()
            logger.info(
                "[emotion] 情感档案初始化完成，阶段=%s，档案长度=%s",
                self.state["stage"],
                len(self.state.get("profile") or ""),
            )

    def _read_memories(self) -> str:
        if not self.lm_db.is_file():
            return ""
        conn = sqlite3.connect(f"file:{self.lm_db}?mode=ro", uri=True)
        try:
            parts: list[str] = []
            for (text,) in conn.execute(
                "select text from documents where text is not null and length(text)>0 "
                "order by id desc limit 24"
            ):
                text = str(text).strip()
                if text:
                    parts.append(text[:500])
            for (text,) in conn.execute(
                "select content from memory_atoms where content is not null and length(content)>0 "
                "order by id desc limit 80"
            ):
                text = str(text).strip()
                if text:
                    parts.append(text[:300])
            return "\n".join(parts)[:9000]
        finally:
            conn.close()

    async def _ask_json(self, prompt: str, system: str) -> dict[str, Any]:
        try:
            provider = self.context.get_using_provider()
            if inspect.isawaitable(provider):
                provider = await provider
            if provider is None:
                logger.warning("[emotion] 后台模型还没有就绪")
                return {}
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=system,
                session_id=uuid.uuid4().hex,
                persist=False,
            )
            text = getattr(response, "completion_text", "") or ""
            data = _parse_json(text)
            if not data:
                logger.warning(
                    "[emotion] 后台 JSON 解析失败，响应长度=%s，开头=%r",
                    len(text),
                    text[:120],
                )
            return data
        except Exception as exc:
            logger.warning("[emotion] 后台模型调用失败：%s", exc)
            return {}

    async def _reflect(self):
        try:
            async with self._lock:
                pending = self._pending[-6:]
                self._pending = []
                if not pending:
                    self._turns = 0
                    return
                st = self.state
                convo = "\n".join(
                    f"主人：{item['user']}\n这个角色：{item['assistant']}" for item in pending
                )
                prompt = (
                    "根据下面这段聊天和当前情感状态，判断这个角色对主人的情感有没有变化。"
                    "只做小步调整，不因一次玩笑大起大落。输出严格 JSON：\n"
                    '{"deltas":{"affection":-2..2,"trust":-2..2,"security":-2..2,'
                    '"mood":-8..8,"energy":-5..5,"possessiveness":-2..2},'
                    '"event":"发生了什么","feeling":"这个角色的感受","evidence":"依据"}\n'
                    "没有变化就全填 0；不要输出 JSON 以外的内容。\n\n"
                    f"当前状态：{json.dumps({k: st.get(k) for k in ('affection','trust','security','mood','energy','possessiveness','stage')}, ensure_ascii=False)}\n"
                    f"关系档案：{str(st.get('profile') or '')[:800]}\n"
                    f"最近对话：\n{convo[:5000]}"
                )
                data = await self._ask_json(prompt, "你是情感状态分析器，只输出 JSON。")
                if not data:
                    self._turns = 0
                    return
                deltas = data.get("deltas") or {}
                caps = {
                    "affection": 2.0,
                    "trust": 2.0,
                    "security": 2.0,
                    "mood": 8.0,
                    "energy": 5.0,
                    "possessiveness": 2.0,
                }
                applied: dict[str, float] = {}
                for key, cap in caps.items():
                    try:
                        value = float(deltas.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        value = 0.0
                    value = _clamp(value, -cap, cap)
                    if key == "mood":
                        st[key] = round(_clamp(float(st[key]) + value, -50.0, 50.0), 1)
                    elif key == "possessiveness":
                        st[key] = round(_clamp(float(st[key]) + value, 0.0, 100.0), 1)
                    elif key == "energy":
                        st[key] = round(_clamp(float(st[key]) + value, 0.0, 100.0), 1)
                    else:
                        st[key] = round(_clamp(float(st[key]) + value, 0.0, 100.0), 1)
                    applied[key] = round(value, 1)
                base_mood = float((st.get("baseline") or {}).get("mood", 0.0))
                st["mood"] = round(st["mood"] * 0.85 + base_mood * 0.15, 1)
                st["stage"] = _stage(float(st["affection"]))
                if data.get("event"):
                    st.setdefault("events", []).append(
                        {
                            "at": datetime.now().isoformat(timespec="seconds"),
                            "event": str(data.get("event"))[:200],
                            "feeling": str(data.get("feeling"))[:200],
                            "evidence": str(data.get("evidence"))[:200],
                            "deltas": applied,
                        }
                    )
                    st["events"] = st["events"][-30:]
                st["last_reflection"] = datetime.now().isoformat(timespec="seconds")
                self._save_state()
                self._turns = 0
                logger.info(
                    "[emotion] 情感反思完成：阶段=%s 好感=%.1f 信任=%.1f 心情=%+.1f",
                    st["stage"],
                    st["affection"],
                    st["trust"],
                    st["mood"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[emotion] 情感反思失败：%s", exc)
        finally:
            self._reflect_task = None
            self._turns = 0

    def _owner_command(self, event: AstrMessageEvent) -> bool:
        if not self.enabled or not self.owner:
            return False
        try:
            return str(event.get_sender_id()) == str(self.owner) and self._is_private(event)
        except Exception:
            return False

    @filter.command("情感")
    async def show_emotion(self, event: AstrMessageEvent):
        if not self._owner_command(event):
            return
        st = self.state
        recent = st.get("events") or []
        lines = [
            f"现在是「{st.get('stage', '熟悉')}」阶段。",
            f"好感 {float(st.get('affection', 0)):.0f}/100，信任 {float(st.get('trust', 0)):.0f}/100，"
            f"安全感 {float(st.get('security', 0)):.0f}/100，心情 {float(st.get('mood', 0)):+.0f}，"
            f"精力 {float(st.get('energy', 0)):.0f}/100。",
        ]
        if recent:
            last = recent[-1]
            lines.append(f"最近一次：{last.get('event', '')}（{last.get('feeling', '')}）")
        yield event.plain_result("\n".join(lines))

    @filter.command("情感原因")
    async def show_reasons(self, event: AstrMessageEvent):
        if not self._owner_command(event):
            return
        recent = self.state.get("events") or []
        if not recent:
            yield event.plain_result("还没有记录到明显的情感变化。")
            return
        lines = []
        for item in recent[-5:]:
            lines.append(
                f"{item.get('at', '')[:16]}  {item.get('event', '')} → {item.get('feeling', '')}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("情感重置")
    async def reset_emotion(self, event: AstrMessageEvent):
        if not self._owner_command(event):
            return
        profile = self.state.get("profile", "")
        seeded = bool(self.state.get("seeded"))
        base = self.state.get("baseline") or DEFAULTS["baseline"]
        self.state = _state_template()
        self.state["profile"] = profile
        self.state["seeded"] = seeded
        for key in (
            "affection",
            "trust",
            "security",
            "mood",
            "energy",
            "possessiveness",
        ):
            try:
                self.state[key] = float(base.get(key, DEFAULTS[key]))
            except (TypeError, ValueError):
                pass
        self.state["baseline"] = {
            key: float(self.state[key])
            for key in ("affection", "trust", "security", "mood", "energy", "possessiveness")
        }
        self.state["stage"] = _stage(float(self.state["affection"]))
        self.state["last_day"] = date.today().isoformat()
        self._save_state()
        yield event.plain_result("情感状态已重置到初始基线；关系档案保留。")
