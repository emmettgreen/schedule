"""
Python job scheduling for humans.

github.com/dbader/schedule

An in-process scheduler for periodic jobs that uses the builder pattern
for configuration. Schedule lets you run Python functions (or any other
callable) periodically at pre-determined intervals using a simple,
human-friendly syntax.

Inspired by Addam Wiggins' article "Rethinking Cron" [1] and the
"clockwork" Ruby module [2][3].

Features:
    - A simple to use API for scheduling jobs.
    - Very lightweight and no external dependencies.
    - Excellent test coverage.
    - Tested on Python 3.7, 3.8, 3.9, 3.10, 3.11 and 3.12

Usage:
    >>> import schedule
    >>> import time

    >>> def job(message='stuff'):
    >>>     print("I'm working on:", message)

    >>> schedule.every(10).minutes.do(job)
    >>> schedule.every(5).to(10).days.do(job)
    >>> schedule.every().hour.do(job, message='things')
    >>> schedule.every().day.at("10:30").do(job)

    >>> while True:
    >>>     schedule.run_pending()
    >>>     time.sleep(1)

[1] https://adam.herokuapp.com/past/2010/4/13/rethinking_cron/
[2] https://github.com/Rykian/clockwork
[3] https://adam.herokuapp.com/past/2010/6/30/replace_cron_with_clockwork/
"""
from collections.abc import Hashable
import datetime
import functools
import logging
import random
import re
import time
from typing import Set, List, Optional, Callable, Union

logger = logging.getLogger("schedule")


class ScheduleError(Exception):
    """Base schedule exception"""

    pass


class ScheduleValueError(ScheduleError):
    """Base schedule value error"""

    pass


class IntervalError(ScheduleValueError):
    """An improper interval was used"""

    pass


class CancelJob:
    """
    Can be returned from a job to unschedule itself.
    """

    pass


class Scheduler:
    """
    Objects instantiated by the :class:`Scheduler <Scheduler>` are
    factories to create jobs, keep record of scheduled jobs and
    handle their execution.
    """

    def __init__(self, *, is_realtime=True, start_timestamp: datetime.datetime | None = None) -> None:
        self.jobs: List[Job] = []
        self.is_realtime = is_realtime
        self._timestamp = start_timestamp

        if self.is_realtime and self._timestamp is not None:
            raise ScheduleError(
                "Scheduler is configured for realtime operation, but a start timestamp was provided"
            )
        if not self.is_realtime and self._timestamp is None:
            raise ScheduleError(
                "Scheduler is configured for non-realtime operation, but no start timestamp was provided"
            )

    def run_pending(self) -> None:
        """
        Run all jobs that are scheduled to run.

        Please note that it is *intended behavior that run_pending()
        does not run missed jobs*. For example, if you've registered a job
        that should run every minute and you only call run_pending()
        in one hour increments then your job won't be run 60 times in
        between but only once.
        """
        runnable_jobs = (job for job in self.jobs if job.should_run)
        for job in sorted(runnable_jobs):
            self._run_job(job)

    def run_all(self, delay_seconds: int = 0) -> None:
        """
        Run all jobs regardless if they are scheduled to run or not.

        A delay of `delay` seconds is added between each job. This helps
        distribute system load generated by the jobs more evenly
        over time.

        :param delay_seconds: A delay added between every executed job
        """
        logger.debug(
            "Running *all* %i jobs with %is delay in between",
            len(self.jobs),
            delay_seconds,
        )
        for job in self.jobs[:]:
            self._run_job(job)
            time.sleep(delay_seconds)

    def get_jobs(self, tag: Optional[Hashable] = None) -> List["Job"]:
        """
        Gets scheduled jobs marked with the given tag, or all jobs
        if tag is omitted.

        :param tag: An identifier used to identify a subset of
                    jobs to retrieve
        """
        if tag is None:
            return self.jobs[:]
        else:
            return [job for job in self.jobs if tag in job.tags]

    def clear(self, tag: Optional[Hashable] = None) -> None:
        """
        Deletes scheduled jobs marked with the given tag, or all jobs
        if tag is omitted.

        :param tag: An identifier used to identify a subset of
                    jobs to delete
        """
        if tag is None:
            logger.debug("Deleting *all* jobs")
            del self.jobs[:]
        else:
            logger.debug('Deleting all jobs tagged "%s"', tag)
            self.jobs[:] = (job for job in self.jobs if tag not in job.tags)

    def cancel_job(self, job: "Job") -> None:
        """
        Delete a scheduled job.

        :param job: The job to be unscheduled
        """
        try:
            logger.debug('Cancelling job "%s"', str(job))
            self.jobs.remove(job)
        except ValueError:
            logger.debug('Cancelling not-scheduled job "%s"', str(job))

    def every(self, interval: int = 1) -> "Job":
        """
        Schedule a new periodic job.

        :param interval: A quantity of a certain time unit
        :return: An unconfigured :class:`Job <Job>`
        """
        job = Job(interval, self)
        return job

    def _run_job(self, job: "Job") -> None:
        ret = job.run()
        if isinstance(ret, CancelJob) or ret is CancelJob:
            self.cancel_job(job)

    def get_next_run(
        self, tag: Optional[Hashable] = None
    ) -> Optional[datetime.datetime]:
        """
        Datetime when the next job should run.

        :param tag: Filter the next run for the given tag parameter

        :return: A :class:`~datetime.datetime` object
                 or None if no jobs scheduled
        """
        if not self.jobs:
            return None
        jobs_filtered = self.get_jobs(tag)
        if not jobs_filtered:
            return None
        return min(jobs_filtered).next_run

    next_run = property(get_next_run)

    @property
    def timestamp(self) -> datetime.datetime:
        """
        :return: The current time in the scheduler's timezone
        """
        if self.is_realtime:
            return datetime.datetime.now(tz=datetime.UTC)
        else:
            return self._timestamp

    @timestamp.setter
    def timestamp(self, timestamp) -> None:
        """
        Set the current time in the scheduler's timezone.
        """
        if self.is_realtime:
            raise ScheduleError(
                "Cannot update the current time when scheduler is configured for realtime operation"
            )
        if timestamp < self._timestamp:
            raise ScheduleError("Cannot set the current time to a time in the past")

        self._timestamp = timestamp

    @property
    def idle_seconds(self) -> Optional[float]:
        """
        :return: Number of seconds until
                 :meth:`next_run <Scheduler.next_run>`
                 or None if no jobs are scheduled
        """
        if not self.next_run:
            return None
        return (self.next_run - self.timestamp).total_seconds()


class Job:
    """
    A periodic job as used by :class:`Scheduler`.

    :param interval: A quantity of a certain time unit
    :param scheduler: The :class:`Scheduler <Scheduler>` instance that
                      this job will register itself with once it has
                      been fully configured in :meth:`Job.do()`.

    Every job runs at a given fixed time interval that is defined by:

    * a :meth:`time unit <Job.second>`
    * a quantity of `time units` defined by `interval`

    A job is usually created and returned by :meth:`Scheduler.every`
    method, which also defines its `interval`.
    """

    def __init__(self, interval: int, scheduler: Optional[Scheduler] = None):
        self.interval: int = interval  # pause interval * unit between runs
        self.latest: Optional[int] = None  # upper limit to the interval
        self.job_func: Optional[functools.partial] = None  # the job job_func to run

        # time units, e.g. 'minutes', 'hours', ...
        self.unit: Optional[str] = None

        # optional time at which this job runs
        self.at_time: Optional[datetime.time] = None

        # optional time zone of the self.at_time field. Only relevant when at_time is not None
        self.at_time_zone = None

        # datetime of the last run
        self.last_run: Optional[datetime.datetime] = None

        # datetime of the next run
        self.next_run: Optional[datetime.datetime] = None

        # timedelta between runs, only valid for
        self.period: Optional[datetime.timedelta] = None

        # Specific day of the week to start on
        self.start_day: Optional[str] = None

        # optional time of final run
        self.cancel_after: Optional[datetime.datetime] = None

        self.tags: Set[Hashable] = set()  # unique set of tags for the job
        self.scheduler: Optional[Scheduler] = scheduler  # scheduler to register with

    def __lt__(self, other) -> bool:
        """
        PeriodicJobs are sortable based on the scheduled time they
        run next.
        """
        return self.next_run < other.next_run

    def __str__(self) -> str:
        if hasattr(self.job_func, "__name__"):
            job_func_name = self.job_func.__name__  # type: ignore
        else:
            job_func_name = repr(self.job_func)

        return ("Job(interval={}, unit={}, do={}, args={}, kwargs={})").format(
            self.interval,
            self.unit,
            job_func_name,
            "()" if self.job_func is None else self.job_func.args,
            "{}" if self.job_func is None else self.job_func.keywords,
        )

    def __repr__(self):
        def format_time(t):
            return t.strftime("%Y-%m-%d %H:%M:%S") if t else "[never]"

        def is_repr(j):
            return not isinstance(j, Job)

        timestats = "(last run: %s, next run: %s)" % (
            format_time(self.last_run),
            format_time(self.next_run),
        )

        if hasattr(self.job_func, "__name__"):
            job_func_name = self.job_func.__name__
        else:
            job_func_name = repr(self.job_func)

        if self.job_func is not None:
            args = [repr(x) if is_repr(x) else str(x) for x in self.job_func.args]
            kwargs = ["%s=%s" % (k, repr(v)) for k, v in self.job_func.keywords.items()]
            call_repr = job_func_name + "(" + ", ".join(args + kwargs) + ")"
        else:
            call_repr = "[None]"

        if self.at_time is not None:
            return "Every %s %s at %s do %s %s" % (
                self.interval,
                self.unit[:-1] if self.interval == 1 else self.unit,
                self.at_time,
                call_repr,
                timestats,
            )
        else:
            fmt = (
                "Every %(interval)s "
                + ("to %(latest)s " if self.latest is not None else "")
                + "%(unit)s do %(call_repr)s %(timestats)s"
            )

            return fmt % dict(
                interval=self.interval,
                latest=self.latest,
                unit=(self.unit[:-1] if self.interval == 1 else self.unit),
                call_repr=call_repr,
                timestats=timestats,
            )

    @property
    def second(self):
        if self.interval != 1:
            raise IntervalError("Use seconds instead of second")
        return self.seconds

    @property
    def seconds(self):
        self.unit = "seconds"
        return self

    @property
    def minute(self):
        if self.interval != 1:
            raise IntervalError("Use minutes instead of minute")
        return self.minutes

    @property
    def minutes(self):
        self.unit = "minutes"
        return self

    @property
    def hour(self):
        if self.interval != 1:
            raise IntervalError("Use hours instead of hour")
        return self.hours

    @property
    def hours(self):
        self.unit = "hours"
        return self

    @property
    def day(self):
        if self.interval != 1:
            raise IntervalError("Use days instead of day")
        return self.days

    @property
    def days(self):
        self.unit = "days"
        return self

    @property
    def week(self):
        if self.interval != 1:
            raise IntervalError("Use weeks instead of week")
        return self.weeks

    @property
    def weeks(self):
        self.unit = "weeks"
        return self

    @property
    def monday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .monday() jobs is only allowed for weekly jobs. "
                "Using .monday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "monday"
        return self.weeks

    @property
    def tuesday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .tuesday() jobs is only allowed for weekly jobs. "
                "Using .tuesday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "tuesday"
        return self.weeks

    @property
    def wednesday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .wednesday() jobs is only allowed for weekly jobs. "
                "Using .wednesday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "wednesday"
        return self.weeks

    @property
    def thursday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .thursday() jobs is only allowed for weekly jobs. "
                "Using .thursday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "thursday"
        return self.weeks

    @property
    def friday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .friday() jobs is only allowed for weekly jobs. "
                "Using .friday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "friday"
        return self.weeks

    @property
    def saturday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .saturday() jobs is only allowed for weekly jobs. "
                "Using .saturday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "saturday"
        return self.weeks

    @property
    def sunday(self):
        if self.interval != 1:
            raise IntervalError(
                "Scheduling .sunday() jobs is only allowed for weekly jobs. "
                "Using .sunday() on a job scheduled to run every 2 or more weeks "
                "is not supported."
            )
        self.start_day = "sunday"
        return self.weeks

    def tag(self, *tags: Hashable):
        """
        Tags the job with one or more unique identifiers.

        Tags must be hashable. Duplicate tags are discarded.

        :param tags: A unique list of ``Hashable`` tags.
        :return: The invoked job instance
        """
        if not all(isinstance(tag, Hashable) for tag in tags):
            raise TypeError("Tags must be hashable")
        self.tags.update(tags)
        return self

    def at(self, time_str: str, tz: Optional[str] = None):
        """
        Specify a particular time that the job should be run at.

        :param time_str: A string in one of the following formats:

            - For daily jobs -> `HH:MM:SS` or `HH:MM`
            - For hourly jobs -> `MM:SS` or `:MM`
            - For minute jobs -> `:SS`

            The format must make sense given how often the job is
            repeating; for example, a job that repeats every minute
            should not be given a string in the form `HH:MM:SS`. The
            difference between `:MM` and `:SS` is inferred from the
            selected time-unit (e.g. `every().hour.at(':30')` vs.
            `every().minute.at(':30')`).

        :param tz: The timezone that this timestamp refers to. Can be
            a string that can be parsed by pytz.timezone(), or a pytz.BaseTzInfo object

        :return: The invoked job instance
        """
        if self.unit not in ("days", "hours", "minutes") and not self.start_day:
            raise ScheduleValueError(
                "Invalid unit (valid units are `days`, `hours`, and `minutes`)"
            )

        if tz is not None:
            import pytz

            if isinstance(tz, str):
                self.at_time_zone = pytz.timezone(tz)  # type: ignore
            elif isinstance(tz, pytz.BaseTzInfo):
                self.at_time_zone = tz
            else:
                raise ScheduleValueError(
                    "Timezone must be string or pytz.timezone object"
                )

        if not isinstance(time_str, str):
            raise TypeError("at() should be passed a string")
        if self.unit == "days" or self.start_day:
            if not re.match(r"^[0-2]\d:[0-5]\d(:[0-5]\d)?$", time_str):
                raise ScheduleValueError(
                    "Invalid time format for a daily job (valid format is HH:MM(:SS)?)"
                )
        if self.unit == "hours":
            if not re.match(r"^([0-5]\d)?:[0-5]\d$", time_str):
                raise ScheduleValueError(
                    "Invalid time format for an hourly job (valid format is (MM)?:SS)"
                )

        if self.unit == "minutes":
            if not re.match(r"^:[0-5]\d$", time_str):
                raise ScheduleValueError(
                    "Invalid time format for a minutely job (valid format is :SS)"
                )
        time_values = time_str.split(":")
        hour: Union[str, int]
        minute: Union[str, int]
        second: Union[str, int]
        if len(time_values) == 3:
            hour, minute, second = time_values
        elif len(time_values) == 2 and self.unit == "minutes":
            hour = 0
            minute = 0
            _, second = time_values
        elif len(time_values) == 2 and self.unit == "hours" and len(time_values[0]):
            hour = 0
            minute, second = time_values
        else:
            hour, minute = time_values
            second = 0
        if self.unit == "days" or self.start_day:
            hour = int(hour)
            if not (0 <= hour <= 23):
                raise ScheduleValueError(
                    "Invalid number of hours ({} is not between 0 and 23)"
                )
        elif self.unit == "hours":
            hour = 0
        elif self.unit == "minutes":
            hour = 0
            minute = 0
        hour = int(hour)
        minute = int(minute)
        second = int(second)
        self.at_time = datetime.time(hour, minute, second)
        return self

    def to(self, latest: int):
        """
        Schedule the job to run at an irregular (randomized) interval.

        The job's interval will randomly vary from the value given
        to  `every` to `latest`. The range defined is inclusive on
        both ends. For example, `every(A).to(B).seconds` executes
        the job function every N seconds such that A <= N <= B.

        :param latest: Maximum interval between randomized job runs
        :return: The invoked job instance
        """
        self.latest = latest
        return self

    def until(
        self,
        until_time: Union[datetime.datetime, datetime.timedelta, datetime.time, str],
    ):
        """
        Schedule job to run until the specified moment.

        The job is canceled whenever the next run is calculated and it turns out the
        next run is after the until_time. The job is also canceled right before it runs,
        if the current time is after until_time. This latter case can happen when the
        the job was scheduled to run before until_time, but runs after until_time.

        If until_time is a moment in the past, ScheduleValueError is thrown.

        :param until_time: A moment in the future representing the latest time a job can
           be run. If only a time is supplied, the date is set to today.
           The following formats are accepted:

           - datetime.datetime
           - datetime.timedelta
           - datetime.time
           - String in one of the following formats: "%Y-%m-%d %H:%M:%S",
             "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"
             as defined by strptime() behaviour. If an invalid string format is passed,
             ScheduleValueError is thrown.

        :return: The invoked job instance
        """

        if isinstance(until_time, datetime.datetime):
            self.cancel_after = until_time
        elif isinstance(until_time, datetime.timedelta):
            self.cancel_after = self.scheduler.timestamp + until_time
        elif isinstance(until_time, datetime.time):
            self.cancel_after = datetime.datetime.combine(
                self.scheduler.timestamp, until_time
            )
        elif isinstance(until_time, str):
            cancel_after = self._decode_datetimestr(
                until_time,
                [
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M",
                    "%Y-%m-%d",
                    "%H:%M:%S",
                    "%H:%M",
                ],
            )
            if cancel_after is None:
                raise ScheduleValueError("Invalid string format for until()")
            if "-" not in until_time:
                # the until_time is a time-only format. Set the date to today
                now = self.scheduler.timestamp
                cancel_after = cancel_after.replace(
                    year=now.year, month=now.month, day=now.day
                )
            self.cancel_after = cancel_after
        else:
            raise TypeError(
                "until() takes a string, datetime.datetime, datetime.timedelta, "
                "datetime.time parameter"
            )
        if self.cancel_after < self.scheduler.timestamp:
            raise ScheduleValueError(
                "Cannot schedule a job to run until a time in the past"
            )
        return self

    def do(self, job_func: Callable, *args, **kwargs):
        """
        Specifies the job_func that should be called every time the
        job runs.

        Any additional arguments are passed on to job_func when
        the job runs.

        :param job_func: The function to be scheduled
        :return: The invoked job instance
        """
        self.job_func = functools.partial(job_func, *args, **kwargs)
        functools.update_wrapper(self.job_func, job_func)
        self._schedule_next_run()
        if self.scheduler is None:
            raise ScheduleError(
                "Unable to a add job to schedule. "
                "Job is not associated with an scheduler"
            )
        self.scheduler.jobs.append(self)
        return self

    @property
    def should_run(self) -> bool:
        """
        :return: ``True`` if the job should be run now.
        """
        assert self.next_run is not None, "must run _schedule_next_run before"
        return self.scheduler.timestamp >= self.next_run

    def run(self):
        """
        Run the job and immediately reschedule it.
        If the job's deadline is reached (configured using .until()), the job is not
        run and CancelJob is returned immediately. If the next scheduled run exceeds
        the job's deadline, CancelJob is returned after the execution. In this latter
        case CancelJob takes priority over any other returned value.

        :return: The return value returned by the `job_func`, or CancelJob if the job's
                 deadline is reached.

        """
        if self._is_overdue(self.scheduler.timestamp):
            logger.debug("Cancelling job %s", self)
            return CancelJob

        logger.debug("Running job %s", self)
        ret = self.job_func()
        self.last_run = self.scheduler.timestamp
        self._schedule_next_run()

        if self._is_overdue(self.next_run):
            logger.debug("Cancelling job %s", self)
            return CancelJob
        return ret

    def _schedule_next_run(self) -> None:
        """
        Compute the instant when this job should run next.
        """
        if self.unit not in ("seconds", "minutes", "hours", "days", "weeks"):
            raise ScheduleValueError(
                "Invalid unit (valid units are `seconds`, `minutes`, `hours`, "
                "`days`, and `weeks`)"
            )

        if self.latest is not None:
            if not (self.latest >= self.interval):
                raise ScheduleError("`latest` is greater than `interval`")
            interval = random.randint(self.interval, self.latest)
        else:
            interval = self.interval

        # Do all computation in the context of the requested timezone
        if self.at_time_zone is not None:
            if self.scheduler.timestamp.tzinfo is None:
                # If the current timestamp is naive, we assume it's in the same timezone as the at_time_zone
                now = self.scheduler.timestamp.replace(tzinfo=self.at_time_zone)
            else:
                # Otherwise we convert it to the at_time_zone
                now = self.at_time_zone.normalize(self.scheduler.timestamp.astimezone(self.at_time_zone))
        else:
            now = self.scheduler.timestamp

        self.period = datetime.timedelta(**{self.unit: interval})
        self.next_run = now + self.period
        if self.start_day is not None:
            if self.unit != "weeks":
                raise ScheduleValueError("`unit` should be 'weeks'")
            weekdays = (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
            if self.start_day not in weekdays:
                raise ScheduleValueError(
                    "Invalid start day (valid start days are {})".format(weekdays)
                )
            weekday = weekdays.index(self.start_day)
            days_ahead = weekday - self.next_run.weekday()
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            self.next_run += datetime.timedelta(days_ahead) - self.period

        # before we apply the .at() time, we need to normalize the timestamp
        # to ensure we change the time elements in the new timezone
        if self.at_time_zone is not None:
            self.next_run = self.at_time_zone.normalize(self.next_run)

        if self.at_time is not None:
            if self.unit not in ("days", "hours", "minutes") and self.start_day is None:
                raise ScheduleValueError("Invalid unit without specifying start day")
            kwargs = {"second": self.at_time.second, "microsecond": 0}
            if self.unit == "days" or self.start_day is not None:
                kwargs["hour"] = self.at_time.hour
            if self.unit in ["days", "hours"] or self.start_day is not None:
                kwargs["minute"] = self.at_time.minute

            self.next_run = self.next_run.replace(**kwargs)  # type: ignore

            # Make sure we run at the specified time *today* (or *this hour*)
            # as well. This accounts for when a job takes so long it finished
            # in the next period.
            last_run_tz = self._to_at_timezone(self.last_run)
            if not last_run_tz or (self.next_run - last_run_tz) > self.period:
                if (
                    self.unit == "days"
                    and self.next_run.time() > now.time()
                    and self.interval == 1
                ):
                    self.next_run = self.next_run - datetime.timedelta(days=1)
                elif self.unit == "hours" and (
                    self.at_time.minute > now.minute
                    or (
                        self.at_time.minute == now.minute
                        and self.at_time.second > now.second
                    )
                ):
                    self.next_run = self.next_run - datetime.timedelta(hours=1)
                elif self.unit == "minutes" and self.at_time.second > now.second:
                    self.next_run = self.next_run - datetime.timedelta(minutes=1)
        if self.start_day is not None and self.at_time is not None:
            # Let's see if we will still make that time we specified today
            if (self.next_run - now).days >= 7:
                self.next_run -= self.period

        # Calculations happen in the configured timezone, but to execute the schedule we
        # need to know the next_run time in the system time. So we convert back to naive local
        if self.at_time_zone is not None:
            self.next_run = self._normalize_preserve_timestamp(self.next_run)
            # This line has to be commented out so that timezones can be used properly
            # self.next_run = self.next_run.astimezone().replace(tzinfo=None)

    # Usually when normalization of a timestamp causes the timestamp to change,
    # it preserves the moment in time and changes the local timestamp.
    # This method applies pytz normalization but preserves the local timestamp, in fact changing the moment in time.
    def _normalize_preserve_timestamp(
        self, input: datetime.datetime
    ) -> datetime.datetime:
        if self.at_time_zone is None or input is None:
            return input
        normalized = self.at_time_zone.normalize(input)
        return normalized.replace(
            day=input.day,
            hour=input.hour,
            minute=input.minute,
            second=input.second,
            microsecond=input.microsecond,
        )

    def _to_at_timezone(
        self, input: Optional[datetime.datetime]
    ) -> Optional[datetime.datetime]:
        if self.at_time_zone is None or input is None:
            return input
        return input.astimezone(self.at_time_zone)

    def _is_overdue(self, when: datetime.datetime):
        return self.cancel_after is not None and when > self.cancel_after

    def _decode_datetimestr(
        self, datetime_str: str, formats: List[str]
    ) -> Optional[datetime.datetime]:
        for f in formats:
            try:
                return datetime.datetime.strptime(datetime_str, f)
            except ValueError:
                pass
        return None


# The following methods are shortcuts for not having to
# create a Scheduler instance:

#: Default :class:`Scheduler <Scheduler>` object
default_scheduler = Scheduler()

#: Default :class:`Jobs <Job>` list
jobs = default_scheduler.jobs  # todo: should this be a copy, e.g. jobs()?


def every(interval: int = 1) -> Job:
    """Calls :meth:`every <Scheduler.every>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.every(interval)


def run_pending() -> None:
    """Calls :meth:`run_pending <Scheduler.run_pending>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.run_pending()


def run_all(delay_seconds: int = 0) -> None:
    """Calls :meth:`run_all <Scheduler.run_all>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.run_all(delay_seconds=delay_seconds)


def get_jobs(tag: Optional[Hashable] = None) -> List[Job]:
    """Calls :meth:`get_jobs <Scheduler.get_jobs>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.get_jobs(tag)


def clear(tag: Optional[Hashable] = None) -> None:
    """Calls :meth:`clear <Scheduler.clear>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.clear(tag)


def cancel_job(job: Job) -> None:
    """Calls :meth:`cancel_job <Scheduler.cancel_job>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    default_scheduler.cancel_job(job)


def next_run(tag: Optional[Hashable] = None) -> Optional[datetime.datetime]:
    """Calls :meth:`next_run <Scheduler.next_run>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.get_next_run(tag)


def idle_seconds() -> Optional[float]:
    """Calls :meth:`idle_seconds <Scheduler.idle_seconds>` on the
    :data:`default scheduler instance <default_scheduler>`.
    """
    return default_scheduler.idle_seconds


def repeat(job, *args, **kwargs):
    """
    Decorator to schedule a new periodic job.

    Any additional arguments are passed on to the decorated function
    when the job runs.

    :param job: a :class:`Jobs <Job>`
    """

    def _schedule_decorator(decorated_function):
        job.do(decorated_function, *args, **kwargs)
        return decorated_function

    return _schedule_decorator
