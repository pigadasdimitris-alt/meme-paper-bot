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
MIN_LIQUIDITY_USD = 50_000
MIN_VOLUME_M5_USD = 2_000
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
