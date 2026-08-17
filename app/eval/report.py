"""Text rendering for an evaluation run. Pure formatting — no IO, no ORM."""

from __future__ import annotations

from app.eval.harness import COMPONENTS, ComponentReport, HoldoutReport

_RULE = "─" * 74


def _header(title: str) -> list[str]:
    return [_RULE, title, _RULE]


def format_components(report: ComponentReport) -> str:
    lines = _header("COMPONENT DISTRIBUTIONS")
    lines.append(
        f"{'component':<24}{'weight':>7}{'min':>8}{'p50':>8}{'max':>8}"
        f"{'sd':>8}{'distinct':>10}"
    )
    for name in [*COMPONENTS, "total"]:
        d = report.pooled[name]
        weight = COMPONENTS.get(name)
        weight_text = f"{weight:.2f}" if weight is not None else "—"
        lines.append(
            f"{name:<24}{weight_text:>7}{d.minimum:>8.3f}{d.median:>8.3f}"
            f"{d.maximum:>8.3f}{d.stdev:>8.3f}{d.distinct:>10}"
        )

    lines.append("")
    for name, count in report.constant_for.items():
        if count:
            lines.append(
                f"  ! {name} was constant for {count}/{report.queries} queries "
                f"({COMPONENTS[name]:.0%} of the score ranking nothing)"
            )
    if not any(report.constant_for.values()):
        lines.append("  all components vary within every query")
    return "\n".join(lines)


def format_ablation(correlations: dict[str, float]) -> str:
    lines = _header("ABLATION  (rank correlation with the component removed)")
    lines.append("1.000 = removing it reorders nothing; lower = it does ranking work")
    lines.append("")
    for name, rho in sorted(correlations.items(), key=lambda kv: kv[1]):
        flag = "  ! inert" if rho >= 0.9999 else ""
        lines.append(f"{name:<24}{rho:>8.4f}{flag}")
    return "\n".join(lines)


def format_holdout(report: HoldoutReport) -> str:
    lines = _header("HELD-OUT PIVOT PREDICTION")
    if not report.queries:
        lines.append("no evaluable queries (corpus has no pivoters with peers)")
        return "\n".join(lines)

    lines.append(
        f"{report.queries} queries · mean corpus {report.results[0].corpus_size} · "
        f"chance precision {report.mean_base_rate:.3f}"
    )
    lines.append("")
    lines.append(f"{'k':>4}{'precision':>12}{'recall':>10}{'lift':>10}")
    for k in report.k_values:
        lines.append(
            f"{k:>4}{report.mean_precision_at(k):>12.3f}"
            f"{report.mean_recall_at(k):>10.3f}{report.lift_at(k):>9.2f}x"
        )
    lines.append("")
    lines.append(f"MRR {report.mean_reciprocal_rank:.4f}")
    lines.append("")
    lines.append(
        "lift is precision@k over the base rate: 1.00x means the ranking is\n"
        "indistinguishable from drawing at random."
    )
    return "\n".join(lines)
