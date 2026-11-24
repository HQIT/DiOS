"""Compatibility import for the durable subscription timer service."""

from app.services.durable_cron import CronScheduler

cron_scheduler = CronScheduler()
