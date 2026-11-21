"""Ρυθμίσεις του paper bot. Όλα εικονικά — κανένα πραγματικό χρήμα, κανένα πορτοφόλι."""

# --- Κεφάλαιο & ρίσκο ---
START_BALANCE_USD = 70.0        # εικονικό αρχικό κεφάλαιο
POSITION_SIZE_USD = 10.0        # πόσα "βάζει" σε κάθε trade
MAX_OPEN_POSITIONS = 4
MAX_DAILY_LOSS_USD = 15.0       # αν χάσει τόσα σε μία μέρα (ώρα Ελλάδας), σταματά νέες αγορές ως αύριο
MAX_ENTRIES_PER_DAY = 25

# --- Ρεαλιστικά κόστη (για να μην "κερδίζει" στα χαρτιά μόνο) ---
SLIPPAGE_PER_SIDE = 0.025       # 2.5% χειρότερη τιμή σε κάθε αγορά/πώληση
FEE_USD_PER_TX = 0.03           # network/priority fee ανά συναλλαγή

# --- Φίλτρα εισόδου (coin 10-120 λεπτών) ---
MIN_AGE_MIN = 10
MAX_AGE_MIN = 120
MIN_LIQUIDITY_USD = 15_000
MIN_VOLUME_M5_USD = 4_000
MIN_FDV_USD = 40_000
MAX_FDV_USD = 5_000_000
MIN_BUYERS_M5 = 15
MIN_BUY_SELL_RATIO_H1 = 1.25    # αγορές/πωλήσεις τελευταίας ώρας
MIN_PRICE_CHANGE_M5 = 2.0       # % — θέλουμε άνοδο...
MAX_PRICE_CHANGE_M5 = 25.0      # ...αλλά όχι κάθετη (συνήθως εκεί πουλάνε οι πρώτοι)
MAX_PRICE_CHANGE_H1 = 300.0

# --- Έξοδος ---
TAKE_PROFIT_PCT = 30.0
STOP_LOSS_PCT = -15.0
BREAKEVEN_TRIGGER_PCT = 15.0    # αν φτάσει +15%, το stop ανεβαίνει στο 0%
MAX_HOLD_MIN = 60
MAX_JUMP = 4.0                  # αλλαγή τιμής >4x σε έναν γύρο θέλει επιβεβαίωση (προστασία από λάθη API)
LIQUIDITY_DROP_EXIT = 0.5      # αν η ρευστότητα πέσει κάτω από 50% της αρχικής → έξοδος (πιθανό rug)

# --- Λειτουργία ---
POLL_SECONDS = 60
NEW_POOL_PAGES = 2
TRENDING_SOURCES = [("5m", 1), ("5m", 2), ("1h", 1)]
WATCH_MIN_LIQ_FRACTION = 0.4    # μπαίνει στη λίστα παρακολούθησης αν έχει ήδη ≥40% της ελάχιστης ρευστότητας
WATCH_MAX_FETCH = 60            # πόσα από τη λίστα ξαναελέγχονται ανά γύρο
TIMEZONE = "Europe/Athens"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}
