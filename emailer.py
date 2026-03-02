"""
emailer.py — HTML email builder and sender.

Builds a styled daily fantasy basketball report and delivers it via
Gmail SMTP with TLS.
"""

from __future__ import annotations

import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

COLOUR = {
    "bg_header": "#1a3a5c",        # dark navy — section headers
    "bg_table_head": "#2d6a9f",    # medium blue — table column headers
    "bg_row_even": "#f0f5fb",      # very light blue
    "bg_row_odd": "#ffffff",       # white
    "bg_alert": "#fff3cd",         # amber — warning rows
    "bg_danger": "#fde8e8",        # light red — injured / critical
    "bg_success": "#e8f5e9",       # light green — healthy / good
    "bg_untouchable": "#fff9e6",   # light gold — untouchable players
    "text_dark": "#1a1a2e",
    "text_header": "#ffffff",
    "text_muted": "#6c757d",
    "accent_green": "#2e7d32",
    "accent_red": "#c62828",
    "accent_yellow": "#f57f17",
    "accent_blue": "#1565c0",
}


# ---------------------------------------------------------------------------
# HTML primitives
# ---------------------------------------------------------------------------


def _section_header(title: str) -> str:
    return (
        f'<tr><td colspan="99" style="'
        f'background:{COLOUR["bg_header"]};color:{COLOUR["text_header"]};'
        f'font-size:16px;font-weight:700;padding:12px 16px;'
        f'letter-spacing:0.5px;">{title}</td></tr>'
    )


def _col_header(*cols: str) -> str:
    cells = "".join(
        f'<th style="background:{COLOUR["bg_table_head"]};color:{COLOUR["text_header"]};'
        f'padding:8px 12px;text-align:left;font-weight:600;font-size:13px;">{c}</th>'
        for c in cols
    )
    return f"<tr>{cells}</tr>"


def _td(value: str, bold: bool = False, color: Optional[str] = None) -> str:
    style = f"padding:7px 12px;font-size:13px;vertical-align:middle;"
    if bold:
        style += "font-weight:700;"
    if color:
        style += f"color:{color};"
    return f'<td style="{style}">{value}</td>'


def _row(cells: list[str], bg: str) -> str:
    inner = "".join(cells)
    return f'<tr style="background:{bg};">{inner}</tr>'


def _pill(text: str, bg: str, fg: str = "#fff") -> str:
    """Inline badge / pill element."""
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:12px;font-size:11px;font-weight:600;">{text}</span>'
    )


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_do_not_drop_section(dnd_list: list[dict]) -> str:
    if not dnd_list:
        return ""

    rows = _section_header("🛡️ DO NOT DROP (Top 6 by Composite Rank)")
    rows += _col_header("Player", "Composite", "HT Z-Score", "HT Szn", "HT 30d", "HT 14d")

    for i, player in enumerate(dnd_list):
        bg = COLOUR["bg_untouchable"]
        ht = player.get("ht_score")
        szn = player.get("ht_season_rank")
        r30 = player.get("ht_rank_30d")
        r14 = player.get("ht_rank_14d")
        comp = player.get("composite", 999)
        cells = [
            _td(f"⭐ {player['name']}", bold=True),
            _td(f"{comp:.1f}"),
            _td(f"{ht:.2f}" if ht is not None else "—"),
            _td(str(szn) if szn is not None else "—"),
            _td(str(r30) if r30 is not None else "—"),
            _td(str(r14) if r14 is not None else "—"),
        ]
        rows += _row(cells, bg)

    return rows


def _status_badge(status: str) -> str:
    if status in ("INJ", "O"):
        return _pill(status, COLOUR["accent_red"])
    if status in ("Q", "DTD"):
        return _pill(status, COLOUR["accent_yellow"], "#333")
    return _pill("OK", COLOUR["accent_green"])


def _build_active_lineup_section(active: list[dict]) -> str:
    rows = _section_header("✅ RECOMMENDED ACTIVE LINEUP")
    rows += _col_header("Slot", "Player", "HT", "30-Day Rank", "14-Day Rank", "Game Today", "Status", "Flags")

    for i, p in enumerate(active):
        bg = COLOUR["bg_row_even"] if i % 2 == 0 else COLOUR["bg_row_odd"]

        # Override row bg for flagged players
        if p.get("flag_injured"):
            bg = COLOUR["bg_danger"]
        elif p.get("flag_low_rank"):
            bg = COLOUR["bg_alert"]
        elif p.get("is_untouchable"):
            bg = COLOUR["bg_untouchable"]

        game_today = "Yes" if p.get("has_game_today") else "No"
        game_color = COLOUR["accent_green"] if p.get("has_game_today") else COLOUR["accent_red"]

        flags: list[str] = []
        if p.get("is_untouchable"):
            flags.append(_pill("UNTOUCHABLE", COLOUR["accent_yellow"], "#333"))
        if p.get("flag_low_rank"):
            flags.append(_pill("LOW RANK", COLOUR["accent_red"]))
        if p.get("flag_injured"):
            flags.append(_pill("INJURED", COLOUR["accent_red"]))

        ht_str = f"{p['ht_score']:.1f}" if p.get("ht_score") is not None else "—"
        ht_color = COLOUR["accent_blue"] if p.get("ht_score") is not None else COLOUR["text_muted"]

        cells = [
            _td(f"<strong>{p['slot']}</strong>"),
            _td(p["name"], bold=True),
            _td(ht_str, color=ht_color, bold=p.get("ht_score") is not None),
            _td(str(p.get("rank_30day", "—")),
                color=COLOUR["accent_green"] if p.get("rank_30day", 999) <= 50 else COLOUR["accent_red"]),
            _td(str(p.get("rank_14day", "—"))),
            _td(game_today, color=game_color),
            _td(_status_badge(p.get("injury_status", "healthy"))),
            _td(" ".join(flags) if flags else ""),
        ]
        rows += _row(cells, bg)

    return rows


def _build_bench_section(bench: list[dict], bench_shape_desc: Optional[str] = None) -> str:
    rows = _section_header("🪑 RECOMMENDED BENCH")

    if bench_shape_desc:
        rows += (
            f'<tr><td colspan="99" style="padding:6px 16px;font-size:12px;'
            f'color:{COLOUR["text_muted"]};background:{COLOUR["bg_row_even"]};">'
            f'Bench shape: {bench_shape_desc}</td></tr>'
        )

    rows += _col_header("Slot", "Player", "HT", "30-Day Rank", "14-Day Rank", "Game Today", "Status")

    for i, p in enumerate(bench):
        bg = COLOUR["bg_row_even"] if i % 2 == 0 else COLOUR["bg_row_odd"]
        if p.get("flag_low_rank"):
            bg = COLOUR["bg_alert"]

        game_today = "Yes" if p.get("has_game_today") else "No"
        game_color = COLOUR["accent_green"] if p.get("has_game_today") else COLOUR["accent_red"]

        ht_str = f"{p['ht_score']:.1f}" if p.get("ht_score") is not None else "—"
        ht_color = COLOUR["accent_blue"] if p.get("ht_score") is not None else COLOUR["text_muted"]

        cells = [
            _td("BN"),
            _td(p["name"], bold=True),
            _td(ht_str, color=ht_color, bold=p.get("ht_score") is not None),
            _td(str(p.get("rank_30day", "—"))),
            _td(str(p.get("rank_14day", "—"))),
            _td(game_today, color=game_color),
            _td(_status_badge(p.get("injury_status", "healthy"))),
        ]
        rows += _row(cells, bg)

    return rows


def _build_il_section(il_flags: dict) -> str:
    move_to = il_flags.get("should_move_to_il", [])
    activate = il_flags.get("should_activate_from_il", [])

    if not move_to and not activate:
        rows = _section_header("🏥 IL FLAGS")
        rows += (
            f'<tr><td colspan="99" style="padding:10px 16px;color:{COLOUR["accent_green"]};'
            f'font-weight:600;">No IL actions needed — roster is clean.</td></tr>'
        )
        return rows

    rows = _section_header("🏥 IL FLAGS — ACTION REQUIRED")
    rows += _col_header("Action", "Player", "Current Slot", "Status", "Suggested Drop")

    for i, p in enumerate(move_to):
        bg = COLOUR["bg_danger"]
        cells = [
            _td(_pill("Move → IL", COLOUR["accent_red"])),
            _td(p["name"], bold=True),
            _td(p["current_slot"]),
            _td(_status_badge(p["status"])),
            _td(""),
        ]
        rows += _row(cells, bg)

    for i, p in enumerate(activate):
        bg = COLOUR["bg_success"]
        dc = p.get("drop_candidate")
        if dc:
            ht_str = f"{dc['ht_score']:.1f}" if dc.get("ht_score") is not None else "n/a"
            rank_str = str(dc.get("rank_14day", "—"))
            drop_text = (
                f"<strong>{dc['name']}</strong> "
                f"(HT: {ht_str}, rank14: {rank_str})"
            )
        else:
            drop_text = '<span style="color:#999;">—</span>'
        cells = [
            _td(_pill("Activate", COLOUR["accent_green"])),
            _td(p["name"], bold=True),
            _td(p["current_slot"]),
            _td(_status_badge("healthy")),
            _td(drop_text),
        ]
        rows += _row(cells, bg)

    return rows


def _build_waiver_active_section(upgrades: list[dict]) -> str:
    rows = _section_header("🔄 WAIVER WIRE — STARTER UPGRADES")

    if not upgrades:
        rows += (
            f'<tr><td colspan="99" style="padding:10px 16px;color:{COLOUR["text_muted"]};">'
            f'No starter upgrade opportunities found.</td></tr>'
        )
        return rows

    rows += _col_header(
        "Add (FA)", "HT Score", "FA Rank (30d)", "MPG", "Drop", "Their Rank (30d)", "Slot", "+Improve", "Notes"
    )

    for i, u in enumerate(upgrades):
        bg = COLOUR["bg_row_even"] if i % 2 == 0 else COLOUR["bg_row_odd"]
        notes = ""
        if u.get("is_untouchable_replace"):
            notes = _pill("UNTOUCHABLE DROP", COLOUR["accent_red"])

        ht_str = f"{u['fa_ht_score']:.1f}" if u.get("fa_ht_score") is not None else "—"

        cells = [
            _td(u["fa_name"], bold=True),
            _td(ht_str, color=COLOUR["accent_blue"]),
            _td(str(u.get("fa_30day_rank", "—")), color=COLOUR["accent_green"]),
            _td(f"{u.get('fa_mpg', 0):.1f}"),
            _td(u.get("replace_player_name", "—")),
            _td(str(u.get("replace_player_rank", "—")), color=COLOUR["accent_red"]),
            _td(u.get("replace_slot", "—")),
            _td(f"+{u.get('rank_improvement', 0)}", color=COLOUR["accent_green"], bold=True),
            _td(notes),
        ]
        rows += _row(cells, bg)

    return rows


def _build_waiver_bench_section(upgrades: list[dict]) -> str:
    rows = _section_header("🔄 WAIVER WIRE — NON-STARTER UPGRADES")

    if not upgrades:
        rows += (
            f'<tr><td colspan="99" style="padding:10px 16px;color:{COLOUR["text_muted"]};">'
            f'No non-starter upgrade opportunities found.</td></tr>'
        )
        return rows

    rows += _col_header(
        "Add (FA)", "HT Score", "Weekly Val", "FA Rank (14d)", "MPG", "Drop", "Their Rank (14d)", "Fit", "+Improve", "Notes"
    )

    for i, u in enumerate(upgrades):
        bg = COLOUR["bg_row_even"] if i % 2 == 0 else COLOUR["bg_row_odd"]
        notes = ""
        if u.get("is_untouchable_replace"):
            notes = _pill("UNTOUCHABLE DROP", COLOUR["accent_red"])

        ht_str = f"{u['fa_ht_score']:.1f}" if u.get("fa_ht_score") is not None else "—"
        wv_str = f"{u['fa_weekly_value']:.1f}" if u.get("fa_weekly_value") is not None else "—"

        cells = [
            _td(u["fa_name"], bold=True),
            _td(ht_str, color=COLOUR["accent_blue"]),
            _td(wv_str, color=COLOUR["accent_blue"], bold=True),
            _td(str(u.get("fa_14day_rank", "—")), color=COLOUR["accent_green"]),
            _td(f"{u.get('fa_mpg', 0):.1f}"),
            _td(u.get("replace_player_name", "—")),
            _td(str(u.get("replace_player_rank", "—")), color=COLOUR["accent_red"]),
            _td(u.get("position_fit", "—")),
            _td(f"+{u.get('rank_improvement', 0)}", color=COLOUR["accent_green"], bold=True),
            _td(notes),
        ]
        rows += _row(cells, bg)

    return rows


def _build_alerts_section(alerts: list[str]) -> str:
    rows = _section_header("⚠️ ALERTS")

    if not alerts:
        rows += (
            f'<tr><td colspan="99" style="padding:10px 16px;color:{COLOUR["accent_green"]};'
            f'font-weight:600;">No alerts — all good!</td></tr>'
        )
        return rows

    for i, alert in enumerate(alerts):
        bg = COLOUR["bg_alert"] if i % 2 == 0 else COLOUR["bg_row_odd"]
        rows += (
            f'<tr style="background:{bg};">'
            f'<td colspan="99" style="padding:7px 12px;font-size:13px;">⚠️ {alert}</td>'
            f'</tr>'
        )

    return rows


# ---------------------------------------------------------------------------
# Full HTML report builder
# ---------------------------------------------------------------------------


def build_html_report(report_data: dict) -> str:
    """
    Assemble the full HTML email body from report_data.

    report_data keys:
        date                    – date string (YYYY-MM-DD)
        do_not_drop             – list of {name, composite, ht_score, ...}
        active_lineup           – list from optimizer
        bench                   – list from optimizer
        bench_shape_desc        – str from check_bench_shape
        on_il                   – list from optimizer
        il_flags                – dict from il_manager
        waiver_active_upgrades  – list from waiver_scanner
        waiver_bench_upgrades   – list from waiver_scanner
        alerts                  – list of plain-text alert strings
    """
    report_date = report_data.get("date", str(date.today()))

    # Build table body rows
    body_rows = ""

    body_rows += _build_do_not_drop_section(report_data.get("do_not_drop", []))

    body_rows += _build_active_lineup_section(report_data.get("active_lineup", []))
    body_rows += _build_bench_section(
        report_data.get("bench", []),
        bench_shape_desc=report_data.get("bench_shape_desc"),
    )
    body_rows += _build_il_section(report_data.get("il_flags", {}))
    body_rows += _build_waiver_active_section(report_data.get("waiver_active_upgrades", []))
    body_rows += _build_waiver_bench_section(report_data.get("waiver_bench_upgrades", []))
    body_rows += _build_alerts_section(report_data.get("alerts", []))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Fantasy Hoops Report — {report_date}</title>
</head>
<body style="margin:0;padding:0;background:#eef2f7;font-family:'Segoe UI',Arial,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f7;padding:20px 0;">
  <tr>
    <td align="center">
      <table width="700" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;
                    box-shadow:0 2px 8px rgba(0,0,0,0.12);overflow:hidden;">

        <!-- ===== TOP BANNER ===== -->
        <tr>
          <td style="background:{COLOUR["bg_header"]};padding:20px 24px;">
            <p style="margin:0;font-size:22px;font-weight:800;color:#fff;letter-spacing:0.5px;">
              🏀 Fantasy Hoops Daily Report
            </p>
            <p style="margin:4px 0 0;font-size:13px;color:#a8c6e8;">
              {report_date} &nbsp;·&nbsp; Generated automatically
            </p>
          </td>
        </tr>

        <!-- ===== CONTENT TABLE ===== -->
        <tr>
          <td style="padding:0;">
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="border-collapse:collapse;">
              {body_rows}
            </table>
          </td>
        </tr>

        <!-- ===== FOOTER ===== -->
        <tr>
          <td style="background:{COLOUR["bg_header"]};padding:12px 24px;text-align:center;">
            <p style="margin:0;font-size:11px;color:#8aafd4;">
              Fantasy Hoops Bot · {report_date} · Do not reply to this email
            </p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------


def send_daily_report(report_data: dict) -> None:
    """
    Build and send the daily HTML report via Gmail SMTP (SSL port 465).

    Environment variables required:
        GMAIL_USER         – sender Gmail address
        GMAIL_APP_PASSWORD – 16-character Gmail App Password
        NOTIFY_EMAIL       – recipient address
    """
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    notify_email = os.environ["NOTIFY_EMAIL"]

    report_date = report_data.get("date", str(date.today()))
    subject = f"Fantasy Hoops Report — {report_date}"

    print(f"[emailer] Building HTML report …")
    html_body = build_html_report(report_data)

    # Assemble MIME message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = notify_email

    # Plain-text fallback
    plain_text = (
        f"Fantasy Hoops Report — {report_date}\n\n"
        "Please view this email in an HTML-capable client.\n\n"
        "Sections: Active Lineup | Bench | IL Flags | Waiver Wire | Alerts"
    )
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    print(f"[emailer] Sending to {notify_email} via {gmail_user} …")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_user, gmail_pass)
            smtp.sendmail(gmail_user, notify_email, msg.as_string())
        print(f"[emailer] Report sent successfully: '{subject}'")
    except smtplib.SMTPAuthenticationError:
        print(
            "[emailer] SMTP authentication failed. Make sure GMAIL_APP_PASSWORD is a valid "
            "16-character Gmail App Password (not your regular Gmail password)."
        )
        raise
    except Exception as exc:
        print(f"[emailer] Failed to send email: {exc}")
        raise


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample_report = {
        "date": str(date.today()),
        "do_not_drop": [
            {"name": "Nikola Jokic", "composite": 1.7,
             "ht_score": 16.16, "ht_season_rank": 1, "ht_rank_30d": 1, "ht_rank_14d": 2},
            {"name": "Shai Gilgeous-Alexander", "composite": 1.3,
             "ht_score": 13.84, "ht_season_rank": 2, "ht_rank_30d": 2, "ht_rank_14d": 1},
            {"name": "Hot Pickup", "composite": 35.2,
             "ht_score": 4.2, "ht_season_rank": 42, "ht_rank_30d": 38, "ht_rank_14d": 35},
        ],
        "active_lineup": [
            {"name": "Shai Gilgeous-Alexander", "slot": "PG", "rank_30day": 1,
             "rank_14day": 2, "has_game_today": True, "injury_status": "healthy",
             "is_untouchable": True, "flag_low_rank": False, "flag_injured": False,
             "ht_score": 11.2},
            {"name": "Devin Booker", "slot": "SG", "rank_30day": 18,
             "rank_14day": 15, "has_game_today": False, "injury_status": "healthy",
             "is_untouchable": False, "flag_low_rank": False, "flag_injured": False,
             "ht_score": 4.8},
            {"name": "Jimmy Butler", "slot": "SF", "rank_30day": 110,
             "rank_14day": 105, "has_game_today": True, "injury_status": "O",
             "is_untouchable": False, "flag_low_rank": True, "flag_injured": True},
            {"name": "Nikola Jokic", "slot": "C", "rank_30day": 2,
             "rank_14day": 1, "has_game_today": True, "injury_status": "healthy",
             "is_untouchable": True, "flag_low_rank": False, "flag_injured": False,
             "ht_score": 13.5},
        ],
        "bench": [
            {"name": "De'Aaron Fox", "slot": "BN", "rank_30day": 40,
             "rank_14day": 38, "has_game_today": True, "injury_status": "Q",
             "is_untouchable": False, "flag_low_rank": False, "ht_score": 6.3},
        ],
        "bench_shape_desc": "G: 1/1 (OK) | F: 0/1 (NEED) | C: 0/1 (NEED)",
        "on_il": [
            {"name": "Joel Embiid", "slot": "IL", "rank_30day": 15,
             "rank_14day": 12, "injury_status": "INJ"},
        ],
        "il_flags": {
            "should_move_to_il": [
                {"name": "Jimmy Butler", "status": "O", "current_slot": "SF",
                 "action": "Move Jimmy Butler (SF) -> IL  [status: O]"},
            ],
            "should_activate_from_il": [
                {"name": "Joel Embiid", "current_slot": "IL",
                 "returning_positions": ["C"], "returning_ht_score": 8.5,
                 "action": "Activate Joel Embiid from IL — consider dropping GG Jackson (HT: 1.2, rank14: 70)",
                 "drop_candidate": {"name": "GG Jackson", "positions": ["SF", "PF"],
                                    "ht_score": 1.2, "rank_14day": 70,
                                    "reason": "HT: 1.2, rank14: 70, position overlap"}},
            ],
        },
        "waiver_active_upgrades": [
            {"fa_name": "Franz Wagner", "fa_positions": ["SF", "F"],
             "fa_30day_rank": 25, "fa_mpg": 34.5, "fa_percent_owned": 72.0,
             "replace_player_name": "Jimmy Butler", "replace_player_rank": 110,
             "replace_slot": "SF", "rank_improvement": 85,
             "is_untouchable_replace": False, "fa_ht_score": 5.8},
        ],
        "waiver_bench_upgrades": [],
        "alerts": [
            "Jimmy Butler (SF) is OUT — consider moving to IL.",
            "Bench shape target not met: missing F and C.",
        ],
    }

    html = build_html_report(sample_report)
    preview_path = "/tmp/report_preview.html"
    with open(preview_path, "w") as f:
        f.write(html)
    print(f"HTML report written to {preview_path} — open in browser to preview.")
