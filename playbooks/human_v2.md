# Playbook `human-v2` (898ce6edace4)

Learned by **anthropic/claude-opus-5.5** from labelled human Diplomacy games (Peskov et al. 2020), games [1, 2, 3, 5, 6, 11]. Held out and linted against: test games 4 and 12, and gate games [7, 8, 9, 10].

84 lessons from 14 batches were merged into 20 heuristics (0 dropped by lint).

## Catching lies
2. When a leading power (roughly 8+ SCs or near solo) disavows solo ambitions, pushes a draw, insists on loyalty, offers a smaller ally guaranteed centres or generous supports, or asks you to delay a gain, raise p_lie sharply. Check what the offer actually costs the sender.  
   _support 30: Most strongly supported lie pattern: leaders lull the partners they depend on._
4. Treat unverifiable third-party intel as suspect, especially when paired with a request to ally, reposition or attack a rival. Examples are claims that a power plots against you, is going solo or heads elsewhere, mirrored warnings, hearsay and copy-paste coalition calls. Verify with the named party or the board.  
   _support 35: Cheap, convenient wedge claims recur across many lessons._
6. When asked about contacts with a third power, a minimizing or evasive answer deserves a moderate rise in p_lie. Examples are haven't heard anything, barely talking, only non-aggression, or haven't responded yet. Stay at or below about 0.2 without other signals.  
   _support 25: Denials of side channels are the most common small lie but are weak evidence alone._
7. Check promises and past orders against the public order history. A sender whose recent orders contradicted earlier promises to you deserves a higher p_lie on new promises. Judge them by their next verifiable move, not by their words.  
   _support 6: Kept from the playbook; track records are concrete evidence and are reinforced by several lessons._
8. When advice framed as being for your benefit, or a suggested plan, would pull your units off a shared border or leave your supply centre uncovered, check who gains. Also check whether a promised move would expose the sender's own centres. Raise p_lie if mainly the sender benefits.  
   _support 18: Merges the playbook's exposure and border heuristics with lessons on self-serving advice._
9. Raise p_lie for sweeping loyalty speeches, absolute denials (never, no way) and long emotional pleas about honesty or past sacrifice in reply to an accusation, especially from a stronger sender. Also raise it for smears that call the accuser a liar.  
   _support 20: Indignant self-portraits replace verifiable commitments and often cover stabs._
11. Treat cheap assent as weak evidence and raise p_lie slightly when it comes after you pressed, after a conflict, or when it costs the sender tempo. Examples are terse consider it done, I'm in, or an instant full order list given in reply to your own plan or demanded reassurance.  
   _support 30: Many lessons note that demanded or effortless assurances cost nothing to give._
13. When a hostile move is explained after the fact and the explanation ends with we're still good or keep going, raise p_lie moderately. Examples are a clerical error, stale orders, a precautionary build, or only passing through. Judge by where the unit can go next turn.  
   _support 20: After-the-fact excuses paired with reassurance were a recurring cover for stabs._
15. Raise p_lie for deferred or conditional reciprocity that requires you to commit first, such as support me now and I'll support you later or I'll support if you move X. Raise it especially when paired with flattery or an offer to help you attack a third party.  
   _support 14: Deferred payment is the cheapest promise to break._
16. A sender who proposes betraying an exposed partner, asks you to keep the alliance secret, or boasts of feeding a third party fake plans is willing to double-deal. Raise p_lie on their promises to you accordingly.  
   _support 6: Demonstrated double talk with others predicts double talk with you._
17. Keep p_lie low for early proposals that match standard openings or both sides' current interests. Raise it slightly for eager all-in alliance pitches, grand flattery, and DMZ promises that cannot be verified until moves resolve. Then check the actual orders.  
   _support 14: Opening flattery is a lie cluster, but suspicion of conventional plans caused many false alarms._
19. When a sender who just stabbed you, or is clearly stronger, quickly accepts your limits or assures you that you are safe, raise p_lie. Treat it as placation until their orders confirm it.  
   _support 4: Quick acceptance after betrayal was often placation._

## Avoiding false alarms
3. Do not flag small talk, greetings, jokes, logistics, scheduling, real-life absences, venting, apologies or post-turn reactions as lies, even in tense contexts. Keep them near the base rate unless the content makes a checkable claim that conflicts with the board.  
   _support 40: Social and logistical messages produced many false alarms and are overwhelmingly truthful._
5. Lower p_lie when a sender volunteers a move that looks hostile to you, admits an error or weakness, or gives concrete move mechanics or board-checkable reasons that expose their own risk. Disclosures against the sender's interest are usually honest.  
   _support 35: Self-damaging disclosures and specific mechanics were consistently truthful._
10. Keep p_lie low for hedged or noncommittal replies (maybe, can't help yet), honest refusals, frankly stated self-interest and candid admissions of passivity. Vagueness alone is not deception and is weak evidence either way. Only evasion of a direct question about contacts or allegiances is mildly suspicious.  
   _support 25: Reconciles the playbook's vagueness heuristic with lessons that hedging is usually honest._
12. Keep p_lie low for a short confirmation of a support you requested, or for specific orders consistent with prior coordination, when they fit both sides' board incentives. Discount it only if it came after repeated pressure or costs the sender heavily.  
   _support 16: Balances the terse-assent lesson; matching confirmations were usually true._
14. Do not treat a misorder apology as a lie by default. If the sender offers a concrete fix next turn, keep p_lie low. Raise it only if the excuse comes with no remedy, an effusive apology, or a sudden plan switch.  
   _support 9: Prevents over-applying the excuse heuristic; many such apologies were sincere._

## Calibration
1. Keep baseline p_lie low (about 5-10%) for ordinary messages. Raise it only when two or more concrete signals coincide, such as sender motive, an unverifiable commitment, a recent deviation, a big SC lead, or a request that you act first. Go above 0.4 only with such a combination.  
   _support 10: Lies are rare and humans over-suspect; many lessons stress a low base rate and requiring combined signals._
18. Before going above about 30% p_lie, require a concrete mismatch between words and position, or between words and earlier claims. Strict conditions, distrust of a third party and cautious bargaining are not such mismatches.  
   _support 6: Humans over-suspect cautious but honest bargaining._
20. Focus suspicion on claims about the sender's own future moves, third-party contacts and motives. Questions, tactical analysis and board facts the sender gains nothing from falsifying should stay near the base rate. Warm tone is not evidence either way.  
   _support 18: Directs scrutiny to message types where lies occur and neutralises tone._

## Consolidation notes
Merged 84 lessons and 6 playbook heuristics into 20. The playbook's exposure (H1) and border (H4) heuristics were folded into the self-serving-advice heuristic. Its vagueness heuristic (H3) was reconciled with the hedged-reply lessons. Its base-rate heuristic (H5) became the lead calibration rule. Its late-game incentive heuristic (H6) was absorbed into the leading-power heuristic. Conflicts were resolved with conditions: terse assent versus matching confirmations, excuse lessons versus misorder apologies, and warm tone treated as neutral evidence.
