# PFT-7 Scanner — GitHub Free

Runs an experimental Bitget USDT Futures scanner with GitHub Actions.

## Cost
For a PUBLIC GitHub repository, standard GitHub-hosted runners are free.
Scheduled workflows support a shortest interval of 5 minutes.

## What it scans
- H1 and 15m futures candles
- Open Interest history
- Current funding rate
- Futures active buy/sell volume

No Bitget account API key is required in this alert-only version.

## Alert logic
- 0–5/7: no email
- 6/7: WATCH email once for that signal
- 7/7: CONFIRMED email once for that signal
- Same signal is de-duplicated using a small state file saved in GitHub Actions cache.

## Setup on Android

1. Create a new PUBLIC GitHub repository, for example `pft7-scanner`.
2. Upload these files while preserving `.github/workflows/pft7.yml`.
3. Open repository > Settings > Secrets and variables > Actions.
4. Add repository secrets:

Required for Gmail:
- `SMTP_USER` = your Gmail address
- `SMTP_APP_PASSWORD` = Google App Password (NOT your normal Gmail password)
- `ALERT_TO` = email that should receive alerts

Optional:
- `SMTP_HOST` = `smtp.gmail.com`
- `SMTP_PORT` = `465`

If the optional secrets are absent, the Python defaults are Gmail + port 465.

5. Open Actions > PFT-7 Scanner > Run workflow.
6. Check the log. If successful, the cron schedule will keep running.

## Important GitHub behavior
- Scheduled workflows run only from the default branch.
- GitHub may delay scheduled jobs during high load; 5-minute cron is not a real-time guarantee.
- In a public repository, scheduled workflows can be disabled after 60 days with no repository activity.

## Security
- Do not put your Gmail password, App Password, exchange API secret, or private key into source files.
- Put credentials only in GitHub Actions Secrets.
- This version does not place trades.

## Trading warning
PFT-7 is experimental research. A 6/7 or 7/7 alert is not proof that a trade will be profitable.
Do not assume a 20% daily account return is achievable or reliable.
