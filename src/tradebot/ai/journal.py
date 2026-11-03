"""Daily journal commentary.

Two modes, same output shape:

* **template** (always available, deterministic, zero cost) - reads the report's own
  numbers and writes plain-English bullets.
* **LLM** (optional) - one call per day, grounded strictly in the JSON of the day.
  Every number the model writes is cross-checked against the JSON; if the check
  fails, the template commentary is used instead. The model has no tools and cannot
  place orders or change limits, so a prompt injection cannot reach the trading path.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..config import Config
from ..core.logging import get_logger

log = get_logger(__name__)

_NUMBER = re.compile(r"[-+]?\d[\d,]*\.?\d*")


class JournalAgent:
    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self.cfg = config
        self.client = client
        self.used_llm = False
        self.last_error: str | None = None

    # --- public -----------------------------------------------------------
    def commentary(self, report: Any) -> list[str]:
        template = self.template_notes(report)
        if not (self.cfg.secrets.llm_api_key and self.cfg.secrets.llm_provider):
            return template
        # The synchronous path used by the engine keeps the daily report fast and
        # dependency-free; LLM calls are made by `commentary_async` when available.
        return template

    @staticmethod
    def template_notes(report: Any) -> list[str]:
        notes: list[str] = []
        pnl = report.net_pnl
        pct = report.net_pnl_pct * 100
        direction = "up" if pnl >= 0 else "down"
        notes.append(
            f"Equity finished {direction} {abs(pct):.2f}% ({pnl:+.2f} {report.mode.upper()} quote units) "
            f"on {report.trades.count} closed trade(s)."
        )
        if report.trades.count:
            notes.append(
                f"Win rate {report.trades.win_rate * 100:.0f}% with profit factor "
                f"{report.trades.profit_factor:.2f}; average {report.trades.avg_r:+.2f}R per trade."
            )
        else:
            notes.append("No trades closed today: signals were either absent or vetoed by risk.")
        if report.by_strategy:
            best = max(report.by_strategy.items(), key=lambda kv: float(kv[1]["pnl"]))
            worst = min(report.by_strategy.items(), key=lambda kv: float(kv[1]["pnl"]))
            notes.append(f"Best strategy: {best[0]} ({best[1]['pnl']:+.2f}); weakest: {worst[0]} ({worst[1]['pnl']:+.2f}).")
        notes.append(
            f"Fees {report.fees:.2f}, tax reserve accrued {report.tax_reserve:.2f} "
            f"(post-tax result {report.post_tax_pnl:+.2f})."
        )
        notes.append(
            f"Risk: max intraday drawdown {report.max_intraday_drawdown * 100:+.2f}%, "
            f"peak exposure {report.peak_exposure_pct * 100:.0f}%, kill switch {report.kill_level}."
        )
        if report.limits_hit:
            notes.append(f"Risk vetoes recorded: {', '.join(report.limits_hit[:3])}.")
        if report.incidents:
            notes.append(f"{len(report.incidents)} incident(s) logged; see the incident list before tomorrow's session.")
        notes.append("These notes describe the recorded numbers only; they are not advice or a forecast.")
        return notes[:8]

    # --- optional LLM path ------------------------------------------------
    async def commentary_async(self, report: Any) -> list[str]:
        template = self.template_notes(report)
        if not (self.cfg.secrets.llm_api_key and self.cfg.secrets.llm_provider):
            return template
        payload = report.to_dict()
        prompt = (
            "You are a trading journal assistant. Using ONLY the JSON numbers below, write 5 to 8 "
            "short bullets about the day. Do not predict, do not advise, do not invent numbers. "
            "Every number you write must appear in the JSON.\n\n"
            f"JSON:\n{json.dumps(payload, default=str)[:6000]}"
        )
        try:
            text = await self._call_llm(prompt)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning('{"event":"llm_journal_failed","error":"%s"}', exc)
            return template
        bullets = [line.strip("-* ").strip() for line in text.splitlines() if line.strip()]
        if not bullets or not self._numbers_are_grounded(bullets, payload):
            self.last_error = "number check failed"
            return template
        self.used_llm = True
        return bullets[:8]

    async def _call_llm(self, prompt: str) -> str:
        provider = self.cfg.secrets.llm_provider.lower()
        key = self.cfg.secrets.llm_api_key
        if provider in ("openai", "openai-compatible"):
            url = "https://api.openai.com/v1/chat/completions"
            body = {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": 0.2,
            }
            headers = {"Authorization": f"Bearer {key}"}
        elif provider == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            body = {
                "model": "claude-3-5-haiku-latest",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            }
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        else:
            raise ValueError(f"unsupported LLM provider {provider!r}")
        if self.client is not None:
            response = await self.client.post(url, json=body, headers=headers, timeout=30)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=body, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        if provider == "anthropic":
            return "".join(block.get("text", "") for block in data.get("content", []))
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _numbers_are_grounded(bullets: list[str], payload: dict[str, Any]) -> bool:
        corpus = json.dumps(payload, default=str)
        allowed = {_normalise(m.group(0)) for m in _NUMBER.finditer(corpus)}
        for bullet in bullets:
            for match in _NUMBER.finditer(bullet):
                token = _normalise(match.group(0))
                if not token:
                    continue
                if token not in allowed and not any(token in a or a in token for a in allowed):
                    return False
        return True


def _normalise(token: str) -> str:
    token = token.replace(",", "").lstrip("+")
    if token.endswith("."):
        token = token[:-1]
    try:
        value = float(token)
    except ValueError:
        return token
    if value.is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")
