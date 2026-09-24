"""backend/tools/rich_text_detector.py

Rich-text annotation detection: rule-based vocabulary check first,
then LLM batch judgment for ambiguous cases (20 texts per call).
Vocabulary file: backend/reference/rich_text_vocab.txt (hot-reloadable).
"""
from __future__ import annotations
import re
from pathlib import Path

from backend.config import STORAGE_DIR

VOCAB_FILE = STORAGE_DIR / "reference" / "rich_text_vocab.txt"


def load_vocab() -> list[str]:
    """Read vocab from file, one entry per line.

    Strips whitespace, skips empty lines and comment lines (starting with #).
    Returns [] if file missing (not an error — hot-reload tolerant).
    """
    if not VOCAB_FILE.exists():
        return []
    with open(VOCAB_FILE, "r", encoding="utf-8") as f:
        raw = [line.strip() for line in f]
    return [line for line in raw if line and not line.startswith("#")]


def rule_check(text: str, vocab: list[str]) -> tuple[bool, float]:
    """Return (is_rich_text, ratio) where ratio = matching_chars / len(text).

    Character-level overlap: every character in *text* that appears in
    any vocab entry counts as a match.  Ratio >= 0.3 → is_rich.
    Empty text → (False, 0.0).
    """
    if len(text) == 0:
        return (False, 0.0)

    # Build flat set of characters covered by the vocabulary
    vocab_chars: set[str] = set()
    for entry in vocab:
        vocab_chars.update(entry)

    matching = sum(1 for ch in text if ch in vocab_chars)
    ratio = matching / len(text)
    return (ratio >= 0.3, ratio)


def _parse_llm_response(content: str, expected_count: int) -> list[bool]:
    """Parse an LLM response text into a list of bools.

    Looks for lines like ``1. yes``, ``2) no``, ``3. Yes`` etc.
    Falls back to scanning for bare yes/no if no numbered lines found.
    Truncates or pads to *expected_count* with ``False``.
    """
    results: list[bool] = []
    for line in content.strip().split("\n"):
        m = re.match(r"\s*(\d+)[\.\)]\s*(yes|no)", line, re.IGNORECASE)
        if m:
            results.append(m.group(2).lower() == "yes")

    # If we got no numbered lines, try bare yes/no tokens
    if not results:
        tokens = re.findall(r"\b(yes|no)\b", content, re.IGNORECASE)
        results = [t.lower() == "yes" for t in tokens]

    # Pad / truncate
    while len(results) < expected_count:
        results.append(False)
    return results[:expected_count]


def llm_batch_check(texts: list[str]) -> list[bool]:
    """Send up to 20 texts per LLM call, return bool list (True = rich text).

    Uses ``anthropic.Anthropic`` with ``backend.config.ANTHROPIC_MODEL`` /
    ``ANTHROPIC_API_KEY``.  On any error returns ``[False] * len(texts)``
    (fail-safe: we don't block the pipeline on API issues).
    """
    if not texts:
        return []

    try:
        import anthropic
        from backend.config import ANTHROPIC_MODEL, ANTHROPIC_API_KEY

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except Exception:
        return [False] * len(texts)

    BATCH_SIZE = 20
    all_results: list[bool] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        try:
            numbered = "\n".join(f"{j + 1}. {t}" for j, t in enumerate(batch))
            prompt = (
                "For each numbered text below, reply with just 'yes' or 'no' "
                "— does it require rich-text annotation (contains paralanguage, "
                f"interjections, or non-standard pronunciation markers)?\n\n{numbered}"
            )

            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            content = response.content[0].text
            batch_results = _parse_llm_response(content, len(batch))
            all_results.extend(batch_results)
        except Exception:
            all_results.extend([False] * len(batch))

    return all_results


def detect_rich_text(
    text: str,
    vocab: list[str] | None = None,
    ratio_threshold: float = 0.3,
    ambiguous_range: tuple[float, float] = (0.1, 0.3),
) -> tuple[bool, str]:
    """Full detection pipeline: rule → LLM for ambiguous cases.

    Returns (is_rich_text, reason).

    * ratio >= *ratio_threshold* → flagged by rule
    * ratio < *ambiguous_range[0]* → clean by rule
    * otherwise (ambiguous) → one-shot LLM call
    """
    if vocab is None:
        vocab = load_vocab()

    _is_rich, ratio = rule_check(text, vocab)

    if ratio >= ratio_threshold:
        return (True, f"规则检测: ratio={ratio:.2f}")
    if ratio < ambiguous_range[0]:
        return (False, f"规则检测: ratio={ratio:.2f}")

    # Ambiguous — fall back to LLM
    llm_results = llm_batch_check([text])
    return (llm_results[0], "LLM判定")


def flag_batch(
    texts: list[str],
    ratio_threshold: float = 0.3,
) -> list[dict]:
    """Process a batch of texts, return ``[{text, flagged, reason}, ...]``.

    Loads vocab once, applies ``rule_check`` to every text, collects
    ambiguous-ratio texts into a single ``llm_batch_check`` call, then merges.
    """
    vocab = load_vocab()
    results: list[dict | None] = [None] * len(texts)
    ambiguous_indices: list[int] = []
    ambiguous_texts: list[str] = []

    for i, text in enumerate(texts):
        is_rich, ratio = rule_check(text, vocab)
        if is_rich:
            results[i] = {
                "text": text,
                "flagged": True,
                "reason": f"规则检测: ratio={ratio:.2f}",
            }
        elif ratio < 0.1:
            results[i] = {
                "text": text,
                "flagged": False,
                "reason": f"规则检测: ratio={ratio:.2f}",
            }
        else:
            ambiguous_indices.append(i)
            ambiguous_texts.append(text)

    if ambiguous_texts:
        llm_results = llm_batch_check(ambiguous_texts)
        for idx, flagged in zip(ambiguous_indices, llm_results):
            results[idx] = {
                "text": texts[idx],
                "flagged": flagged,
                "reason": "LLM判定",
            }

    # Safety: fill any remaining None slots (shouldn't happen, but guard)
    for i in range(len(results)):
        if results[i] is None:
            results[i] = {
                "text": texts[i],
                "flagged": False,
                "reason": "规则检测: ratio=0.00",
            }

    return results  # type: ignore[return-value]
