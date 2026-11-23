#!/usr/bin/env python3
"""EBM Mactool - macOS backend automation tool."""

import logging
import signal
import sys
import argparse
from pathlib import Path

import uvicorn

from config import get_config
from scheduler import get_scheduler
from bot_manager import run_bot_manager_job
from api import app
from log_setup import setup_logging

LOG_DIR = Path(__file__).resolve().parent / "logs"
logger = logging.getLogger()

# Global state
_scheduler = None
_api_server = None


def setup_signal_handlers():
    """Setup graceful shutdown handlers."""

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}. Initiating graceful shutdown...")
        shutdown()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def shutdown():
    """Graceful shutdown."""
    logger.info("Shutting down mactool...")

    # Stop scheduler
    global _scheduler
    if _scheduler:
        try:
            _scheduler.stop()
            logger.info("Scheduler stopped")
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}")

    logger.info("Shutdown complete")
    sys.exit(0)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="EBM Mactool - macOS automation tool")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--web-ui", action="store_true", help="Start FastAPI web UI")
    parser.add_argument("--web-port", type=int, default=8000, help="Web UI port (default: 8000)")
    parser.add_argument("--sync", action="store_true", help="Run sync once and exit")
    parser.add_argument("--check-devices", action="store_true", help="Check devices once and exit")
    parser.add_argument("--bot-restart", action="store_true", help="Restart bot and exit")
    parser.add_argument(
        "--customer-stats",
        action="store_true",
        help="Run the customer statistics upload once and exit",
    )
    parser.add_argument(
        "--customer-stats-preview",
        action="store_true",
        help="Collect and map customer statistics without uploading, then exit",
    )
    parser.add_argument(
        "--diag-upload",
        action="store_true",
        help="Analyse why bot logs are not reaching the bucket, print JSON and exit",
    )
    parser.add_argument(
        "--diag-db",
        action="store_true",
        help="Check the super.db and print row counts as JSON, then exit",
    )
    parser.add_argument(
        "--remote-agent-once",
        action="store_true",
        help="Run one remote-control poll cycle and exit (for testing)",
    )

    args = parser.parse_args()

    setup_logging(LOG_DIR, logging.DEBUG if args.debug else logging.INFO)

    logger.info("=" * 80)
    logger.info("EBM Mactool started")
    logger.info("=" * 80)

    try:
        # Load config
        config = get_config()
        logger.info(f"Configuration loaded: server={config.server_name}")

        # One-off commands
        if args.sync:
            logger.info("Running one-time sync...")
            from sync import trigger_sync
            # CLI is a manual trigger like the Sync Now button → upload all logs
            result = trigger_sync(upload_all_logs=True)
            logger.info(f"Sync result: {result}")
            return

        if args.check_devices:
            logger.info("Checking devices once...")
            from device_monitor import run_device_monitor_job
            result = run_device_monitor_job()
            logger.info(f"Device check result: {result}")
            return

        if args.bot_restart:
            logger.info("Restarting bot...")
            from bot_manager import restart_bot
            success = restart_bot()
            logger.info(f"Bot restart: {'success' if success else 'failed'}")
            return

        if args.diag_upload or args.diag_db:
            import json
            import diagnostics

            result = (
                diagnostics.diagnose_log_upload()
                if args.diag_upload
                else diagnostics.diagnose_database()
            )
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            logger.info(f"Diagnose: {result.get('verdict')}")
            return

        if args.remote_agent_once:
            import json
            from remote_agent import poll_once

            logger.info("Running one remote-control poll cycle...")
            print(json.dumps(poll_once(), indent=2, ensure_ascii=False, default=str))
            return

        if args.customer_stats or args.customer_stats_preview:
            from customer_stats import run_customer_stats_upload

            dry_run = args.customer_stats_preview
            logger.info(
                "Collecting customer statistics (preview)..."
                if dry_run
                else "Running customer statistics upload..."
            )
            result = run_customer_stats_upload(
                trigger="preview" if dry_run else "manual", dry_run=dry_run
            )
            if dry_run:
                import json

                logger.info(
                    f"Preview: {result.get('accounts')} accounts, "
                    f"{result.get('sessions')} sessions, "
                    f"{result.get('missing_sessions')} missing entries"
                )
                print(json.dumps(result.get("preview", {}), indent=2, default=str))
            else:
                logger.info(f"Customer statistics result: {result}")
            return

        # Setup signal handlers for graceful shutdown
        setup_signal_handlers()

        # Initialize scheduler
        global _scheduler
        _scheduler = get_scheduler()
        _scheduler.start()
        logger.info("Scheduler started")

        # Run initial bot check
        logger.info("Running initial bot manager check...")
        result = run_bot_manager_job()
        logger.info(f"Bot check result: {result}")

        # Run initial RustDesk check
        logger.info("Running initial RustDesk watchdog check...")
        from rustdesk_manager import run_rustdesk_manager_job
        rd_result = run_rustdesk_manager_job()
        logger.info(f"RustDesk check result: {rd_result}")

        # Start web UI if requested
        if args.web_ui:
            logger.info(f"Starting FastAPI web UI on http://localhost:{args.web_port}")
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=args.web_port,
                log_level="info",
                log_config=None,  # uvicorn-Meldungen ins Tageslog (log_setup), nicht eigene Handler
                access_log=False,  # jede Dashboard-Abfrage einzeln — füllte stdout.log
            )
        else:
            # Keep running in foreground
            logger.info("Mactool running in background. Press Ctrl+C to stop.")
            try:
                signal.pause()  # Keep the main thread alive
            except KeyboardInterrupt:
                shutdown()

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        shutdown()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
