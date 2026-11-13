| Metric | baseline (3 runs) | two_stage (3 runs) | agents-graph (3 runs) |
| --- | --- | --- | --- |
| Recall on suspicious, all | 88% (88%-88%) | 90% (88%-91%) | 94% (91%-97%) |
| Recall, shallow cases | 97% (96%-100%) | 99% (96%-100%) | 95% (92%-96%) |
| Recall, deep cases | 58% (50%-62%) | 62% (50%-75%) | 92% (88%-100%) |
| Recall, model-decided only | 78% (76%-79%) | 74% (67%-85%) | 77% (67%-88%) |
| False escalations, all benign | 48% (45%-52%) | 51% (48%-55%) | 67% (64%-73%) |
| False escalations, shallow | 35% (32%-40%) | 37% (36%-40%) | 57% (52%-64%) |
| False escalations, deep | 92% (88%-100%) | 92% (88%-100%) | 96% (88%-100%) |
| Failures (became escalate) | 15% (15%-15%) | 18% (14%-24%) | 25% (23%-27%) |
| Typology accuracy | 83% (80%-87%) | 87% (84%-89%) | 94% (92%-96%) |
| Citation validity | 99% (98%-100%) | 99% (98%-100%) | 85% (81%-88%) |
| Latency p50 | 5.6 s (5.4-5.8) | 7.2 s (6.6-7.5) | 30.6 s (30.2-30.9) |
| Tokens per case | 13,541 | 34,559 | 60,990 |
| Cost per case | $0.0025 | $0.0064 | $0.0115 |
