from __future__ import annotations

from dataclasses import dataclass

from app.src.config import Settings
from app.src.persistence.database import Database
from app.src.persistence.repositories import Repository
from app.src.services.accounts import DouyinAccountService, ShipinAccountService
from app.src.services.browser_coordinator import BrowserCoordinator
from app.src.services.callback_worker import CallbackWorker
from app.src.services.douyin_proxy import DouyinProxyManager
from app.src.services.material_worker import MaterialWorker
from app.src.services.materials import MaterialService
from app.src.services.publisher import DouyinPublisherService, ShipinPublisherService
from app.src.services.task_worker import TaskWorker
from app.src.services.tasks import TaskService
from app.src.services.verification import VerificationHub


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    database: Database
    repository: Repository
    douyin_proxy: DouyinProxyManager
    accounts: DouyinAccountService
    shipin_accounts: ShipinAccountService
    materials: MaterialService
    publisher: DouyinPublisherService
    shipin_publisher: ShipinPublisherService
    browser_coordinator: BrowserCoordinator
    verification: VerificationHub
    tasks: TaskService
    worker: TaskWorker
    material_worker: MaterialWorker
    callback_worker: CallbackWorker


def build_container(settings: Settings) -> AppContainer:
    database = Database(settings.database_url)
    repository = Repository(database)
    browser_coordinator = BrowserCoordinator(settings.max_browser_tasks)
    douyin_proxy = DouyinProxyManager.from_settings(settings)
    accounts = DouyinAccountService(
        settings,
        repository,
        browser_coordinator,
        douyin_proxy,
    )
    shipin_accounts = ShipinAccountService(settings, repository, browser_coordinator)
    materials = MaterialService(settings, repository)
    publisher = DouyinPublisherService(settings, repository, douyin_proxy)
    shipin_publisher = ShipinPublisherService(settings, repository)
    verification = VerificationHub()
    tasks = TaskService(
        settings,
        repository,
        accounts,
        shipin_accounts,
        verification,
    )
    worker = TaskWorker(
        settings,
        repository,
        accounts,
        shipin_accounts,
        publisher,
        shipin_publisher,
        browser_coordinator,
        verification,
    )
    material_worker = MaterialWorker(settings, repository, materials)
    callback_worker = CallbackWorker(settings, repository)
    tasks.worker = worker
    tasks.material_worker = material_worker
    return AppContainer(
        settings=settings,
        database=database,
        repository=repository,
        douyin_proxy=douyin_proxy,
        accounts=accounts,
        shipin_accounts=shipin_accounts,
        materials=materials,
        publisher=publisher,
        shipin_publisher=shipin_publisher,
        browser_coordinator=browser_coordinator,
        verification=verification,
        tasks=tasks,
        worker=worker,
        material_worker=material_worker,
        callback_worker=callback_worker,
    )
