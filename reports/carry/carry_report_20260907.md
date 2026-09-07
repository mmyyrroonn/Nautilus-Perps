# Cross-venue funding carry: HL x Lighter x Aster (60 d)

Window: `2026-07-09T14:00:00Z` .. `2026-09-07T13:00:00Z` (1440 UTC hours)
Generated: `2026-09-07T14:16:21+00:00` | hurdle: **6.0 bps** round-trip | symbol table: src/spread_watch.py INSTRUMENTS

Public REST history only: no keys, no orders, no account data. Positive funding means longs pay shorts, so a pair that is long A and short B earns `carry = f_B - f_A` every hour.

## Conclusion

- Ranked **110** ordered venue pairs across **19** symbols (4 more had overlap but never a full 168 h window, listed separately).
- **1** pairs have a positive 7-day net edge after the 6 bps round trip and a p90 basis shock.
- **0** of those are *robust*: the 10th-percentile 7-day carry clears the hurdle, the differential keeps its sign in at least 70% of hours, and the pair has at least 30 days of overlapping history. None. Every positive-edge pair either depends on the good tail of the distribution or rests on too little history to trust.
- Best by net edge: **PONS long HL / short ASTER** at 0.468 bps/h mean (41.0%/y), median 7-day carry 80.4 bps, p10 75.5 bps, net 7-day edge 22.3 bps.
- At its mean rate that pair needs **12.8 hours** (0.5 days) of undisturbed holding just to repay the 6 bps round trip.

## Ranked pairs

Ranked by the explicit score

```
edge_7d_bps = mean_carry_bps_per_hour * 168
            - 6.0                       # round-trip taker cost, both legs
            - p90(|basis[t+168] - basis[t]|)  # bad-case 7 d basis move you unwind into
```

`carry7d` columns are the p10 / p50 / p90 of every fully populated rolling 168 h window; `same%` is the share of hours whose differential keeps the sign of its mean; `run` is the longest unbroken same-sign stretch in hours; `bas.sd` is the stdev of the hourly A/B basis and `bas.7d90` the p90 absolute 7-day basis move. All figures in bps unless noted. A pair only enters this table once it has completed at least one 168 h window, so a freshly listed market cannot be ranked on an extrapolated mean.

| #   | symbol | long    | short   | h    | mean/h | med/h  | same% | run  | carry24h | c7d p10 | c7d p50 | c7d p90 | 7d>hurdle | ann%  | bas.sd | bas.7d90 | edge7d |
|-----|--------|---------|---------|------|--------|--------|-------|------|----------|---------|---------|---------|-----------|-------|--------|----------|--------|
| 1   | PONS   | HL      | ASTER   | 178  | 0.468  | 0.272  | 60    | 16   | 14.2     | 75.5    | 80.4    | 84.1    | 100%      | 41.0  | 28.8   | 50.3     | 22.3   |
| 2   | ZEC    | ASTER   | LIGHTER | 1440 | 0.123  | 0.052  | 62    | 55   | 2.0      | 8.3     | 15.2    | 40.9    | 100%      | 10.8  | 7.0    | 15.1     | -0.4   |
| 3   | ETH    | ASTER   | HL      | 1434 | 0.047  | 0.047  | 70    | 196  | 1.1      | 2.1     | 6.5     | 13.3    | 58%       | 4.1   | 2.8    | 6.2      | -4.3   |
| 4   | ZEC    | ASTER   | HL      | 1440 | 0.109  | 0.035  | 57    | 55   | 1.8      | 2.7     | 11.2    | 46.1    | 70%       | 9.6   | 7.8    | 17.2     | -4.9   |
| 5   | ETH    | ASTER   | LIGHTER | 1434 | 0.037  | 0.037  | 67    | 126  | 0.8      | 0.6     | 4.5     | 12.2    | 39%       | 3.3   | 2.2    | 5.3      | -5.0   |
| 6   | SOL    | ASTER   | LIGHTER | 1434 | 0.036  | 0.017  | 59    | 36   | 0.6      | -0.6    | 5.0     | 15.2    | 39%       | 3.2   | 2.4    | 5.5      | -5.4   |
| 7   | SOL    | ASTER   | HL      | 1434 | 0.034  | 0.019  | 58    | 53   | 0.7      | -1.5    | 5.6     | 11.4    | 46%       | 3.0   | 2.3    | 5.7      | -6.0   |
| 8   | BTC    | LIGHTER | HL      | 1440 | 0.023  | 0.005  | 79    | 136  | 0.7      | -0.8    | 2.1     | 10.6    | 34%       | 2.0   | 2.3    | 4.4      | -6.5   |
| 9   | BTC    | ASTER   | HL      | 1434 | 0.031  | 0.032  | 68    | 160  | 0.8      | -1.3    | 4.5     | 9.5     | 39%       | 2.7   | 2.7    | 6.4      | -7.1   |
| 10  | HYPE   | LIGHTER | HL      | 1440 | 0.038  | 0.005  | 83    | 153  | 0.5      | -2.7    | 7.0     | 12.9    | 55%       | 3.3   | 4.6    | 8.8      | -8.5   |
| 11  | GOLD   | ASTER   | HL      | 1438 | 0.040  | 0.062  | 80    | 246  | 1.5      | -17.6   | 11.3    | 15.0    | 80%       | 3.5   | 4.4    | 9.5      | -8.8   |
| 12  | HYPE   | ASTER   | HL      | 1438 | 0.033  | 0.000  | 34    | 48   | 0.4      | -5.6    | 6.5     | 14.9    | 51%       | 2.9   | 5.6    | 9.4      | -10.0  |
| 13  | SOL    | HL      | LIGHTER | 1440 | 0.002  | -0.005 | 35    | 22   | -0.1     | -6.7    | 1.1     | 7.2     | 12%       | 0.2   | 2.4    | 5.2      | -10.8  |
| 14  | GOLD   | LIGHTER | HL      | 1440 | 0.028  | 0.022  | 84    | 297  | 0.5      | -4.7    | 4.5     | 13.0    | 37%       | 2.5   | 4.9    | 9.6      | -10.9  |
| 15  | GOLD1  | LIGHTER | HL      | 1440 | 0.028  | 0.022  | 84    | 297  | 0.5      | -4.7    | 4.5     | 13.0    | 37%       | 2.5   | 4.9    | 9.6      | -10.9  |
| 16  | ETH    | LIGHTER | HL      | 1440 | 0.009  | 0.005  | 81    | 318  | 0.2      | -2.1    | 1.3     | 5.7     | 9%        | 0.8   | 2.7    | 6.7      | -11.2  |
| 17  | BTC    | ASTER   | LIGHTER | 1434 | 0.008  | 0.008  | 54    | 72   | 0.1      | -5.0    | -0.2    | 6.5     | 13%       | 0.7   | 2.9    | 6.9      | -11.6  |
| 18  | SOL    | LIGHTER | HL      | 1440 | -0.002 | 0.005  | 35    | 22   | 0.1      | -7.2    | -1.1    | 6.7     | 14%       | -0.2  | 2.4    | 5.2      | -11.6  |
| 19  | GOLD   | ASTER   | LIGHTER | 1438 | 0.012  | 0.040  | 78    | 234  | 1.0      | -11.3   | 6.4     | 8.6     | 52%       | 1.0   | 3.8    | 8.6      | -12.6  |
| 20  | HYPE   | LIGHTER | ASTER   | 1438 | 0.005  | 0.005  | 76    | 176  | 0.1      | -7.1    | 2.0     | 8.0     | 17%       | 0.5   | 3.4    | 7.8      | -12.9  |
| 21  | TSLA   | LIGHTER | HL      | 1440 | 0.045  | 0.022  | 92    | 127  | 0.7      | 1.9     | 5.9     | 16.6    | 49%       | 3.9   | 7.6    | 15.2     | -13.6  |
| 22  | ETH    | HL      | LIGHTER | 1440 | -0.009 | -0.005 | 81    | 318  | -0.2     | -5.7    | -1.3    | 2.1     | 0%        | -0.8  | 2.7    | 6.7      | -14.2  |
| 23  | BTC    | LIGHTER | ASTER   | 1434 | -0.008 | -0.008 | 54    | 72   | -0.1     | -6.5    | 0.2     | 5.0     | 2%        | -0.7  | 2.9    | 6.9      | -14.3  |
| 24  | BTC    | HL      | LIGHTER | 1440 | -0.023 | -0.005 | 79    | 136  | -0.7     | -10.6   | -2.1    | 0.8     | 0%        | -2.0  | 2.3    | 4.4      | -14.3  |
| 25  | HYPE   | ASTER   | LIGHTER | 1438 | -0.005 | -0.005 | 76    | 176  | -0.1     | -8.0    | -2.0    | 7.1     | 17%       | -0.5  | 3.4    | 7.8      | -14.6  |
| 26  | NVDA   | LIGHTER | HL      | 1440 | 0.037  | 0.022  | 88    | 123  | 0.5      | 1.1     | 5.8     | 9.2     | 49%       | 3.2   | 7.3    | 15.3     | -15.2  |
| 27  | GOLD   | LIGHTER | ASTER   | 1438 | -0.012 | -0.040 | 78    | 234  | -1.0     | -8.6    | -6.4    | 11.3    | 15%       | -1.0  | 3.8    | 8.6      | -16.6  |
| 28  | SOL    | HL      | ASTER   | 1434 | -0.034 | -0.019 | 58    | 53   | -0.7     | -11.4   | -5.6    | 1.5     | 0%        | -3.0  | 2.3    | 5.7      | -17.4  |
| 29  | ZEC    | HL      | LIGHTER | 1440 | 0.014  | -0.005 | 26    | 28   | 0.1      | -9.5    | 2.7     | 8.3     | 35%       | 1.3   | 6.8    | 13.9     | -17.5  |
| 30  | SOL    | LIGHTER | ASTER   | 1434 | -0.036 | -0.017 | 59    | 36   | -0.6     | -15.2   | -5.0    | 0.6     | 0%        | -3.2  | 2.4    | 5.5      | -17.6  |
| 31  | ETH    | LIGHTER | ASTER   | 1434 | -0.037 | -0.037 | 67    | 126  | -0.8     | -12.2   | -4.5    | -0.6    | 0%        | -3.3  | 2.2    | 5.3      | -17.6  |
| 32  | BTC    | HL      | ASTER   | 1434 | -0.031 | -0.032 | 68    | 160  | -0.8     | -9.5    | -4.5    | 1.3     | 0%        | -2.7  | 2.7    | 6.4      | -17.6  |
| 33  | ETH    | HL      | ASTER   | 1434 | -0.047 | -0.047 | 70    | 196  | -1.1     | -13.3   | -6.5    | -2.1    | 0%        | -4.1  | 2.8    | 6.2      | -20.0  |
| 34  | GOLD   | HL      | LIGHTER | 1440 | -0.028 | -0.022 | 84    | 297  | -0.5     | -13.0   | -4.5    | 4.7     | 5%        | -2.5  | 4.9    | 9.6      | -20.3  |
| 35  | GOLD1  | HL      | LIGHTER | 1440 | -0.028 | -0.022 | 84    | 297  | -0.5     | -13.0   | -4.5    | 4.7     | 5%        | -2.5  | 4.9    | 9.6      | -20.3  |
| 36  | HYPE   | HL      | ASTER   | 1438 | -0.033 | 0.000  | 34    | 48   | -0.4     | -14.9   | -6.5    | 5.6     | 8%        | -2.9  | 5.6    | 9.4      | -20.9  |
| 37  | HYPE   | HL      | LIGHTER | 1440 | -0.038 | -0.005 | 83    | 153  | -0.5     | -12.9   | -7.0    | 2.7     | 0%        | -3.3  | 4.6    | 8.8      | -21.2  |
| 38  | GOLD1  | ASTER   | HL      | 1438 | 0.072  | 0.062  | 89    | 308  | 1.5      | -5.1    | 13.0    | 21.7    | 80%       | 6.3   | 16.3   | 27.4     | -21.3  |
| 39  | MU     | LIGHTER | HL      | 1440 | 0.006  | 0.022  | 76    | 92   | 0.6      | -8.2    | 1.6     | 6.4     | 12%       | 0.5   | 7.3    | 16.3     | -21.3  |
| 40  | TSLA   | ASTER   | HL      | 1434 | 0.043  | 0.062  | 79    | 120  | 1.3      | -3.9    | 10.3    | 15.4    | 69%       | 3.7   | 10.0   | 22.6     | -21.4  |
| 41  | GOLD   | HL      | ASTER   | 1438 | -0.040 | -0.062 | 80    | 246  | -1.5     | -15.0   | -11.3   | 17.6    | 14%       | -3.5  | 4.4    | 9.5      | -22.3  |
| 42  | ZEC    | LIGHTER | HL      | 1440 | -0.014 | 0.005  | 26    | 28   | -0.1     | -8.3    | -2.7    | 9.5     | 20%       | -1.3  | 6.8    | 13.9     | -22.3  |
| 43  | SNDK   | HL      | LIGHTER | 1440 | 0.013  | -0.022 | 31    | 27   | -0.3     | -7.3    | 0.6     | 15.6    | 32%       | 1.2   | 8.1    | 18.8     | -22.6  |
| 44  | MU     | HL      | LIGHTER | 1440 | -0.006 | -0.022 | 76    | 92   | -0.6     | -6.4    | -1.6    | 8.2     | 15%       | -0.5  | 7.3    | 16.3     | -23.4  |
| 45  | ASTER  | HL      | ASTER   | 1438 | 0.001  | -0.011 | 47    | 72   | 0.3      | -41.2   | 5.0     | 24.5    | 48%       | 0.1   | 8.0    | 17.9     | -23.6  |
| 46  | ASTER  | ASTER   | HL      | 1438 | -0.001 | 0.011  | 47    | 72   | -0.3     | -24.5   | -5.0    | 41.2    | 22%       | -0.1  | 8.0    | 17.9     | -24.1  |
| 47  | NVDA   | ASTER   | HL      | 1434 | 0.057  | 0.062  | 84    | 123  | 1.5      | 2.6     | 11.5    | 15.8    | 79%       | 5.0   | 11.3   | 27.8     | -24.2  |
| 48  | SPCX   | LIGHTER | HL      | 1440 | 0.008  | 0.022  | 61    | 87   | -0.3     | -17.2   | 3.2     | 22.3    | 46%       | 0.7   | 8.2    | 20.0     | -24.6  |
| 49  | LIT    | HL      | LIGHTER | 1440 | 0.006  | -0.005 | 26    | 19   | 0.3      | -19.8   | 7.4     | 17.1    | 55%       | 0.6   | 9.4    | 19.7     | -24.6  |
| 50  | HOOD   | LIGHTER | HL      | 1440 | 0.037  | 0.022  | 81    | 129  | 1.1      | -7.8    | 8.9     | 26.5    | 59%       | 3.3   | 12.1   | 25.1     | -24.8  |
| 51  | MU     | ASTER   | HL      | 280  | 0.056  | 0.062  | 80    | 63   | 1.5      | 6.1     | 10.2    | 11.9    | 92%       | 4.9   | 13.7   | 28.7     | -25.2  |
| 52  | GOLD1  | ASTER   | LIGHTER | 1438 | 0.044  | 0.040  | 93    | 1150 | 1.0      | -0.3    | 7.7     | 18.2    | 78%       | 3.9   | 16.0   | 27.5     | -26.0  |
| 53  | LIT    | LIGHTER | HL      | 1440 | -0.006 | 0.005  | 26    | 19   | -0.3     | -17.1   | -7.4    | 19.8    | 27%       | -0.6  | 9.4    | 19.7     | -26.8  |
| 54  | SNDK   | LIGHTER | HL      | 1440 | -0.013 | 0.022  | 31    | 27   | 0.3      | -15.6   | -0.6    | 7.3     | 16%       | -1.2  | 8.1    | 18.8     | -27.1  |
| 55  | SPCX   | HL      | LIGHTER | 1440 | -0.008 | -0.022 | 61    | 87   | 0.3      | -22.3   | -3.2    | 17.2    | 40%       | -0.7  | 8.2    | 20.0     | -27.4  |
| 56  | NVDA   | HL      | LIGHTER | 1440 | -0.037 | -0.022 | 88    | 123  | -0.5     | -9.2    | -5.8    | -1.1    | 0%        | -3.2  | 7.3    | 15.3     | -27.5  |
| 57  | NVDA   | ASTER   | LIGHTER | 1434 | 0.020  | 0.040  | 88    | 446  | 1.0      | -1.0    | 4.8     | 8.0     | 48%       | 1.8   | 11.4   | 25.4     | -28.0  |
| 58  | MU     | ASTER   | LIGHTER | 280  | 0.020  | 0.040  | 80    | 79   | 1.0      | 2.1     | 7.9     | 9.6     | 70%       | 1.8   | 13.9   | 25.6     | -28.2  |
| 59  | TSLA   | LIGHTER | ASTER   | 1434 | 0.003  | -0.040 | 23    | 104  | -0.9     | -6.7    | 0.2     | 9.4     | 23%       | 0.3   | 10.5   | 22.8     | -28.3  |
| 60  | LIT    | ASTER   | LIGHTER | 1440 | 0.012  | -0.005 | 21    | 10   | 0.2      | -11.8   | 4.9     | 12.6    | 37%       | 1.0   | 10.8   | 24.4     | -28.4  |
| 61  | TSLA   | HL      | LIGHTER | 1440 | -0.045 | -0.022 | 92    | 127  | -0.7     | -16.6   | -5.9    | -1.9    | 0%        | -3.9  | 7.6    | 15.2     | -28.8  |
| 62  | TSLA   | ASTER   | LIGHTER | 1434 | -0.003 | 0.040  | 23    | 104  | 0.9      | -9.4    | -0.2    | 6.7     | 31%       | -0.3  | 10.5   | 22.8     | -29.3  |
| 63  | LIT    | ASTER   | HL      | 1440 | 0.005  | 0.000  | 17    | 14   | -0.1     | -13.4   | -1.9    | 18.8    | 19%       | 0.5   | 10.2   | 24.5     | -29.6  |
| 64  | LIT    | HL      | ASTER   | 1440 | -0.005 | 0.000  | 17    | 14   | 0.1      | -18.8   | 1.9     | 13.4    | 27%       | -0.5  | 10.2   | 24.5     | -31.4  |
| 65  | PUMP   | LIGHTER | HL      | 1440 | 0.027  | 0.005  | 87    | 370  | 0.1      | -3.1    | 2.0     | 39.3    | 41%       | 2.4   | 13.9   | 30.8     | -32.2  |
| 66  | LIT    | LIGHTER | ASTER   | 1440 | -0.012 | 0.005  | 21    | 10   | -0.2     | -12.6   | -4.9    | 11.8    | 22%       | -1.0  | 10.8   | 24.4     | -32.4  |
| 67  | NVDA   | LIGHTER | ASTER   | 1434 | -0.020 | -0.040 | 88    | 446  | -1.0     | -8.0    | -4.8    | 1.0     | 0%        | -1.8  | 11.4   | 25.4     | -34.8  |
| 68  | MU     | LIGHTER | ASTER   | 280  | -0.020 | -0.040 | 80    | 79   | -1.0     | -9.6    | -7.9    | -2.1    | 0%        | -1.8  | 13.9   | 25.6     | -35.0  |
| 69  | TSLA   | HL      | ASTER   | 1434 | -0.043 | -0.062 | 79    | 120  | -1.3     | -15.4   | -10.3   | 3.9     | 3%        | -3.7  | 10.0   | 22.6     | -35.7  |
| 70  | HOOD   | HL      | LIGHTER | 1440 | -0.037 | -0.022 | 81    | 129  | -1.1     | -26.5   | -8.9    | 7.8     | 11%       | -3.3  | 12.1   | 25.1     | -37.3  |
| 71  | PUMP   | ASTER   | HL      | 1438 | 0.115  | 0.000  | 42    | 93   | 1.3      | -0.5    | 11.5    | 49.0    | 59%       | 10.1  | 22.6   | 51.4     | -38.0  |
| 72  | ARB    | HL      | LIGHTER | 1440 | 0.047  | -0.005 | 44    | 63   | 1.3      | -9.0    | 11.7    | 23.7    | 67%       | 4.1   | 18.5   | 41.1     | -39.3  |
| 73  | GOLD1  | LIGHTER | ASTER   | 1438 | -0.044 | -0.040 | 93    | 1150 | -1.0     | -18.2   | -7.7    | 0.3     | 0%        | -3.9  | 16.0   | 27.5     | -40.9  |
| 74  | PUMP   | HL      | LIGHTER | 1440 | -0.027 | -0.005 | 87    | 370  | -0.1     | -39.3   | -2.0    | 3.1     | 9%        | -2.4  | 13.9   | 30.8     | -41.3  |
| 75  | ZEC    | HL      | ASTER   | 1440 | -0.109 | -0.035 | 57    | 55   | -1.8     | -46.1   | -11.2   | -2.7    | 0%        | -9.6  | 7.8    | 17.2     | -41.5  |
| 76  | ASTER  | LIGHTER | HL      | 1440 | 0.044  | 0.005  | 93    | 470  | 0.1      | -0.8    | 3.4     | 29.1    | 40%       | 3.8   | 19.7   | 43.0     | -41.7  |
| 77  | ZEC    | LIGHTER | ASTER   | 1440 | -0.123 | -0.052 | 62    | 55   | -2.0     | -40.9   | -15.2   | -8.3    | 0%        | -10.8 | 7.0    | 15.1     | -41.8  |
| 78  | NVDA   | HL      | ASTER   | 1434 | -0.057 | -0.062 | 84    | 123  | -1.5     | -15.8   | -11.5   | -2.6    | 0%        | -5.0  | 11.3   | 27.8     | -43.4  |
| 79  | ASTER  | LIGHTER | ASTER   | 1438 | 0.045  | -0.004 | 49    | 92   | 0.4      | -13.9   | 9.0     | 28.3    | 55%       | 3.9   | 20.8   | 45.3     | -43.7  |
| 80  | MU     | HL      | ASTER   | 280  | -0.056 | -0.062 | 80    | 63   | -1.5     | -11.9   | -10.2   | -6.1    | 0%        | -4.9  | 13.7   | 28.7     | -44.1  |
| 81  | PUMP   | ASTER   | LIGHTER | 1438 | 0.089  | -0.005 | 39    | 80   | 0.9      | -0.5    | 7.5     | 33.9    | 61%       | 7.8   | 23.0   | 53.3     | -44.3  |
| 82  | GOLD1  | HL      | ASTER   | 1438 | -0.072 | -0.062 | 89    | 308  | -1.5     | -21.7   | -13.0   | 5.1     | 9%        | -6.3  | 16.3   | 27.4     | -45.6  |
| 83  | ARB    | LIGHTER | HL      | 1440 | -0.047 | 0.005  | 44    | 63   | -1.3     | -23.7   | -11.7   | 9.0     | 18%       | -4.1  | 18.5   | 41.1     | -54.9  |
| 84  | ASTER  | HL      | LIGHTER | 1440 | -0.044 | -0.005 | 93    | 470  | -0.1     | -29.1   | -3.4    | 0.8     | 0%        | -3.8  | 19.7   | 43.0     | -56.4  |
| 85  | ARB    | HL      | ASTER   | 1434 | 0.073  | 0.000  | 47    | 62   | 1.6      | 6.5     | 13.9    | 25.1    | 92%       | 6.4   | 29.2   | 64.0     | -57.8  |
| 86  | ASTER  | ASTER   | LIGHTER | 1438 | -0.045 | 0.004  | 49    | 92   | -0.4     | -28.3   | -9.0    | 13.9    | 14%       | -3.9  | 20.8   | 45.3     | -58.9  |
| 87  | SNDK   | ASTER   | HL      | 428  | 0.075  | 0.062  | 73    | 38   | 1.3      | 9.4     | 10.3    | 12.6    | 100%      | 6.5   | 29.7   | 68.2     | -61.7  |
| 88  | SPCX   | ASTER   | LIGHTER | 428  | 0.085  | 0.040  | 100   | 428  | 1.1      | 9.7     | 18.7    | 22.8    | 100%      | 7.5   | 29.3   | 74.5     | -66.2  |
| 89  | SNDK   | ASTER   | LIGHTER | 428  | 0.042  | 0.040  | 78    | 55   | 0.8      | 0.8     | 6.7     | 11.7    | 68%       | 3.7   | 29.6   | 69.1     | -68.0  |
| 90  | SPCX   | ASTER   | HL      | 428  | 0.025  | 0.056  | 62    | 44   | 0.6      | -5.4    | 8.5     | 14.8    | 57%       | 2.2   | 28.5   | 69.0     | -70.8  |
| 91  | ARB    | LIGHTER | ASTER   | 1434 | 0.026  | 0.005  | 88    | 208  | 0.1      | -3.1    | 2.4     | 19.0    | 33%       | 2.3   | 31.3   | 71.3     | -72.9  |
| 92  | DASH   | ASTER   | HL      | 1434 | 0.105  | 0.109  | 90    | 299  | 2.7      | 13.5    | 18.4    | 26.3    | 100%      | 9.2   | 38.1   | 85.8     | -74.1  |
| 93  | PUMP   | LIGHTER | ASTER   | 1438 | -0.089 | 0.005  | 39    | 80   | -0.9     | -33.9   | -7.5    | 0.5     | 0%        | -7.8  | 23.0   | 53.3     | -74.3  |
| 94  | PUMP   | HL      | ASTER   | 1438 | -0.115 | 0.000  | 42    | 93   | -1.3     | -49.0   | -11.5   | 0.5     | 0%        | -10.1 | 22.6   | 51.4     | -76.8  |
| 95  | SPCX   | HL      | ASTER   | 428  | -0.025 | -0.056 | 62    | 44   | -0.6     | -14.8   | -8.5    | 5.4     | 5%        | -2.2  | 28.5   | 69.0     | -79.3  |
| 96  | ARB    | ASTER   | LIGHTER | 1434 | -0.026 | -0.005 | 88    | 208  | -0.1     | -19.0   | -2.4    | 3.1     | 0%        | -2.3  | 31.3   | 71.3     | -81.6  |
| 97  | SNDK   | LIGHTER | ASTER   | 428  | -0.042 | -0.040 | 78    | 55   | -0.8     | -11.7   | -6.7    | -0.8    | 0%        | -3.7  | 29.6   | 69.1     | -82.2  |
| 98  | ARB    | ASTER   | HL      | 1434 | -0.073 | 0.000  | 47    | 62   | -1.6     | -25.1   | -13.9   | -6.5    | 2%        | -6.4  | 29.2   | 64.0     | -82.2  |
| 99  | SNDK   | HL      | ASTER   | 428  | -0.075 | -0.062 | 73    | 38   | -1.3     | -12.6   | -10.3   | -9.4    | 0%        | -6.5  | 29.7   | 68.2     | -86.8  |
| 100 | SPCX   | LIGHTER | ASTER   | 428  | -0.085 | -0.040 | 100   | 428  | -1.1     | -22.8   | -18.7   | -9.7    | 0%        | -7.5  | 29.3   | 74.5     | -94.9  |
| 101 | DASH   | HL      | ASTER   | 1434 | -0.105 | -0.109 | 90    | 299  | -2.7     | -26.3   | -18.4   | -13.5   | 0%        | -9.2  | 38.1   | 85.8     | -109.4 |
| 102 | HOOD   | ASTER   | HL      | 1434 | 0.133  | 0.070  | 92    | 132  | 3.0      | 10.0    | 21.8    | 35.2    | 100%      | 11.6  | 54.4   | 132.2    | -115.9 |
| 103 | HOOD   | ASTER   | LIGHTER | 1434 | 0.094  | 0.040  | 97    | 626  | 1.0      | 5.7     | 7.6     | 38.0    | 75%       | 8.2   | 53.6   | 129.2    | -119.4 |
| 104 | DASH   | LIGHTER | HL      | 1440 | 0.037  | 0.005  | 93    | 243  | 0.2      | -2.2    | 2.8     | 33.2    | 26%       | 3.3   | 53.5   | 125.3    | -125.1 |
| 105 | DASH   | ASTER   | LIGHTER | 1434 | 0.068  | 0.104  | 82    | 189  | 2.5      | -13.1   | 15.5    | 19.3    | 81%       | 5.9   | 56.8   | 134.6    | -129.2 |
| 106 | PONS   | ASTER   | HL      | 178  | -0.468 | -0.272 | 60    | 16   | -14.2    | -84.1   | -80.4   | -75.5   | 0%        | -41.0 | 28.8   | 50.3     | -135.0 |
| 107 | DASH   | HL      | LIGHTER | 1440 | -0.037 | -0.005 | 93    | 243  | -0.2     | -33.2   | -2.8    | 2.2     | 3%        | -3.3  | 53.5   | 125.3    | -137.6 |
| 108 | HOOD   | LIGHTER | ASTER   | 1434 | -0.094 | -0.040 | 97    | 626  | -1.0     | -38.0   | -7.6    | -5.7    | 0%        | -8.2  | 53.6   | 129.2    | -150.9 |
| 109 | DASH   | LIGHTER | ASTER   | 1434 | -0.068 | -0.104 | 82    | 189  | -2.5     | -19.3   | -15.5   | 13.1    | 13%       | -5.9  | 56.8   | 134.6    | -152.0 |
| 110 | HOOD   | HL      | ASTER   | 1434 | -0.133 | -0.070 | 92    | 132  | -3.0     | -35.2   | -21.8   | -10.0   | 0%        | -11.6 | 54.4   | 132.2    | -160.5 |

### Not ranked: no complete 168 h window

Recently listed on at least one venue. `edge7d` here is `mean * 168` extrapolated from a shorter sample and must not be compared with the table above.

| #   | symbol | long    | short   | h   | mean/h | med/h  | same% | run | carry24h | c7d p10 | c7d p50 | c7d p90 | 7d>hurdle | ann%  | bas.sd | bas.7d90 | edge7d |
|-----|--------|---------|---------|-----|--------|--------|-------|-----|----------|---------|---------|---------|-----------|-------|--------|----------|--------|
| -   | PONS   | LIGHTER | ASTER   | 116 | 0.943  | 0.348  | 68    | 21  | 24.9     | -       | -       | -       | -         | 82.6  | 37.1   | -        | 152.4  |
| -   | PONS   | HL      | LIGHTER | 116 | 0.063  | -0.005 | 49    | 12  | 2.4      | -       | -       | -       | -         | 5.5   | 33.6   | -        | 4.6    |
| -   | PONS   | LIGHTER | HL      | 116 | -0.063 | 0.005  | 49    | 12  | -2.4     | -       | -       | -       | -         | -5.5  | 33.6   | -        | -16.6  |
| -   | PONS   | ASTER   | LIGHTER | 116 | -0.943 | -0.348 | 68    | 21  | -24.9    | -       | -       | -       | -         | -82.6 | 37.1   | -        | -164.4 |

## Mean funding by venue (bps/h, positive = longs pay)

| symbol | HL mean | LIGHTER mean | ASTER mean | HL h | LIGHTER h | ASTER h |
|--------|---------|--------------|------------|------|-----------|---------|
| ARB    | 0.042   | 0.088        | 0.114      | 1440 | 1440      | 1434    |
| ASTER  | 0.150   | 0.107        | 0.152      | 1440 | 1440      | 1438    |
| BTC    | 0.100   | 0.077        | 0.069      | 1440 | 1440      | 1434    |
| DASH   | 0.123   | 0.086        | 0.018      | 1440 | 1440      | 1434    |
| ETH    | 0.104   | 0.095        | 0.058      | 1440 | 1440      | 1434    |
| GOLD   | 0.089   | 0.061        | 0.049      | 1440 | 1440      | 1438    |
| GOLD1  | 0.089   | 0.061        | 0.016      | 1440 | 1440      | 1438    |
| HOOD   | 0.133   | 0.096        | 0.003      | 1440 | 1440      | 1434    |
| HYPE   | 0.119   | 0.081        | 0.087      | 1440 | 1440      | 1438    |
| LIT    | 0.132   | 0.138        | 0.127      | 1440 | 1440      | 1440    |
| MU     | 0.097   | 0.091        | 0.012      | 1440 | 1440      | 280     |
| NVDA   | 0.078   | 0.041        | 0.021      | 1440 | 1440      | 1434    |
| PONS   | 0.823   | 0.710        | 0.129      | 178  | 116       | 318     |
| PUMP   | 0.169   | 0.142        | 0.052      | 1440 | 1440      | 1438    |
| SNDK   | 0.098   | 0.111        | 0.007      | 1440 | 1440      | 428     |
| SOL    | 0.079   | 0.081        | 0.045      | 1440 | 1440      | 1434    |
| SPCX   | 0.023   | 0.015        | -0.045     | 1440 | 1440      | 428     |
| TSLA   | 0.084   | 0.039        | 0.043      | 1440 | 1440      | 1434    |
| ZEC    | 0.166   | 0.181        | 0.057      | 1440 | 1440      | 1440    |

## Data caveats

**Funding units.** Each venue reports funding in a different unit; every number above is normalised to bps per hour.

| venue   | endpoint                               | raw unit                            | -> bps/h                     |
|---------|----------------------------------------|-------------------------------------|------------------------------|
| HL      | `POST /info {"type":"fundingHistory"}` | fraction / hour                     | `rate * 1e4`                 |
| LIGHTER | `GET /api/v1/fundings`                 | percent / hour, sign in `direction` | `+/-rate * 1e2`              |
| ASTER   | `GET /fapi/v1/fundingRate`             | fraction / settlement interval      | `rate * 1e4 / intervalHours` |

The Lighter unit is the one the public docs do not state. Two independent checks pin it to percent: (1) the sibling `value` field in the same row equals `price * rate / 100` (BTC row `rate=0.0010`, `value=0.79433800`, hourly close 79283.2 -> 79283.2 * 0.0010 / 100 = 0.7928, matching to ~0.2%; a fraction reading would be 100x off); (2) the Nautilus Lighter adapter reads this same endpoint and signs `rate` by `direction` with no division by 100, which is the convention `src/analysis/opportunities.py` already documents.

**Aster settlement intervals used** (from `GET /fapi/v1/fundingInfo`, fetched once and cached; symbols absent there default to 8 h):

`ARBUSDT=8h, ASTERUSDT=4h, BTCUSDT=8h, DASHUSDT=8h, ETHUSDT=8h, HOODUSDT=8h, HYPEUSDT=4h, LITUSDT=1h, MUUSD1=8h, NVDAUSDT=8h, PONSUSDT=1h, PUMPUSDT=4h, SNDKUSD1=8h, SOLUSDT=8h, SPCXUSD1=8h, TSLAUSDT=8h, XAUUSD1=4h, XAUUSDT=4h, ZECUSDT=1h`

A 4 h or 8 h Aster rate is spread evenly backwards over the hours it accrued over, so it lines up with HL's and Lighter's hourly settlement. That makes the hourly series smooth by construction: Aster's true cashflow is lumpy, and a position closed mid-interval collects none of it.

**Prices.** The basis uses 1 h candle closes, LAST TRADE on all three venues (HL and Lighter publish no mark-price candles, so trade prices are the only consistent choice):

- `HL`: candleSnapshot 1h close (last trade)
- `LIGHTER`: /api/v1/candles 1h close (last trade)
- `ASTER`: /fapi/v1/klines 1h close (last trade)

Aster's stock perps trade only a few times an hour, so their closes are stale relative to HL and Lighter and the basis series for those pairs is noisier than the real executable spread. Aster does expose `/fapi/v1/markPriceKlines`; it was not used because the other two venues have no equivalent.

**Coverage.** Hours with no funding row on one leg are dropped from that pair, and rolling 24 h / 7 d windows are computed only over fully populated stretches. The window is 1440 h; a leg that returned materially less than that was listed later than the window start (or trades only part of the day), which is a fact about the market, not a fetch failure. Legs below 90% coverage:

| symbol | venue   | funding h | coverage | candle h |
|--------|---------|-----------|----------|----------|
| MU     | ASTER   | 280       | 19%      | 195      |
| PONS   | HL      | 178       | 12%      | 180      |
| PONS   | LIGHTER | 116       | 8%       | 116      |
| PONS   | ASTER   | 318       | 22%      | 309      |
| SNDK   | ASTER   | 428       | 30%      | 428      |
| SPCX   | ASTER   | 428       | 30%      | 428      |

Per-symbol notes and failures:

- none: every mapped symbol returned funding and candles on every venue.

`GOLD` and `GOLD1` share the same HL and Lighter legs and differ only in the Aster listing (`XAUUSDT` vs `XAUUSD1`), so their HL/Lighter rows are identical by construction and must not be counted as two independent observations.

**What this study does not measure.** Depth and slippage (see `src/analysis/opportunities.py` for the live-book capacity work), margin and liquidation mechanics on either leg, funding-rate caps, borrow, or the risk that a HIP-3 stock perp halts while its Aster or Lighter twin keeps trading. Trading-hours differences on the stock names mean the basis series contains hours where one venue's price is simply not moving.

_Raw responses cached under `reports/carry/raw/`; delete that directory or pass `--refresh` to refetch._
