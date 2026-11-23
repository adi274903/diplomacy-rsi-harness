"""Prompt rendering, laid out for maximum KV prefix-cache reuse on the engine.

Every prompt is ordered from most-shared to least-shared text:

    [system]  genome: system prompt + playbook + exemplars     shared by every call of a league member
    [user]    public board for this phase                        shared by all 7 powers in the phase
              private view: identity, own dialogs, memory        unique per power
              task block                                         unique per call

so SGLang's radix cache / vLLM's prefix cache only prefill the private tail of most calls.
Nothing in this module may read private data of a power other than the one being rendered for;
the runner additionally scans every rendered prompt with the leak guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rsi import decision_graph
from rsi.genome import Genome


def system_message(genome: Genome) -> dict:
    parts = [genome.system_prompt, "\n## Trust-assessment playbook\n" + genome.render_playbook()]
    if genome.exemplars:
        parts.append("\n## Worked examples\n" + "\n\n".join(genome.exemplars))
    return {"role": "system", "content": "\n".join(parts)}


# ---- views (built by the runner from public + own-private state only) ---------------------------

@dataclass
class ShownMessage:
    id: int
    phase: str
    sender: str
    recipient: str
    text: str


@dataclass
class PlayerView:
    power: str
    units: list[str]
    centers: list[str]
    memory: list[str]
    dialogs: dict[str, list[ShownMessage]]          # counterpart -> messages between power and counterpart
    possible_orders: dict[str, list[str]] = field(default_factory=dict)

    def shown_messages(self) -> list[ShownMessage]:
        return [m for msgs in self.dialogs.values() for m in msgs]


def board_block(phase: str, units: dict[str, list[str]], centers: dict[str, list[str]],
                last_orders: dict[str, list[str]], last_phase: str | None) -> str:
    lines = [f"## Public board — phase {phase}", "Supply centres (count: list):"]
    for p in sorted(centers):
        lines.append(f"- {p}: {len(centers[p])}: {' '.join(sorted(centers[p])) or '-'}")
    lines.append("Units:")
    for p in sorted(units):
        lines.append(f"- {p}: {', '.join(sorted(units[p])) or '-'}")
    if last_orders:
        lines.append(f"Orders revealed after {last_phase}:")
        for p in sorted(last_orders):
            lines.append(f"- {p}: {'; '.join(last_orders[p]) or '(none)'}")
    return "\n".join(lines)


def private_block(view: PlayerView) -> str:
    lines = [f"## You are {view.power}",
             f"Your units: {', '.join(view.units) or 'none'}",
             f"Your supply centres: {', '.join(view.centers) or 'none'}"]
    if view.memory:
        lines.append("Your private memory notes (oldest first):")
        lines += [f"- {m}" for m in view.memory]
    if any(view.dialogs.values()):
        lines.append("Your private conversations (oldest first):")
        for other in sorted(view.dialogs):
            msgs = view.dialogs[other]
            if not msgs:
                continue
            lines.append(f"### with {other}")
            for m in msgs:
                who = "you" if m.sender == view.power else m.sender
                lines.append(f"[#{m.id} {m.phase}] {who}: {m.text}")
    return "\n".join(lines)


def send_task(recipients: list[str], max_messages: int, round_no: int, rounds: int) -> str:
    return (f"## Task: SEND (press round {round_no} of {rounds})\n"
            f"You may send up to {max_messages} messages, to any of: {', '.join(recipients)}. "
            "Sending none is fine. Give each message its honest private intent label, state the orders you "
            "currently intend (plan), and one memory note.")


def judge_task(incoming: list[ShownMessage], structured: bool = False) -> str:
    lines = ["## Task: JUDGE", "Judge each of these newly received messages (one judgment per msg_id):"]
    lines += [f"[#{m.id}] from {m.sender}: {m.text}" for m in incoming]
    if structured:
        lines += ["", decision_graph.INSTRUCTIONS]
    return "\n".join(lines)


def orders_task(possible: dict[str, list[str]]) -> str:
    lines = ["## Task: ORDERS", "Choose at most one order per location, copied exactly from these legal orders:"]
    for loc in sorted(possible):
        lines.append(f"- {loc}: {' | '.join(possible[loc])}")
    lines.append("Any location you leave out will hold (or be handled by default). Add one memory note.")
    return "\n".join(lines)


def player_messages(genome: Genome, board: str, private: str, task: str) -> list[dict]:
    return [system_message(genome), {"role": "user", "content": f"{board}\n\n{private}\n\n{task}"}]


# ---- single-item judging (gate replays + human data) -------------------------------------------

def item_messages(genome: Genome, item) -> list[dict]:
    """Receiver's-seat judgment of one message given only what the receiver could see then."""
    ctx = [f"## Context — {item.season} {item.year}"]
    if item.board:
        ctx.append(item.board)
    else:
        ctx.append("(Full board not available; supply-centre counts below.)")
    if item.sender_sc is not None and item.receiver_sc is not None:
        ctx.append(f"Supply centres: you ({item.receiver}) {item.receiver_sc}, {item.sender} {item.sender_sc}.")
    ctx.append(f"\n## Your conversation with {item.sender} (oldest first)")
    structured = genome.structured_judgments
    if item.history:
        ctx += [f"{f'[h{k}] ' if structured else ''}{'you' if s == item.receiver else s}: {t}"
                for k, (s, t) in enumerate(item.history, 1)]
    else:
        ctx.append("(no earlier messages)")
    task = (f"\n## Task: JUDGE\nYou are {item.receiver}. Judge this newest message from {item.sender}:\n"
            f"{item.target}")
    if structured:
        task += "\n\n" + decision_graph.INSTRUCTIONS
    return [system_message(genome), {"role": "user", "content": "\n".join(ctx) + task}]
