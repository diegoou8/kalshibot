import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from root
env_path = Path(__file__).resolve().parent.parent.parent.parent / '.env'
load_dotenv(env_path)

class Config:
    # Kalshi Demo Credentials
    KALSHI_DEMO_KEY_ID = os.getenv("KALSHI_DEMO_KEY_ID", "44e84c19-3ea5-4b91-aaeb-b97bae8ab615")
    
    # Path to your Demo Key
    _default_demo_key_path = Path(__file__).resolve().parent.parent.parent / "Credentials" / "DiegoDemoKey.txt"
    KALSHI_DEMO_KEY_FILE_PATH = _default_demo_key_path

    # Execution mode: PAPER (demo API) or LIVE (live API).
    # Always uppercase so DB CHECK constraints and comparisons are consistent.
    ENV_EXECUTION_MODE: str = os.getenv("ENV_EXECUTION_MODE", "PAPER").upper()

    # Set base URL based on mode — PAPER hits the demo sandbox
    BASE_URL = (
        "https://trading-api.kalshi.co/trade-api/v2"
        if os.getenv("ENV_EXECUTION_MODE", "PAPER").upper() == "LIVE"
        else "https://demo-api.kalshi.co/trade-api/v2"
    )
    
    # Capital Limits
    BANKROLL = float(os.getenv("BANKROLL", "2500.0"))
    MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "50.0"))
    DAILY_VOLUME_LIMIT = float(os.getenv("DAILY_VOLUME_LIMIT", "5000.0"))
    DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", "250.0"))

    # City Coordinates for Weather Ingestion
    CITY_COORDS = {
        "NEW YORK": (40.7128, -74.0060), "NYC": (40.7128, -74.0060),
        "AUSTIN": (30.2672, -97.7431), "CHICAGO": (41.8781, -87.6298),
        "MIAMI": (25.7617, -80.1918), "LOS ANGELES": (34.0522, -118.2437),
        "SEATTLE": (47.6062, -122.3321), "DALLAS": (32.7767, -96.7970),
        "HOUSTON": (29.7604, -95.3698), "BOSTON": (42.3601, -71.0589),
        "SAN FRANCISCO": (37.7749, -122.4194), "DENVER": (39.7392, -104.9903),
        "PHOENIX": (33.4484, -112.0740), "ATLANTA": (33.7490, -84.3880),
        "WASHINGTON DC": (38.9072, -77.0369), "PHILADELPHIA": (39.9526, -75.1652),
        "SAN ANTONIO": (29.4241, -98.4936), "MINNEAPOLIS": (44.9778, -93.2650),
        "OKLAHOMA CITY": (35.4676, -97.5164)
    }

    # Adding a simple method to get the private key content
    @classmethod
    def get_private_key_content(cls) -> str:
        try:
            with open(cls.KALSHI_DEMO_KEY_FILE_PATH, 'r') as f:
                return f.read().strip()
        except FileNotFoundError:
            print(f"Key file not found at: {cls.KALSHI_DEMO_KEY_FILE_PATH}")
            return ""
