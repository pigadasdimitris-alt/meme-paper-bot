"""Γράφει το README.md με τη σύνοψη — φαίνεται αμέσως στην αρχική σελίδα του repo από το κινητό."""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config as C
from . import engine


def _local(s):
    dt = engine.parse_time(s)
    return dt.astimezone(ZoneInfo(C.TIMEZONE)).strftime("%d/%m %H:%M") if dt else "-"


def summary(state):
    closed = state["closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    eq = engine.equity(state)
    return {
        "equity": eq,
        "pnl": eq - state["start_balance"],
        "pnl_pct": 100 * (eq / state["start_balance"] - 1),
        "trades": len(closed),
        "wins": len(wins),
        "win_rate": 100 * len(wins) / len(closed) if closed else 0.0,
        "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0,
        "best": max((t["pnl"] for t in closed), default=0.0),
        "worst": min((t["pnl"] for t in closed), default=0.0),
    }


def write_readme(state, path):
    s = summary(state)
    lines = [
        "# meme-paper-bot",
        "",
        "**Μόνο εικονικά trades (paper).** Διαβάζει live δεδομένα για νέα meme coins στο Solana, "
        "«αγοράζει» και «πουλάει» με κανόνες και καταγράφει τι θα είχε γίνει — με ρεαλιστικά κόστη "
        f"({C.SLIPPAGE_PER_SIDE*100:.1f}% slippage ανά πλευρά + ${C.FEE_USD_PER_TX:.2f} fee ανά συναλλαγή). "
        "Κανένα πορτοφόλι, κανένα πραγματικό χρήμα.",
        "",
        f"Dashboard: https://pigadasdimitris-alt.github.io/meme-paper-bot/",
        "",
        "## Σύνοψη",
        "",
        "| | |",
        "|---|---|",
        f"| Αρχικό κεφάλαιο | ${state['start_balance']:.2f} |",
        f"| **Τώρα** | **${s['equity']:.2f}** ({s['pnl']:+.2f}$, {s['pnl_pct']:+.1f}%) |",
        f"| Κλειστά trades | {s['trades']} (κερδισμένα {s['wins']}, {s['win_rate']:.0f}%) |",
        f"| Μέσο κέρδος / μέση ζημιά | {s['avg_win']:+.2f}$ / {s['avg_loss']:+.2f}$ |",
        f"| Καλύτερο / χειρότερο | {s['best']:+.2f}$ / {s['worst']:+.2f}$ |",
        f"| Ανοιχτά τώρα | {len(state['positions'])} |",
        f"| Ξεκίνησε | {_local(state['started_at'])} |",
        f"| Τελευταίος έλεγχος | {_local(state['last_run'])} |",
        f"| Σαρώσεις / coins που ελέγχθηκαν | {state['stats']['scans']} / {state['stats']['pools_checked']} |",
        "",
    ]
    if state["positions"]:
        lines += ["## Ανοιχτά", "", "| # | Coin | Αγορά | Τώρα | Κορυφή |", "|---|---|---|---|---|"]
        for p in state["positions"]:
            lines.append(f"| {p['id']} | {p['name']} | {_local(p['entry_time'])} | {p['last_pct']:+.1f}% | {p['peak_pct']:+.1f}% |")
        lines.append("")
    if state["closed"]:
        lines += ["## Τελευταία trades", "", "| # | Coin | Λεπτά | Αποτέλεσμα | Λόγος |", "|---|---|---|---|---|"]
        for t in reversed(state["closed"][-20:]):
            lines.append(f"| {t['id']} | {t['name']} | {t['hold_min']:.0f} | {t['pnl']:+.2f}$ ({t['pnl_pct']:+.0f}%) | {t['reason']} |")
        lines.append("")
    rej = sorted(state["stats"]["rejects"].items(), key=lambda kv: -kv[1])[:8]
    if rej:
        lines += ["## Γιατί απορρίφθηκαν coins", "", "| Λόγος | Φορές |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in rej]
        lines.append("")
    lines += [
        "## Κανόνες",
        "",
        f"- Coins ηλικίας {C.MIN_AGE_MIN}–{C.MAX_AGE_MIN}′, ρευστότητα ≥ ${C.MIN_LIQUIDITY_USD:,}, όγκος 5′ ≥ ${C.MIN_VOLUME_M5_USD:,}, "
        f"≥ {C.MIN_BUYERS_M5} αγοραστές στα 5′, αγορές/πωλήσεις 1 ώρας ≥ {C.MIN_BUY_SELL_RATIO_H1}, άνοδος 5′ {C.MIN_PRICE_CHANGE_M5:.0f}–{C.MAX_PRICE_CHANGE_M5:.0f}%.",
        f"- ${C.POSITION_SIZE_USD:.0f} ανά trade, έως {C.MAX_OPEN_POSITIONS} ανοιχτά, στοπ νέων αγορών αν χάσει ${C.MAX_DAILY_LOSS_USD:.0f} σε μία μέρα.",
        f"- Έξοδος: +{C.TAKE_PROFIT_PCT:.0f}% κέρδος, {C.STOP_LOSS_PCT:.0f}% stop (ανεβαίνει στο 0 μετά από +{C.BREAKEVEN_TRIGGER_PCT:.0f}%), "
        f"μέγιστο {C.MAX_HOLD_MIN}′, ή αν η ρευστότητα πέσει στο μισό.",
        "",
        f"_Ενημερώθηκε αυτόματα: {datetime.now(ZoneInfo(C.TIMEZONE)).strftime('%d/%m/%Y %H:%M')}_",
        "",
    ]
    path.write_text("\n".join(lines))
