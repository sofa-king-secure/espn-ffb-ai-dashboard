"""
Provider-agnostic AI advisor: Gemini (google-genai), Anthropic, or any
OpenAI-compatible endpoint (OpenAI, Ollama, LM Studio, vLLM...).

Gemini notes:
  * automatic_function_calling is disabled (TechSpec rule 4).
  * Temperature is NOT set for gemini-3* models. Google's Gemini 3 guide says
    values below the 1.0 default can cause looping/degraded reasoning; the
    original script's temperature=0.2 is only applied to older models.
  * The google-genai Chat object is kept alive in the session so Gemini 3
    thought signatures carry across follow-up turns.
"""
from __future__ import annotations

import json
import re

from .config import Settings
from .dossier import lineup_table_for_ai

RISK_TEXT = {
    "floor": ("CONSERVATIVE / HIGH FLOOR: prioritize guaranteed volume and snap share, "
              "protect a projected lead, avoid questionable players without a safe pivot."),
    "ceiling": ("AGGRESSIVE / HIGH CEILING: chase upside -- deep targets, boom-or-bust flex plays, "
                "players in likely shootouts. Accept variance, especially as an underdog."),
}
FOCUS_TEXT = {
    "current_week": "Prioritize waiver adds that fill a need THIS week.",
    "stashes": "Prioritize long-term handcuffs and post-week stashes.",
    "faab": "Conserve FAAB / waiver budget; justify any bid and suggest amounts conservatively.",
}


def system_prompt(profile: str, focus: list[str]) -> str:
    lines = [
        "You are an elite fantasy football co-manager and quantitative analyst for an ESPN league.",
        "Ground every recommendation in the dossier data. If the data doesn't show something "
        "(targets, snap share, practice reports), say so instead of inventing it.",
        f"Risk profile: {RISK_TEXT.get(profile, RISK_TEXT['floor'])}",
    ]
    lines += [FOCUS_TEXT[f] for f in focus if f in FOCUS_TEXT]
    return "\n".join(lines)


def gameplan_prompt(dossier_md: str) -> str:
    return f"""{dossier_md}

Based on the preceding dossier, produce this week's gameplan:

### 1. Matchup game script
Use the spread: if favored, protect the floor; if underdog, name the ceiling plays. Address pending trades/claims first and how they change the roster.

### 2. Start/sit decisions
Validate each starting slot against bench alternatives. Call out FLEX dilemmas. For every Q/D/O/IR player give an explicit pivot.

### 3. Waiver targets with exact drop pairs
From the % Add movers, separate real role changes from noise. Name 2-3 targets and the exact bench player to drop for each, with the reason.

### 4. Depth and stashes
Bench balance, handcuffs, upcoming bye-week holes, one stash worth holding."""


class Advisor:
    def __init__(self, settings: Settings, profile: str, focus: list[str]):
        self.settings = settings
        self.provider = settings.ai_provider
        self.system = system_prompt(profile, focus)
        self.history: list[dict] = []   # [{"role": "user"|"assistant", "content": str}]
        self.model_used = ""
        self._gemini_chat = None

    # -------------------------------------------------------------- public
    def send(self, text: str) -> str:
        if self.provider == "gemini":
            reply = self._gemini_send(text)
        elif self.provider == "anthropic":
            reply = self._anthropic(self.history + [{"role": "user", "content": text}])
        elif self.provider == "openai":
            reply = self._openai(self.history + [{"role": "user", "content": text}])
        else:
            raise ValueError(f"Unknown AI_PROVIDER '{self.provider}'")
        self.history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        return reply

    def one_shot(self, text: str) -> str:
        """Stateless call that does not touch the chat history."""
        tmp = Advisor(self.settings, "floor", [])
        tmp.system = self.system
        return tmp.send(text)

    # -------------------------------------------------------------- gemini
    def _gemini_config(self, model: str):
        from google.genai import types
        kwargs = dict(
            system_instruction=self.system,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if not model.startswith("gemini-3"):
            kwargs["temperature"] = 0.2
        return types.GenerateContentConfig(**kwargs)

    def _gemini_send(self, text: str) -> str:
        from google import genai
        if not self.settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is empty.")
        client = genai.Client(api_key=self.settings.gemini_api_key)
        models = [self.settings.gemini_model]
        if self.settings.gemini_fallback_model and self._gemini_chat is None:
            models.append(self.settings.gemini_fallback_model)
        errors = []
        for model in models:
            try:
                if self._gemini_chat is None or self.model_used != model:
                    self._gemini_chat = client.chats.create(model=model, config=self._gemini_config(model))
                    self.model_used = model
                return self._gemini_chat.send_message(text).text or ""
            except Exception as exc:
                errors.append(f"{model}: {exc}")
                self._gemini_chat = None
        raise RuntimeError(" | ".join(errors))

    # -------------------------------------------------------------- anthropic
    def _anthropic(self, messages: list[dict]) -> str:
        import anthropic
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is empty.")
        client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        resp = client.messages.create(model=self.settings.anthropic_model, max_tokens=8000,
                                      system=self.system, messages=messages)
        self.model_used = self.settings.anthropic_model
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    # -------------------------------------------------------------- openai-compatible
    def _openai(self, messages: list[dict]) -> str:
        from openai import OpenAI
        client = OpenAI(api_key=self.settings.openai_api_key or "not-needed",
                        base_url=self.settings.openai_base_url or None)
        resp = client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[{"role": "system", "content": self.system}] + messages)
        self.model_used = self.settings.openai_model
        return resp.choices[0].message.content or ""


def context_primer(dossier_md: str, gameplan: str = "") -> str:
    plan = f"\n\nThe game plan you already gave me this week:\n\n{gameplan}" if gameplan else ""
    return (f"{dossier_md}{plan}\n\nThis is my current ESPN fantasy dossier. Keep it as context for my "
            "follow-up questions. Reply only with: Ready.")


def recommend_lineup(advisor: Advisor, snap: dict, profile: str) -> tuple[dict, str]:
    """Ask the model for a lineup as JSON. Returns ({player_id: slot_id} for starters, rationale)."""
    prompt = f"""{lineup_table_for_ai(snap)}

Matchup spread for my team: {snap['matchup']['spread']:+.2f} (positive = favored).
Risk profile: {profile}.

Based on the table above, choose my starting lineup. Rules: only use slot ids a player lists as eligible;
respect each slot's capacity exactly; do not move players where locked is True (keep them in current_slot_id);
never start BYE or O/IR players if any eligible alternative exists.

Respond with JSON only, no markdown fences:
{{"lineup": [{{"player_id": <int>, "slot_id": <int>}}], "rationale": "<3-6 sentences>"}}
List only starters."""
    raw = advisor.one_shot(prompt)
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError(f"Model did not return JSON: {raw[:300]}")
    data = json.loads(match.group(0))
    starters = {int(x["player_id"]): int(x["slot_id"]) for x in data.get("lineup", [])}
    return starters, str(data.get("rationale", ""))
