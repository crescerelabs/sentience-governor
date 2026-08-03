"""Governance report -- executive PDF over a synthesized workflow session.

Generates a polished governance report styled for institutional
audiences (CIOs, CISOs, compliance leaders, deans, enterprise
stakeholders). The report frames AI agent behavior as something
that can be reviewed, audited, and held accountable -- not a
developer metrics dashboard.

The trace is synthesized by driving the Sentience MCP wrapper against
a fake backend. Every event, every violation, every advisory flag is
emitted by the same engine that runs in production. The narrative on
top is institutional; the data underneath is real.

Run:

    python examples/v023_business_report.py

Outputs:

    /tmp/v023-business-report-trace.jsonl   (the trace)
    /tmp/v023-business-report.md            (markdown for review)
    /tmp/v023-business-report.pdf           (the report)

Dependencies (pip-installable, not part of the governor wheel):

    pip install reportlab matplotlib pillow
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.schema.events import ClassificationSource
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter
from sentience_governor.wrapper.mcp import (
    ClassificationHint,
    SentienceMCPAdapter,
    wrap_mcp_client,
)


# =====================================================================
# DESIGN SYSTEM
# =====================================================================

NAVY = "#0B1A33"
SLATE_DEEP = "#2D3748"
SLATE = "#4A5568"
SLATE_LIGHT = "#718096"
INK_BORDER = "#E2E8F0"
INK_GRID = "#EDF2F7"
PAPER = "#F7FAFC"
PAPER_DEEPER = "#EDF2F7"
INDIGO = "#3C366B"
TEAL = "#2C7A7B"
TEAL_BG = "#E6FFFA"
AMBER = "#B7791F"
AMBER_BG = "#FFFBEB"
RED = "#9B2C2C"
RED_BG = "#FEF2F2"

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.titleweight": "semibold",
    "axes.titlecolor": NAVY,
    "axes.labelcolor": SLATE,
    "axes.edgecolor": INK_BORDER,
    "axes.linewidth": 0.5,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.color": SLATE_LIGHT,
    "ytick.color": SLATE_LIGHT,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": INK_GRID,
    "grid.linewidth": 0.5,
    "grid.linestyle": "-",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "legend.frameon": False,
    "legend.fontsize": 9,
})


# =====================================================================
# HUMAN LABELS + SEVERITY for codes -- the institutional layer.
# =====================================================================

# Severity levels used for findings
SEVERITY_HIGH = "HIGH"
SEVERITY_MODERATE = "MODERATE"
SEVERITY_LOW = "LOW"

SEVERITY_COLORS = {
    SEVERITY_HIGH: RED,
    SEVERITY_MODERATE: AMBER,
    SEVERITY_LOW: SLATE_LIGHT,
}

POLICY_RULES = {
    "POL-001": {
        "label": "Action outside declared scope or intent",
        "severity": SEVERITY_HIGH,
        "consequence": (
            "In enforcement mode, this action would have been blocked before "
            "execution. Repeated occurrences indicate behavioral drift."
        ),
    },
    "POL-002": {
        "label": "Unregistered agent",
        "severity": SEVERITY_HIGH,
        "consequence": (
            "The session would have been blocked at the control plane. "
            "Unregistered agents cannot access institutional tools."
        ),
    },
    "POL-003": {
        "label": "Unclassified context surface",
        "severity": SEVERITY_MODERATE,
        "consequence": (
            "Downstream actions requiring classified context would have been "
            "restricted pending classification."
        ),
    },
    "POL-004": {
        "label": "Persistent change without classification or retention",
        "severity": SEVERITY_HIGH,
        "consequence": (
            "The write would have been blocked. State changes require both "
            "classification and an explicit retention policy."
        ),
    },
    "POL-005": {
        "label": "Sensitivity escalation without authorization",
        "severity": SEVERITY_HIGH,
        "consequence": (
            "A boundary check would have been triggered and the session "
            "paused for authorization review."
        ),
    },
}

ADVISORY_FLAGS = {
    "AGENT_UNREGISTERED": {
        "label": "Tool call before agent registration",
        "severity": SEVERITY_HIGH,
    },
    "INTENT_MISSING": {
        "label": "Tool call before intent declaration",
        "severity": SEVERITY_MODERATE,
    },
    "SCOPE_OPERATION_UNEXPECTED": {
        "label": "Unexpected operation type for declared scope",
        "severity": SEVERITY_MODERATE,
    },
    "SCOPE_INTENT_MISMATCH": {
        "label": "Access outside declared scope",
        "severity": SEVERITY_MODERATE,
    },
    "CONTEXT_UNCLASSIFIED": {
        "label": "Unclassified read-side data",
        "severity": SEVERITY_LOW,
    },
    "SENSITIVITY_ESCALATION": {
        "label": "Increasing data sensitivity over session",
        "severity": SEVERITY_MODERATE,
    },
    "MEMORY_WRITE_UNCLASSIFIED": {
        "label": "Persistent change without classification",
        "severity": SEVERITY_HIGH,
    },
    "MEMORY_WRITE_CANDIDATE": {
        "label": "Candidate state mutation",
        "severity": SEVERITY_LOW,
    },
}

SYSTEM_LABELS = {
    "crm": "CRM",
    "slack": "Slack",
    "postgres": "Postgres",
    "web": "Web",
}


# =====================================================================
# SYNTHESIZED SESSION
# =====================================================================


class FakeBackend:
    def call_tool(self, tool_name: str, args: dict):
        if tool_name.startswith("crm."):
            if "write" in tool_name:
                return {"ok": True, "id": f"S-{uuid.uuid4().hex[:6]}"}
            return {"ok": True, "rows": [{"id": args.get("id", "X"), "name": "Account"}]}
        if tool_name.startswith("slack."):
            return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
        if tool_name.startswith("postgres."):
            return {"ok": True, "rows": [{"col": "value"}]}
        return {"ok": True}


TURN_PROFILES = [
    ("turn-1", 1820, 410, 800, 0, [
        ("crm.get_customer", {"id": "C-123"}, "read"),
    ]),
    ("turn-2", 4812, 932, 1200, 240, [
        ("crm.get_customer", {"id": "C-123"}, "read"),
        ("crm.list_invoices", {"customer_id": "C-123"}, "read"),
        ("crm.write_snapshot", {"id": "C-123", "q": "2026-Q1"}, "write_classified"),
    ]),
    ("turn-3", 3104, 712, 950, 0, [
        ("crm.list_invoices", {"customer_id": "C-456"}, "read"),
        ("postgres.query", {"sql": "SELECT * FROM ledger LIMIT 10"}, "read"),
    ]),
    ("turn-4", 9420, 2180, 2400, 480, [
        ("crm.get_customer", {"id": "C-789"}, "read"),
        ("crm.list_invoices", {"customer_id": "C-789"}, "read"),
        ("crm.write_snapshot", {"id": "C-789", "q": "2026-Q1"}, "write_unclassified"),
        ("slack.write_message", {"text": "Quarterly report ready for review"}, "write_classified"),
    ]),
    ("turn-5", 2240, 540, 700, 0, [
        ("crm.get_customer", {"id": "C-999"}, "read"),
        ("postgres.query", {"sql": "SELECT * FROM audit"}, "read"),
    ]),
]


def make_hint_factory(turn_label: str, tokens: dict, turn_id: str):
    def hook(tool_name: str, args: dict, result):
        common = dict(llm_turn_id=turn_id, **tokens)
        target = tool_name.split(".")[0]
        if "write" in tool_name:
            mode = next(
                (m for tn, _a, m in next(p[5] for p in TURN_PROFILES if p[0] == turn_label) if tn == tool_name),
                "write_unclassified",
            )
            if mode == "write_classified":
                return ClassificationHint(
                    data_classifications=[f"{target}_confidential"],
                    classification_source=ClassificationSource.vendor,
                    provenance=[f"{target}.api"],
                    retention_flags=[f"{target}_data_30d"],
                    write_classification=f"{target}_confidential",
                    retention_requested="30d",
                    **common,
                )
            return ClassificationHint(
                data_classifications=[],
                classification_source=ClassificationSource.vendor,
                provenance=[f"{target}.api"],
                retention_flags=[],
                write_classification=None,
                retention_requested=None,
                **common,
            )
        return ClassificationHint(
            data_classifications=[f"{target}_confidential"],
            classification_source=ClassificationSource.vendor,
            provenance=[f"{target}.api"],
            retention_flags=[f"{target}_data_30d"],
            **common,
        )
    return hook


async def synthesize_trace(sink_path: Path) -> None:
    sm = SessionManager()
    cache = InProcessCache()
    sink = SinkWriter(FileSink(str(sink_path)))

    adapted = SentienceMCPAdapter(
        delegate=FakeBackend(),
        call_fn=lambda c, n, a: c.call_tool(n, a),
    )

    wrapped = wrap_mcp_client(
        target=adapted,
        session_manager=sm,
        cache=cache,
        sink_writer=sink,
        agent_id="institutional-reporting-workflow",
        agent_version="1.0",
        vendor_id="institutional-research",
        declared_capabilities=["crm.read", "crm.write", "slack.write"],
        owner_claim="operations-team",
        stated_objective="Quarterly institutional reporting workflow",
        classification_hook=lambda *a, **kw: None,
    )

    async with wrapped:
        for turn_label, prompt, completion, cached_r, cached_w, tools in TURN_PROFILES:
            turn_id = uuid.uuid4().hex[:12]
            tokens = dict(
                llm_prompt_tokens=prompt,
                llm_completion_tokens=completion,
                llm_cached_read_tokens=cached_r,
                llm_cached_write_tokens=cached_w,
                model_identifier="claude-sonnet-4-5",
                provider="anthropic",
            )
            wrapped._proxy._classification_hook = make_hint_factory(
                turn_label, tokens, turn_id
            )
            for tool_name, args, _mode in tools:
                wrapped.send_tool_call(tool_name, args)


# =====================================================================
# AGGREGATION
# =====================================================================


def load_events(sink_path: Path) -> list[dict]:
    return [json.loads(line) for line in sink_path.read_text().splitlines() if line.strip()]


def compute_stats(events: list[dict]) -> dict:
    session_id = events[0].get("session_id", "?")

    seen = set()
    deduped = defaultdict(int)
    naive = defaultdict(int)
    per_turn = {}

    tool_calls = []
    target_systems_touched = set()
    write_attempts = 0
    write_blocks = 0  # writes that produced a violation
    scope_deviations = 0  # SCOPE_INTENT_MISMATCH advisory occurrences
    out_of_scope_actions = 0  # SCOPE_INTENT_MISMATCH OR POL-001 (deduped per event)
    external_comm_systems = set()

    timeline = []  # list of dicts: {kind, primary, secondary, severity}

    for e in events:
        et = e["event_type"]
        payload = e.get("payload", {})

        if et == "AGENT_REGISTERED":
            timeline.append({
                "kind": "start",
                "primary": "Agent registered",
                "secondary": f"Agent {payload.get('agent_id', '')} v{payload.get('agent_version', '')}",
                "severity": None,
            })
        elif et == "INTENT_DECLARED":
            obj = payload.get("stated_objective") or "(no objective)"
            timeline.append({
                "kind": "start",
                "primary": "Intent declared",
                "secondary": obj,
                "severity": None,
            })
        elif et == "SCOPE_ASSERTED":
            tool_id = payload["tool_id"]
            target = (payload.get("target_system") or tool_id.split(".")[0]).lower()
            tool_calls.append(tool_id)
            target_systems_touched.add(target)
            if target == "slack":
                external_comm_systems.add("Slack")
            op = (payload.get("operation_type") or "").upper()
            if op == "WRITE":
                write_attempts += 1

            sys_label = SYSTEM_LABELS.get(target, target.title())
            method = tool_id.split(".", 1)[1] if "." in tool_id else tool_id
            primary = f"{sys_label} — {method}"

            kind = "action"
            severity = None
            secondary_bits = [op.lower() if op else "access"]
            if e.get("policy_violations"):
                kind = "violation"
                worst = max(
                    (POLICY_RULES.get(v, {}).get("severity", SEVERITY_MODERATE)
                     for v in e["policy_violations"]),
                    key=lambda s: ["LOW", "MODERATE", "HIGH"].index(s),
                )
                severity = worst
                hl = ", ".join(
                    POLICY_RULES.get(v, {}).get("label", v) for v in e["policy_violations"]
                )
                secondary_bits.append(f"would block: {hl.lower()}")
                if op == "WRITE":
                    write_blocks += 1
                # POL-001 also counts as out-of-scope
                if "POL-001" in e["policy_violations"]:
                    out_of_scope_actions += 1
            elif e.get("advisory_flags"):
                kind = "advisory"
                worst = max(
                    (ADVISORY_FLAGS.get(f, {}).get("severity", SEVERITY_MODERATE)
                     for f in e["advisory_flags"]),
                    key=lambda s: ["LOW", "MODERATE", "HIGH"].index(s),
                )
                severity = worst
                if "SCOPE_INTENT_MISMATCH" in e["advisory_flags"]:
                    scope_deviations += 1
                    if not e.get("policy_violations"):
                        out_of_scope_actions += 1
                hl = ", ".join(
                    ADVISORY_FLAGS.get(f, {}).get("label", f).lower()
                    for f in e["advisory_flags"]
                )
                secondary_bits.append(f"flag: {hl}")
            timeline.append({
                "kind": kind,
                "primary": primary,
                "secondary": " — ".join(secondary_bits),
                "severity": severity,
            })

        if "llm_prompt_tokens" in payload:
            prompt = payload.get("llm_prompt_tokens", 0)
            completion = payload.get("llm_completion_tokens", 0) or 0
            cr = payload.get("llm_cached_read_tokens", 0) or 0
            cw = payload.get("llm_cached_write_tokens", 0) or 0
            tid = payload.get("llm_turn_id")

            naive["prompt"] += prompt
            naive["completion"] += completion
            naive["cached_read"] += cr
            naive["cached_write"] += cw

            if tid and tid not in seen:
                seen.add(tid)
                deduped["prompt"] += prompt
                deduped["completion"] += completion
                deduped["cached_read"] += cr
                deduped["cached_write"] += cw
                per_turn[tid] = dict(
                    prompt=prompt, completion=completion,
                    cached_read=cr, cached_write=cw,
                    model=payload.get("model_identifier", "?"),
                )

    timeline.append({
        "kind": "end",
        "primary": "Session closed",
        "secondary": f"{len(tool_calls)} actions across {len(seen)} workflow step(s)",
        "severity": None,
    })

    violation_counts = Counter()
    flag_counts = Counter()
    for e in events:
        for v in e.get("policy_violations", []):
            violation_counts[v] += 1
        for f in e.get("advisory_flags", []):
            flag_counts[f] += 1

    return dict(
        session_id=session_id,
        total_events=len(events),
        total_tool_calls=len(tool_calls),
        total_turns=len(seen),
        per_turn=per_turn,
        deduped=dict(deduped),
        naive=dict(naive),
        violation_counts=dict(violation_counts),
        flag_counts=dict(flag_counts),
        tool_counter=dict(Counter(tool_calls)),
        target_systems_touched=sorted(target_systems_touched),
        write_attempts=write_attempts,
        write_blocks=write_blocks,
        scope_deviations=scope_deviations,
        out_of_scope_actions=out_of_scope_actions,
        external_comm_systems=sorted(external_comm_systems),
        timeline=timeline,
    )


# =====================================================================
# CHARTS
# =====================================================================


CHART_DIR = Path("/tmp/v023-business-report-charts")
CHART_DIR.mkdir(exist_ok=True)


def _style_axes(ax, show_y_grid=True, show_x_grid=False):
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_color(INK_BORDER)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.yaxis.grid(show_y_grid)
    ax.xaxis.grid(show_x_grid)


def _annotate_bars(ax, bars, values, fmt="{:,}", color=NAVY):
    ymax = max(values) if values else 1
    pad = ymax * 0.012
    for b, v in zip(bars, values):
        ax.text(
            b.get_x() + b.get_width() / 2, v + pad, fmt.format(v),
            ha="center", va="bottom", fontsize=10, color=color, weight="semibold",
        )


def chart_naive_vs_deduped(stats: dict) -> Path:
    fig, ax = plt.subplots(figsize=(7.0, 3.0))
    labels = ["Naive aggregation", "Reasoning-turn attribution"]
    values = [stats["naive"]["prompt"], stats["deduped"]["prompt"]]
    bars = ax.bar(labels, values, color=[AMBER, TEAL], width=0.42)
    ax.set_ylabel("Prompt tokens", color=SLATE)
    _style_axes(ax)
    _annotate_bars(ax, bars, values)
    inflation = (values[0] / values[1]) if values[1] else 0
    ax.text(
        0.5, -0.22,
        f"Naive aggregation overstates compute consumption by {inflation:.1f}x.",
        transform=ax.transAxes, ha="center", fontsize=9.5,
        color=SLATE_LIGHT, style="italic",
    )
    plt.tight_layout()
    out = CHART_DIR / "naive_vs_deduped.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def chart_tokens_by_turn(stats: dict) -> Path:
    turns = list(stats["per_turn"].items())
    labels = [f"Step {i+1}" for i, _ in enumerate(turns)]
    prompts = [t["prompt"] for _, t in turns]
    completions = [t["completion"] for _, t in turns]
    cached = [t["cached_read"] + t["cached_write"] for _, t in turns]

    fig, ax = plt.subplots(figsize=(7.0, 3.0))
    ax.bar(labels, prompts, color=NAVY, label="Prompt", width=0.45)
    ax.bar(labels, completions, bottom=prompts, color=INDIGO, label="Completion", width=0.45)
    ax.bar(
        labels, cached,
        bottom=[p + c for p, c in zip(prompts, completions)],
        color=TEAL, label="Cached", width=0.45, alpha=0.9,
    )
    ax.set_ylabel("Tokens", color=SLATE)
    _style_axes(ax)
    ax.legend(loc="upper left")
    plt.tight_layout()
    out = CHART_DIR / "tokens_by_turn.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def chart_tool_frequency(stats: dict) -> Path:
    items = sorted(stats["tool_counter"].items(), key=lambda kv: kv[1])
    labels = [k for k, _ in items]
    counts = [v for _, v in items]
    fig, ax = plt.subplots(figsize=(7.0, 0.42 * len(labels) + 1.0))
    ax.barh(labels, counts, color=NAVY, height=0.5)
    for i, v in enumerate(counts):
        ax.text(v, i, f"  {v}", va="center", fontsize=10, color=NAVY)
    ax.set_xlabel("Calls", color=SLATE)
    _style_axes(ax, show_y_grid=False, show_x_grid=True)
    plt.tight_layout()
    out = CHART_DIR / "tool_frequency.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def chart_governance_findings(stats: dict) -> Path | None:
    """Findings chart -- color by severity."""
    items = []
    for k, v in stats["violation_counts"].items():
        meta = POLICY_RULES.get(k, {})
        items.append((meta.get("label", k), v, SEVERITY_COLORS[meta.get("severity", SEVERITY_MODERATE)]))
    for k, v in stats["flag_counts"].items():
        meta = ADVISORY_FLAGS.get(k, {})
        items.append((meta.get("label", k), v, SEVERITY_COLORS[meta.get("severity", SEVERITY_MODERATE)]))
    if not items:
        return None
    items.sort(key=lambda x: x[1])
    labels = [x[0] for x in items]
    counts = [x[1] for x in items]
    barcolors = [x[2] for x in items]
    fig, ax = plt.subplots(figsize=(7.0, 0.5 * len(labels) + 1.0))
    ax.barh(labels, counts, color=barcolors, height=0.5)
    for i, v in enumerate(counts):
        ax.text(v, i, f"  {v}", va="center", fontsize=10, color=NAVY)
    ax.set_xlabel("Occurrences", color=SLATE)
    _style_axes(ax, show_y_grid=False, show_x_grid=True)
    plt.tight_layout()
    out = CHART_DIR / "governance_findings.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def chart_governance_timeline(stats: dict) -> Path:
    """Hero timeline -- vertical flow with colored dots and connector line."""
    timeline = stats["timeline"]
    n = len(timeline)
    row_h = 0.38
    fig_h = max(row_h * n + 0.4, 3.0)

    fig, ax = plt.subplots(figsize=(7.2, fig_h))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, n)
    ax.invert_yaxis()
    ax.axis("off")

    color_for = {
        "start": TEAL,
        "action": SLATE_LIGHT,
        "advisory": AMBER,
        "violation": RED,
        "end": SLATE_LIGHT,
    }
    label_for = {
        "start": "START",
        "action": "ACTION",
        "advisory": "ADVISORY",
        "violation": "BLOCKED",
        "end": "END",
    }

    # Connector line behind dots
    ax.plot(
        [0.45, 0.45], [0.5, n - 0.5],
        color=INK_BORDER, lw=1.4, zorder=1, solid_capstyle="round",
    )

    for i, item in enumerate(timeline):
        kind = item["kind"]
        col = color_for[kind]
        y = i + 0.5

        # White outline ring (for separation against connector)
        ax.scatter([0.45], [y], s=200, color="white", zorder=2)
        ax.scatter([0.45], [y], s=130, color=col, zorder=3)

        # Marker label
        ax.text(
            0.95, y - 0.16, label_for[kind],
            color=col, fontsize=7.2, weight="bold", va="bottom",
        )
        # Severity tag (right side) for advisory/violation
        sev = item.get("severity")
        if sev:
            sev_color = SEVERITY_COLORS[sev]
            ax.text(
                9.95, y - 0.16, sev,
                color=sev_color, fontsize=7.2, weight="bold",
                va="bottom", ha="right",
            )
        # Primary label
        ax.text(
            0.95, y + 0.07, item["primary"],
            color=NAVY, fontsize=10.5, weight="semibold", va="top",
        )
        # Secondary label
        if item.get("secondary"):
            ax.text(
                0.95, y + 0.32, item["secondary"],
                color=SLATE, fontsize=8.8, va="top",
            )

    plt.tight_layout()
    out = CHART_DIR / "governance_timeline.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


# =====================================================================
# PDF: BUILDING BLOCKS
# =====================================================================


def _styles():
    base = getSampleStyleSheet()
    s = {}
    s["title"] = ParagraphStyle(
        "title", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=24, leading=28, textColor=colors.HexColor(NAVY), spaceAfter=2,
    )
    s["subtitle"] = ParagraphStyle(
        "subtitle", parent=base["BodyText"], fontName="Helvetica",
        fontSize=11, textColor=colors.HexColor(SLATE), spaceAfter=4,
    )
    s["meta"] = ParagraphStyle(
        "meta", parent=base["BodyText"], fontName="Helvetica",
        fontSize=8.5, textColor=colors.HexColor(SLATE_LIGHT), spaceAfter=14,
    )
    s["section"] = ParagraphStyle(
        "section", parent=base["Heading1"], fontName="Helvetica-Bold",
        fontSize=18, leading=22, textColor=colors.HexColor(NAVY),
        spaceBefore=2, spaceAfter=10,
    )
    s["sub"] = ParagraphStyle(
        "sub", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=16, textColor=colors.HexColor(NAVY),
        spaceBefore=10, spaceAfter=6,
    )
    s["body"] = ParagraphStyle(
        "body", parent=base["BodyText"], fontName="Helvetica",
        fontSize=10, leading=15, textColor=colors.HexColor(SLATE_DEEP),
        spaceAfter=8, alignment=TA_LEFT,
    )
    s["small"] = ParagraphStyle(
        "small", parent=base["BodyText"], fontName="Helvetica",
        fontSize=8.5, leading=12, textColor=colors.HexColor(SLATE),
    )
    s["caption"] = ParagraphStyle(
        "caption", parent=base["BodyText"], fontName="Helvetica-Oblique",
        fontSize=8.5, leading=12, textColor=colors.HexColor(SLATE_LIGHT),
        spaceAfter=8,
    )
    s["insight_label"] = ParagraphStyle(
        "insight_label", parent=base["BodyText"], fontName="Helvetica-Bold",
        fontSize=8.5, leading=11, textColor=colors.HexColor(NAVY),
        spaceAfter=2,
    )
    s["insight_body"] = ParagraphStyle(
        "insight_body", parent=base["BodyText"], fontName="Helvetica",
        fontSize=10, leading=14, textColor=colors.HexColor(SLATE_DEEP),
    )
    s["kpi_label"] = ParagraphStyle(
        "kpi_label", fontName="Helvetica", fontSize=7.5, leading=10,
        textColor=colors.HexColor(SLATE_LIGHT), alignment=TA_CENTER, spaceAfter=4,
    )
    s["kpi_value"] = ParagraphStyle(
        "kpi_value", fontName="Helvetica-Bold", fontSize=20, leading=24,
        textColor=colors.HexColor(NAVY), alignment=TA_CENTER, spaceAfter=2,
    )
    s["kpi_sub"] = ParagraphStyle(
        "kpi_sub", fontName="Helvetica", fontSize=7.5, leading=10,
        textColor=colors.HexColor(SLATE_LIGHT), alignment=TA_CENTER,
    )
    return s


def kpi_card(label: str, value: str, sub: str, S):
    inner = [
        [Paragraph(label.upper(), S["kpi_label"])],
        [Paragraph(value, S["kpi_value"])],
        [Paragraph(sub, S["kpi_sub"])],
    ]
    t = Table(inner, colWidths=[1.7 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(PAPER)),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(INK_BORDER)),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def insight_box(title: str, body: str, S, accent_hex: str = NAVY):
    cell = Table(
        [
            [Paragraph(title.upper(), S["insight_label"])],
            [Paragraph(body, S["insight_body"])],
        ],
        colWidths=[6.6 * inch],
    )
    cell.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(PAPER)),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor(accent_hex)),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return cell


def session_risk_panel(stats: dict, S):
    sys_labels = [SYSTEM_LABELS.get(s, s.title()) for s in stats["target_systems_touched"]]
    sensitive = ", ".join(s for s in sys_labels if s in ("CRM", "Postgres")) or "None"
    external = ", ".join(stats["external_comm_systems"]) or "None"

    rows = [
        ["Category", "Status"],
        [
            "Actions outside declared scope",
            f"{stats['out_of_scope_actions']} detected"
            if stats["out_of_scope_actions"] > 0 else "None detected",
        ],
        [
            "Unauthorized write attempts",
            f"{stats['write_blocks']} detected"
            if stats["write_blocks"] > 0 else "None detected",
        ],
        ["Sensitive systems touched", sensitive],
        ["External communications", external],
    ]
    tbl = Table(rows, colWidths=[2.6 * inch, 4.0 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(SLATE_DEEP)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor(PAPER)]),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(INK_BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor(INK_BORDER)),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


def governance_posture_panel(S):
    """Mode + confidence indicators -- mirrors a SOC posture panel."""
    rows = [
        ["Indicator", "Value"],
        ["Governance mode", "Advisory (observe-only)"],
        ["Enforcement coverage", "Partial -- execution boundary"],
        ["Trace completeness", "Full -- every action recorded"],
        ["Policy confidence", "High -- rule-based evaluation"],
    ]
    tbl = Table(rows, colWidths=[2.6 * inch, 4.0 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(SLATE_DEEP)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica"),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(SLATE_DEEP)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor(PAPER)]),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(INK_BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor(INK_BORDER)),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


def severity_pill(sev: str, S):
    """Return a Paragraph styled as a pill for a severity tag."""
    color_hex = SEVERITY_COLORS[sev]
    return Paragraph(
        f"<font color='{color_hex}'><b>{sev}</b></font>",
        S["small"],
    )


def comparison_table(S):
    rows = [
        ["Traditional logging", "Execution-boundary governance"],
        ["Records what happened", "Records whether it should have happened"],
        ["After-the-fact reconstruction", "Pre-action policy evaluation"],
        ["Per-system, fragmented", "Cross-system, unified"],
        ["Application-level visibility", "Behavioral-level accountability"],
    ]
    tbl = Table(rows, colWidths=[3.3 * inch, 3.3 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor(SLATE_LIGHT)),
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor(NAVY)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(SLATE_DEEP)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor(PAPER)]),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(INK_BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor(INK_BORDER)),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return tbl


# =====================================================================
# PDF: COMPOSITION
# =====================================================================


def _draw_chrome(canvas, doc, show_header: bool = True):
    """Draw page header + footer (title, page #, confidentiality)."""
    canvas.saveState()
    width, height = LETTER
    # Header rule + title (skip on cover page where the report header lives)
    if show_header:
        canvas.setStrokeColor(colors.HexColor(INK_BORDER))
        canvas.setLineWidth(0.4)
        canvas.line(0.75 * inch, height - 0.55 * inch,
                    width - 0.75 * inch, height - 0.55 * inch)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor(SLATE_LIGHT))
        canvas.drawString(0.75 * inch, height - 0.45 * inch,
                          "Sentience Governance Report")
        canvas.drawRightString(width - 0.75 * inch, height - 0.45 * inch,
                               "Confidential prototype")
    # Footer rule + page number + confidentiality
    canvas.setStrokeColor(colors.HexColor(INK_BORDER))
    canvas.setLineWidth(0.4)
    canvas.line(0.75 * inch, 0.55 * inch,
                width - 0.75 * inch, 0.55 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor(SLATE_LIGHT))
    canvas.drawString(0.75 * inch, 0.4 * inch,
                      "Sentience Governance Report - Confidential prototype")
    canvas.drawRightString(width - 0.75 * inch, 0.4 * inch,
                           f"Page {doc.page}")
    canvas.restoreState()


def _on_first_page(canvas, doc):
    _draw_chrome(canvas, doc, show_header=False)


def _on_later_page(canvas, doc):
    _draw_chrome(canvas, doc, show_header=True)


def render_pdf(stats: dict, out_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.85 * inch,
        title="Sentience Governance Report",
        author="Sentience",
    )
    S = _styles()
    story = []

    # =================================================================
    # PAGE 1 -- COVER & EXECUTIVE SUMMARY
    # =================================================================
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    story.append(Paragraph("Sentience Governance Report", S["title"]))
    story.append(Paragraph(
        "Execution-boundary governance analysis for AI-assisted workflows.",
        S["subtitle"],
    ))
    story.append(Paragraph(
        f"Workflow: <b>Institutional reporting</b> &nbsp;&middot;&nbsp; "
        f"Session <font color='{SLATE_LIGHT}'>{stats['session_id'][:8]}...</font> "
        f"&nbsp;&middot;&nbsp; Generated {now}",
        S["meta"],
    ))
    story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor(INK_BORDER), spaceAfter=14))

    deduped_total = sum(stats["deduped"].values())
    naive_total = sum(stats["naive"].values())
    inflation = (naive_total / deduped_total) if deduped_total else 1
    findings = sum(stats["violation_counts"].values()) + sum(stats["flag_counts"].values())

    kpi_row = Table(
        [[
            kpi_card("Workflow steps", str(stats["total_turns"]),
                     "AI processing steps", S),
            kpi_card("External system actions", str(stats["total_tool_calls"]),
                     "actions across systems", S),
            kpi_card("Compute consumption", f"{deduped_total:,}",
                     "tokens (execution-attributed)", S),
            kpi_card("Governance review findings", str(findings),
                     "review-flagged actions", S),
        ]],
        colWidths=[1.7 * inch] * 4,
        hAlign="LEFT",
    )
    kpi_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(kpi_row)
    story.append(Spacer(1, 18))

    # Executive summary
    story.append(Paragraph("Executive summary", S["sub"]))
    story.append(Paragraph(
        f"This report reconstructs a complete AI-assisted workflow session, "
        f"including workflow activity, external system access, "
        f"governance review findings, and execution-boundary policy evaluation. "
        f"During the session, an AI agent completed <b>{stats['total_turns']} workflow "
        f"steps</b> and executed <b>{stats['total_tool_calls']} actions</b> across "
        f"{', '.join(SYSTEM_LABELS.get(s, s.title()) for s in stats['target_systems_touched'])}. "
        f"Compute consumption -- attributed at the execution level to ensure "
        f"accurate cost accounting -- totaled <b>{deduped_total:,} tokens</b>. "
        f"A naive event-level aggregation would have reported <b>{naive_total:,}</b>, "
        f"an overstatement of <b>{inflation:.1f}x</b>. The governance engine "
        f"recorded <b>{findings} review-flagged "
        f"action{'' if findings == 1 else 's'}</b>, each traceable to its "
        f"originating workflow step and the systems it touched.",
        S["body"],
    ))
    story.append(Spacer(1, 4))

    # Risk + posture panels side by side
    story.append(Paragraph("Session risk summary", S["sub"]))
    story.append(session_risk_panel(stats, S))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Governance posture", S["sub"]))
    story.append(governance_posture_panel(S))

    # =================================================================
    # PAGE 2 -- COMPUTE CONSUMPTION
    # =================================================================
    story.append(PageBreak())
    story.append(Paragraph("1. Compute consumption", S["section"]))
    story.append(Paragraph(
        "Compute costs are attributed at the execution level so that "
        "multi-step AI workflows are not double-counted in financial or "
        "operational reporting. The chart below contrasts naive aggregation "
        "with execution-attributed accounting for this session.",
        S["body"],
    ))
    story.append(Image(str(chart_naive_vs_deduped(stats)), width=6.6 * inch, height=2.85 * inch))
    story.append(Spacer(1, 8))
    story.append(insight_box(
        "Operational interpretation",
        f"Naive aggregation overstated this session's compute by "
        f"<b>{inflation:.1f}x</b>. Accurate attribution becomes critical when "
        f"AI agents orchestrate multiple downstream systems -- without it, "
        f"cost dashboards inflate, vendor chargebacks drift, and budget "
        f"conversations become unreliable.<br/><br/>"
        f"<i>Institutional implication: inaccurate attribution creates budget "
        f"distortion across shared AI infrastructure.</i>",
        S, accent_hex=NAVY,
    ))

    story.append(Paragraph("Per-step breakdown", S["sub"]))
    story.append(Image(str(chart_tokens_by_turn(stats)), width=6.6 * inch, height=2.85 * inch))

    rows = [["Step", "Prompt", "Completion", "Cached (R / W)"]]
    for i, (tid, t) in enumerate(stats["per_turn"].items()):
        rows.append([
            f"Step {i+1}",
            f"{t['prompt']:,}",
            f"{t['completion']:,}",
            f"{t['cached_read']:,} / {t['cached_write']:,}",
        ])
    tbl = Table(rows, colWidths=[1.2 * inch, 1.4 * inch, 1.4 * inch, 1.6 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(SLATE_DEEP)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(PAPER)]),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(INK_BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor(INK_BORDER)),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(Spacer(1, 4))
    story.append(tbl)

    # =================================================================
    # PAGE 3 -- GOVERNANCE FINDINGS
    # =================================================================
    story.append(PageBreak())
    story.append(Paragraph("2. Governance findings", S["section"]))
    story.append(Paragraph(
        "Every action this agent took was evaluated against a defined set "
        "of governance rules. <b>Enforcement violations</b> would have "
        "blocked execution in enforcing mode. <b>Advisory signals</b> "
        "do not block but warrant institutional review. Severity reflects "
        "the operational risk of the finding pattern.",
        S["body"],
    ))

    chart = chart_governance_findings(stats)
    if chart is not None:
        chart_h = 0.5 * (len(stats["violation_counts"]) + len(stats["flag_counts"])) + 1.0
        story.append(Image(str(chart), width=6.6 * inch, height=min(chart_h * inch, 3.0 * inch)))
        story.append(Spacer(1, 6))

    # Operational interpretation -- automatic narrative
    if stats["out_of_scope_actions"] > 0 or stats["write_blocks"] > 0:
        bits = []
        if stats["out_of_scope_actions"] > 0:
            bits.append(
                f"<b>{stats['out_of_scope_actions']} action"
                f"{'s' if stats['out_of_scope_actions'] != 1 else ''} occurred outside the agent's "
                f"declared operational scope.</b> In enforcement mode "
                f"{'these actions' if stats['out_of_scope_actions'] != 1 else 'this action'} "
                "would have been prevented before execution. Repeated occurrences "
                "of undeclared actions are a leading indicator of behavioral drift "
                "and governance breakdown in autonomous workflows."
            )
        if stats["write_blocks"] > 0:
            bits.append(
                f"<b>{stats['write_blocks']} write attempt"
                f"{'s were' if stats['write_blocks'] != 1 else ' was'} flagged for missing "
                f"classification or retention policy.</b> Persistent state changes "
                f"without retention metadata create downstream compliance and "
                f"data-lifecycle exposure."
            )
        story.append(insight_box(
            "Operational interpretation",
            " ".join(bits) + "<br/><br/><i>Institutional implication: repeated "
            "undeclared actions are a leading indicator of workflow drift.</i>",
            S, accent_hex=RED,
        ))

    # Enforcement violations table -- with severity
    if stats["violation_counts"]:
        story.append(Paragraph("Enforcement violations", S["sub"]))
        v_rows = [["Severity", "Code", "Finding", "Consequence", "Count"]]
        for rule, count in sorted(
            stats["violation_counts"].items(),
            key=lambda kv: -POLICY_RULES.get(kv[0], {}).get("severity", "LOW").__hash__(),
        ):
            meta = POLICY_RULES.get(rule, {"label": rule, "consequence": "", "severity": SEVERITY_MODERATE})
            v_rows.append([
                severity_pill(meta["severity"], S),
                rule,
                Paragraph(meta["label"], S["small"]),
                Paragraph(meta["consequence"], S["small"]),
                str(count),
            ])
        v_tbl = Table(v_rows, colWidths=[0.7 * inch, 0.55 * inch, 1.7 * inch, 3.2 * inch, 0.45 * inch])
        v_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(SLATE_DEEP)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(PAPER)]),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(INK_BORDER)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (4, 1), (4, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(v_tbl)

    if stats["flag_counts"]:
        story.append(Paragraph("Advisory signals", S["sub"]))
        f_rows = [["Severity", "Signal", "Count"]]
        for flag, count in sorted(stats["flag_counts"].items()):
            meta = ADVISORY_FLAGS.get(flag, {"label": flag, "severity": SEVERITY_MODERATE})
            f_rows.append([
                severity_pill(meta["severity"], S),
                Paragraph(meta["label"], S["small"]),
                str(count),
            ])
        f_tbl = Table(f_rows, colWidths=[0.85 * inch, 5.2 * inch, 0.55 * inch])
        f_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor(SLATE_DEEP)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(PAPER)]),
            ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor(INK_BORDER)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (2, 1), (2, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(f_tbl)

    # =================================================================
    # PAGE 4 -- SYSTEM ACCESS OVERVIEW
    # =================================================================
    story.append(PageBreak())
    story.append(Paragraph("3. System access overview", S["section"]))
    sys_list = ", ".join(SYSTEM_LABELS.get(s, s.title()) for s in stats["target_systems_touched"])
    story.append(Paragraph(
        f"During this session, the agent accessed the following systems: "
        f"<b>{sys_list}</b>. The chart below summarizes call frequency by tool. "
        "This view supports capacity planning, vendor cost attribution, and "
        "early detection of behavioral drift away from the agent's stated objective.",
        S["body"],
    ))
    story.append(Image(str(chart_tool_frequency(stats)), width=6.6 * inch, height=3.0 * inch))
    story.append(Spacer(1, 6))
    story.append(insight_box(
        "Institutional read",
        "External system access is the most reviewable surface of agent "
        "behavior. Concentrated access to a small set of systems is generally "
        "expected; sudden expansion -- particularly into systems outside the "
        "agent's stated objective -- is a leading indicator of scope drift."
        "<br/><br/><i>Institutional implication: expansion into unexpected "
        "systems often precedes policy violations.</i>",
        S, accent_hex=NAVY,
    ))

    # =================================================================
    # PAGE 5 -- GOVERNANCE TIMELINE (HERO)
    # =================================================================
    story.append(PageBreak())
    story.append(Paragraph("4. Governance timeline", S["section"]))
    story.append(Paragraph(
        "The session reconstructed as a sequence of governance milestones. "
        "Each event is independently traceable to the underlying audit trail. "
        "This is the institutional analog of a flight recorder: declared "
        "intent, executed actions, and any moments where the agent's behavior "
        "diverged from policy.",
        S["body"],
    ))
    story.append(Spacer(1, 4))
    timeline_chart = chart_governance_timeline(stats)
    # Constrain to available page space. The intro paragraph above takes
    # ~1.6 in; section header ~0.6 in; bottom margin ~0.75 in. Leaves
    # ~6.7 in on US Letter. Cap at 6.4 in to be safe and let reportlab
    # preserve the source aspect ratio (which we already controlled in
    # chart_governance_timeline).
    from PIL import Image as PILImage
    with PILImage.open(timeline_chart) as im:
        src_w, src_h = im.size
    aspect = src_h / src_w
    target_w = 6.6 * inch
    target_h = target_w * aspect
    max_h = 6.4 * inch
    if target_h > max_h:
        target_h = max_h
        target_w = target_h / aspect
    story.append(Image(str(timeline_chart), width=target_w, height=target_h))

    # =================================================================
    # PAGE 6 -- WHY THIS MATTERS TO INSTITUTIONS
    # =================================================================
    story.append(PageBreak())
    story.append(Paragraph("Why this matters to institutions", S["section"]))
    story.append(Paragraph(
        "AI systems increasingly operate across institutional infrastructure -- "
        "CRM platforms, collaboration tools, databases, and internal "
        "knowledge stores. Traditional logging explains what happened after "
        "execution. Governance at the execution boundary establishes whether "
        "the action should have occurred in the first place.",
        S["body"],
    ))
    story.append(Spacer(1, 4))
    story.append(comparison_table(S))
    story.append(Spacer(1, 14))

    story.append(insight_box(
        "What this enables",
        "Reviewable AI behavior. Auditable execution. Behavioral accountability "
        "across vendors, tools, and workflow steps. Governance evidence "
        "produced as a byproduct of operation -- not assembled retroactively "
        "from disconnected logs.",
        S, accent_hex=TEAL,
    ))

    # =================================================================
    # PAGE 7 -- TECHNICAL APPENDIX
    # =================================================================
    story.append(PageBreak())
    story.append(Paragraph("Technical appendix", S["section"]))
    story.append(Paragraph(
        "This appendix is provided for technical reviewers. The main "
        "report is intended for institutional and executive audiences "
        "and excludes implementation detail.",
        S["body"],
    ))

    story.append(Paragraph("Trace primitives", S["sub"]))
    story.append(Paragraph(
        "Each external action emits up to five governance events: agent "
        "registration, intent declaration, scope assertion, context "
        "snapshot, and -- for persistent state changes -- a memory "
        "candidate event. Together they reconstruct the action against "
        "declared intent, declared scope, and surrounding data context.",
        S["body"],
    ))

    story.append(Paragraph("Compute attribution", S["sub"]))
    story.append(Paragraph(
        "Token spend is captured per workflow step. When one step produces "
        "multiple downstream actions, the spend is recorded once and the "
        "step identifier is propagated to each action. Aggregation "
        "consumers dedupe by (session, step) before summing to avoid "
        "multi-count inflation.",
        S["body"],
    ))

    story.append(Paragraph("Severity assignment", S["sub"]))
    story.append(Paragraph(
        "Severity reflects the operational risk of the finding pattern. "
        "<b>HIGH</b> findings would block execution in enforcing mode. "
        "<b>MODERATE</b> findings warrant review and indicate drift. "
        "<b>LOW</b> findings are surfaced for completeness.",
        S["body"],
    ))

    story.append(Paragraph("Disclaimer", S["sub"]))
    story.append(Paragraph(
        "This report demonstrates governance visibility generated from "
        "execution-boundary traces during an AI-assisted workflow session. "
        "It is intended to illustrate operational reviewability, "
        "behavioral accountability, and institutional oversight "
        "capabilities. Numbers and identifiers in this report are derived "
        "from a synthesized session.",
        S["caption"],
    ))

    doc.build(story, onFirstPage=_on_first_page, onLaterPages=_on_later_page)


# =====================================================================
# MARKDOWN RENDERER -- iterate on copy here before regenerating PDF
# =====================================================================


def render_markdown(stats: dict, out_path: Path) -> None:
    deduped_total = sum(stats["deduped"].values())
    naive_total = sum(stats["naive"].values())
    inflation = (naive_total / deduped_total) if deduped_total else 1
    findings = sum(stats["violation_counts"].values()) + sum(stats["flag_counts"].values())
    sys_labels = [SYSTEM_LABELS.get(s, s.title()) for s in stats["target_systems_touched"]]
    sensitive = ", ".join(s for s in sys_labels if s in ("CRM", "Postgres")) or "None"
    external = ", ".join(stats["external_comm_systems"]) or "None"
    out_of_scope = stats["out_of_scope_actions"]
    write_blocks = stats["write_blocks"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []

    # ---- Cover ----
    lines.append("# Sentience Governance Report")
    lines.append("")
    lines.append("**Execution-boundary governance analysis for AI-assisted workflows.**")
    lines.append("")
    lines.append(
        f"Workflow: **Institutional reporting** &middot; "
        f"Session: `{stats['session_id'][:8]}...` &middot; "
        f"Generated: {now}"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- KPI row ----
    lines.append("## At a glance")
    lines.append("")
    lines.append("| Workflow steps | External system actions | Compute consumption | Governance review findings |")
    lines.append("| :-: | :-: | :-: | :-: |")
    lines.append(
        f"| **{stats['total_turns']}** | **{stats['total_tool_calls']}** | "
        f"**{deduped_total:,}** tokens | **{findings}** review-flagged |"
    )
    lines.append(
        f"| _AI processing steps_ | _actions across systems_ | "
        f"_execution-attributed_ | _violations + advisory signals_ |"
    )
    lines.append("")

    # ---- Executive summary ----
    lines.append("## Executive summary")
    lines.append("")
    lines.append(
        f"This report reconstructs a complete AI-assisted workflow session, "
        f"including workflow activity, external system access, governance "
        f"review findings, and execution-boundary policy evaluation. During "
        f"the session, an AI agent completed **{stats['total_turns']} "
        f"workflow steps** and executed **{stats['total_tool_calls']} "
        f"actions** across {', '.join(sys_labels)}. Compute consumption — "
        f"attributed at the execution level to ensure accurate cost "
        f"accounting — totaled **{deduped_total:,} tokens**. A naive "
        f"event-level aggregation would have reported **{naive_total:,}**, "
        f"an overstatement of **{inflation:.1f}x**. The governance engine "
        f"recorded **{findings} review-flagged "
        f"action{'' if findings == 1 else 's'}**, each traceable to its "
        f"originating workflow step and the systems it touched."
    )
    lines.append("")

    # ---- Risk summary ----
    lines.append("## Session risk summary")
    lines.append("")
    lines.append("| Category | Status |")
    lines.append("| :-- | :-- |")
    lines.append(
        f"| Actions outside declared scope | "
        f"{f'{out_of_scope} detected' if out_of_scope else 'None detected'} |"
    )
    lines.append(
        f"| Unauthorized write attempts | "
        f"{f'{write_blocks} detected' if write_blocks else 'None detected'} |"
    )
    lines.append(f"| Sensitive systems touched | {sensitive} |")
    lines.append(f"| External communications | {external} |")
    lines.append("")

    # ---- Governance posture ----
    lines.append("## Governance posture")
    lines.append("")
    lines.append("| Indicator | Value |")
    lines.append("| :-- | :-- |")
    lines.append("| Governance mode | Advisory (observe-only) |")
    lines.append("| Enforcement coverage | Partial — execution boundary |")
    lines.append("| Trace completeness | Full — every action recorded |")
    lines.append("| Policy confidence | High — rule-based evaluation |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- Section 1: Compute consumption ----
    lines.append("# 1. Compute consumption")
    lines.append("")
    lines.append(
        "Compute costs are attributed at the execution level so that "
        "multi-step AI workflows are not double-counted in financial or "
        "operational reporting. The table below contrasts naive aggregation "
        "with execution-attributed accounting for this session."
    )
    lines.append("")
    lines.append("| Method | Prompt tokens | Completion | Cached (R/W) | Total |")
    lines.append("| :-- | --: | --: | --: | --: |")
    lines.append(
        f"| Naive aggregation | {stats['naive']['prompt']:,} | "
        f"{stats['naive']['completion']:,} | "
        f"{stats['naive']['cached_read']:,} / {stats['naive']['cached_write']:,} | "
        f"**{naive_total:,}** |"
    )
    lines.append(
        f"| Execution-attributed | {stats['deduped']['prompt']:,} | "
        f"{stats['deduped']['completion']:,} | "
        f"{stats['deduped']['cached_read']:,} / {stats['deduped']['cached_write']:,} | "
        f"**{deduped_total:,}** |"
    )
    lines.append(f"| _Inflation factor_ | | | | **{inflation:.1f}x** |")
    lines.append("")
    lines.append("> **Operational interpretation**")
    lines.append(">")
    lines.append(
        f"> Naive aggregation overstated this session's compute by "
        f"**{inflation:.1f}x**. Accurate attribution becomes critical when "
        f"AI agents orchestrate multiple downstream systems — without it, "
        f"cost dashboards inflate, vendor chargebacks drift, and budget "
        f"conversations become unreliable."
    )
    lines.append(">")
    lines.append(
        "> _Institutional implication: inaccurate attribution creates "
        "budget distortion across shared AI infrastructure._"
    )
    lines.append("")

    lines.append("## Per-step breakdown")
    lines.append("")
    lines.append("| Step | Prompt | Completion | Cached (R / W) |")
    lines.append("| :-- | --: | --: | --: |")
    for i, (tid, t) in enumerate(stats["per_turn"].items()):
        lines.append(
            f"| Step {i+1} | {t['prompt']:,} | "
            f"{t['completion']:,} | "
            f"{t['cached_read']:,} / {t['cached_write']:,} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- Section 2: Governance findings ----
    lines.append("# 2. Governance findings")
    lines.append("")
    lines.append(
        "Every action this agent took was evaluated against a defined set "
        "of governance rules. **Enforcement violations** would have blocked "
        "execution in enforcing mode. **Advisory signals** do not block but "
        "warrant institutional review. Severity reflects the operational "
        "risk of the finding pattern."
    )
    lines.append("")

    if out_of_scope > 0 or write_blocks > 0:
        bits = []
        if out_of_scope > 0:
            bits.append(
                f"**{out_of_scope} action{'s' if out_of_scope != 1 else ''} "
                f"occurred outside the agent's declared operational scope.** "
                f"In enforcement mode "
                f"{'these actions' if out_of_scope != 1 else 'this action'} "
                "would have been prevented before execution. Repeated "
                "occurrences of undeclared actions are a leading indicator "
                "of behavioral drift and governance breakdown in autonomous "
                "workflows."
            )
        if write_blocks > 0:
            bits.append(
                f"**{write_blocks} write attempt"
                f"{'s were' if write_blocks != 1 else ' was'} flagged for "
                f"missing classification or retention policy.** Persistent "
                f"state changes without retention metadata create downstream "
                f"compliance and data-lifecycle exposure."
            )
        lines.append("> **Operational interpretation**")
        lines.append(">")
        lines.append("> " + " ".join(bits))
        lines.append(">")
        lines.append(
            "> _Institutional implication: repeated undeclared actions are "
            "a leading indicator of workflow drift._"
        )
        lines.append("")

    if stats["violation_counts"]:
        lines.append("## Enforcement violations")
        lines.append("")
        lines.append("| Severity | Code | Finding | Consequence | Count |")
        lines.append("| :-- | :-- | :-- | :-- | :-: |")
        for rule, count in sorted(stats["violation_counts"].items()):
            meta = POLICY_RULES.get(rule, {})
            lines.append(
                f"| **{meta.get('severity', '—')}** | `{rule}` | "
                f"{meta.get('label', rule)} | {meta.get('consequence', '')} | "
                f"{count} |"
            )
        lines.append("")

    if stats["flag_counts"]:
        lines.append("## Advisory signals")
        lines.append("")
        lines.append("| Severity | Signal | Count |")
        lines.append("| :-- | :-- | :-: |")
        for flag, count in sorted(stats["flag_counts"].items()):
            meta = ADVISORY_FLAGS.get(flag, {})
            lines.append(
                f"| **{meta.get('severity', '—')}** | "
                f"{meta.get('label', flag)} | {count} |"
            )
        lines.append("")
    lines.append("---")
    lines.append("")

    # ---- Section 3: System access ----
    lines.append("# 3. System access overview")
    lines.append("")
    lines.append(
        f"During this session, the agent accessed the following systems: "
        f"**{', '.join(sys_labels)}**. The table below summarizes call "
        f"frequency by tool. This view supports capacity planning, vendor "
        f"cost attribution, and early detection of behavioral drift away "
        f"from the agent's stated objective."
    )
    lines.append("")
    lines.append("| Tool | Calls |")
    lines.append("| :-- | :-: |")
    for tool, count in sorted(
        stats["tool_counter"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        lines.append(f"| `{tool}` | {count} |")
    lines.append("")
    lines.append("> **Institutional read**")
    lines.append(">")
    lines.append(
        "> External system access is the most reviewable surface of agent "
        "behavior. Concentrated access to a small set of systems is "
        "generally expected; sudden expansion — particularly into systems "
        "outside the agent's stated objective — is a leading indicator of "
        "scope drift."
    )
    lines.append(">")
    lines.append(
        "> _Institutional implication: expansion into unexpected systems "
        "often precedes policy violations._"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- Section 4: Timeline ----
    lines.append("# 4. Governance timeline")
    lines.append("")
    lines.append(
        "The session reconstructed as a sequence of governance milestones. "
        "Each event is independently traceable to the underlying audit "
        "trail. This is the institutional analog of a flight recorder: "
        "declared intent, executed actions, and any moments where the "
        "agent's behavior diverged from policy."
    )
    lines.append("")
    glyph_for = {
        "start": "🟢",
        "action": "⚪",
        "advisory": "🟠",
        "violation": "🔴",
        "end": "⚫",
    }
    marker_for = {
        "start": "START",
        "action": "ACTION",
        "advisory": "ADVISORY",
        "violation": "BLOCKED",
        "end": "END",
    }
    n = len(stats["timeline"])
    for idx, item in enumerate(stats["timeline"]):
        glyph = glyph_for[item["kind"]]
        marker = marker_for[item["kind"]]
        sev = item.get("severity")
        sev_tag = f" &nbsp; **{sev}**" if sev else ""
        lines.append(f"{glyph} &nbsp; **{marker}** &nbsp; — &nbsp; {item['primary']}{sev_tag}")
        secondary = item.get("secondary", "")
        if secondary:
            lines.append(f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; _{secondary}_")
        if idx < n - 1:
            lines.append("&nbsp;&nbsp;&nbsp;&nbsp; │")
        lines.append("")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- Why this matters ----
    lines.append("# Why this matters to institutions")
    lines.append("")
    lines.append(
        "AI systems increasingly operate across institutional infrastructure "
        "— CRM platforms, collaboration tools, databases, and internal "
        "knowledge stores. Traditional logging explains what happened after "
        "execution. Governance at the execution boundary establishes whether "
        "the action should have occurred in the first place."
    )
    lines.append("")
    lines.append("| Traditional logging | Execution-boundary governance |")
    lines.append("| :-- | :-- |")
    lines.append("| Records what happened | Records whether it should have happened |")
    lines.append("| After-the-fact reconstruction | Pre-action policy evaluation |")
    lines.append("| Per-system, fragmented | Cross-system, unified |")
    lines.append("| Application-level visibility | Behavioral-level accountability |")
    lines.append("")
    lines.append("> **What this enables**")
    lines.append(">")
    lines.append(
        "> Reviewable AI behavior. Auditable execution. Behavioral "
        "accountability across vendors, tools, and workflow steps. "
        "Governance evidence produced as a byproduct of operation — not "
        "assembled retroactively from disconnected logs."
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "_This report demonstrates governance visibility generated from "
        "execution-boundary traces during an AI-assisted workflow session. "
        "It is intended to illustrate operational reviewability, behavioral "
        "accountability, and institutional oversight capabilities. Numbers "
        "and identifiers in this report are derived from a synthesized "
        "session._"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("# Technical appendix")
    lines.append("")
    lines.append(
        "This appendix is provided for technical reviewers. The main "
        "report above is intended for institutional and executive audiences "
        "and excludes implementation detail."
    )
    lines.append("")
    lines.append("## Trace primitives")
    lines.append("")
    lines.append(
        "Each external action emits up to five governance events: agent "
        "registration, intent declaration, scope assertion, context "
        "snapshot, and — for persistent state changes — a memory "
        "candidate event. Together they reconstruct the action against "
        "declared intent, declared scope, and surrounding data context."
    )
    lines.append("")
    lines.append("## Compute attribution")
    lines.append("")
    lines.append(
        "Token spend is captured per workflow step. When one step produces "
        "multiple downstream actions, the spend is recorded once and the "
        "step identifier is propagated to each action. Aggregation "
        "consumers dedupe by (session, step) before summing to avoid "
        "multi-count inflation."
    )
    lines.append("")
    lines.append("## Severity assignment")
    lines.append("")
    lines.append(
        "Severity reflects the operational risk of the finding pattern. "
        "**HIGH** findings would block execution in enforcing mode. "
        "**MODERATE** findings warrant review and indicate drift. "
        "**LOW** findings are surfaced for completeness."
    )
    lines.append("")

    out_path.write_text("\n".join(lines))


# =====================================================================
# MAIN
# =====================================================================


async def main():
    sink_path = Path("/tmp/v023-business-report-trace.jsonl")
    md_path = Path("/tmp/v023-business-report.md")
    pdf_path = Path("/tmp/v023-business-report.pdf")
    if sink_path.exists():
        sink_path.unlink()

    print(f"  [1/4] Synthesizing trace -> {sink_path}")
    await synthesize_trace(sink_path)

    print(f"  [2/4] Aggregating events")
    events = load_events(sink_path)
    stats = compute_stats(events)

    print(f"  [3/4] Rendering Markdown -> {md_path}")
    render_markdown(stats, md_path)

    print(f"  [4/4] Rendering PDF      -> {pdf_path}")
    render_pdf(stats, pdf_path)

    print()
    print(f"  TRACE: {sink_path}  ({len(events)} events)")
    print(f"  MD   : {md_path}")
    print(f"  PDF  : {pdf_path}")
    print()
    print(f"  Open with:  open {pdf_path}")


if __name__ == "__main__":
    asyncio.run(main())
