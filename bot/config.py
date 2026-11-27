"""Ρυθμίσεις του paper bot. Όλα εικονικά — κανένα πραγματικό χρήμα, κανένα πορτοφόλι."""

# --- Κεφάλαιο & ρίσκο ---
START_BALANCE_USD = 100.0       # εικονικό αρχικό κεφάλαιο
POSITION_SIZE_USD = 10.0        # πόσα "βάζει" σε κάθε trade
MAX_OPEN_POSITIONS = 4
MAX_DAILY_LOSS_USD = 1_000_000.0  # paper: χωρίς στοπ ημέρας, για να φανεί καθαρά αν δουλεύει η στρατηγική (το live έχει δικό του όριο)
MAX_ENTRIES_PER_DAY = 150

# --- Ρεαλιστικά κόστη (για να μην "κερδίζει" στα χαρτιά μόνο) ---
SLIPPAGE_PER_SIDE = 0.025       # 2.5% χειρότερη τιμή σε κάθε αγορά/πώληση
FEE_USD_PER_TX = 0.03           # network/priority fee ανά συναλλαγή

# --- Φίλτρα εισόδου: ΚΑΘΙΕΡΩΜΕΝΑ meme coins (>1 μέρα) με ορμή — τα φρέσκα coins κάνουν τα περισσότερα rugs ---
MIN_AGE_MIN = 24 * 60           # τουλάχιστον 1 μέρα ζωής
MAX_AGE_MIN = 365 * 24 * 60     # έως 1 χρόνο
MIN_LIQUIDITY_USD = 25_000
MIN_VOLUME_M5_USD = 1_000
MIN_FDV_USD = 500_000
MAX_FDV_USD = 300_000_000
MIN_BUYERS_M5 = 20
MIN_BUY_SELL_RATIO_H1 = 1.1     # αγορές/πωλήσεις τελευταίας ώρας
MIN_PRICE_CHANGE_M5 = 1.0       # % — θέλουμε άνοδο...
MAX_PRICE_CHANGE_M5 = 10.0      # ...αλλά όχι κάθετη (συνήθως εκεί πουλάνε οι πρώτοι)
MIN_PRICE_CHANGE_H1 = 0.0       # ανοδική τάση την τελευταία ώρα
MAX_PRICE_CHANGE_H1 = 40.0      # ...αλλά όχι "τρελαμένο"

# --- Έξοδος ---
TAKE_PROFIT_PCT = 20.0
STOP_LOSS_PCT = -10.0
BREAKEVEN_TRIGGER_PCT = 8.0     # αν φτάσει +8%, το stop ανεβαίνει στο 0%
MAX_HOLD_MIN = 120
SOURCE_TOLERANCE = 0.3          # δύο πηγές τιμής "συμφωνούν" αν διαφέρουν λιγότερο από 30%
MAX_JUMP = 4.0                 # αλλαγή τιμής >4x σε έναν γύρο θέλει επιβεβαίωση (προστασία από λάθη API)
LIQUIDITY_DROP_EXIT = 0.5      # αν η ρευστότητα πέσει κάτω από 50% της αρχικής → έξοδος (πιθανό rug)

# --- Λειτουργία ---
POLL_SECONDS = 60
NEW_POOL_PAGES = 0               # δεν χρειαζόμαστε πια τα ολοκαίνουργια pools
TRENDING_SOURCES = [("5m", 1), ("5m", 2), ("5m", 3), ("1h", 1), ("1h", 2), ("6h", 1)]
TOP_VOLUME_PAGES = 10           # + τα pools με τον μεγαλύτερο όγκο 24ώρου (~200 καθιερωμένα coins)
TOP_TX_PAGES = 5                # + τα pools με τις περισσότερες συναλλαγές 24ώρου (~100)
REENTRY_HOURS = 2               # μετά από trade, το ίδιο pool μπορεί να ξαναγοραστεί μετά από 2 ώρες (τα rugs ποτέ)
WATCH_MIN_LIQ_FRACTION = 0.4    # μπαίνει στη λίστα παρακολούθησης αν έχει ήδη ≥40% της ελάχιστης ρευστότητας
WATCH_MAX_FETCH = 60            # πόσα από τη λίστα ξαναελέγχονται ανά γύρο
TIMEZONE = "Europe/Athens"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}


# --- Live (πραγματικά χρήματα): μικρότερα όρια, ενεργό μόνο όταν control.json λέει "on" ---
LIVE_OVERRIDES = {
    "POSITION_SIZE_USD": 4.0,
    "MAX_OPEN_POSITIONS": 3,
    "MAX_DAILY_LOSS_USD": 8.0,
    "MAX_ENTRIES_PER_DAY": 80,
    "SLIPPAGE_PER_SIDE": 0.01,   # μόνο για την εκτίμηση αξίας ανοιχτών θέσεων· τα πραγματικά ποσά έρχονται από το blockchain
    "FEE_USD_PER_TX": 0.0,
}

# --- Αυτόματη μεταφορά κερδών στο Revolut ---
PAYOUT_ADDRESS = "83q2aSN1mSkdDpq68P54uGHuYXWeK9zq1Lm5c4HHym6i"   # διεύθυνση SOL του Revolut (μόνο εδώ στέλνει)
PAYOUT_KEEP_USD = 30.0          # τόσα μένουν πάντα στο bot
PAYOUT_TRIGGER_USD = 20.0       # μεταφορά όταν το κέρδος πάνω από τα 30$ φτάσει τα 20$
PAYOUT_MIN_HOURS = 12           # το πολύ μία μεταφορά ανά 12 ώρες

# Έλεγχος rug πριν από κάθε αγορά (RugCheck) — ίδιος σε paper και live
RUGCHECK = True
MAX_TOP10_PCT = 35.0       # οι 10 μεγαλύτεροι κάτοχοι (χωρίς pools/lockers) κάτω από 35%
MIN_LP_LOCKED_PCT = 90.0   # η ρευστότητα του pool κλειδωμένη/καμένη τουλάχιστον 90%

COPY_BOT = False  # 28/11: σταμάτησε — 3 trades σε 4 μέρες, 0 κερδισμένα (97,24$)

# ---------------- ΕΡΓΑΣΤΗΡΙΟ: στρατηγικές που τρέχουν παράλληλα (paper, 100$ η καθεμία) ----------------
# Η "momentum" είναι το κύριο paper (docs/state.json). Οι υπόλοιπες αλλάζουν μόνο όσα γράφονται εδώ.
LAB_EPOCH = "2026-11-24-lab"
LAB = {
    # 26/11: «trench» από βίντεο TikTok (@nabiltalks). ΜΟΝΟ paper — εξαιρείται ο κανόνας της 1 μέρας.
    # Coins που μόλις «αποφοίτησαν» από pump.fun → PumpSwap, με τα φίλτρα του: top-10 <30%, όγκος > market cap,
    # 100+ κάτοχοι, ενδιαφέρον (αγοραστές). Γρήγορη έξοδος +50% / −25% / 1 ώρα. Κόστη 4% ανά πλευρά (μικρά pools).
    "trench": {"label": "Trench (pump.fun)", "TRENCH_SOURCE": True, "SEPARATE": True,  # δικός του γρήγορος runner (trench.yml)
               "MIN_AGE_MIN": 3, "MAX_AGE_MIN": 6 * 60, "MIN_LIQUIDITY_USD": 10_000,
               "MIN_FDV_USD": 30_000, "MAX_FDV_USD": 5_000_000, "MIN_VOL_TO_FDV": 1.0,
               "MIN_VOLUME_M5_USD": 1_000, "MIN_BUYERS_M5": 15,
               "MIN_BUY_SELL_RATIO_H1": 1.0, "MIN_BUY_SELL_RATIO_M5": 1.0,
               "MIN_PRICE_CHANGE_M5": 0.0, "MAX_PRICE_CHANGE_M5": 40.0,
               "MIN_PRICE_CHANGE_H1": -20.0, "MAX_PRICE_CHANGE_H1": 300.0,
               "MAX_TOP10_PCT": 30.0, "MIN_HOLDERS": 100,
               "TAKE_PROFIT_PCT": 50.0, "STOP_LOSS_PCT": -25.0, "BREAKEVEN_TRIGGER_PCT": 1e9, "MAX_HOLD_MIN": 60,
               "SLIPPAGE_PER_SIDE": 0.04},
}

# 26/11: scalp & spike σταμάτησαν — στο backtest (87 meme coins, 10 μέρες, ~1.150 trades) δεν ήταν καλύτερα
# από τυχαίες αγορές (−2,6% έως −2,8% ανά trade με 1% κόστος). Κρατιούνται εδώ μόνο για αναφορά/backtest.
LAB_RETIRED = {
    # 26/11 βράδυ: trail & dip σταμάτησαν — στο backtest (87 coins, 10 μέρες) trail = ίδιο με τυχαίες αγορές
    # (−2,9%/trade), dip αρνητικό σε μεγαλύτερο δείγμα και καμία από 300 παραλλαγές δεν πέρασε τον τελικό έλεγχο.
    "trail": {"label": "Trailing", "TAKE_PROFIT_PCT": 1e9, "STOP_LOSS_PCT": -10.0, "TRAIL_PCT": 8.0,
              "BREAKEVEN_TRIGGER_PCT": 1e9, "MAX_HOLD_MIN": 240},
    "dip": {"label": "Αγορά στη βουτιά", "MIN_LIQUIDITY_USD": 50_000, "MIN_PRICE_CHANGE_H1": -30.0,
            "MAX_PRICE_CHANGE_H1": -12.0, "MIN_PRICE_CHANGE_M5": 0.5, "MAX_PRICE_CHANGE_M5": 8.0,
            "MIN_BUY_SELL_RATIO_H1": 0.0, "MIN_BUY_SELL_RATIO_M5": 1.2, "MIN_BUYERS_M5": 15,
            "TAKE_PROFIT_PCT": 15.0, "STOP_LOSS_PCT": -10.0, "MAX_HOLD_MIN": 120},

    "scalp": {"label": "Γρήγορο scalp", "TAKE_PROFIT_PCT": 8.0, "STOP_LOSS_PCT": -5.0, "MAX_HOLD_MIN": 30,
              "BREAKEVEN_TRIGGER_PCT": 4.0},
    "spike": {"label": "Έκρηξη όγκου", "MIN_VOL_SPIKE": 3.0, "MIN_BUY_SELL_RATIO_M5": 1.3,
              "MIN_BUY_SELL_RATIO_H1": 0.0, "MIN_PRICE_CHANGE_M5": 0.5, "MAX_PRICE_CHANGE_M5": 8.0,
              "MIN_PRICE_CHANGE_H1": -10.0, "MAX_PRICE_CHANGE_H1": 30.0,
              "TAKE_PROFIT_PCT": 15.0, "STOP_LOSS_PCT": -8.0, "BREAKEVEN_TRIGGER_PCT": 7.0, "MAX_HOLD_MIN": 60},
}

MOMENTUM_ON = False   # 25/11: το momentum σταμάτησε νέες αγορές (89,20$ μετά από 18 trades) — τη θέση του παίρνει το "spike"
