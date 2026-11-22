"""Private bilateral message bus. A power can only ever read dialogs it is a party to."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from rsi.prompts import ShownMessage


@dataclass
class Message:
    id: int
    phase: str
    season: str
    year: int
    round: int
    sender: str
    recipient: str
    text: str
    sender_sc: int
    recipient_sc: int

    def shown(self) -> ShownMessage:
        return ShownMessage(self.id, self.phase, self.sender, self.recipient, self.text)

    def to_dict(self) -> dict:
        return asdict(self)


class Bus:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def post(self, **kw) -> Message:
        m = Message(id=len(self.messages), **kw)
        self.messages.append(m)
        return m

    def dialog(self, a: str, b: str, before_id: int | None = None) -> list[Message]:
        return [m for m in self.messages
                if {m.sender, m.recipient} == {a, b} and (before_id is None or m.id < before_id)]

    def dialogs_for(self, power: str, window: int) -> dict[str, list[ShownMessage]]:
        out: dict[str, list[ShownMessage]] = {}
        for m in self.messages:
            if power not in (m.sender, m.recipient):
                continue
            other = m.recipient if m.sender == power else m.sender
            out.setdefault(other, []).append(m.shown())
        return {k: v[-window:] for k, v in out.items()}
