# Playbook `human-v1` (a7a4d7f1fa93)

Learned by **anthropic/claude-opus-5.5** from labelled human Diplomacy games (Peskov et al. 2020, train + validation splits). Test split held out and linted against.

84 lessons from 14 batches were merged into 18 heuristics (0 dropped by lint).

## Catching lies
2. When a power well ahead on centres (about 8-10+) answers distrust with long loyalty appeals, lists of past favours, claims never to have lied, or offended sincerity, raise p_lie sharply. Treat solo denials and casual draw assent from such a leader the same way, and judge it by its orders near you.  
   _support 12: This was the most supported lie pattern. Leaders gain most from lulling allies, and sincerity theatre costs them nothing. Absorbs the old late-game incentive heuristic._
4. Treat a sender's report of what a third party said, plans or agreed as unverified, and raise p_lie moderately when it steers your units, your alliances, or excuses the sender's own move. Unusually detailed intel you cannot check is not evidence of honesty, so verify it with the third party.  
   _support 9: Third-party gossip and relayed agreements were a frequent vehicle for deception._
5. When a sender hands you an exact order set, tactical advice, or a support pledge tied to you moving or weakening a front, check whom it benefits. Raise p_lie if it leaves your centres or a shared border uncovered, brings their units adjacent to you, or is framed as a feint toward you.  
   _support 9: Merges the old exposure and border-scrutiny heuristics with many move-script lessons._
7. Check promises against the public order history. A sender whose past orders contradicted promises to you deserves a higher p_lie, especially when they now pair a technical excuse or apology with a fresh promise or a rebuild-trust appeal.  
   _support 7: Combines the order-history heuristic with post-betrayal excuse-plus-promise lessons._
9. After a stab, discount apologies, self-blame, out-of-game excuses and claims of hidden motives for sparing you. Contrition usually aims to slow your counterattack, so judge the sender only by whether their units withdraw.  
   _support 5: Post-stab contrition was frequently manipulative._
11. When a sender moves or builds a unit next to your centre and pre-emptively says it targets someone else, or soothes you with an after-the-fact excuse, raise p_lie. Demand a verifiable act, such as a support order or a move away next turn.  
   _support 4: Pre-explained threatening positioning often preceded stabs._
12. Treat bare one-line assent (yes, done, consider it done, sure I can) to a detailed plan you cannot verify, especially one that exposes your unit, as unverified rather than a commitment. Keep p_lie slightly above base and hedge the critical square.  
   _support 5: Cheap assent costs nothing. The heuristic is limited to unverifiable plans that put you at risk, so ordinary acknowledgements are not flagged._
13. Raise p_lie on unusually generous offers, deals where you act first and they reciprocate later, and conditional future help. This includes desperate weak powers offering centres or warning of outside threats. Ask for a simultaneous or verifiable first step.  
   _support 6: Deferred or lopsided favours are cheap cover for deception._
14. Raise p_lie modestly when a message is mainly affection, loyalty or honesty assertion (I've always been honest, if I lied I'd act differently) with no new checkable move. Discount sincerity claims entirely as evidence of trust.  
   _support 4: Meta-honesty claims and pure warmth carry no evidential value and often accompanied deceit._
15. Raise p_lie only slightly for vague deferrals, feigned ignorance, or denied contact with a third party, especially about a unit that borders you. Such denials occur both honestly and dishonestly, so seek specifics and rely on board evidence.  
   _support 6: Concealing plans is a common cheap lie, but the signal is weak, so the heuristic sets a modest bump._
17. Raise p_lie moderately on early-game messages that combine high warmth or anti-third-party bonding with noncommittal substance. Do the same for invitations into a new coalition or long-term trio and for requests that you attack or bounce a third power for the sender.  
   _support 5: Recruitment pitches and bonding with no concrete content were frequent early deception vehicles._
18. Distrust a short message that abruptly reverses a firm demand with no stated reason, such as suddenly conceding a contested centre after you resist. Check what the sender gains if you relax your defence.  
   _support 1: Sudden unexplained concessions were a lesson with limited support but a distinct pattern._

## Avoiding false alarms
3. Do not flag short reactive replies, greetings, banter, sign-offs, questions back to you, logistics or plain acknowledgements as lies. They carry no exploitable commitment, so keep p_lie near the base rate even when the tone feels evasive or terse.  
   _support 9: This was the most common false-alarm category across many games._
6. Lower p_lie when the sender argues openly from board geography, admits self-interest or weakness, rejects your plan for a reasoned alternative, or gives contingent, hedged plans. Anger, bluntness, fear, and open admissions of hostility are also usually truthful.  
   _support 10: Candid reasoning and hostile honesty were repeatedly misread as deception._
8. Give concrete, checkable order confirmations, tactical corrections and supports that serve an agreed shared goal a low p_lie. Next turn's adjudication will expose them, so lying in them is costly.  
   _support 7: Specific, verifiable statements were usually honest. Replaces the old vague-versus-specific heuristic._
10. Do not doubt explanations of already-resolved mistakes or past aggressive moves by default. Keep p_lie low when the explanation fits the visible board and the stated new aim serves the sender's own interest.  
   _support 5: These post-hoc explanations were mostly true. The heuristic separates them from excuses that come bundled with new asks._

## Calibration
1. Start every message near a 5-10% p_lie base rate, since most messages are truthful. Move above about 30% only when several concrete signals stack: an unverifiable claim, a big ask that benefits the sender, a size advantage, a prior breach, or reassurance paired with a request.  
   _support 9: Lies are rare. Stacking signals avoids over-flagging while still catching high-risk messages. Merges the old base-rate heuristic with many calibration lessons._
16. Scale suspicion by stakes. Early-game greetings, information sharing and alliance proposals with concrete opening orders are mostly true and deserve at most a mild bump. Late-game messages from big powers to smaller partners they could absorb deserve markedly higher p_lie.  
   _support 7: Game phase and the power imbalance strongly shift lie rates._

## Consolidation notes
Merged 84 candidate lessons and 6 existing heuristics into 18 heuristics: 11 catch_lies, 5 avoid_false_alarms and 2 calibration. Old H1 and H4 are folded into the order-script heuristic. H2 is merged with post-betrayal excuses. H3 is replaced by the verifiable-specifics heuristic. H5 is now the base-rate heuristic. H6 is folded into the dominant-leader heuristic. Two conflicts were resolved. Cheap assent is flagged only for unverifiable plans that expose you. Excuses are flagged only when they contradict a promise and come with a new ask; other explanations of resolved mistakes stay low.
