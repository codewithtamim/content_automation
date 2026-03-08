#!/usr/bin/env python3
"""Clear all video jobs. Run with app STOPPED."""
import sys

# Add project root
sys.path.insert(0, ".")

from config import get_settings
from sqlalchemy import create_engine, text

def main():
    settings = get_settings()
    engine = create_engine(
        settings.database_url,
        connect_args={"timeout": 60} if "sqlite" in settings.database_url else {},
    )
    with engine.connect() as conn:
        result = conn.execute(text("DELETE FROM video_jobs"))
        conn.commit()
        print(f"Deleted {result.rowcount} rows")
    print("Done.")

if __name__ == "__main__":
    main()
