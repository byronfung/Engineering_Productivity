from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


DEFAULT_DATA = Path("data/posthog_contributors_90d.json")
BOT_RE = re.compile(
    r"(\[bot\]$|-bot$|bot@|noreply\.github\.com.*bot|^github-actions$|"
    r"^copilot-pull-request-reviewer$|^chatgpt-codex-connector$|^cursor$|"
    r"-app(s)?$|-security$|^scheduled-actions-|^deployment-status-|"
    r"^tests-|^posthog-js-upgrader$|^posthog-local-dev$|^posthog$|"
    r"^force-merge-|^assign-reviewers-)",
    re.I,
)
COMPONENTS = {
    "Commits Authored": ("commits_authored", 1.0),
    "PRs Merged": ("prs_merged", 1.0),
    "Lines Changed": ("lines_changed", 1.0),
    "Reviews": ("reviews", 1.0),
    "Approvals": ("approvals", 1.0),
    "Comments": ("comments", 1.0),
    "Change Requests": ("change_requests", 1.0),
    "Reverts": ("revert_like_commits", -1.0),
}
NORMALIZED_LABELS = {
    "Commits Authored": "Normalized Commits",
    "PRs Merged": "Normalized PRs",
    "Lines Changed": "Normalized Lines",
    "Reviews": "Normalized Reviews",
    "Approvals": "Normalized Approvals",
    "Comments": "Normalized Comments",
    "Change Requests": "Normalized Change Requests",
    "Reverts": "Normalized Reverts",
}


st.set_page_config(page_title="PostHog Engineering Impact", layout="wide")


@st.cache_data(show_spinner=False)
def load_data(path: str) -> tuple[dict, pd.DataFrame]:
    payload = json.loads(Path(path).read_text())
    df = pd.DataFrame(payload["contributors"])
    if df.empty:
        return payload, df
    for _, (column, _) in COMPONENTS.items():
        if column not in df:
            df[column] = 0
    return payload, df


def score_frame(df: pd.DataFrame, weights: dict[str, float]) -> tuple[pd.DataFrame, list[str], list[str]]:
    scored = df.copy()
    scored["Name"] = (
        scored.get("display_name", pd.Series(index=scored.index, dtype=object))
        .replace("", pd.NA)
        .fillna(scored.get("login", pd.Series(index=scored.index, dtype=object)).replace("", pd.NA))
        .fillna(scored["contributor"])
    )
    score_columns: list[str] = []
    normalized_columns: list[str] = []
    for label, (column, _) in COMPONENTS.items():
        score_column = f"{label} score"
        normalized_column = NORMALIZED_LABELS[label]
        values = scored[column].fillna(0)
        maximum = values.max()
        scored[normalized_column] = values / maximum if maximum > 0 else 0.0
        scored[score_column] = scored[normalized_column] * weights[label]
        normalized_columns.append(normalized_column)
        score_columns.append(score_column)
    scored["Impact score"] = scored[score_columns].sum(axis=1)
    return scored.sort_values("Impact score", ascending=False), score_columns, normalized_columns


def is_bot_account(row: pd.Series) -> bool:
    stored_is_bot = row.get("is_bot", False)
    if isinstance(stored_is_bot, str):
        if stored_is_bot.lower() == "true":
            return True
    elif pd.notna(stored_is_bot) and bool(stored_is_bot):
        return True
    values = [
        row.get("contributor", ""),
        row.get("display_name", ""),
        row.get("login", ""),
        row.get("email", ""),
    ]
    return any(BOT_RE.search(str(value)) for value in values if pd.notna(value))


st.title("PostHog Engineering Impact")
data_path = DEFAULT_DATA

payload, raw_df = load_data(data_path)
meta = payload["metadata"]
bot_mask = raw_df.apply(is_bot_account, axis=1) if not raw_df.empty else pd.Series(dtype=bool)

top_controls = st.container()
with top_controls:
    c1, c2, c3, c4, c5, c6 = st.columns([1.8, 1, 1, 1, 1, 1.2])
    remove_bots = c6.toggle("Remove bots", value=True)
    visible_contributors = len(raw_df) - int(bot_mask.sum()) if remove_bots else len(raw_df)
    c1.metric("Window", f"{meta['start_date']} to {meta['end_date']}", f"{meta['days']} days")
    c2.metric("Commits", f"{meta['commit_count']:,}")
    c3.metric("Merged PRs", f"{meta['merged_pr_count']:,}")
    c4.metric("Contributors", f"{visible_contributors:,}")
    c5.metric("Bots filtered", f"{int(bot_mask.sum()) if remove_bots else 0:,}")

st.divider()

st.subheader("Customizable Score Weights")
weight_cols = st.columns(4)
weights = {}
for idx, (label, (_, default)) in enumerate(COMPONENTS.items()):
    with weight_cols[idx % 4]:
        weights[label] = st.number_input(label, value=float(default), step=0.25, format="%.2f")

df = raw_df.copy()
if remove_bots and not df.empty:
    df = df[~bot_mask].copy()

if df.empty:
    st.warning("No contributor data available for the selected filters.")
    st.stop()

scored, score_columns, normalized_columns = score_frame(df, weights)
top = scored.head(5)

chart_data = top[["Name", "Impact score", *score_columns]].melt(
    id_vars=["Name", "Impact score"],
    value_vars=score_columns,
    var_name="Component",
    value_name="Weighted contribution",
)
chart_data["Component"] = chart_data["Component"].str.replace(" score", "", regex=False)
chart_data["Name"] = pd.Categorical(
    chart_data["Name"],
    categories=list(top["Name"]),
    ordered=True,
)

fig = px.bar(
    chart_data,
    y="Name",
    x="Weighted contribution",
    color="Component",
    orientation="h",
    height=360,
    labels={"Name": "", "Weighted contribution": "Weighted Score"},
    color_discrete_sequence=px.colors.qualitative.Safe,
)
negative_extent = top[score_columns].where(top[score_columns] < 0, 0).sum(axis=1).min()
positive_extent = top[score_columns].where(top[score_columns] > 0, 0).sum(axis=1).max()
score_labels = [f"{row['Name']}  {row['Impact score']:.3f}" for _, row in top.iterrows()]
fig.update_layout(
    barmode="relative",
    legend_title_text="",
    margin=dict(l=112, r=8, t=8, b=8),
    xaxis={
        "range": [
            negative_extent * 1.1 if negative_extent < 0 else 0,
            positive_extent * 1.15 if positive_extent > 0 else 1,
        ]
    },
    yaxis={
        "categoryorder": "array",
        "categoryarray": list(reversed(top["Name"].tolist())),
        "tickmode": "array",
        "tickvals": top["Name"].tolist(),
        "ticktext": score_labels,
    },
)
st.subheader("Top 5 Engineers")
st.plotly_chart(fig, use_container_width=True)

detail_cols = [
    "contributor",
    "Impact score",
    "commits_authored",
    "prs_merged",
    "lines_changed",
    "reviews",
    "approvals",
    "comments",
    "change_requests",
    "revert_like_commits",
    *normalized_columns,
]
detail_header_col, detail_toggle_col = st.columns([3, 1])
with detail_header_col:
    st.subheader("Cohort Detail")
with detail_toggle_col:
    show_full_cohort = st.toggle("Show full cohort", value=False)
detail = scored[detail_cols].rename(
    columns={
        "contributor": "Contributor",
        "Impact score": "Impact Score",
        "commits_authored": "Commits",
        "prs_merged": "PRs",
        "lines_changed": "Lines",
        "reviews": "Reviews",
        "approvals": "Approvals",
        "comments": "Comments",
        "change_requests": "Change Requests",
        "revert_like_commits": "Reverts",
    }
)
visible_detail = detail if show_full_cohort else detail.head(5)
table_height = min(1200, max(260, 38 * (len(visible_detail) + 1)))
st.dataframe(
    visible_detail,
    width="stretch",
    hide_index=True,
    height=table_height,
    column_config={
        "Impact Score": st.column_config.NumberColumn(format="%.3f"),
        **{column: st.column_config.NumberColumn(format="%.3f") for column in normalized_columns},
    },
)

st.caption(
    "Scoring logic: each contribution signal is normalized to the highest value in the filtered cohort, then multiplied by the weight above and added into one impact score. "
    "The default model weights each positive signal at 1.00 and revert-like commits at -1.00. "
    "The weights are adjustable, so the chart shows the scoring model you choose rather than a fixed truth."
)
