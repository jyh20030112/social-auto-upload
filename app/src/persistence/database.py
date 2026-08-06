from __future__ import annotations

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.src.persistence.tables import Base, SchemaVersionRecord


class Database:
    SCHEMA_VERSION = 3

    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url, future=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

        if database_url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

    async def initialize(self) -> None:
        fresh_database = False
        async with self.engine.begin() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
            if not table_names:
                fresh_database = True
                await connection.run_sync(Base.metadata.create_all)
            elif "schema_version" not in table_names:
                raise RuntimeError(
                    "数据库 schema 版本过旧；开发阶段请手工删除旧 app.db 后重启"
                )
        async with self.session_factory() as session:
            version = await session.get(SchemaVersionRecord, 1)
            if version is None:
                if not fresh_database:
                    raise RuntimeError(
                        "数据库缺少 schema 版本记录；开发阶段请手工删除旧 app.db 后重启"
                    )
                session.add(SchemaVersionRecord(id=1, version=self.SCHEMA_VERSION))
            elif version.version != self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本为 {version.version}，当前需要 {self.SCHEMA_VERSION}；"
                    "开发阶段请手工删除旧 app.db 后重启"
                )
            await session.commit()

    async def ping(self) -> bool:
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.engine.dispose()
