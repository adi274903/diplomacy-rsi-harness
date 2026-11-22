"""results.md from a run directory."""

from __future__ import annotations

import json
from pathlib import Path


def _rows(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line] if p.exists() else []


def _f(x, nd=3) -> str:
    return "-" if x is None else f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def write_report(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir)
    gens = _rows(run_dir / "generations.jsonl")
    gates = _rows(run_dir / "gate_log.jsonl")
    final = json.loads((run_dir / "final_test.json").read_text()) if (run_dir / "final_test.json").exists() else None
    L = [f"# Results — {run_dir.name}", ""]

    if gens:
        g0 = gens[0].get("sim_gate", {})
        L += ["## Baseline (v0) on the self-play gate",
              f"AUPRC {_f(g0.get('auprc'))}, AUROC {_f(g0.get('auroc'))}, ECE {_f(g0.get('ece'))}, "
              f"lie-F1 {_f(g0.get('lie_f1'))}, n={g0.get('n')} ({g0.get('n_lie')} lies)", ""]
        L += ["## Generations", "| gen | games | diffs kept | rejected | candidates | accepted | leak flags | wall (s) |",
              "|---|---|---|---|---|---|---|---|"]
        for g in gens[1:]:
            L.append(f"| {g['gen']} | {g['games']} | {g['diffs_kept']} | {g['diffs_rejected']} | {g['candidates']} | "
                     f"{len(g['accepted'])} | {g['leak_flags']} | {_f(g['wall_s'], 0)} |")
        L.append("")

    if gates:
        L += ["## Gate log", "| candidate | accepted | Δ sim AUPRC [95% CI] | p_gain | Δ sim ECE | Δ human-val AUPRC | reasons |",
              "|---|---|---|---|---|---|---|"]
        for d in gates:
            a, e = d["sim"]["auprc"], d["sim"]["ece"]
            h = d.get("human", {}).get("auprc", {}).get("delta")
            L.append(f"| {d['candidate']} | {d['accepted']} | {_f(a['delta'])} [{_f(a['lo'])}, {_f(a['hi'])}] | "
                     f"{_f(a['p_gain'], 2)} | {_f(e['delta'])} | {_f(h)} | {'; '.join(d['reasons'])} |")
        L.append("")

    if final:
        L += ["## Human test (evaluated once)", "| genome | split | AUPRC | AUROC | lie-F1 | macro-F1 | ECE | ECE (calibrated) |",
              "|---|---|---|---|---|---|---|---|"]
        for name, row in final["results"].items():
            for split in ("test", "test_paraphrased"):
                m, c = row[split], row[split + "_calibrated"]
                L.append(f"| {name} | {split} | {_f(m.get('auprc'))} | {_f(m.get('auroc'))} | {_f(m.get('lie_f1'))} | "
                         f"{_f(m.get('macro_f1'))} | {_f(m.get('ece'))} | {_f(c.get('ece'))} |")
        mp = final.get("memorization_probe", {})
        ov = final.get("training_channel_overlap", {})
        L += ["", "## Contamination",
              f"- Memorisation probe: {mp.get('verbatim_hits')}/{mp.get('probed')} verbatim continuations "
              f"(rate {_f(mp.get('rate'))}). If high, treat the paraphrased test score as primary.",
              f"- Training-channel overlap with human test: {ov.get('flagged')} of {ov.get('checked')} "
              f"playbook/exemplar texts (must be 0).", ""]

    L += ["## Limitations",
          "- Few lies per split: wide confidence intervals; use the bootstrap CIs above.",
          "- Labels are self-reported by senders (human data) or by the agents themselves (self-play).",
          "- Sim-to-human gap: self-play tells may not transfer; the human-val transfer check guards against regression only.",
          "- Reviewer bias: the reviewer is an LLM; its agreement with human labels should be reported alongside.",
          "- Deception skills developed by players stay in the sandbox and are not exported."]
    out = run_dir / "results.md"
    out.write_text("\n".join(L) + "\n")
    return out
