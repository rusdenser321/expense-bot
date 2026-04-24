import os
import aiosqlite
from datetime import date

DB_PATH = "/data/expenses.db" if os.path.isdir("/data") else "expenses.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL DEFAULT 'прочее',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def add_transaction(user_id: int, amount: float, category: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO transactions (user_id, amount, category) VALUES (?, ?, ?)",
            (user_id, amount, category),
        )
        await db.commit()
        return cursor.lastrowid


async def get_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0]


async def get_stats(user_id: int, start: date, end: date) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0),
                COUNT(*)
            FROM transactions
            WHERE user_id = ? AND date(created_at) BETWEEN ? AND ?
            """,
            (user_id, start.isoformat(), end.isoformat()),
        )
        row = await cursor.fetchone()
        return {"expenses": row[0], "income": row[1], "count": row[2]}


async def get_category_breakdown(user_id: int, start: date, end: date) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT category, SUM(ABS(amount)) as total
            FROM transactions
            WHERE user_id = ? AND amount < 0 AND date(created_at) BETWEEN ? AND ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (user_id, start.isoformat(), end.isoformat()),
        )
        return await cursor.fetchall()


async def get_history(user_id: int, limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT id, amount, category, created_at
            FROM transactions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        return await cursor.fetchall()


async def delete_transaction(user_id: int, tx_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (tx_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0
