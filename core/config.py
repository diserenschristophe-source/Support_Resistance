"""
Central configuration for the S/R analysis engine.
====================================================
All tuneable parameters in one place.
"""

# ── Multi-Window Strategy ────────────────────────────────────
WINDOWS = [
    {"days": 20,  "label": "short",  "weight": 1.5},
    {"days": 60,  "label": "medium", "weight": 1.0},
    {"days": 180, "label": "long",   "weight": 0.7},
]

# ── SR Analysis ──────────────────────────────────────────────
ZONE_ATR_MULT = 0.2
MAX_ZONES_PER_SIDE = 3
MIN_STRENGTH = 0.10
MAX_DISTANCE_PCT = 30.0
MERGE_THRESHOLD_PCT = 0.02    # 2% for merging nearby zones
DEDUP_THRESHOLD_PCT = 0.02    # 2% for cross-window dedup

# ── Market Structure Detector ────────────────────────────────
MS_MIN_SWING_ATR = 1.0        # minimum swing size in ATR multiples
MS_BREAK_ATR_MULT = 0.25      # body must close beyond level by this * ATR to count as break
MS_MAX_BREAKS = 3.0            # cumulative (volume-weighted) breaks before removal
MS_CONSEC_KILL = 2             # consecutive body closes that kill a level
MS_RECENCY_HALFLIFE = 30       # exponential decay halflife in bars
MS_MAX_VOLUME_MULT = 2.0       # cap relative volume at 2x average
MS_FLIP_THRESHOLD = 2.0        # break count to emit flipped polarity level
MS_BREAK_PENALTY_SLOPE = 0.3   # linear penalty per break (1.0 → 0.7 → 0.4 → 0.1)
MS_MIN_PENALTY_FLOOR = 0.1     # minimum penalty floor

# Strength tiers for market structure levels
MS_STRENGTH_CHOCH = 0.85       # base strength for CHOCH points
MS_STRENGTH_HL_LH = 0.65       # base strength for HL/LH swings
MS_STRENGTH_DEFAULT = 0.45     # base strength for other swings
MS_STRENGTH_FLIPPED = 0.30     # base strength for flipped polarity levels
MS_WICK_MODIFIER = 0.85        # wick swings get this fraction of body strength
MS_WEIGHT_BASE = 0.7           # weight for base strength in final calc
MS_WEIGHT_RECENCY = 0.3        # weight for recency in final calc

# ── Ensemble Merge ───────────────────────────────────────────
MERGE_DISTANCE_PCT = 0.5       # % for merging raw levels
BODY_WEIGHT_BONUS = 1.5        # body levels get 1.5x weight in strength-weighted merge
MULTI_METHOD_BONUS_PER = 0.10  # bonus per additional detection method
MULTI_METHOD_BONUS_CAP = 0.30  # max multi-method bonus
POLARITY_FLIP_BONUS = 0.10     # strength bonus when polarity_flip confirms

METHOD_WEIGHTS = {
    "market_structure": 0.40,
    "volume_profile": 0.10,
    "touch_count": 0.25,
    "nison_body": 0.15,
    "polarity_flip": 0.10,
}

BODY_METHODS = {"market_structure", "nison_body", "polarity_flip"}
WICK_METHODS = {"touch_count"}
BLENDED_METHODS = {"volume_profile"}

# ── Zone Scoring (used in merge and ranking) ─────────────────
ZONE_SCORE_CONFLUENCE_MULT = 10  # confluence_score * this
ZONE_SCORE_VOL_BONUS = 5         # bonus if volume confirmed
ZONE_SCORE_FLIP_BONUS = 10       # bonus if polarity flip
ZONE_SCORE_CHOCH_BONUS = 8       # bonus if CHOCH

# ── Zone Ranking ─────────────────────────────────────────────
RANK_PROXIMITY_WEIGHT = 0.80     # weight for proximity in ranking
RANK_QUALITY_WEIGHT = 0.20       # weight for quality in ranking
RANK_MAJOR_SCORE = 0.30          # quality score for Major tier
RANK_FLIP_SCORE = 0.20           # quality score for flip
RANK_CHOCH_SCORE = 0.10          # quality score for CHOCH
RANK_VOLUME_SCORE = 0.10         # quality score for volume confirmed
RANK_CONFLUENCE_MAX = 0.15       # max quality score for confluence

# ── Body Anchor Snapping ─────────────────────────────────────
ANCHOR_BODY_THRESHOLD = 0.3      # min body size as ATR mult to be an anchor
ANCHOR_SNAP_TOLERANCE = 0.6      # snap distance in ATR units
ANCHOR_ROUND_SNAP_PCT = 0.003    # tolerance for snapping to round numbers (0.3%)

# ── Volume Confirmation ──────────────────────────────────────
VOLUME_CHECK_BAND = 0.5          # ATR band around level for volume check
VOLUME_MIN_TOUCHES = 2           # min touches to check volume
VOLUME_CONFIRM_MULT = 1.2        # level confirmed if avg vol > this * overall avg

# ── Tier Classification ──────────────────────────────────────
TIER_STRENGTH_THRESHOLD = 0.5    # strength threshold for Major
TIER_TOUCHES_THRESHOLD = 25      # touch count threshold for Major
TIER_BIAS_THRESHOLD = 0.5        # bias threshold for bias-based promotion
TIER_BIAS_TOUCHES = 10           # touch threshold for bias-based promotion

# ── SMA / POC Injection ──────────────────────────────────────
SMA_PERIODS = [50, 100, 200]     # SMA periods for injection
SMA_MAX_DISTANCE = 0.20          # max distance from price (20%)
SMA_MIN_DISTANCE = 0.01          # min distance from price (1%)
SMA_DEDUP_TOLERANCE = 0.03       # tolerance for checking if SMA already exists
SMA_INJECTED_STRENGTH = 0.35     # strength for injected SMA levels
POC_DEDUP_TOLERANCE = 0.02       # tolerance for checking if POC already exists
POC_GUARANTEE_TOLERANCE = 0.03   # tolerance for post-ranking POC guarantee
POC_INJECTED_STRENGTH = 0.45     # strength for injected POC
POC_VOLUME_WEIGHT = 0.9          # volume weight for injected POC

# ── Fibonacci ────────────────────────────────────────────────
FIB_MATCH_TOLERANCE = 0.03       # tolerance for matching zones to Fib levels

# ── Backfill ─────────────────────────────────────────────────
BACKFILL_MAX_LEVELS = 30         # max raw levels to pull
BACKFILL_MIN_STRENGTH = 0.05     # min strength for backfill candidates
BACKFILL_WEAKNESS_FLOOR = 0.10   # min strength unless high touch count
BACKFILL_MIN_TOUCHES_WEAK = 3    # min touches to include weak levels
BACKFILL_MAX_DISTANCE = 0.50     # max distance from price (50%)

# ── Detector Defaults per Window ─────────────────────────────
DETECTOR_CONFIG_SHORT = {
    "market_structure": {"swing_window": 3, "recency_halflife": 15},
    "volume": {"num_bins": 80, "value_area_pct": 0.70, "hvn_threshold_percentile": 65},
    "touch": {"window_sizes": [3, 5, 10], "body_tolerance_pct": 0.5,
              "wick_tolerance_pct": 1.0, "min_weighted_touches": 1.0,
              "recency_halflife": 7},
    "nison": {"atr_multiplier": 1.0, "recency_halflife": 10},
    "polarity": {"tolerance_atr_mult": 0.5, "min_touches_per_side": 2},
}

DETECTOR_CONFIG_MEDIUM = {
    "market_structure": {"swing_window": 5, "recency_halflife": 30},
    "volume": {"num_bins": 120, "value_area_pct": 0.70, "hvn_threshold_percentile": 70},
    "touch": {"window_sizes": [5, 10, 20], "body_tolerance_pct": 0.5,
              "wick_tolerance_pct": 1.0, "min_weighted_touches": 1.5,
              "recency_halflife": 20},
    "nison": {"atr_multiplier": 1.0, "recency_halflife": 20},
    "polarity": {"tolerance_atr_mult": 0.5, "min_touches_per_side": 2},
}

DETECTOR_CONFIG_LONG = {
    "market_structure": {"swing_window": 5, "recency_halflife": 45},
    "volume": {"num_bins": 150, "value_area_pct": 0.70, "hvn_threshold_percentile": 70},
    "touch": {"window_sizes": [5, 10, 20, 50], "body_tolerance_pct": 0.5,
              "wick_tolerance_pct": 1.0, "min_weighted_touches": 1.5,
              "recency_halflife": 60},
    "nison": {"atr_multiplier": 1.0, "recency_halflife": 40},
    "polarity": {"tolerance_atr_mult": 0.5, "min_touches_per_side": 2},
}

def get_detector_config(days: int) -> dict:
    """Return detector config for a given window size."""
    if days <= 30:
        return DETECTOR_CONFIG_SHORT
    elif days <= 90:
        return DETECTOR_CONFIG_MEDIUM
    else:
        return DETECTOR_CONFIG_LONG

# ── TPSL ─────────────────────────────────────────────────────

# ── Data Fetching ────────────────────────────────────────────
DEFAULT_DAYS = 180
API_RATE_LIMIT_DELAY = 0.2    # seconds between API calls
MAX_INCREMENTAL_DAYS = 30     # beyond this, full re-download

# ── Symbol Mappings ──────────────────────────────────────────
BINANCE_SYMBOL_MAP = {
    # Top 20
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT",
    "XRP": "XRPUSDT", "SOL": "SOLUSDT", "TRX": "TRXUSDT",
    "DOGE": "DOGEUSDT", "ADA": "ADAUSDT", "LINK": "LINKUSDT",
    "XLM": "XLMUSDT", "LTC": "LTCUSDT", "HBAR": "HBARUSDT",
    "AVAX": "AVAXUSDT", "SUI": "SUIUSDT", "SHIB": "SHIBUSDT",
    "TON": "TONUSDT", "DOT": "DOTUSDT", "BCH": "BCHUSDT",
    "XMR": "XMRUSDT", "ZEC": "ZECUSDT",
    # 21-50
    "TAO": "TAOUSDT", "PAXG": "PAXGUSDT", "UNI": "UNIUSDT",
    "NEAR": "NEARUSDT", "AAVE": "AAVEUSDT", "PEPE": "PEPEUSDT",
    "ICP": "ICPUSDT", "ETC": "ETCUSDT", "ONDO": "ONDOUSDT",
    "WLD": "WLDUSDT", "POL": "POLUSDT", "QNT": "QNTUSDT",
    "ATOM": "ATOMUSDT", "ENA": "ENAUSDT", "RENDER": "RENDERUSDT",
    "FET": "FETUSDT", "TRUMP": "TRUMPUSDT", "ALGO": "ALGOUSDT",
    "APT": "APTUSDT", "FIL": "FILUSDT",
    # 51-80
    "VET": "VETUSDT", "ARB": "ARBUSDT", "JUP": "JUPUSDT",
    "BONK": "BONKUSDT", "STX": "STXUSDT", "SEI": "SEIUSDT",
    "ZRO": "ZROUSDT", "ETHFI": "ETHFIUSDT", "MORPHO": "MORPHOUSDT",
    "CAKE": "CAKEUSDT", "PENGU": "PENGUUSDT", "DCR": "DCRUSDT",
    "JST": "JSTUSDT", "VIRTUAL": "VIRTUALUSDT", "NEXO": "NEXOUSDT",
    "PUMP": "PUMPUSDT", "SKY": "SKYUSDT", "KAS": "KASUSDT", "MNT": "MNTUSDT",
    # Legacy / less common
    "OP": "OPUSDT", "MATIC": "MATICUSDT", "HYPE": "HYPEUSDT",
    "BORG": "BORGUSDT", "ASTER": "ASTERUSDT",
    "FLOKI": "FLOKIUSDT", "INJ": "INJUSDT", "IMX": "IMXUSDT",
    "MKR": "MKRUSDT", "GRT": "GRTUSDT", "TIA": "TIAUSDT",
    "WIF": "WIFUSDT", "DYDX": "DYDXUSDT", "PENDLE": "PENDLEUSDT",
    "ENS": "ENSUSDT", "LDO": "LDOUSDT", "CRV": "CRVUSDT",
    "COMP": "COMPUSDT", "SNX": "SNXUSDT", "RUNE": "RUNEUSDT",
    "EGLD": "EGLDUSDT", "THETA": "THETAUSDT", "IOTA": "IOTAUSDT",
    "EOS": "EOSUSDT", "NEO": "NEOUSDT",
}

GECKOTERMINAL_POOL_MAP = {
    "BORG": ("solana", "Ab5pqdTEw1McsizEaQfLEyMLhkfxwzrpyqFASpftQcpq"),
}

COINGECKO_ID_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "SUI": "sui",
    "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche-2",
    "DOT": "polkadot", "LINK": "chainlink", "MATIC": "matic-network",
    "UNI": "uniswap", "AAVE": "aave", "OP": "optimism",
    "ARB": "arbitrum", "HYPE": "hyperliquid", "PAXG": "pax-gold",
    "NEAR": "near", "FET": "artificial-superintelligence-alliance",
    "RENDER": "render-token", "TAO": "bittensor", "LTC": "litecoin",
    "BORG": "swissborg",
}

# Tokens excluded from auto-discovery (stablecoins, wrapped, RWA, etc.)
EXCLUDE_SYMBOLS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDD", "PYUSD",
    "USDS", "USD1", "RLUSD", "USDG", "USDF", "USDY", "USD0", "USDTB",
    "GHO", "EURC", "EUTBL", "BFUSD", "USTB", "YLDS", "STABLE",
    "WBTC", "WETH", "STETH", "WSTETH", "CBBTC", "CBETH", "RETH",
    "LBTC", "BETH", "WBETH", "TBTC", "SUSDE", "USDE", "WEETH", "BSDETH",
    "BUIDL", "USYC", "OUSG", "JTRSY", "JAAA", "HASH",
    "LEO", "OKB", "CRO", "GT", "KCS", "HT", "MX", "BGB", "HTX", "FTN", "WBT",
    "FIGR_HELOC", "CC", "RAIN", "M", "SIREN", "RIVER", "A7A5", "NIGHT",
    "WLFI",
}

COINGECKO_SYMBOL_FIX = {
    "miota": "IOTA",
    "matic-network": "MATIC",
}

# Tokens always included regardless of market cap rank
ALWAYS_INCLUDE = {"BORG"}

# ── Token Tier Lists (canonical source) ───────��──────────────
TOP_3 = ["BTC", "ETH", "SOL"]

SELECTED = [
    "BTC", "ETH", "SOL", "HYPE", "BNB", "TAO", "DOGE", "NEAR", "SUI", "LINK",
]

TOP_20 = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "TRX", "DOGE", "HYPE", "ADA", "LINK",
    "XLM", "LTC", "HBAR", "AVAX", "SHIB", "TAO", "SUI", "TON", "PAXG", "MNT",
]

ALL = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "TRX", "DOGE", "HYPE", "ADA", "LINK",
    "XLM", "LTC", "HBAR", "AVAX", "SHIB", "TAO", "SUI", "TON", "PAXG", "MNT",
    "UNI", "DOT", "SKY", "ASTER", "NEAR", "AAVE", "PEPE", "ONDO", "ICP", "POL",
    "KAS", "RENDER", "WLD", "QNT", "ATOM", "FIL", "ARB", "FET", "APT", "TRUMP",
    "ALGO", "INJ", "ENA", "VET", "BONK", "SEI", "STX", "JUP", "FLOKI", "MKR",
    "OP", "BORG",
]

TOKEN_TIERS = {
    "top_3": TOP_3,
    "selected": SELECTED,
    "top_20": TOP_20,
    "all": ALL,
}

# Convenience sets for membership tests
TOP_3_SET = set(TOP_3)
SELECTED_SET = set(SELECTED)
TOP_20_SET = set(TOP_20)
ALL_SET = set(ALL)


def get_tier(symbol: str) -> str:
    """Return the most specific tier a token belongs to."""
    if symbol in TOP_3_SET:
        return "top_3"
    if symbol in SELECTED_SET:
        return "selected"
    if symbol in TOP_20_SET:
        return "top_20"
    if symbol in ALL_SET:
        return "all"
    return "unknown"


# Backward compat alias (used by fetcher.py fallback)
FALLBACK_TOP_50 = ALL

# ── LLM Report Generation ───────────────────────────────────
LLM_MODEL = "claude-sonnet-4-20250514"
LLM_MAX_TOKENS = 1500
LLM_RETRY_ATTEMPTS = 3
LLM_RETRY_DELAY = 5
