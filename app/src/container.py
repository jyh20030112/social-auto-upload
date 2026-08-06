from __future__ import annotations

from dataclasses import dataclass

from app.src.config import Settings
from app.src.persistence.database import Database
from app.src.persistence.repositories import Repository
from app.src.services.accounts import AccountService
from app.src.services.materials import MaterialService
from app.src.services.publisher import PublisherService
from app.src.services.tasks import TaskService
from app.src.services.task_worker import TaskWorker
from app.src.services.verification import VerificationHub


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    database: Database
    repository: Repository
    accounts: AccountService
    materials: MaterialService
    publisher: PublisherService
    verification: VerificationHub
    tasks: TaskService
    worker: TaskWorker


def build_container(settings: Settings) -> AppContainer:
    database = Database(settings.database_url)
    repository = Repository(database)
    accounts = AccountService(settings, repository)
    materials = MaterialService(settings, repository)
    publisher = PublisherService(settings, repository)
    verification = VerificationHub()
    tasks = TaskService(settings, repository, accounts, verification)
    worker = TaskWorker(settings, repository, accounts, publisher, verification)
    tasks.worker = worker
    return AppContainer(
        settings=settings,
        database=database,
        repository=repository,
        accounts=accounts,
        materials=materials,
        publisher=publisher,
        verification=verification,
        tasks=tasks,
        worker=worker,
    )
