"""
Stock universe definitions for NSE.
These are the NSE trading symbols (without exchange prefix).
"""
from __future__ import annotations

# --- Nifty 50 ---
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BPCL", "BHARTIARTL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC",
    "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NTPC", "NESTLEIND", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TECHM", "TITAN", "ULTRACEMCO", "WIPRO", "ZOMATO",
]

# --- Nifty Bank ---
NIFTY_BANK = [
    "AXISBANK", "BANDHANBNK", "FEDERALBNK", "HDFCBANK", "ICICIBANK",
    "IDFCFIRSTB", "INDUSINDBK", "KOTAKBANK", "PNB", "SBIN",
    "AUBANK", "BANKBARODA",
]

# --- Nifty IT ---
NIFTY_IT = [
    "COFORGE", "HCLTECH", "INFY", "LTIM", "MPHASIS",
    "PERSISTENT", "TCS", "TECHM", "WIPRO", "OFSS",
]

# --- Nifty Pharma ---
NIFTY_PHARMA = [
    "ABBOTINDIA", "ALKEM", "AUROPHARMA", "BIOCON", "CIPLA",
    "DIVISLAB", "DRREDDY", "GLAND", "GLAXO", "IPCALAB",
    "LUPIN", "SUNPHARMA", "TORNTPHARM", "ZYDUSLIFE",
]

# --- Nifty FMCG ---
NIFTY_FMCG = [
    "BRITANNIA", "COLPAL", "DABUR", "EMAMILTD", "GODREJCP",
    "HINDUNILVR", "ITC", "MARICO", "NESTLEIND", "PGHH",
    "RADICO", "TATACONSUM", "UBL", "VBL",
]

# --- Nifty Midcap 150 (representative sample — full list via instruments API) ---
NIFTY_MIDCAP_150_SAMPLE = [
    "ABCAPITAL", "AARTIIND", "APLAPOLLO", "APOLLOTYRE", "ASTRAL",
    "BALKRISIND", "BATAINDIA", "BHARATFORG", "CAMS", "CANFINHOME",
    "CESC", "CHOLAFIN", "CROMPTON", "CUMMINSIND", "DEEPAKNTR",
    "DIXON", "ESCORTS", "EXIDEIND", "FLUOROCHEM", "GLENMARK",
    "GODREJIND", "GRINDWELL", "HAL", "ICICIGI", "ICICIPRULI",
    "INDHOTEL", "ISEC", "JKCEMENT", "JUBLFOOD", "KAJARIACER",
    "KANSAINER", "LICHSGFIN", "LINDEINDIA", "LALPATHLAB", "LTTS",
    "MFSL", "MAXHEALTH", "METROPOLIS", "MOTILALOFS", "MRF",
    "NATIONALUM", "NHPC", "NMDC", "OBEROIRLTY", "OFSS",
    "PAGEIND", "PERSISTENT", "PFIZER", "PIIND", "POLYCAB",
    "RBLBANK", "SAIL", "SCHAEFFLER", "SOLARINDS", "SONACOMS",
    "SUNDARMFIN", "SUNDRMFAST", "SUNTV", "SUPRAJIT", "SUPREMEIND",
    "SUZLON", "TATACOMM", "TIINDIA", "TORNTPOWER", "TRENT",
    "TTKPRESTIG", "VGUARD", "VOLTAS", "WHIRLPOOL", "ZEEL",
]

# --- Nifty Smallcap 250 (sample — apply extra liquidity filters) ---
NIFTY_SMALLCAP_250_SAMPLE = [
    "AAVAS", "ACRYSIL", "AMARAJABAT", "ANGELONE", "APTUS",
    "ARVINDFASN", "ASAHIINDIA", "ASKAUTOLTD", "ATGL", "AVANTIFEED",
    "BAJAJHCARE", "BALRAMCHIN", "BASF", "BBL", "BFDL",
    "BLKASHYAP", "BOROLTD", "BSOFT", "CANOFINANCE", "CCL",
    "CENTURYPLY", "CHALET", "CLEAN", "CMSINFO", "COROMANDEL",
    "CRAFTSMAN", "DCB", "DELTACORP", "DFMFOODS", "EMCURE",
    "EPIGRAL", "ESTER", "ETHOS", "FINEORG", "FINPIPE",
    "GALAXYSURF", "GARFIBRES", "GHCL", "GLOBALHEALTH", "GPPL",
    "GREENPLY", "GRSE", "GULFOILLUB", "HATSUN", "HAWKINCOOK",
    "HERITGFOOD", "HEG", "HINDWAREAP", "HOMEFIRST", "IBREALEST",
]

# Sector map for exposure tracking
SECTOR_MAP: dict[str, list[str]] = {
    "BANKING": NIFTY_BANK,
    "IT": NIFTY_IT,
    "PHARMA": NIFTY_PHARMA,
    "FMCG": NIFTY_FMCG,
}


def get_universe(name: str) -> list[str]:
    """Return stock list by universe name."""
    mapping = {
        "nifty50": NIFTY50,
        "nifty_bank": NIFTY_BANK,
        "nifty_it": NIFTY_IT,
        "nifty_pharma": NIFTY_PHARMA,
        "nifty_fmcg": NIFTY_FMCG,
        "midcap150": NIFTY_MIDCAP_150_SAMPLE,
        "smallcap250": NIFTY_SMALLCAP_250_SAMPLE,
    }
    return mapping.get(name.lower(), [])


def get_all_symbols() -> list[str]:
    """Return deduplicated list of all tracked symbols."""
    all_symbols: set[str] = set()
    all_symbols.update(NIFTY50)
    all_symbols.update(NIFTY_BANK)
    all_symbols.update(NIFTY_IT)
    all_symbols.update(NIFTY_PHARMA)
    all_symbols.update(NIFTY_FMCG)
    all_symbols.update(NIFTY_MIDCAP_150_SAMPLE)
    all_symbols.update(NIFTY_SMALLCAP_250_SAMPLE)
    return sorted(all_symbols)


def get_sector_for_symbol(symbol: str) -> str | None:
    """Return the sector for a given symbol, if known."""
    for sector, symbols in SECTOR_MAP.items():
        if symbol in symbols:
            return sector
    return None
