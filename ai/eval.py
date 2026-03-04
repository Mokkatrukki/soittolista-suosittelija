"""CLI-testipenkki ConductorDSPy:lle.

Käyttö:
    uv run python ai/eval.py                          # evaluoi kaikki esimerkit
    uv run python ai/eval.py --opt                    # BootstrapFewShot-optimointi
    uv run python ai/eval.py --model gemini-2.5-flash # vertaa eri mallilla
"""
import argparse
import json
from pathlib import Path

import dspy
from dotenv import load_dotenv

load_dotenv()

from ai.conductor import ConductorDSPy, _get_module  # noqa: E402

EXAMPLES_FILE = Path(__file__).parent.parent / "data" / "conductor_examples.json"
OPTIMIZED_FILE = Path(__file__).parent.parent / "data" / "conductor_optimized.json"


# ---------------------------------------------------------------------------
# Esimerkkien lataus
# ---------------------------------------------------------------------------

def load_examples() -> list[dict]:
    with open(EXAMPLES_FILE, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Metriikka
# ---------------------------------------------------------------------------

def _jaccard(a: list[str], b: list[str]) -> float:
    """Jaccard-samanlaisuus kahdelle tagilistaalle (case-insensitive)."""
    if not a and not b:
        return 1.0
    sa = {s.lower() for s in a}
    sb = {s.lower() for s in b}
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union > 0 else 0.0


def metric(example, pred, trace=None) -> float:
    """Arviointimetriikka — toimii sekä dict- että dspy.Example-muodossa.

    Pisteet:
      state väärä          → 0.0
      asking, state oikein → 1.0
      ready, state oikein  → 0.5 + 0.25 (strategy) + 0.25 (genres Jaccard)
    """
    # Hae odotetut arvot — tukee sekä dict että dspy.Example
    if isinstance(example, dict):
        expected_state = example.get("expected_state", "asking")
        expected_strategy = example.get("expected_strategy", "")
        expected_genres: list[str] = example.get("expected_genres", [])
    else:
        # dspy.Example — kenttien nimet ovat suoria attribuutteja
        expected_state = getattr(example, "state", "asking")
        expected_strategy = getattr(example, "strategy", "")
        raw_genres = getattr(example, "genres", [])
        expected_genres = list(raw_genres) if raw_genres else []

    # Hae ennustetut arvot
    actual_state = getattr(pred, "state", "") or ""
    actual_strategy = getattr(pred, "strategy", "") or ""
    actual_genres = list(getattr(pred, "genres", []) or [])

    if actual_state != expected_state:
        return 0.0

    if expected_state == "asking":
        return 1.0

    # ready-tapaus
    score = 0.5

    if expected_strategy:
        score += 0.25 if actual_strategy == expected_strategy else 0.0
    else:
        score += 0.25  # ei rajoitusta → täydet pisteet

    if expected_genres:
        score += 0.25 * _jaccard(expected_genres, actual_genres)
    else:
        score += 0.25  # ei rajoitusta → täydet pisteet

    return score


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

def evaluate(model: str | None = None) -> None:
    examples = load_examples()
    module = _get_module(model)

    print(f"\nMalli: {model or 'gemini-2.5-flash-lite-preview-09-2025'}")
    print(f"{'ID':<10} {'Viesti':<32} {'Odotettu':<10} {'Tulos':<6} {'Pisteet'}")
    print("-" * 72)

    total_score = 0.0
    for ex in examples:
        try:
            pred = module(history=ex["history"], message=ex["message"])
            score = metric(ex, pred)
            state_mark = "✓" if pred.state == ex["expected_state"] else "✗"
            msg_short = ex["message"][:30]
            print(
                f"{ex['id']:<10} {msg_short:<32} {ex.get('expected_state', ''):<10} "
                f"{state_mark:<6} {score:.2f}"
            )
            total_score += score
        except Exception as e:
            print(f"{ex['id']:<10} ERROR: {e}")

    print("-" * 72)
    print(f"Yhteensä: {total_score:.2f} / {len(examples):.0f} pistettä\n")


# ---------------------------------------------------------------------------
# Optimize
# ---------------------------------------------------------------------------

def optimize(model: str | None = None) -> None:
    examples = load_examples()
    module = _get_module(model)

    # Muunna dspy.Example-olioiksi — labelit: state, strategy, genres
    trainset = [
        dspy.Example(
            history=ex["history"],
            message=ex["message"],
            state=ex["expected_state"],
            strategy=ex.get("expected_strategy", "genre_tags"),
            genres=ex.get("expected_genres", []),
        ).with_inputs("history", "message")
        for ex in examples
    ]

    def dspy_metric(example, pred, trace=None) -> bool:
        return metric(example, pred, trace) > 0.5

    print(f"Optimoidaan {len(trainset)} esimerkillä (BootstrapFewShot)...")
    optimizer = dspy.BootstrapFewShot(metric=dspy_metric, max_bootstrapped_demos=4)
    optimized = optimizer.compile(module, trainset=trainset)

    OPTIMIZED_FILE.parent.mkdir(exist_ok=True)
    optimized.save(str(OPTIMIZED_FILE))
    print(f"Tallennettu: {OPTIMIZED_FILE}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ConductorDSPy evaluaattori ja optimoija"
    )
    parser.add_argument(
        "--opt",
        action="store_true",
        help="Aja BootstrapFewShot-optimointi ja tallenna tulokset",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Gemini-malli, esim. gemini-2.5-flash (oletus: flash-lite)",
    )
    args = parser.parse_args()

    if args.opt:
        optimize(args.model)
    else:
        evaluate(args.model)


if __name__ == "__main__":
    main()
