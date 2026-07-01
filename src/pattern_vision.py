"""Gemini VISION confirmation for chart patterns.

The algorithmic detector (`pattern_detect`) finds the geometry; this layer asks
a multimodal model to look at the actual candlestick chart and confirm (or
reject) what the algorithm thinks it sees — the "AI" half of the user's
"algorithm + AI vision" design. It is the pattern strategy's equivalent of
`ai_validator.validate` and is consulted in the same place in main.py.

Reuses ai_validator's proven machinery: the `from google import genai` client,
key rotation, the model cascade, 429/quota handling, and a strict FAIL-SAFE —
any missing key / quota / render / parse error returns "confirm, neutral" so the
vision layer can NEVER silently kill a valid algorithmic signal. Only a
high-confidence 'reject' can gate an entry, and only when the caller is in
blocking mode (PATTERN_VISION_BLOCKING).

No new dependency: matplotlib (already required) renders the chart to PNG bytes
in memory; google-genai (already used by ai_validator) sends the image.
"""
from __future__ import annotations

import io
import json
import logging
import re

import matplotlib
matplotlib.use("Agg")          # headless — no display, safe in a scheduler/daemon
import matplotlib.pyplot as plt
import pandas as pd

from . import ai
from .indicators import Signal

log = logging.getLogger(__name__)


PROMPT = """You are a technical-analysis assistant for an automated, LONG-ONLY
US-stock trading bot. A geometric algorithm has flagged a BULLISH chart pattern
on {symbol} and wants to BUY at ${price}.

Algorithm's claim:
- pattern: {pattern_type}
- detector confidence: {pattern_confidence}/100
- already triggered (key level cleared): {triggered}
- key levels: {key_levels}

Look at the attached candlestick chart (most recent bars on the right; the
dashed line marks the algorithm's breakout/key level). Judge ONLY whether the
chart VISUALLY supports a bullish {pattern_type} (or another clear bullish
setup). Be skeptical of noise, single-bar flukes, and patterns that have already
run too far.

Respond with STRICT JSON, no markdown fence:
{{"verdict": "confirm" | "reject", "pattern": "what you actually see",
  "confidence": 0-100, "reason": "one short sentence"}}

If the chart is ambiguous, default to "confirm" with a low confidence (~40).
"""


def _render_chart(df: pd.DataFrame, signal: Signal, bars: int = 60) -> bytes:
    """Render the last `bars` candles to a PNG (bytes). Manual candles avoid an
    mplfinance dependency. Draws the algo's breakout level as a dashed line."""
    d = df.tail(bars).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
    for i, row in d.iterrows():
        up = row["close"] >= row["open"]
        color = "#26a69a" if up else "#ef5350"
        ax.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.8, zorder=1)
        lo, hi = sorted((row["open"], row["close"]))
        ax.add_patch(plt.Rectangle((i - 0.3, lo), 0.6, max(hi - lo, 1e-6),
                                   color=color, zorder=2))
    level = (signal.meta or {}).get("key_levels", {}).get("breakout")
    if level:
        ax.axhline(level, color="#888", linestyle="--", linewidth=1.0, zorder=0)
    ax.set_title(f"{signal.symbol}  {signal.meta.get('pattern_type', '')}",
                 fontsize=10)
    ax.margins(x=0.01)
    ax.grid(True, alpha=0.15)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def confirm(signal: Signal, df: pd.DataFrame) -> tuple[bool, int | None, str, str]:
    """Ask Gemini to vision-confirm the algo pattern.

    Returns (confirmed, vision_conf_0_100 | None, label, reason).
    FAIL-SAFE: any error / no key / quota → (True, None, "vision n/a", reason).
    """
    # Vision is multimodal — only Gemini supports it. When the active provider is
    # DeepSeek (text-only), skip this confirmation layer entirely (the algorithmic
    # pattern detector still gates the trade); fail-safe pass.
    if not ai.supports_vision():
        return True, None, "vision-skip", "provider has no vision — pass (algo pattern stands)"
    if not ai.has_key():
        return True, None, "vision-skip", "no AI key — vision layer inert (pass)"
    if not signal.meta.get("pattern_type"):
        return True, None, "vision-skip", "no pattern meta — pass"

    try:
        png = _render_chart(df, signal)
    except Exception as e:
        log.warning("pattern_vision: chart render failed for %s: %s", signal.symbol, e)
        return True, None, "vision-error", f"render failed ({e}) — pass"

    prompt = PROMPT.format(
        symbol=signal.symbol,
        price=signal.price,
        pattern_type=signal.meta.get("pattern_type"),
        pattern_confidence=signal.meta.get("pattern_confidence"),
        triggered=signal.meta.get("pattern_triggered"),
        key_levels=signal.meta.get("key_levels"),
    )

    try:
        text, model_name = ai.generate(prompt, image=png)
        match = re.search(r"\{.*\}", text, re.DOTALL)
        payload = json.loads(match.group(0) if match else text)
        verdict = str(payload.get("verdict", "confirm")).lower()
        confidence = int(payload.get("confidence", 40))
        reason = payload.get("reason", "")
        seen = payload.get("pattern", "")
        confirmed = verdict == "confirm"
        log.info("pattern_vision [%s] %s → %s (%d) %s",
                 model_name, signal.symbol, verdict, confidence, seen)
        return confirmed, confidence, seen, reason
    except Exception as e:
        log.warning("pattern_vision: AI vision unavailable for %s (%s) — pass (fail-safe)",
                    signal.symbol, e)
        return True, None, "vision-exhausted", "AI vision unavailable — pass (fail-safe)"
