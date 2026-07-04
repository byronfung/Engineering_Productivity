# Engineering Impact Dashboard

Streamlit dashboard for measuring recent GitHub engineering contribution signals for `PostHog/posthog`.

Dashboard is published to https://byronfung-engineering-productivity-dashboard-dpcpgk.streamlit.app/

## Quick start

```bash
python3 -m pip install -r requirements.txt
python3 src/fetch_engineering_analytics.py --days 7
streamlit run dashboard.py
```

The extractor reads commit history from `/Users/byron/Documents/GitHub/posthog` and GitHub PR/review/comment metadata from the GitHub GraphQL API. Put `GITHUB_TOKEN=...` in `.env` or export it in the shell.

Fetch the full 90-day window after validating the 7-day dashboard:

```bash
python3 src/fetch_engineering_analytics.py --days 90
```
