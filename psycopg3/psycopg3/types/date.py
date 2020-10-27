"""
Adapters for date/time types.
"""

# Copyright (C) 2020 The Psycopg Team

import re
import sys
import codecs
from datetime import date, datetime, time, timedelta
from typing import cast

from ..adapt import Dumper, Loader
from ..proto import AdaptContext, EncodeFunc, DecodeFunc
from ..errors import InterfaceError, DataError
from .oids import builtins

_encode_ascii = codecs.lookup("ascii").encode
_decode_ascii = codecs.lookup("ascii").decode


@Dumper.text(date)
class DateDumper(Dumper):

    DATE_OID = builtins["date"].oid

    def dump(self, obj: date, __encode: EncodeFunc = _encode_ascii) -> bytes:
        # NOTE: whatever the PostgreSQL DateStyle input format (DMY, MDY, YMD)
        # the YYYY-MM-DD is always understood correctly.
        return __encode(str(obj))[0]

    @property
    def oid(self) -> int:
        return self.DATE_OID


@Dumper.text(time)
class TimeDumper(Dumper):

    TIMETZ_OID = builtins["timetz"].oid

    def dump(self, obj: time, __encode: EncodeFunc = _encode_ascii) -> bytes:
        return __encode(str(obj))[0]

    @property
    def oid(self) -> int:
        return self.TIMETZ_OID


@Dumper.text(datetime)
class DateTimeDumper(Dumper):

    TIMESTAMPTZ_OID = builtins["timestamptz"].oid

    def dump(self, obj: date, __encode: EncodeFunc = _encode_ascii) -> bytes:
        # NOTE: whatever the PostgreSQL DateStyle input format (DMY, MDY, YMD)
        # the YYYY-MM-DD is always understood correctly.
        return __encode(str(obj))[0]

    @property
    def oid(self) -> int:
        return self.TIMESTAMPTZ_OID


@Dumper.text(timedelta)
class TimeDeltaDumper(Dumper):
    INTERVAL_OID = builtins["interval"].oid

    def __init__(self, src: type, context: AdaptContext = None):
        super().__init__(src, context)
        if self.connection:
            if (
                self.connection.pgconn.parameter_status(b"IntervalStyle")
                == b"sql_standard"
            ):
                self.dump = self._dump_sql  # type: ignore[assignment]

    def dump(
        self, obj: timedelta, __encode: EncodeFunc = _encode_ascii
    ) -> bytes:
        return __encode(str(obj))[0]

    def _dump_sql(self, obj: timedelta) -> bytes:
        # sql_standard format needs explicit signs
        # otherwise -1 day 1 sec will mean -1 sec
        return b"%+d day %+d second %+d microsecond" % (
            obj.days,
            obj.seconds,
            obj.microseconds,
        )

    @property
    def oid(self) -> int:
        return self.INTERVAL_OID


@Loader.text(builtins["date"].oid)
class DateLoader(Loader):
    def __init__(self, oid: int, context: AdaptContext):
        super().__init__(oid, context)
        self._format = self._format_from_context()

    def load(self, data: bytes, __decode: DecodeFunc = _decode_ascii) -> date:
        try:
            return datetime.strptime(__decode(data)[0], self._format).date()
        except ValueError as e:
            return self._raise_error(data, e)

    def _format_from_context(self) -> str:
        ds = self._get_datestyle()
        if ds.startswith(b"I"):  # ISO
            return "%Y-%m-%d"
        elif ds.startswith(b"G"):  # German
            return "%d.%m.%Y"
        elif ds.startswith(b"S"):  # SQL
            return "%d/%m/%Y" if ds.endswith(b"DMY") else "%m/%d/%Y"
        elif ds.startswith(b"P"):  # Postgres
            return "%d-%m-%Y" if ds.endswith(b"DMY") else "%m-%d-%Y"
        else:
            raise InterfaceError(f"unexpected DateStyle: {ds.decode('ascii')}")

    def _get_datestyle(self) -> bytes:
        rv = b"ISO, DMY"
        if self.connection:
            ds = self.connection.pgconn.parameter_status(b"DateStyle")
            if ds:
                rv = ds

        return rv

    def _raise_error(self, data: bytes, exc: ValueError) -> date:
        # Most likely we received a BC date, which Python doesn't support
        # Otherwise the unexpected value is displayed in the exception.
        if data.endswith(b"BC"):
            raise DataError(
                "Python doesn't support BC date:"
                f" got {data.decode('utf8', 'replace')}"
            )

        if self._get_year_digits(data) > 4:
            raise DataError(
                "Python date doesn't support years after 9999:"
                f" got {data.decode('utf8', 'replace')}"
            )

        # We genuinely received something we cannot parse
        raise exc

    def _get_year_digits(self, data: bytes) -> int:
        datesep = self._format[2].encode("ascii")
        parts = data.split(b" ")[0].split(datesep)
        return max(map(len, parts))


@Loader.text(builtins["time"].oid)
class TimeLoader(Loader):

    _format = "%H:%M:%S.%f"
    _format_no_micro = _format.replace(".%f", "")

    def load(self, data: bytes, __decode: DecodeFunc = _decode_ascii) -> time:
        # check if the data contains microseconds
        fmt = self._format if b"." in data else self._format_no_micro
        try:
            return datetime.strptime(__decode(data)[0], fmt).time()
        except ValueError as e:
            return self._raise_error(data, e)

    def _raise_error(self, data: bytes, exc: ValueError) -> time:
        # Most likely, time 24:00
        if data.startswith(b"24"):
            raise DataError(
                f"time not supported by Python: {data.decode('ascii')}"
            )

        # We genuinely received something we cannot parse
        raise exc


@Loader.text(builtins["timetz"].oid)
class TimeTzLoader(TimeLoader):
    _format = "%H:%M:%S.%f%z"
    _format_no_micro = _format.replace(".%f", "")

    def __init__(self, oid: int, context: AdaptContext):
        if sys.version_info < (3, 7):
            # fun fact: mypy doesn't complain about this on Python 3.8
            # but it does on Python 3.6. Adding "# type: ignore[assignment]"
            # will result in Python 3.6 passing but 3.8 complaining of unused
            # assignment. You cannot win.
            # https://github.com/python/mypy/issues/9652
            self.load = self._load_py36

        super().__init__(oid, context)

    def load(self, data: bytes, __decode: DecodeFunc = _decode_ascii) -> time:
        # Hack to convert +HH in +HHMM
        if data[-3] in (43, 45):
            data += b"00"

        fmt = self._format if b"." in data else self._format_no_micro
        try:
            dt = datetime.strptime(__decode(data)[0], fmt)
        except ValueError as e:
            return self._raise_error(data, e)

        return dt.time().replace(tzinfo=dt.tzinfo)

    def _load_py36(
        self, data: bytes, __decode: DecodeFunc = _decode_ascii
    ) -> time:
        # Drop seconds from timezone for Python 3.6
        # Also, Python 3.6 doesn't support HHMM, only HH:MM
        if data[-6] in (43, 45):  # +-HH:MM -> +-HHMM
            data = data[:-3] + data[-2:]
        elif data[-9] in (43, 45):  # +-HH:MM:SS -> +-HHMM
            data = data[:-6] + data[-5:-3]

        return TimeTzLoader.load(self, data)


@Loader.text(builtins["timestamp"].oid)
class TimestampLoader(DateLoader):
    def __init__(self, oid: int, context: AdaptContext):
        super().__init__(oid, context)
        self._format_no_micro = self._format.replace(".%f", "")

    def load(
        self, data: bytes, __decode: DecodeFunc = _decode_ascii
    ) -> datetime:
        # check if the data contains microseconds
        fmt = (
            self._format if data.find(b".", 19) >= 0 else self._format_no_micro
        )
        try:
            return datetime.strptime(__decode(data)[0], fmt)
        except ValueError as e:
            return self._raise_error(data, e)

    def _format_from_context(self) -> str:
        ds = self._get_datestyle()
        if ds.startswith(b"I"):  # ISO
            return "%Y-%m-%d %H:%M:%S.%f"
        elif ds.startswith(b"G"):  # German
            return "%d.%m.%Y %H:%M:%S.%f"
        elif ds.startswith(b"S"):  # SQL
            return (
                "%d/%m/%Y %H:%M:%S.%f"
                if ds.endswith(b"DMY")
                else "%m/%d/%Y %H:%M:%S.%f"
            )
        elif ds.startswith(b"P"):  # Postgres
            return (
                "%a %d %b %H:%M:%S.%f %Y"
                if ds.endswith(b"DMY")
                else "%a %b %d %H:%M:%S.%f %Y"
            )
        else:
            raise InterfaceError(f"unexpected DateStyle: {ds.decode('ascii')}")

    def _raise_error(self, data: bytes, exc: ValueError) -> datetime:
        return cast(datetime, super()._raise_error(data, exc))

    def _get_year_digits(self, data: bytes) -> int:
        # Find the year from the date.
        if not self._get_datestyle().startswith(b"P"):  # Postgres
            return super()._get_year_digits(data)
        else:
            parts = data.split()
            if len(parts) > 4:
                return len(parts[4])
            else:
                return 0


@Loader.text(builtins["timestamptz"].oid)
class TimestamptzLoader(TimestampLoader):
    def __init__(self, oid: int, context: AdaptContext):
        if sys.version_info < (3, 7):
            self.load = self._load_py36

        super().__init__(oid, context)

    def _format_from_context(self) -> str:
        ds = self._get_datestyle()
        if ds.startswith(b"I"):  # ISO
            return "%Y-%m-%d %H:%M:%S.%f%z"

        # These don't work: the timezone name is not always displayed
        # elif ds.startswith(b"G"):  # German
        #     return "%d.%m.%Y %H:%M:%S.%f %Z"
        # elif ds.startswith(b"S"):  # SQL
        #     return (
        #         "%d/%m/%Y %H:%M:%S.%f %Z"
        #         if ds.endswith(b"DMY")
        #         else "%m/%d/%Y %H:%M:%S.%f %Z"
        #     )
        # elif ds.startswith(b"P"):  # Postgres
        #     return (
        #         "%a %d %b %H:%M:%S.%f %Y %Z"
        #         if ds.endswith(b"DMY")
        #         else "%a %b %d %H:%M:%S.%f %Y %Z"
        #     )
        # else:
        #     raise InterfaceError(f"unexpected DateStyle: {ds.decode('ascii')}")
        else:
            self.load = self._load_notimpl  # type: ignore[assignment]
            return ""

    def load(
        self, data: bytes, __decode: DecodeFunc = _decode_ascii
    ) -> datetime:
        # Hack to convert +HH in +HHMM
        if data[-3] in (43, 45):
            data += b"00"

        return super().load(data)

    def _load_py36(
        self, data: bytes, __decode: DecodeFunc = _decode_ascii
    ) -> datetime:
        # Drop seconds from timezone for Python 3.6
        # Also, Python 3.6 doesn't support HHMM, only HH:MM
        tzsep = (43, 45)  # + and - bytes
        if data[-3] in tzsep:  # +HH, -HH
            data += b"00"
        elif data[-6] in tzsep:
            data = data[:-3] + data[-2:]
        elif data[-9] in tzsep:
            data = data[:-6] + data[-5:-3]

        return super().load(data)

    def _load_notimpl(self, data: bytes) -> datetime:
        raise NotImplementedError(
            "can't parse datetimetz with DateStyle"
            f" {self._get_datestyle().decode('ascii')}: {data.decode('ascii')}"
        )


@Loader.text(builtins["interval"].oid)
class IntervalLoader(Loader):

    _re_interval = re.compile(
        br"""
        (?: (?P<years> [-+]?\d+) \s+ years? \s* )?
        (?: (?P<months> [-+]?\d+) \s+ mons? \s* )?
        (?: (?P<days> [-+]?\d+) \s+ days? \s* )?
        (?: (?P<hsign> [-+])?
            (?P<hours> \d+ )
          : (?P<minutes> \d+ )
          : (?P<seconds> \d+ (?:\.\d+)? )
        )?
        """,
        re.VERBOSE,
    )

    def __init__(self, oid: int, context: AdaptContext):
        super().__init__(oid, context)
        if self.connection:
            ints = self.connection.pgconn.parameter_status(b"IntervalStyle")
            if ints != b"postgres":
                self.load = self._load_notimpl  # type: ignore[assignment]

    def load(self, data: bytes) -> timedelta:
        m = self._re_interval.match(data)
        if not m:
            raise ValueError("can't parse interval: {data.decode('ascii')}")

        days = 0
        seconds = 0.0

        tmp = m.group("years")
        if tmp:
            days += 365 * int(tmp)

        tmp = m.group("months")
        if tmp:
            days += 30 * int(tmp)

        tmp = m.group("days")
        if tmp:
            days += int(tmp)

        if m.group("hours"):
            seconds = (
                3600 * int(m.group("hours"))
                + 60 * int(m.group("minutes"))
                + float(m.group("seconds"))
            )
            if m.group("hsign") == b"-":
                seconds = -seconds

        try:
            return timedelta(days=days, seconds=seconds)
        except OverflowError as e:
            raise DataError(str(e))

    def _load_notimpl(self, data: bytes) -> timedelta:
        ints = (
            self.connection
            and self.connection.pgconn.parameter_status(b"IntervalStyle")
            or b"unknown"
        )
        raise NotImplementedError(
            "can't parse interval with IntervalStyle"
            f" {ints.decode('ascii')}: {data.decode('ascii')}"
        )
