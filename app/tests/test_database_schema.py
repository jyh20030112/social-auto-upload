from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from app.src.persistence.database import Database


class DatabaseSchemaTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_schema_fails_without_deleting_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            database_url = f"sqlite+aiosqlite:///{path}"
            database = Database(database_url)
            await database.initialize()
            async with database.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE schema_version SET version = 2 WHERE id = 1")
                )
            await database.close()

            reopened = Database(database_url)
            with self.assertRaisesRegex(RuntimeError, "手工删除旧 app.db"):
                await reopened.initialize()
            await reopened.close()
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
