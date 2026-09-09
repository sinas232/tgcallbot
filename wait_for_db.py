import asyncio
import os
import asyncpg
import time
import sys

async def wait_for_db():
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    db_name = os.getenv("POSTGRES_DB", "telegram_bot")
    host = os.getenv("POSTGRES_HOST", "db")
    port = os.getenv("POSTGRES_PORT", "5432")

    dsn = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
    
    print(f"🔄 Checking database connection at {host}:{port}...")
    
    retries = 30
    delay = 2

    for i in range(retries):
        try:
            conn = await asyncpg.connect(dsn)
            await conn.close()
            print("✅ Database is ready! Starting bot...")
            return
        except Exception as e:
            print(f"⏳ Database not ready yet ({i+1}/{retries}): {e}")
            await asyncio.sleep(delay)
            
    print("🚨 Could not connect to database. Exiting.")
    sys.exit(1)

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(wait_for_db())
    except Exception as e:
        print(f"Error in wait script: {e}")
        # Fallback to exit 0 to let main script try, or 1 to fail hard.
        # Let's fail hard to prevent loop crash.
        sys.exit(1)