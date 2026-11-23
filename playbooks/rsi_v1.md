# Playbook `rsi-v1` (2e42460d478e)

Refined live by **Claude Opus 5.5** (the judge) from human-v2, after reviewing 7 finished self-play games on the GCP run (update 7, 2026-11-23). It was accepted by the gate on human train games 7–8 with 99% bootstrap confidence, judged by Inkling 2 Small:

| | v0 = human-v2 | **v1** |
|---|---|---|
| Gate AUPRC | 0.107 | **0.142** |
| Lie-F1 at 0.5 | 0.072 | **0.152** |
| Lie-F1 at tuned cutoff | 0.130 | **0.177** |

## Changes vs human-v2
- **added H21:** Before judging a stated move, check it against the map. Does the province exist, and can the named unit reach it? Never call a move impossible without checking adjacency. A move that names a nonexistent province or is illegal is unreliable as a commitment. Lower your confidence that it will be executed, but do not treat it as proof of malice.
- **added H22:** Always give reasoning tied to the message content. If you cannot identify any concrete signal, default to the base rate, about 5-10%, rather than a neutral 0.5. An unexplained 0.5 is miscalibrated for ordinary diplomatic messages.
- **added H23:** Questions, requests for clarification and demands that you state your plans contain no claim and cannot be lies. Keep p_lie at the base rate for them, however insistent or repeated they are. Commit-first heuristics apply only when the sender offers something conditional on you acting first, not when they merely ask what you will do.

## Full playbook
H1. Keep baseline p_lie low (about 5-10%) for ordinary messages. Raise it only when two or more concrete signals coincide, such as sender motive, an unverifiable commitment, a recent deviation, a big SC lead, or a request that you act first. Go above 0.4 only with such a combination.
H2. When a leading power (roughly 8+ SCs or near solo) disavows solo ambitions, pushes a draw, insists on loyalty, offers a smaller ally guaranteed centres or generous supports, or asks you to delay a gain, raise p_lie sharply. Check what the offer actually costs the sender.
H3. Do not flag small talk, greetings, jokes, logistics, scheduling, real-life absences, venting, apologies or post-turn reactions as lies, even in tense contexts. Keep them near the base rate unless the content makes a checkable claim that conflicts with the board.
H4. Treat unverifiable third-party intel as suspect, especially when paired with a request to ally, reposition or attack a rival. Examples are claims that a power plots against you, is going solo or heads elsewhere, mirrored warnings, hearsay and copy-paste coalition calls. Verify with the named party or the board.
H5. Lower p_lie when a sender volunteers a move that looks hostile to you, admits an error or weakness, or gives concrete move mechanics or board-checkable reasons that expose their own risk. Disclosures against the sender's interest are usually honest.
H6. When asked about contacts with a third power, a minimizing or evasive answer deserves a moderate rise in p_lie. Examples are haven't heard anything, barely talking, only non-aggression, or haven't responded yet. Stay at or below about 0.2 without other signals.
H7. Check promises and past orders against the public order history. A sender whose recent orders contradicted earlier promises to you deserves a higher p_lie on new promises. Judge them by their next verifiable move, not by their words.
H8. When advice framed as being for your benefit, or a suggested plan, would pull your units off a shared border or leave your supply centre uncovered, check who gains. Also check whether a promised move would expose the sender's own centres. Raise p_lie if mainly the sender benefits.
H9. Raise p_lie for sweeping loyalty speeches, absolute denials (never, no way) and long emotional pleas about honesty or past sacrifice in reply to an accusation, especially from a stronger sender. Also raise it for smears that call the accuser a liar.
H10. Keep p_lie low for hedged or noncommittal replies (maybe, can't help yet), honest refusals, frankly stated self-interest and candid admissions of passivity. Vagueness alone is not deception and is weak evidence either way. Only evasion of a direct question about contacts or allegiances is mildly suspicious.
H11. Treat cheap assent as weak evidence and raise p_lie slightly when it comes after you pressed, after a conflict, or when it costs the sender tempo. Examples are terse consider it done, I'm in, or an instant full order list given in reply to your own plan or demanded reassurance.
H12. Keep p_lie low for a short confirmation of a support you requested, or for specific orders consistent with prior coordination, when they fit both sides' board incentives. Discount it only if it came after repeated pressure or costs the sender heavily.
H13. When a hostile move is explained after the fact and the explanation ends with we're still good or keep going, raise p_lie moderately. Examples are a clerical error, stale orders, a precautionary build, or only passing through. Judge by where the unit can go next turn.
H14. Do not treat a misorder apology as a lie by default. If the sender offers a concrete fix next turn, keep p_lie low. Raise it only if the excuse comes with no remedy, an effusive apology, or a sudden plan switch.
H15. Raise p_lie for deferred or conditional reciprocity that requires you to commit first, such as support me now and I'll support you later or I'll support if you move X. Raise it especially when paired with flattery or an offer to help you attack a third party.
H16. A sender who proposes betraying an exposed partner, asks you to keep the alliance secret, or boasts of feeding a third party fake plans is willing to double-deal. Raise p_lie on their promises to you accordingly.
H17. Keep p_lie low for early proposals that match standard openings or both sides' current interests. Raise it slightly for eager all-in alliance pitches, grand flattery, and DMZ promises that cannot be verified until moves resolve. Then check the actual orders.
H18. Before going above about 30% p_lie, require a concrete mismatch between words and position, or between words and earlier claims. Strict conditions, distrust of a third party and cautious bargaining are not such mismatches.
H19. When a sender who just stabbed you, or is clearly stronger, quickly accepts your limits or assures you that you are safe, raise p_lie. Treat it as placation until their orders confirm it.
H20. Focus suspicion on claims about the sender's own future moves, third-party contacts and motives. Questions, tactical analysis and board facts the sender gains nothing from falsifying should stay near the base rate. Warm tone is not evidence either way.
H21. Before judging a stated move, check it against the map. Does the province exist, and can the named unit reach it? Never call a move impossible without checking adjacency. A move that names a nonexistent province or is illegal is unreliable as a commitment. Lower your confidence that it will be executed, but do not treat it as proof of malice.
H22. Always give reasoning tied to the message content. If you cannot identify any concrete signal, default to the base rate, about 5-10%, rather than a neutral 0.5. An unexplained 0.5 is miscalibrated for ordinary diplomatic messages.
H23. Questions, requests for clarification and demands that you state your plans contain no claim and cannot be lies. Keep p_lie at the base rate for them, however insistent or repeated they are. Commit-first heuristics apply only when the sender offers something conditional on you acting first, not when they merely ask what you will do.
