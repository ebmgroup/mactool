"""Job scheduler using APScheduler."""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import get_config
from sync import trigger_sync
from customer_stats import run_customer_stats_upload
from device_monitor import run_device_monitor_job
from bot_manager import run_bot_manager_job
from rustdesk_manager import run_rustdesk_manager_job
from remote_agent import run_remote_agent_job
from ig_worker import run_ig_worker_job

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: BackgroundScheduler | None = None


def run_scheduled_uploads() -> dict:
    """Run both statistics uploads, one after the other.

    Sequential on purpose: running them in parallel would hit two Supabase
    projects with large batches at the same time, which is exactly what made the
    old standalone script time out.
    """
    results = {}

    try:
        results["dashboard"] = trigger_sync()
    except Exception as e:
        logger.error(f"Statistik (Dashboard) fehlgeschlagen: {e}", exc_info=True)
        results["dashboard"] = {"status": "error", "error": str(e)}

    try:
        if get_config().customer_stats_enabled:
            results["customer"] = run_customer_stats_upload(trigger="auto")
        else:
            logger.info("Statistik (Kunde) ist ausgeschaltet — übersprungen")
            results["customer"] = {"status": "disabled"}
    except Exception as e:
        logger.error(f"Statistik (Kunde) fehlgeschlagen: {e}", exc_info=True)
        results["customer"] = {"status": "error", "error": str(e)}

    return results


class SchedulerManager:
    """Manages scheduled jobs."""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self._jobs_registered = False

    def _register_sync_jobs(self):
        """Register the statistics upload jobs based on config."""
        config = get_config()
        sync_times = config.sync_times

        logger.info(f"Registering statistics upload jobs for times: {sync_times}")

        for time_str in sync_times:
            try:
                hour, minute = map(int, time_str.split(":"))

                # Create cron trigger: every day at specified time
                trigger = CronTrigger(hour=hour, minute=minute)

                job = self.scheduler.add_job(
                    run_scheduled_uploads,
                    trigger=trigger,
                    id=f"sync_{hour}_{minute}",
                    name=f"Statistik-Upload at {time_str}",
                    replace_existing=True,
                )

                logger.info(f"Registered statistics upload job at {time_str}")

            except Exception as e:
                logger.error(f"Failed to register sync job for {time_str}: {e}")

    def _register_device_monitor_job(self):
        """Register device monitor job (every N hours)."""
        config = get_config()
        interval_hours = config.device_check_interval_hours

        logger.info(f"Registering device monitor job (every {interval_hours} hours)")

        trigger = IntervalTrigger(hours=interval_hours)

        job = self.scheduler.add_job(
            run_device_monitor_job,
            trigger=trigger,
            id="device_monitor",
            name=f"Device monitor (every {interval_hours}h)",
            replace_existing=True,
        )

        logger.info("Device monitor job registered")

    def _register_bot_manager_job(self):
        """Register bot manager job (every N minutes)."""
        config = get_config()
        interval_minutes = config.bot_check_interval_minutes

        logger.info(f"Registering bot manager job (every {interval_minutes} minutes)")

        trigger = IntervalTrigger(minutes=interval_minutes)

        job = self.scheduler.add_job(
            run_bot_manager_job,
            trigger=trigger,
            id="bot_manager",
            name=f"Bot manager (every {interval_minutes}m)",
            replace_existing=True,
        )

        logger.info("Bot manager job registered")

    def _register_rustdesk_manager_job(self):
        """Register RustDesk watchdog job (every N minutes)."""
        config = get_config()
        interval_minutes = config.rustdesk_check_interval_minutes

        logger.info(f"Registering RustDesk watchdog job (every {interval_minutes} minutes)")

        trigger = IntervalTrigger(minutes=interval_minutes)

        job = self.scheduler.add_job(
            run_rustdesk_manager_job,
            trigger=trigger,
            id="rustdesk_manager",
            name=f"RustDesk watchdog (every {interval_minutes}m)",
            replace_existing=True,
        )

        logger.info("RustDesk watchdog job registered")

    def _register_remote_agent_job(self):
        """Register the remote-control poll job (every N seconds)."""
        config = get_config()

        if not config.remote_control_enabled:
            logger.info("Fernzugriff ist ausgeschaltet — kein Poll-Job registriert")
            return

        interval_seconds = max(5, int(config.remote_poll_seconds or 15))
        logger.info(f"Registering remote agent job (every {interval_seconds} seconds)")

        self.scheduler.add_job(
            run_remote_agent_job,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id="remote_agent",
            name=f"Fernzugriff (alle {interval_seconds}s)",
            replace_existing=True,
            # Ein Sync über die Ferne dauert Minuten. Ohne diese beiden würde
            # APScheduler unterdessen weitere Durchläufe starten und der Mac
            # arbeitete denselben Befehl mehrfach ab.
            max_instances=1,
            coalesce=True,
        )

        logger.info("Remote agent job registered")

    def _register_ig_worker_job(self):
        """IG Versionen: Bestand melden, Update-Aufträge abarbeiten (jede Minute)."""
        if not get_config().ig_worker_enabled:
            logger.info("IG-Worker ist ausgeschaltet")
            return
        self.scheduler.add_job(
            run_ig_worker_job,
            trigger=IntervalTrigger(seconds=60),
            id="ig_worker",
            name="IG Versionen (jede Minute)",
            replace_existing=True,
            # Ein Auftrag für einen ganzen Mac läuft Stunden — nie doppelt
            max_instances=1,
            coalesce=True,
        )
        logger.info("IG worker job registered")

    def register_jobs(self):
        """Register all scheduled jobs."""
        if self._jobs_registered:
            logger.debug("Jobs already registered")
            return

        self._register_sync_jobs()
        self._register_device_monitor_job()
        self._register_bot_manager_job()
        self._register_rustdesk_manager_job()
        self._register_remote_agent_job()
        self._register_ig_worker_job()

        self._jobs_registered = True
        logger.info("All jobs registered")

    def start(self):
        """Start the scheduler."""
        if self.scheduler.running:
            logger.info("Scheduler is already running")
            return

        try:
            self.register_jobs()
            self.scheduler.start()
            logger.info("Scheduler started")
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}")
            raise

    def stop(self):
        """Stop the scheduler."""
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=True)
                logger.info("Scheduler stopped")
        except Exception as e:
            logger.error(f"Failed to stop scheduler: {e}")

    def reload_sync_jobs(self) -> list[str]:
        """Die Upload-Termine neu aus der Konfiguration aufbauen.

        Ohne das wirkte eine Änderung an `sync_times` erst nach einem Neustart des
        Dienstes — auch die über das Dashboard. Alte Termine müssen dabei entfernt
        werden: `replace_existing` ersetzt nur gleichnamige Jobs, ein gestrichener
        Zeitpunkt liefe sonst weiter.
        """
        for job in self.scheduler.get_jobs():
            if job.id.startswith("sync_"):
                self.scheduler.remove_job(job.id)

        self._register_sync_jobs()
        return [job.id for job in self.scheduler.get_jobs() if job.id.startswith("sync_")]

    def trigger_sync_now(self):
        """Trigger sync immediately."""
        logger.info("Triggering sync immediately...")
        result = trigger_sync()
        return result

    def get_jobs(self) -> list[dict]:
        """Get list of scheduled jobs."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "trigger": str(job.trigger),
                    "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )
        return jobs


def get_scheduler() -> SchedulerManager:
    """Get singleton scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerManager()
    return _scheduler


def stop_scheduler():
    """Stop the global scheduler."""
    if _scheduler:
        _scheduler.stop()
