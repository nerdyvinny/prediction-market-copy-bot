# The roster

The bot copies the wallets listed under `roster:` in
`pmbot/config/leaders.yaml`. That list is maintained by hand. There is no
automatic discovery, ranking, vetting, or rotation in the live loop.

## Why it works this way

The first design picked leaders automatically: crawl the feeds, filter on win
rate and profit concentration, rank, vet against a copy-backtest, follow the
top 8, redo it every 24 hours. Thirty-four days of paper trading
(2026-07-27..08-30, 423 fills, 115 closed round-trips) measured what that
machinery was predicting, and the answer was nothing:

| Question | Answer |
| --- | --- |
| Does a leader's trailing ROI in our own fills predict their next trade? | corr = **-0.018** (n=70) |
| Next-trade ROI after a POSITIVE record with us | **-11.3%** (n=30) |
| Next-trade ROI after a NEGATIVE record with us | **-9.6%** (n=40) |
| Did the funnel's picks beat copying every candidate? | No (p = 0.717) |
| Leaders with enough tape to split in half | 3 of 3 decayed first-half to second-half |

It also churned: 33 wallets held a seat across 37 rescores, 14 lasted two
rescores or fewer, and 11 never produced a single trade. The median leader
contributed **2** closed round-trips. Nothing ever accumulated enough evidence
to be judged, and the daily rebuild guaranteed it never would.

For context on how little the month could have proven either way: per-round ROI
has a standard deviation of 58.8%, so at n=115 the smallest effect detectable
at 80% power is **+/-15.4pp per trade**. Proving a 5pp edge needs roughly 1,084
closed rounds -- about 321 days at the current rate. Treat any bucket, leader,
or filter that looks good over a month as unproven.

## Changing who we copy

Adding a wallet:

1. `pmbot leaders` -- ranks candidates and prints the funnel report. Nothing it
   prints is followed. `[R]` marks wallets already on your roster.
2. `pmbot leaders --vet` -- additionally replays each candidate as *we* would
   have copied it (our sizing, caps, price band). Slow.
3. `pmbot backtest --leaders 0x...` -- a closer look at one wallet.
4. Add the address under `roster:` in `pmbot/config/leaders.yaml`.
5. Restart: `sudo systemctl restart pmbot-paper`.

Removing a wallet: delete the line and restart. Positions you already hold from
that wallet become **exit-only** -- their SELLs are still mirrored so the
position stays managed, their BUYs are ignored. Nothing is stranded.

The roster takes effect at startup only. There is no background re-read, on
purpose: a static roster that changes itself is not a static roster.

## What to watch for

- **Do not add wallets quickly.** Newly-added leaders were where the first
  month's losses came from: the two added on 08-21 and 08-22 accounted for
  -$33.35 of that epoch's -$57.25.
- **A month of a wallet's history is not evidence.** See the power numbers
  above. `pmbot leaders --vet` shows you a wallet's copyable record; it does
  not show you whether that record will persist, and the measured answer so
  far is that it does not.
- **There is no minimum lineup size.** An empty roster trades nothing. That is
  correct behaviour -- the old funnel's habit of scrambling to refill seats is
  what put unproven wallets into the lineup.
